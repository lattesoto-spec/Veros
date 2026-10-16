# Move from one deployment to two repositories

## Prepared local layout

```text
Active/
  carelog/           existing Git repo → lattesoto-spec/Veros → existing Flask Vercel project
  caremin-frontend/  independent local Git repo → NEW private GitHub repo → NEW Vite Vercel project
  caremin-local-archive-2026-10-15/  preserved local data and generated reports; do not publish
```

Nothing has been committed, pushed or changed in Vercel/GitHub by this cleanup. The backend retains its history and remote. The new frontend needs its first commit and remote. The frontend was previously untracked, so no history-rewriting operation is needed.

The visitor opens the frontend domain. Its Vercel edge proxies `/api/*` and `/healthz` to the backend; all other app paths load the static SPA. Sessions, CSRF tokens, uploads and downloads use that same public hostname. Vercel documents [external rewrites](https://vercel.com/docs/routing/rewrites) and [environment-driven programmatic configuration](https://vercel.com/docs/project-configuration/vercel-ts).

## 1. Prevent an accidental production replacement

Before pushing backend changes to `main`, open the **existing** Vercel project and open **Settings → Environments → Production → Branch Tracking** and turn off **Auto-assign Custom Production Domains**. Record the current working deployment URL for rollback. This lets a production build be tested before receiving customer traffic. See [staged deployment promotion](https://vercel.com/docs/deployments/promoting-a-deployment).

Do not simply push a backend-only replacement to the live project with automatic promotion enabled: the old website routes will stop serving HTML.

Keep the current project, Neon connection, Blob integration, session secret and MFA encryption key. Do not create a second production database or rotate keys as part of this split.

## 2. Publish the frontend repository

On GitHub create an **empty private** repo, for example `lattesoto-spec/caremin-frontend`, without an initial README/gitignore. In the new local folder, review and commit the source:

```sh
cd ../caremin-frontend
git status --short
git add .
git diff --cached --stat
git commit -m "Extract static CareMin frontend"
git remote add origin https://github.com/lattesoto-spec/caremin-frontend.git
git push -u origin main
```

Use the URL GitHub actually gives you if you choose another name. The directory is already initialized as an independent repository; it contains no backend secrets. Only the frontend source and lockfile should be staged, not `node_modules`, `.env` or `dist`.

## 3. Prepare the backend build

In `carelog`, review both staged and unstaged work: this checkout already had staged user changes before the refactor. `git add -A` is needed to reconcile those staged changes with the final deletions, but inspect the resulting diff before committing.

```sh
cd ../carelog
git status --short
git add -A
git diff --cached --stat
git diff --cached --check
git commit -m "Separate Flask API from static frontend"
git push origin main
```

Keep Vercel's root directory `.`. Framework: **Flask**; build command: **python build.py**; output directory: **unset**; dependency install: framework default. The checked-in backend config replaces the earlier combined Services config.

Configure backend Preview and Production as listed in `deployment.md`. Set `PUBLIC_APP_ORIGINS` to the new frontend's exact project hostname plus your future public custom domain. Use separate values for preview frontend hosts. Changing this list requires a backend redeploy.

Wait for the staged backend production build, then check its own `/healthz`. Use its unique deployment URL as the initial frontend `BACKEND_ORIGIN` while the old site remains live.

## 4. Create the frontend Vercel project

Import the new frontend GitHub repo into a **new** Vercel project:

| Setting | Value |
|---|---|
| Root directory | `.` |
| Framework | Vite |
| Install | `npm ci` |
| Build | `npm run build` |
| Output | `dist` |
| Production branch | `main` |

The frontend uses `vercel.mjs`; do not add a competing `vercel.json`.

Set only these routing variables, never database/session/AI credentials:

- **Production:** `BACKEND_ORIGIN=https://<staged-backend-deployment>` initially.
- **Preview:** `BACKEND_PREVIEW_ORIGIN=https://<isolated-backend-preview>`.
- **When backend deployment protection applies:** `BACKEND_BYPASS_SECRET` set to that backend environment's Protection Bypass for Automation secret. It is sent by the edge proxy, never by browser JavaScript. Preserve application authentication and CSRF checks.

No production fallback exists for preview builds: a preview without `BACKEND_PREVIEW_ORIGIN` fails configuration intentionally. For preview frontend access, add its exact deployment hostname or a stable preview hostname to the preview backend's `PUBLIC_APP_ORIGINS` and redeploy that backend. Do not allow every `*.vercel.app` host.

The initial frontend project hostname is known after creating the Vercel project. If needed, complete that configuration, add it to the backend origin list and redeploy both before testing.

## 5. Test, then move the public domain

On the **new frontend** URL, verify sign-in/MFA, facility switching, imports, polling, receipts, downloads and `/dashboard` reload. Inspect failed `/api` responses if login fails: HTML often means deployment protection or a bad rewrite; a 400 token error can mean a missing trusted frontend origin. Validate with preview data first.

Then:

1. Move the user-facing custom domain from the old Vercel project to the new frontend project. Keep any dedicated backend domain on the backend. Vercel may request DNS verification.
2. Promote the tested backend production deployment after the public domain is no longer attached to that project.
3. Point frontend `BACKEND_ORIGIN` to the backend's stable project `.vercel.app` address (or dedicated backend domain), then redeploy the frontend. Keep the matching trusted frontend origins on the backend.
4. Re-test the custom domain, then re-enable automatic production promotion for future backend releases if desired.

If you only use the old project's generated `.vercel.app` address, that address now identifies the backend; use the new frontend project's address for the website. A custom domain gives you an independent user-facing URL.

For rollback, restore the old working deployment and return the custom domain to the old project. Schema changes in this refactor are additive, but review the actual migration before rolling back. Never delete Neon, Blob or the old deployment during this cutover.

## Ongoing work

Edit and push UI code only from `caremin-frontend`. Edit and push Python/API code only from `carelog`. Deploy API changes compatibly first, then frontend changes. Both repos have their own dependencies, tests, environment example and deployment configuration; neither build reads its sibling directory.

## Verification completed locally

- 45 backend unit tests and 3 PostgreSQL integration tests passed.
- Seven mapping fixtures still produce identical totals: 6,000 direct-care minutes over three days.
- Eight frontend and deployment-routing tests passed; independent production build succeeded.
- Clean backend upload bundle migrated an isolated PostgreSQL database and returned HTTP 200 from `/healthz`.
- Both Vercel configuration objects validated against the official schema.
- Frontend dependency audit reported no known vulnerabilities.

Live Vercel proxying, domain assignment and production connectivity have not been exercised. GitHub repositories, remote branches, production data and Vercel settings have not been changed.
