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


def known_columns(target):
    return (set(target.get("status_decisions", {})) | set(target.get("status_attestations", {}))
            | set(target.get("status_review_required", {}))
            | {f["column"] for f in target.get("status_filters", [])})


def status_columns(sheet, ignored=(), recognised=()):
    return [
        header for header in sheet.headers
        if normalize_header(header) in STATUS_COLUMNS
        or header in recognised
        or (header not in ignored and any(
            normalize_header(str(row.get(header) or "")) in REJECTED for row in sheet.rows
        ))
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
        recognised = known_columns(target)
        columns = status_columns(sheet, target.get("non_status_columns", {}), recognised)
        for header in columns:
            filters = [target.get("row_filter", {})] + target.get("status_filters", [])
            positive = [f for f in filters if f.get("column") == header and f.get("include_values")
                        and all(v and not disallowed(v) for v in f["include_values"])]
            if not positive:
                continue
            included = set.intersection(*[{v.casefold() for v in f["include_values"]} for f in positive])
            seen = {v.casefold() for v in target.get("status_decisions", {}).get(header, {}).get("seen", [])}
            observed = {str(row.get(header) or "").strip().casefold() for row in sheet.rows
                        if str(row.get(header) or "").strip() and not disallowed(row.get(header))}
            if observed - included - seen:
                target.setdefault("status_review_required", {})[header] = {
                    "included": sorted({v for f in positive for v in f["include_values"]})}
                target["status_filters"] = [f for f in target.get("status_filters", []) if f["column"] != header]
                if target.get("row_filter", {}).get("column") == header:
                    target.pop("row_filter")
        target["excluded_status_filters"] = [
            {"column": header, "exclude_values": [
                str(row.get(header) or "").strip() for row in sheet.rows
                if disallowed(row.get(header))
            ]}
            for header in columns
            if normalize_header(header) in STATUS_COLUMNS or header in recognised
        ]
