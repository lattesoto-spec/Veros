"""Reviewer-built mapping specs.

Anyone who can import can tell CareMin what each column means, for any layout,
without an external service. The choices become an ordinary mapping spec, so
they run through the same engine, validation and confirmation as every import.
"""

from .mapping import MappingError, apply_header_overrides, run_spec
from .reader import rebuild_with_header

ALWAYS_TRUE = "__always_true__"

_DATE = {"parse": "date"}
_TIME = {"parse": "time"}
_BOOLEAN = {"parse": "boolean", "required": True}
_NUMBER = {"parse": "number"}
_MINUTES = {"parse": "duration", "unit": "minutes"}

KINDS = {
    "shifts": {
        "label": "Staff shifts or timesheet",
        "fields": [
            {"name": "staff_id", "label": "Staff ID", "required": True},
            {"name": "staff_name", "label": "Staff name"},
            {"name": "role", "label": "Role or position", "required": True, "rule": {"normalize": "role"}},
            {"name": "date", "label": "Work date", "required": True, "rule": _DATE},
            {"name": "start_time", "label": "Start time", "rule": _TIME, "help": "Use the actual start, not the rostered one."},
            {"name": "end_time", "label": "Finish time", "rule": _TIME},
            {"name": "hours", "label": "Duration in hours", "rule": {"parse": "duration", "unit": "hours"}, "stores": "minutes"},
            {"name": "minutes", "label": "Duration in minutes", "rule": _MINUTES},
            {"name": "duration_hhmm", "label": "Duration as h:mm", "rule": {"parse": "duration", "unit": "h:mm"}, "stores": "minutes"},
            {"name": "break_minutes", "label": "Unpaid break", "rule": _MINUTES,
             "always": "These times already exclude unpaid breaks", "always_value": "0"},
            {"name": "is_direct_care", "label": "Direct care", "required": True, "rule": _BOOLEAN,
             "always": "Every row in this sheet is direct care"},
            {"name": "is_agency", "label": "Agency", "rule": _BOOLEAN},
            {"name": "pay_category", "label": "Pay category", "help": "Needed when payroll has several lines for one person on one day."},
            {"name": "labour_cost", "label": "Labour cost", "rule": _NUMBER},
        ],
    },
    "staff": {
        "label": "Staff directory and credentials",
        "fields": [
            {"name": "staff_id", "label": "Staff ID", "required": True},
            {"name": "staff_name", "label": "Staff name"},
            {"name": "role", "label": "Role or position", "rule": {"normalize": "role"}},
            {"name": "employment_type", "label": "Employment type"},
            {"name": "classification", "label": "Award classification"},
            {"name": "registration_number", "label": "Ahpra registration number"},
            {"name": "registration_expiry", "label": "Registration expiry", "rule": _DATE},
        ],
    },
    "residents": {
        "label": "Resident list",
        "fields": [
            {"name": "resident_id", "label": "Resident ID", "required": True},
            {"name": "name", "label": "Resident name", "required": True},
            {"name": "ancc_class", "label": "AN-ACC class"},
            {"name": "admitted_date", "label": "Admission date", "rule": _DATE},
            {"name": "discharged_date", "label": "Discharge date", "rule": _DATE},
        ],
    },
    "resident_days": {
        "label": "Daily resident census",
        "fields": [
            {"name": "date", "label": "Census date", "required": True, "rule": _DATE},
            {"name": "resident_id", "label": "Resident ID", "required": True},
            {"name": "resident_name", "label": "Resident name"},
            {"name": "occupied", "label": "Occupied bed day", "required": True, "rule": _BOOLEAN,
             "always": "Every row in this sheet is an occupied bed day"},
            {"name": "service_type", "label": "Service type", "required": True,
             "always": "Every resident in this sheet is a permanent AN-ACC resident",
             "always_value": "permanent",
             "help": "Rows without a service type do not count as occupied bed days."},
            {"name": "leave_type", "label": "Leave type"},
            {"name": "leave_day_number", "label": "Leave day number", "rule": _NUMBER},
            {"name": "ancc_class", "label": "AN-ACC class"},
            {"name": "exclusion_reason", "label": "Exclusion reason"},
        ],
    },
    "care_episodes": {
        "label": "Delivered care log",
        "fields": [
            {"name": "date", "label": "Care date", "required": True, "rule": _DATE},
            {"name": "resident_id", "label": "Resident ID", "required": True},
            {"name": "resident_name", "label": "Resident name"},
            {"name": "care_type", "label": "Care type"},
            {"name": "care_category", "label": "Care category"},
            {"name": "staff_id", "label": "Staff ID"},
            {"name": "staff_name", "label": "Staff name"},
            {"name": "role", "label": "Role", "rule": {"normalize": "role"}},
            {"name": "start_time", "label": "Start time", "rule": _TIME},
            {"name": "end_time", "label": "End time", "rule": _TIME},
            {"name": "minutes", "label": "Duration in minutes", "rule": _MINUTES},
            {"name": "hours", "label": "Duration in hours", "rule": {"parse": "duration", "unit": "hours"}, "stores": "minutes"},
            {"name": "duration_hhmm", "label": "Duration as h:mm", "rule": {"parse": "duration", "unit": "h:mm"}, "stores": "minutes"},
        ],
    },
}

DATE_FORMATS = [
    ["", "Detect it (dates that could be either way are rejected)"],
    ["%d/%m/%Y", "Day first: 31/08/2026"],
    ["%d/%m/%y", "Day first, short year: 31/08/26"],
    ["%d-%m-%Y", "Day first with dashes: 31-08-2026"],
    ["%Y-%m-%d", "Year first: 2026-08-31"],
    ["%d %b %Y", "Written month: 31 Aug 2026"],
    ["%m/%d/%Y", "Month first: 08/31/2026"],
]


def public_catalogue() -> dict:
    return {
        "kinds": {
            kind: {
                "label": definition["label"],
                "fields": [
                    {key: field[key] for key in ("name", "label", "required", "help", "always") if key in field}
                    for field in definition["fields"]
                ],
            }
            for kind, definition in KINDS.items()
        },
        "date_formats": DATE_FORMATS,
        "always_true": ALWAYS_TRUE,
        "duration_units": [["minutes", "Minutes: 30"], ["hours", "Hours: 0.5"], ["h:mm", "Hours:minutes: 0:30"]],
    }


def _candidates(sheet) -> list[dict]:
    """Each row that could be the header, with the columns it would produce."""
    if not sheet.raw_rows:
        return [_candidate(sheet, sheet.header_row_index)]
    return [_candidate(rebuild_with_header(sheet, row), row) for row in range(len(sheet.preview))]


def _candidate(sheet, row) -> dict:
    examples = {}
    for record in sheet.rows[:20]:
        for header in sheet.headers:
            value = str(record.get(header) or "").strip()
            if value:
                examples.setdefault(header, value)
    return {"row": row, "headers": sheet.headers, "examples": examples}


def describe_file(filename, sheets, known, targets) -> dict:
    by_sheet = {target.get("sheet"): target for target in targets or []}
    return {
        "filename": filename,
        "known": known,
        "sheets": [
            {
                "name": sheet.name,
                "row_count": len(sheet.rows),
                "header_row": sheet.header_row_index,
                "grid": [{"row": i, "cells": cells} for i, cells in enumerate(sheet.preview)],
                "candidates": _candidates(sheet),
                "mapping": target_to_mapping(by_sheet.get(sheet.name), sheet),
            }
            for sheet in sheets
        ],
    }


def target_to_mapping(target, sheet) -> dict:
    """Show an existing interpretation in the same terms the reviewer edits."""
    if not target:
        return {"kind": "", "header_row": sheet.header_row_index, "fields": {}, "date_format": "",
                "filter_column": "", "filter_values": ""}
    fields, date_format = {}, ""
    for name, rule in target.get("fields", {}).items():
        if rule.get("source") == "constant":
            fields[name] = ALWAYS_TRUE
            continue
        if name == "minutes":
            if rule.get("unit") == "h:mm":
                name = "duration_hhmm"
            elif rule.get("unit") == "hours" or rule.get("multiply") == 60:
                name = "hours"
        if rule.get("column"):
            fields[name] = rule["column"]
        if rule.get("parse") == "date" and rule.get("format"):
            date_format = rule["format"]
    row_filter = target.get("row_filter") or {}
    return {
        "kind": target.get("kind", ""),
        "header_row": target.get("header_row", sheet.header_row_index),
        "fields": fields,
        "date_format": date_format,
        "filter_column": row_filter.get("column", ""),
        "filter_values": ", ".join(row_filter.get("include_values") or []),
        "units": {name: rule.get("unit") or ("hours" if rule.get("multiply") == 60 else "")
                  for name, rule in target.get("fields", {}).items() if name in ("minutes", "break_minutes")},
    }


def _shift_problems(chosen, label) -> list[str]:
    start, end = bool(chosen.get("start_time")), bool(chosen.get("end_time"))
    duration = [key for key in ("hours", "minutes", "duration_hhmm") if chosen.get(key)]
    if start != end:
        return [f"{label}: choose both the start and finish time columns."]
    if not start and not duration:
        return [f"{label}: choose start and finish times, or a duration column."]
    if len(duration) > 1:
        return [f"{label}: choose one duration column and unit, not several."]
    if start and not chosen.get("break_minutes"):
        return [f"{label}: choose the unpaid break column, or confirm the times already exclude breaks."]
    return []


def _build_target(mapping, sheet, problems):
    kind = mapping.get("kind")
    definition = KINDS.get(kind)
    label = sheet.name
    if definition is None:
        problems.append(f"{label}: choose what this sheet contains.")
        return None
    header_row = mapping.get("header_row")
    usable_row = isinstance(header_row, int) and not isinstance(header_row, bool)
    if sheet.raw_rows and (not usable_row or not 0 <= header_row < len(sheet.raw_rows)):
        problems.append(f"{label}: choose the row that holds the column names.")
        return None
    columns = rebuild_with_header(sheet, header_row).headers if sheet.raw_rows else sheet.headers
    chosen = {name: value for name, value in (mapping.get("fields") or {}).items() if value}
    units = mapping.get("units") or {}
    if not isinstance(units, dict):
        problems.append(f"{label}: duration units must be selected by field.")
        units = {}
    allowed = {field["name"]: field for field in definition["fields"]}
    fields = {}
    for name, column in chosen.items():
        field = allowed.get(name)
        if field is None:
            problems.append(f"{label}: {name} is not a field of this record type.")
        elif column == ALWAYS_TRUE and field.get("always"):
            fields[name] = {"source": "constant", "value": field.get("always_value", "true")}
            if field.get("rule", {}).get("parse"):
                fields[name]["parse"] = field["rule"]["parse"]
            if field.get("rule", {}).get("unit"):
                fields[name]["unit"] = field["rule"]["unit"]
        elif column not in columns:
            problems.append(f"{label}: column {column!r} is not in the chosen header row.")
        else:
            rule = {"column": column, **field.get("rule", {})}
            if name == "break_minutes":
                from .durations import UNITS
                unit = units.get(name)
                if unit not in UNITS:
                    problems.append(f"{label}: choose minutes, hours or h:mm for the unpaid break column.")
                rule["unit"] = unit
            if rule.get("parse") == "date" and mapping.get("date_format"):
                rule["format"] = mapping["date_format"]
            fields[field.get("stores", name)] = rule
    for field in definition["fields"]:
        if field.get("required") and field["name"] not in chosen:
            problems.append(f"{label}: choose a column for {field['label'].lower()}.")
    if kind == "shifts":
        problems.extend(_shift_problems(chosen, label))
    elif kind == "care_episodes" and sum(bool(chosen.get(name)) for name in ("minutes", "hours", "duration_hhmm")) > 1:
        problems.append(f"{label}: choose one duration column and unit, not several.")
    target = {"kind": kind, "sheet": sheet.name, "fields": fields}
    if kind == "shifts" and chosen.get("break_minutes") == ALWAYS_TRUE:
        target["break_basis"] = "Reviewer confirmed these times already exclude unpaid breaks."
    if sheet.raw_rows:
        target["header_row"] = header_row
    filter_column = (mapping.get("filter_column") or "").strip()
    filter_values = [v.strip() for v in str(mapping.get("filter_values") or "").split(",") if v.strip()]
    if filter_column and filter_values:
        if filter_column not in columns:
            problems.append(f"{label}: the filter column is not in the chosen header row.")
        target["row_filter"] = {"column": filter_column, "include_values": filter_values}
    elif filter_column or filter_values:
        problems.append(f"{label}: a row filter needs both a column and the values to keep.")
    return target


def build_spec(mappings, sheets) -> tuple[dict | None, list[str]]:
    """Turn reviewer choices into a spec, or say exactly what is wrong."""
    if not isinstance(mappings, list):
        return None, ["The column mapping could not be read. Reload and try again."]
    by_name = {sheet.name: sheet for sheet in sheets}
    problems, targets = [], []
    for mapping in mappings:
        sheet = by_name.get(mapping.get("sheet")) if isinstance(mapping, dict) else None
        if sheet is None:
            problems.append("A mapped sheet is no longer in this file. Reload and try again.")
            continue
        target = _build_target(mapping, sheet, problems)
        if target:
            targets.append(target)
    if not targets and not problems:
        problems.append("Choose what at least one sheet contains.")
    if problems:
        return None, problems
    spec = {"targets": targets, "reasoning": "reviewer-built mapping"}
    try:
        results = run_spec(spec, apply_header_overrides(spec, sheets))
    except MappingError as error:
        return None, [str(error)]
    for result in results:
        if result.row_errors:
            problems.append(
                f"{result.sheet}: {len(result.row_errors)} of {result.rows_seen} rows could not be read. "
                f"First problem: {result.row_errors[0]}"
            )
        elif not result.records:
            problems.append(f"{result.sheet}: no rows remain after the row filter.")
    return (None, problems) if problems else (spec, [])
