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
