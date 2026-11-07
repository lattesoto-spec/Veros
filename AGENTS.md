# CareMin agent instructions

Read README.md and docs/architecture.md before changing architecture. This is regulated-domain monitoring software: ambiguous inputs must be excluded or rejected visibly, never silently included.

## Structure and development

- This repo is backend only. Static React + TypeScript lives in the independent sibling `../caremin-frontend` repository. Neither build may depend on its sibling.
- The existing GitHub/Vercel project remains the backend. Read docs/repository-split.md before changing deployment connections or domains.
- Flask API factory in `carelog/app.py`; routes in `carelog/api/`, workspace helpers in `carelog/services/`, persistence in `models.py`/`database.py`.
- `domain/` holds calculations and report builders without Flask request state.
- Root `app.py` is the backend entrypoint; root `build.py` runs additive deployment migrations.
- PostgreSQL is the only runtime database. SQLite is permitted only in isolated tests.
- `README.md` has current run/test commands. Do not restore removed templates, prototype proposals or legacy migration scripts.

## Required invariants

1. Read customer data via current organisation/facility scope. URL IDs require `owned_or_404` or an equivalent explicit tenant predicate. Never select the first global facility.
2. `domain/eligibility.py` alone decides whose minutes count. Unknown roles such as assistant/support stay pending and excluded. Direct-care flags never override an ineligible role.
3. Care minutes are total eligible **worked** minutes divided by occupied bed days. Keep rostered/unverified time and care episodes separate. Preserve resident-day exclusions and nursing registration/effective dates.
4. All three quarterly tests must pass: total target, RN-only 90% of RN target, RN+EN 100%. Exactly 30 minutes is a reportable RN gap.
5. Bump `CALC_VERSION` (2026.11.5) whenever arithmetic outputs change. Never change test expectations simply to make changed totals pass.
6. The analyzer sees the grid with data values masked to their shape (`ingestion/masking.py`) and can correct header detection. External mapping is off until an administrator opts in; reviewers can always map columns from the raw grid in the app. Preserve file/sheet/page/row provenance. Parser versions are part of cache identity.
7. Xberg 1.3.9 extracts document tables; structured JSON/SQL/SQLite readers remain necessary. Do not silently accept extraction warnings or OCR transcription as care evidence.
8. Care-data batches and their receipts commit atomically. Progress updates must use a separate session. Preserve original files even when an import fails.
9. Session authentication, MFA, CSRF and backend permission checks remain required. Serialize ORM fields with allowlists; never expose credential fields. Platform API credentials come from the environment only.

## Deployment

Keep Vercel where practical; this repo deploys Flask only in `syd1`. The separate frontend proxies `/api` and `/healthz`; configured PUBLIC_APP_ORIGINS and CSRF tokens are both required on HTTPS mutations. Deploy from main. Preview and Production variables/databases are separate. Check `/healthz` first. Verify the uploaded tree using the rsync procedure in docs/deployment.md after changing build/deploy files. `.vercelignore` excludes tests/scripts/docs; runtime dependencies must live elsewhere. Schema changes require additive migration coverage; destructive changes need a deliberate migration, not `create_all()`.

Vercel Blob protocol is implemented in storage.py. If changing it, inspect the shipped `@vercel/blob` source: pathname is a query parameter, access header is `x-vercel-blob-access`, API version 12, store ID comes from token or configured store ID without `store_` prefix. Integration-prefixed tokens are supported. Do not guess the protocol from errors.

## Known limits

Not GPMS-ready: source completeness and allocation still need review. No notifications/email recovery, SSO, durable queue, automatic retention, soft deletion or browser-direct large uploads. Resident-day ledgers and worked/rostered separation ARE implemented; do not describe them as absent. Explain verified results and unverified deployment behavior plainly.
