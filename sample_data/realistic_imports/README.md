# Realistic CareMin import samples

All names, identifiers and records in this directory are fictional. The files model one synthetic Australian residential aged-care home for July 2026 and are safe to use in demonstrations.

## Minimum compliance inputs

1. `resident_census_july_2026.xlsx` — resident master plus a daily occupied-bed-day ledger.
2. `worked_staffing_july_2026.xlsx` — approved time entries plus the employee and Ahpra credential directory. CareMin classifies its shift rows as actual worked automatically.

## Optional reconciliation input

`care_delivery_july_2026.xlsx` contains resident-level delivered-care evidence. It helps compare documented care against worked staffing, but it is not required for the compliance numerator or occupied-bed-day denominator.

The workbooks deliberately look like exports from three unrelated systems. They contain title rows, summary sheets, unfamiliar headers, extra business columns, varied role labels, agency staff, leave records, non-care rows, overnight shifts and excluded resident days. Their headers do not match CareMin's built-in exact presets, so they exercise the format-learning path.

## Approximate scale

- Resident register: 50 residents
- Daily census ledger: 1,485 rows
- Staffing export: 764 approved worked rows and 8 non-worked/exception rows
- Care activity export: 4,809 completed activities and 46 exception rows (4,855 total)

These are import test inputs, not expected regulatory results. Review the mapping summary, automatic evidence classification and row warnings after each upload before relying on the figures.
