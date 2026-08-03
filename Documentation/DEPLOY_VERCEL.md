# Deploying Carelog on Vercel

Run `bash scripts/setup_vercel.sh` for the guided path. This document explains what
each piece is, why it exists, and what to do when something misbehaves.

## The three parts

| Part | What it is | Why |
| --- | --- | --- |
| **Function** | The Flask app, deployed as one Vercel Function from `carelog/app.py` | Vercel finds the `app` object at a supported entrypoint; no wrapper needed |
| **Database** | Neon Postgres, created through Vercel Storage | Replaces the single SQLite file. Managed backups, and no data loss when an instance is recycled |
| **Blob store** | Vercel Blob, **private**, holding retained upload evidence | Serverless has no persistent disk. These are resident care records, so the store must not be public |

Set all three to a **Sydney** region. This is Australian aged-care data, and
region is fixed at creation time for both the database and the blob store —
getting it wrong means recreating them.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. `postgres://` and `postgresql://` are both accepted and rewritten to the psycopg driver |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob credential. Its presence alone switches storage to the blob backend |
| `SECRET_KEY` | Signs session cookies. Random, and never the dev default in production |
| `WORKER_SECRET` | Required header on `/import/run/<job>`. Without it, anyone could trigger import workers |
| `ANTHROPIC_API_KEY` | Format learning. Environment only — it is deliberately not settable from the UI |
| `STORAGE_BACKEND` | `local` or `vercel_blob`. Inferred from the token when unset |
| `IMPORT_WORKER` | `thread` or `invoke`. Defaults to `invoke` on Vercel |
| `DEBUG_TOKEN` | Opens `/debug/*` to a caller sending it as `x-debug-token`, without a session |

## How imports run without a background process

A serverless function stops the moment it returns a response, so the thread
that used to run imports would be frozen mid-work. Instead:

1. `POST /import` writes the uploaded files to blob storage, creates an
   `ImportJob` row, and returns a redirect immediately.
2. It fires a request at `/import/run/<job_id>` and abandons it after 5
   seconds. That request runs in its own invocation with up to 300s.
3. The browser polls `/import/status/<job_id>.json`, which reads the job row.

Because job state lives in Postgres rather than memory, polling works no matter
which instance answers, and a job that dies leaves a `failed` row explaining
why instead of disappearing. `run_job` ignores any job that is not `queued`, so
a duplicate dispatch cannot double-import.

## Verifying a deployment

Two self-test endpoints report from inside the running instance:

- `GET /debug/storage` — database dialect, tables, missing tables, and a full
  write/read/list round trip against the blob store.
- `GET /debug/network` — DNS, per-address TCP, and a real authenticated call to
  the Anthropic API.

Both require a superuser session or the `DEBUG_TOKEN` header and return 404 to
anyone else, so they are safe to leave deployed. They make small real API calls,
so do not poll them. Check `/debug/storage` first after any platform change.

## Bringing existing data across

```bash
fly ssh sftp get /data/vero.db ./vero.db -a veros    # export from Fly
DATABASE_URL='postgres://…' BLOB_READ_WRITE_TOKEN='…' STORAGE_BACKEND=vercel_blob \
  python scripts/migrate_to_postgres.py ./vero.db --uploads ./uploads
```

The script preserves every primary key, converts SQLite's integer booleans and
string dates to real Postgres types, fast-forwards the identity sequences so the
next insert does not collide, and repoints each receipt at its new storage
prefix. It skips rows that already exist, so a failed run can just be repeated.

Receipts created before the migration recorded an absolute disk path; the audit
trail still serves those from the filesystem, so nothing breaks mid-transition.

## Known limits and rough edges

- **Uploads are capped at 4.5 MB per request.** This is a hard Vercel limit on
  the request body, not something the app can raise. Bigger files need the
  browser to upload straight to blob storage using Vercel's JavaScript client —
  not yet implemented. Until then a larger file fails at the platform edge.
- **The blob backend speaks a semi-documented REST API.** Vercel ships no
  Python SDK, so `carelog/storage.py` talks to the same HTTP endpoints the JavaScript
  SDK uses, with the API version pinned in `VERCEL_BLOB_API_VERSION`. If Vercel
  moves it, `/debug/storage` will show the failure and the version can be
  changed without touching code. The `Storage` interface means swapping in S3
  or R2 later is one new class.
- **Cold starts.** The first request after idle pays Python import time.
- **Schema changes** are applied by `flask --app app init-db`, which only ever
  adds missing tables. Anything destructive (dropping or retyping a column)
  needs a real migration tool — Alembic is the natural next step.

## Running elsewhere

Nothing here is Vercel-only. With no `DATABASE_URL` the app uses SQLite; with no
`BLOB_READ_WRITE_TOKEN` it stores evidence on disk; with `IMPORT_WORKER=thread`
it runs imports in-process. That combination is exactly the Fly deployment, so
the Dockerfile and `fly.toml` still work as a fallback.
