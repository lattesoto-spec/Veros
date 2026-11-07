# Architecture and import contract

## Web boundary

The frontend is a separate repository (`caremin-frontend`) with its own Node build, static assets and Vercel project. This repository builds only Flask. Neither imports source or dependencies from the other. The frontend has no database credentials or calculation logic. Flask exposes `/api/*`, plus `/healthz`. Page resources return `{view, data, context, csrf_token, messages}`. Mutations return an updated resource or `{navigate, csrf_token}` with a frontend-relative destination. Downloads return binary responses. Failures are JSON, and unauthenticated requests return 401 plus a sign-in destination.

The browser uses same-origin signed sessions and sends CSRF tokens on every mutation. Its Vercel edge proxies `/api` to the backend. HTTPS referrers and supplied Origins must match the backend host or an exact `PUBLIC_APP_ORIGINS` entry; Flask-WTF still validates every browser mutation token. Only the internal shared-secret worker bypasses browser CSRF checks. Only explicitly public account/bootstrap resources bypass login. ORM serialization uses field allowlists; password hashes, MFA secrets, trusted-device tokens and storage paths never enter ordinary resource responses. The MFA enrollment secret is returned only during the authenticated enrollment flow.

API modules own request handling; services own workspace queries; domain modules hold calculations without Flask request state. The API folder's resource names are the frontend's page contract, not server templates.

## Extraction and mapping

`upload → Xberg / structured adapter → Sheet → fingerprint → mapping → validation → persistence`

[Xberg](https://github.com/xberg-io/xberg) is pinned to **1.3.9**, installed from its Python distribution and called locally. CSV/TSV, Excel/ODS and supported document tables go through Xberg. Cache, OCR and text quality transformations are disabled. Parser warnings, extraction errors and documents without tables fail explicitly. Limits bound tables, pages, expanded archives and compression ratios.

CareMin retains its JSON, JSONL, SQL-dump and SQLite adapters because Xberg's general text output does not preserve the record tables these formats need. ZIP handling is bounded, rejects nested archives and never extracts paths onto disk. This adapter boundary allows another extraction engine without changing business rules.

A `Sheet` contains original cells, guessed headers, source row numbers, sheet/page information and parser version. The analyzer sees the grid with data values masked to their shape and can override the guessed header. Reviewers see the unmasked grid in the app and can set the header themselves. Physical row numbers survive blank rows and header correction. Document-table row numbers are relative to the extracted table; page information is retained when supplied by the parser.

Fingerprints include parser identity/version as well as normalized headers. Old cached mappings are not reused after an extraction change. Exact built-in mappings work without AI. An unfamiliar layout waits in the `mapping` state for a reviewer to choose its columns (`ingestion/manual.py`). If an administrator has opted in and the service is configured, the analyzer suggests a mapping first, and if it fails the job returns to the reviewer. Mappings are scoped to the organisation. A stored mapping specification then runs deterministically, and every import still passes review.

CareMin continues to own evidence classification, role eligibility, strict required fields, exception reporting, tenant isolation, source retention, resident-day rules and all calculations. Xberg does not decide which minutes count. Do not enable OCR-derived care records without a reviewed validation workflow.

## Import transaction

A job atomically claims its queued status, locks the destination facility in PostgreSQL, and commits the entire batch's care records, mapping changes and receipt together. Progress writes use a separate session so a status update cannot commit partial care data. Failed batches roll back their care changes and retain their source files and failure status. Duplicate filenames in a batch are rejected to avoid overwriting retained evidence.

The initial invocation prepares an interpretation review, then rolls back every proposed care/receipt/mapping write. Its specifications, evidence classification and replacement impact are stored on the job. A provider confirms or cancels. Confirmation reuses the reviewed specifications and verifies the facility-state fingerprint under its row lock before committing the batch. A changed snapshot requires a new review. Failed/cancelled jobs remain visible with original downloads; a 15-minute progress timeout is shown as stalled and recovery prepares a fresh review from retained files.

A local process uses a worker thread; Vercel invokes the authenticated worker endpoint with `WORKER_SECRET`. This remains an HTTP dispatch mechanism rather than a durable queue: operators must investigate a job stuck in queued/running after a host failure. A production queue/retry policy is a future improvement.

## Calculation invariants

Only approved RN, EN and PCW/AIN direct-care worked time enters care-minute totals. Unknown broad roles stay pending. Nursing registration and effective dates still apply. Total eligible minutes are divided by occupied bed days; daily ratios are never averaged unweighted. All three quarterly tests must pass, using exact minutes and Decimal target comparisons before display rounding. Unverified denominators produce provisional verdicts. RN gaps shorter than 30 minutes are ignored; exactly 30 minutes is reportable, including a continuous gap crossing midnight. Duration-only shifts prove minutes but never clock intervals. `CALC_VERSION` is 2026.11.8. Worked minutes are attributed to the shift start date; coverage uses actual calendar intervals. See [product changes](product-changes.md).
