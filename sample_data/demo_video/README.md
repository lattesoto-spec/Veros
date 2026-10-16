# CareMin demo-video dataset

Use [`caremin_demo_actual_worked_export.xlsx`](caremin_demo_actual_worked_export.xlsx)
for the short product walkthrough. Every person, identifier and record is
fictional.

## Recording setup

1. Sign in to an empty demonstration organisation and open **Imports**.
2. If the organisation has no facility yet, enter `Banksia Grove Residential Care`.
3. Upload the single workbook and wait for the import receipt.
4. Open the dashboard, then open the RN coverage review for 25 August 2026.
5. Open the audit trail to show the retained source workbook and source-row lineage.

The workbook matches a deterministic built-in mapping, so the recorded import
does not depend on a live AI-format-learning call.

## Expected CareMin story

- Reporting data: 1 July to 25 August 2026.
- 42 residents and 2,352 occupied bed days in the current reporting period.
- Complete statutory target reference ledger for 1 March to 31 May 2026.
- Independently reconciled targets: 215 total and 44 RN minutes per occupied bed day.
- Overall result: **213.5 total minutes** — below the 215 target.
- RN-only result: **42.8 minutes** — passes the 90% nursing threshold of 39.6.
- RN + EN result: **64.2 minutes** — passes the 44-minute nursing target.
- The trend contains above-target Thursdays and several clearly below-target days.
- Tuesday 25 August contains a reportable **07:00–08:00 RN coverage gap**.
- The original export retains an explicit `Not Worked` RN relief row stating
  that an agency replacement was not secured.

CareMin excludes that non-worked row from all compliance figures. It remains in
the retained workbook solely as evidence explaining the coverage gap.

## Regenerate

From the repository root, with the project requirements installed:

```bash
python -m sample_data.demo_video._build_demo
```

