# Deploying Carelog on Vercel

Run `bash scripts/setup_vercel.sh` for the guided path. This document explains what
each piece is, why it exists, and what to do when something misbehaves.

## The three parts

| Part | What it is | Why |
| --- | --- | --- |
| **Function** | The Flask app, deployed as one Vercel Function from `carelog/app.py` | Vercel finds the `app` object at a supported entrypoint; no wrapper needed |
| **Database** | Neon Postgres, created through Vercel Storage | The only supported database. Managed backups, and no data loss when an instance is recycled |
| **Blob store** | Vercel Blob, **private**, holding retained upload evidence | Serverless has no persistent disk. These are resident care records, so the store must not be public |

Set all three to a **Sydney** region. This is Australian aged-care data, and
region is fixed at creation time for both the database and the blob store —
getting it wrong means recreating them.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Optional explicit Postgres connection string. `postgres://` and `postgresql://` are both accepted and rewritten to the psycopg driver |
| `STORAGE_DATABASE_URL` | Automatically added by Vercel's current Neon Storage connection and preferred by Vercel deployments |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob credential. Connecting a store injects it as `<PREFIX>_READ_WRITE_TOKEN`, where the prefix is whatever you typed at connection time — the app finds the token under any name, preferring a correctly-shaped one |
| `SECRET_KEY` | Signs session cookies. Random, and never the dev default in production |
| `WORKER_SECRET` | Required header on `/import/run/<job>`. Without it, anyone could trigger import workers |
| `ANTHROPIC_API_KEY` | Format learning. Environment only — it is deliberately not settable from the UI |
| `STORAGE_BACKEND` | `local` or `vercel_blob`. Inferred from the token when unset |
| `VERCEL_BLOB_ACCESS` | `private` (default) or `public`. Only an optimisation — a wrong value is detected and corrected automatically |
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

## When a deployment returns FUNCTION_INVOCATION_FAILED

That error means the function could not start, so *every* path fails — a trace
showing `GET /` does not mean the homepage is at fault, only that it was the
first thing requested. Check `/healthz` first: it answers without a database
and lists exactly what is missing.

The usual cause is environment variables missing for the environment being
deployed. **Vercel configures Preview and Production separately**, so a
variable ticked only for Production leaves every preview build without a
database. There is deliberately no local-file fallback, so a missing `DATABASE_URL`
produces a page naming the variable rather than a crash — or worse, an app that
appears to work while discarding every write.

`scripts/setup_vercel.sh` sets every variable for production, preview and
development, which avoids this entirely.

## Blob token naming

Connecting a Blob store to a project does **not** necessarily create a variable
called `BLOB_READ_WRITE_TOKEN`. Vercel injects `<PREFIX>_STORE_ID` and
`<PREFIX>_READ_WRITE_TOKEN`, using the prefix you type when connecting. Setting
the prefix to `BLOB_READ_WRITE_TOKEN` produces
`BLOB_READ_WRITE_TOKEN_READ_WRITE_TOKEN`, and nothing under the plain name.

`carelog/storage.py` therefore searches: the documented name, then anything
ending `_READ_WRITE_TOKEN`, then any variable whose value is shaped like a blob
token — always preferring one that actually parses. `/debug/storage` reports
`token_from`, so you can see which variable it used.

A token that cannot be parsed produces "Cannot get store id from token or
header" from Vercel; the app turns that into a message naming the cause.

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

## Local development

Postgres everywhere — the same engine locally as in production, so a dialect
difference can never reach a customer:

```bash
docker compose up -d          # Postgres on 127.0.0.1:5433
cp .env.example .env
pip install -r requirements.txt
python -m flask --app app init-db
python -m flask --app app bootstrap-org --name "Local" \
    --admin-email you@example.com --password 'a-long-dev-password' --superuser
python app.py                 # http://127.0.0.1:8080
```

`flask --app app seed-demo --password '...'` fills a separate Demo organisation
with sample data.

## Running elsewhere

Nothing here is Vercel-only. Point `DATABASE_URL` at any Postgres; with no
`BLOB_READ_WRITE_TOKEN` evidence is stored on local disk, and with
`IMPORT_WORKER=thread` imports run in-process. That combination is the Fly
deployment, so the Dockerfile and `fly.toml` still work as a fallback — set
`DATABASE_URL` with `fly secrets set`, since there is no volume any more.
