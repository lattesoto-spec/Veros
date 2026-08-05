"""Conservative evidence classification for imported shift sheets.

The upload form should not ask a provider to understand CareMin's internal
evidence streams.  We can classify a source automatically when its structure
contains explicit time-and-attendance or planned-roster signals.  Ambiguous
files remain unverified and therefore cannot inflate compliance figures.
"""

from .fingerprint import normalize_header


WORKED_VALUES = "worked"
ROSTERED_VALUES = "rostered"
UNVERIFIED_VALUES = "unverified"


def classify_shift_evidence(filename: str, sheet) -> tuple[str, str]:
    """Return (evidence_type, human-readable basis) for one shift sheet.

    A filename alone is never enough to turn ambiguous rows into statutory
    actual-worked evidence.  Worked classification needs a corroborating field
    from payroll, approval, clocking or actual-time data.
    """
    headers = {normalize_header(h) for h in sheet.headers}
    source = normalize_header(f"{filename} {sheet.name}")

    actual_time_pairs = (
        {"actual_start", "actual_finish"},
        {"actual_start_time", "actual_end_time"},
        {"clock_in", "clock_out"},
        {"clocked_in", "clocked_out"},
        {"punch_in", "punch_out"},
    )
    worked_support = {
        "paid_hours", "hours_worked", "worked_hours", "approval_status",
        "approved_status", "pay_code", "pay_category", "payroll_status",
        "labour_cost", "labour_cost_ex_gst", "gross_cost",
    }
    has_actual_pair = any(pair.issubset(headers) for pair in actual_time_pairs)
    support = sorted(headers & worked_support)
    worked_source = any(word in source for word in (
        "worked", "timesheet", "time_entry", "timekeeping", "payroll", "clock",
    ))
    if has_actual_pair and (support or worked_source):
        signal = support[0].replace("_", " ") if support else "time-and-attendance source"
        return WORKED_VALUES, f"actual start/finish fields with {signal}"
    if worked_source and support:
        return WORKED_VALUES, f"{support[0].replace('_', ' ')} in a worked-time source"

    planned_pairs = (
        {"scheduled_start", "scheduled_finish"},
        {"scheduled_start_time", "scheduled_end_time"},
        {"roster_start", "roster_end"},
        {"planned_start", "planned_finish"},
    )
    planned_support = headers & {
        "roster_status", "schedule_status", "published_status", "planned_hours",
    }
    planned_source = any(word in source for word in (
        "roster", "schedule", "scheduled", "planned", "published_shifts",
    ))
    if any(pair.issubset(headers) for pair in planned_pairs):
        return ROSTERED_VALUES, "scheduled or planned start/finish fields"
    if planned_source and planned_support:
        return ROSTERED_VALUES, "roster status fields in a planned staffing source"

    return UNVERIFIED_VALUES, (
        "source does not contain enough explicit actual-worked or planned-roster evidence"
    )
