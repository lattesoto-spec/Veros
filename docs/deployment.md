# Backend deployment

## Independent Vercel project

This repository deploys **only Flask** using `vercel.json`, framework `flask`, entrypoint `app.py`, region `syd1`, build command `python build.py` and a 300-second function budget. There is no frontend service, SPA fallback or Node build. Keep the existing GitHub connection to `lattesoto-spec/Veros`, project root `.` and production branch `main`.

Vercel supports [Flask entrypoints and per-function settings](https://vercel.com/docs/frameworks/backend/flask). Clear old project-level output-directory, install and framework overrides that conflict with Flask detection. Dependencies come from `requirements.txt`. Database migration failure stops the build.

## Environment

Set Preview and Production separately; Preview must use its own database and Blob storage.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` / `STORAGE_DATABASE_URL` | PostgreSQL / Neon |
| `SECRET_KEY` | Existing persistent session-signing secret |
| `MFA_ENCRYPTION_KEY` | Existing persistent MFA encryption key; preserve its value |
| `PUBLIC_APP_ORIGINS` | Comma-separated exact HTTPS origins of the matching frontend, no paths/wildcards |
| `WORKER_SECRET` | Import worker shared secret |
| `IMPORT_WORKER=invoke` | Serverless worker dispatch |
| `AUTO_INIT_DB=0` | Migrate in build, not on runtime startup |
| `ANTHROPIC_API_KEY` | Optional. AI-suggested mappings for organisations that opt in; reviewers can map any layout without it |
| Blob read/write token | Retained source evidence; integration-prefixed tokens supported |
| `VERCEL_AUTOMATION_BYPASS_SECRET` | If backend deployment protection blocks worker self-invocation |

Optional: `DEBUG_TOKEN`, contact email overrides, `IMPORT_WORKER_URL` for the **same** environment. Worker dispatch otherwise uses the backend's own deployment URL. Never set it to the frontend or route preview work to production.

`PUBLIC_APP_ORIGINS=https://caremin-frontend.vercel.app,https://app.example.com` is an example, not a configured live domain. The backend checks the frontend Origin/Referer and the CSRF token. Cookies remain host-only; the frontend proxies the complete `/api` path. No CORS wildcard or browser-side API origin is needed.

## Uploaded bundle verification

Run from this repository with an activated backend environment:

```sh
bundle=$(mktemp -d /tmp/caremin-backend-bundle.XXXXXX)
rsync -a --exclude-from=.vercelignore --exclude .git ./ "$bundle/"
cd "$bundle"
python build.py
python -c 'from app import app; print(app.test_client().get("/healthz").status_code)'
```

Use an isolated PostgreSQL database for migration checks. Never copy local `.env` files or production credentials. With no database, the build prints an explicit skip and health returns 503; that is failure-mode verification, not deployment readiness. A real deployment requires configured storage, credentials and a healthy schema.

Xberg includes native code. Validate the Linux function size and parser in Preview. Hosted requests are limited to 4 MB by CareMin to leave multipart overhead under Vercel's 4.5 MB edge limit. Source downloads and report generation remain authenticated API responses.

After deployment verify `/healthz`, sign-in/MFA **through the frontend hostname**, an offline sample import, job completion, source download, reports and deep-link reload. JSON resources use `Cache-Control: no-store`. Storage/network diagnostics require a superuser or `x-debug-token`.

## Container alternative

`Dockerfile` builds this API alone and runs gunicorn on port 8080. Keep PostgreSQL and persistent object storage external. The frontend proxy can point to this HTTPS backend without changing browser code.
