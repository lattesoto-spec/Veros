"""Status evidence cannot be bypassed by omitting a column from a mapping."""

from .fingerprint import normalize_header

STATUS_COLUMNS = {
    "status", "approval", "approval_status", "approved_status", "shift_status",
    "timesheet_status", "payroll_status", "attendance_status", "record_status",
}
REJECTED = {
    "rejected", "cancelled", "canceled", "declined", "pending", "unapproved",
    "not_approved", "no_show", "noshow", "void", "voided", "deleted", "draft",
    "awaiting_approval", "not_worked",
}


def disallowed(value):
    normalized = "_" + normalize_header(str(value or "")) + "_"
    return any("_" + status + "_" in normalized for status in REJECTED)


def status_columns(sheet):
    return [
        header for header in sheet.headers
        if normalize_header(header) in STATUS_COLUMNS
        or any(disallowed(row.get(header)) for row in sheet.rows)
    ]


def prepare_status_filters(spec, sheets):
    """Always exclude known non-worked statuses; unknown meanings need review.

    The hold stays open even though the preview excludes unsafe rows. A saved
    include filter cannot admit a newly-arriving rejected status next month.
    """
    for target in spec.get("targets", []):
        if target.get("kind") != "shifts":
            continue
        sheet = next(s for s in sheets if s.name == target["sheet"])
        target["excluded_status_filters"] = [
            {"column": header, "exclude_values": [
                str(row.get(header) or "").strip() for row in sheet.rows
                if disallowed(row.get(header))
            ]}
            for header in status_columns(sheet)
        ]
