"""Conservative classification of the fields actually used as care evidence."""

from .fingerprint import normalize_header

WORKED_VALUES = "worked"
ROSTERED_VALUES = "rostered"
UNVERIFIED_VALUES = "unverified"

ACTUAL_PAIRS = (
    {"actual_start", "actual_finish"}, {"actual_start", "actual_end"},
    {"actual_start_time", "actual_end_time"}, {"clock_in", "clock_out"},
    {"clocked_in", "clocked_out"}, {"punch_in", "punch_out"},
)
PLANNED_TERMS = {"scheduled", "schedule", "roster", "planned", "forecast", "budget"}


def classify_shift_evidence(filename: str, sheet, target=None) -> tuple[str, str]:
    """Use mapped time fields, never unrelated actual/payroll headers.

    Without a mapping this helper can describe an unambiguous layout; the
    ingestion pipeline always supplies the mapping that produced the rows.
    """
    headers = {normalize_header(h) for h in sheet.headers}
    source = normalize_header(f"{filename} {sheet.name}")
    source_tokens = set(source.split("_"))
    if source_tokens & {"budget", "forecast", "budgeting", "forecasting"}:
        return ROSTERED_VALUES, "budget or forecast source; not actual-worked evidence"
    fields = (target or {}).get("fields", {})
    mapped_times = {
        normalize_header(fields.get(key, {}).get("column", ""))
        for key in ("start_time", "end_time")
    } - {""}
    duration_column = normalize_header(fields.get("minutes", {}).get("column", ""))
    actual_pair = any(pair == mapped_times for pair in ACTUAL_PAIRS) if target is not None else any(
        pair.issubset(headers) for pair in ACTUAL_PAIRS
    )
    planned_headers = any(set(h.split("_")) & PLANNED_TERMS for h in headers)
    mapped_planned = any(set(h.split("_")) & PLANNED_TERMS for h in mapped_times | {duration_column})
    if mapped_planned or (planned_headers and not actual_pair):
        return ROSTERED_VALUES, "scheduled or planned fields are present without mapped actual start/finish"
    support = sorted(headers & {
        "paid_hours", "hours_worked", "worked_hours", "approval_status",
        "approved_status", "pay_code", "pay_category", "payroll_status",
        "labour_cost", "labour_cost_ex_gst", "gross_cost",
    })
    worked_source = bool(source_tokens & {"worked", "timesheet", "timekeeping", "payroll", "clock"}) or "time_entry" in source or "time_entries" in source
    if actual_pair and (support or worked_source):
        return WORKED_VALUES, "mapped actual start/finish fields in a time-and-attendance source"
    # Generic time columns need corroboration and no planned signals. A mapped
    # duration must itself identify paid/worked time (not an arbitrary Hours).
    duration_actual = duration_column in {"paid_hours", "hours_worked", "worked_hours", "worked_minutes", "paid_minutes"}
    if worked_source and not source_tokens & PLANNED_TERMS and support and (not target or len(mapped_times) == 2 or duration_actual):
        return WORKED_VALUES, f"{support[0].replace('_', ' ')} in a worked-time source"
    if source_tokens & PLANNED_TERMS:
        return UNVERIFIED_VALUES, "the file name suggests a roster, but its columns do not say; verify it before it counts"
    return UNVERIFIED_VALUES, "source does not contain enough explicit actual-worked evidence"
