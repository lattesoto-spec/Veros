# CareMin product changes

The complete UI-ordered feature inventory lives in `FEATURES.md` in the independent `caremin-frontend` repository. This file records backend behavior supporting those screens.

## 2026-11-07

1. **Imports / Facilities / Settings:** explicitly validate both verified facility targets before any mutation. Non-finite, missing, non-positive or inconsistent targets are rejected. Synthetic two-file demo sources generate in temporary test directories.
2. **Dashboard / Care minutes:** exact Decimal comparisons determine all three test verdicts; display rounding never determines a pass. Census gaps, missing active residents, unexplained count drops and unverified service/leave evidence make results provisional. Estimated admission/discharge denominators remain visible but cannot establish a verified pass.
3. **RN coverage:** reportability uses the continuous gap across midnight; duration-only evidence never invents coverage intervals.
4. **Imports:** replacement is scoped to supplied staff/date/evidence groups and resident/date episode groups. Unrelated dates remain intact. A zero-usable-row result fails. Duplicate, overlapping, future-worked and impossible duration/break records are rejected. Unknown booleans and ambiguous dates are rejected unless a source date format resolves the latter.
5. **Eligibility:** negated nurse titles and educator/research titles remain pending until reviewed.
6. **Reports:** total per-bed-day minutes use the unrounded combined numerator, and provisional verdicts are explicit. `CALC_VERSION` is `2026.11.1`. Worked minutes remain attributed to the shift start date, including overnight shifts; coverage is evaluated by actual calendar interval. Review boundary allocations against source evidence before external use.

Original source files remain retained on failure. Changes to care data and receipts remain atomic. Local verification does not establish deployed integration behavior or GPMS readiness.

## Quarterly targets, permissions and planning

- Store approved target evidence per facility and quarter in an additive `facility_targets` table. A source and approver/time are retained; changes to an existing quarter require an audited reason. Legacy current-value fields are preserved but do not establish approved historical targets. Providers must enter their quarter notices in Settings. Reporting and forecast comparisons resolve the period's own target record.
- The class-allocation reference applies from 1 October 2025, based on table 3 of the [Department's March 2026 guide](https://www.health.gov.au/sites/default/files/2026-03/care-minutes-and-24-7-registered-nurse-requirements-guides-for-registered-providers-of-residential-care-homes.pdf). Unsupported earlier periods are unavailable for derived reconciliation.
- Identifiable facility records require `view_records`; read-only monitoring responses stay aggregate-only.
- URL connectors are disabled and return a visible explanation directing providers to retained-file imports.
- Agency scenarios remove the observed agency contribution separately from each nursing role before applying additional, bounded RN reductions. Invalid inputs and non-positive scenario occupancy are rejected. `CALC_VERSION` is `2026.11.2`.

## Import review and recovery

- Interpretation creates a persisted review while rolling back all proposed care, receipt and mapping changes. Confirmation reuses the reviewed specification, validates a facility-state fingerprint under its row lock and commits care/receipt/mapping changes atomically. Cancellation retains sources and changes no care evidence. Source corrections use cancellation and a corrected/example export.
- Preview includes detected columns, source basis, evidence class, filtered/usable row counts, date range and replacement counts. Separate files cannot contribute overlapping time for the same person.
- Job history includes failures, cancellations, retained original downloads and 15-minute progress timeout visibility. Retry creates a new review from retained files; superseded running workers re-check their state before commit.
- Receipts show active care-row counts and superseded status while preserving original receipt counts. Providers can read their organisation's target, eligibility and import approval decisions.
- Deletion includes unreceipted job prefixes and preserves database records when storage cannot confirm deletion.

## Source traceability

- A tenant/facility-scoped, paginated worked-time record view exposes counted/withheld treatment and file/sheet/page/row provenance. Staff filters cannot access foreign records.
- Managers can resolve unverified receipt evidence with an audited verification basis, and reverse it to unverified. Original roster/mixed origins cannot be laundered into actual-worked evidence; target-stream conflicts require a new replacement review.
- Provider decision history includes evidence-class changes. Date filters and exports reject invalid/reversed ranges and bound periods to 366 days.

## Guided review and operational clarity

- Dashboard includes a live evidence checklist and a worked-data-age warning. Future/unverified/rostered dates cannot present as the latest included actual-worked date.
- Eligibility exposes per-person withheld minutes and source links. Bulk decisions validate all selected staff before a single atomic commit; approvals require effective dates and valid existing role/registration evidence.
- The eligibility authority refuses explicitly ineligible source roles even if an approval/direct-care flag is present. Importing a changed source role cannot silently reclassify stored history. Use an effective eligibility review or distinct employment-role source identifiers. `CALC_VERSION` is `2026.11.3`.
- Period calculations use bounded batched reads rather than daily database loops; new non-destructive facility/date indexes support them. A PostgreSQL regression measures stable query counts as the period doubles.
- Reports include target quarter/source, exact-comparison wording and overnight attribution. PDF text and spreadsheet references remain literal provider text. RN screens show the actual calculation basis rather than unsupported approval/export placeholders.
- Blank counting flags, malformed facility IDs and invalid reporting months are visibly rejected.

## Browser and release verification

- A guarded synthetic browser-test API runs only against a local PostgreSQL database named `caremin_browser_test`; startup resets that fixture and serves the real session/MFA/CSRF/import/report stack. The frontend's Chromium journey covers confirmation before commit, eligibility, source tracing, PDF download and mobile keyboard behavior, with axe checks on key screens.
- CI runs PostgreSQL integration/migration tests. The separate frontend workflow has independent build and browser-integration jobs; production builds have no sibling source dependency.
- Report coverage does not convert unknown/missing clock dates to zero. Percentages use observed clock dates and identify unavailable dates; readiness remains blocked. `CALC_VERSION` is `2026.11.4`.
- `/healthz` reports an unconfigured HTTP worker or missing Vercel retained-source storage as not ready. If the HTTP worker is unconfigured, uploaded originals are retained on a failed job for later recovery.
- Local copied upload trees migrated isolated PostgreSQL and returned `/healthz` 200; the copied frontend built independently and passed protected-backend route validation. Generated recordings, QA output and test-only sources are excluded from uploads. Linux cloud behavior, live proxy behavior, manual screen-reader testing and provider usability research remain separately unverified until exercised.

## Conservative evidence and quarterly position

- Imports classify the actual mapped start/finish or duration fields. Scheduled columns cannot be counted as worked because an unrelated payroll/actual column exists. Budget and forecast sources remain planned evidence. Duplicate census identities, non-boolean occupancy and conflicting occupied/exclusion fields reject the batch before writes.
- Eligibility automatically approves only exact, unambiguous titles. Modified or management nursing titles require a recorded reviewer decision; pending titles remain withheld regardless of their current reporting bucket. Daily, eligibility-header and evidence-summary withheld totals reconcile.
- Dashboard / Care minutes / Facilities distinguish partial-period on-track/below comparisons from completed quarterly pass/fail. Missing worked dates, withheld eligibility and worked minutes on zero-occupancy dates are explicit limitations; report readiness includes those limitations. Missing approved targets serialize as null. Daily statuses use each date's approved quarterly target, and alerts compare exact minutes before display rounding.
- Scenarios retain zero-minute occupied days in the trailing baseline. Settings accepts targets to two decimal places. `CALC_VERSION` is `2026.11.5`.

## Privacy, tenancy and access boundaries

- Evidence requires a private Vercel Blob store. An access-mode mismatch fails closed; uploads are never retried with public access.
- `owned_or_404` denies NULL-owned evidence and evidence whose facility belongs to another organisation. `flask reconcile-evidence-owners` dry-runs legacy repair and, with `--apply`, inherits only a verified facility owner under audit.
- Facility deletion purges failed and cancelled upload sources and keeps database records if storage cannot confirm deletion.
- Organisation-wide deletion is administrator-only. Clinical Managers and Compliance Officers can record eligibility decisions; individual decisions now audit before/after values and require an explicit approval start date. Legacy automatic classifications of pending titles do not count until reviewed.
- Sessions expire after 30 idle minutes, and a session without an activity timestamp is not trusted.
- Calendar boundaries use the organisation's timezone rather than server UTC.

## Corrections without losing history

- Replaced shifts are retained as `superseded` and voided shifts as `voided`. Both leave the worked, rostered and unverified streams, so no calculation can count them, and each keeps its prior evidence class.
- Review shows shifts in a file's date range that the file omits. Confirming a complete corrected export retires them; otherwise they stay counted. Deleting an import restores what it replaced unless newer evidence covers those dates.
- `POST /api/audit/shift/<id>/void` and `/restore` require `review_eligibility`, a recorded reason and the current facility scope. Restoring is refused when it would overlap or duplicate an active row.
- Review calculates all three tests before and after the file inside the uncommitted transaction, then rolls back. Example values are returned only to users with `view_records`.
- Each upload records a SHA-256 on the job and receipt, verified at import, retry and download. A mismatch is refused and audited.
- A later registration expiry records the unevidenced period since the previous expiry. Those days stay withheld until a reviewer confirms continuity with a basis.
- A `pay_category` column lets payroll carry several duration lines for one person and day, capped at 24 hours. Without it, identical lines remain rejected.
- Duration minutes floor without binary-float loss and break minutes round up. `CALC_VERSION` is `2026.11.6`.
- The dashboard no longer lists storage per receipt; the audit page remains the authority on retained files.
