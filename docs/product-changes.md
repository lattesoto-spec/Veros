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
