"""Declarative mapping engine.

The LLM analyzer does not generate code — it generates a JSON *mapping spec*
describing where each normalized field comes from (which column, what date
format, how values translate). This module is the reusable engine that
executes those specs deterministically. Specs are easy to inspect, store,
version, and re-run with zero AI cost.

Normalized schemas produced:
  shifts:    staff_id, staff_name, role, date, start_time, end_time,
             minutes, is_direct_care
  staff:     staff_id, staff_name, role, employment_type, classification,
             registration_number, registration_expiry
  residents: resident_id, name, ancc_class, admitted_date, discharged_date
  resident_days: date, resident_id, resident_name, occupied, service_type,
                 leave_type, ancc_class, exclusion_reason
  care_episodes: date, resident_id, resident_name, care_type, care_category,
                 staff_id, staff_name, role, start_time, end_time, minutes
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from .reader import Sheet

SHIFT_FIELDS = [
    "staff_id", "staff_name", "role", "source_role", "date",
    "start_time", "end_time", "minutes", "break_minutes",
    "is_direct_care", "is_agency", "labour_cost",
]
STAFF_FIELDS = [
    "staff_id", "staff_name", "role", "source_role", "employment_type",
    "classification", "registration_number", "registration_expiry",
]
RESIDENT_FIELDS = [
    "resident_id", "name", "ancc_class", "admitted_date", "discharged_date",
]
RESIDENT_DAY_FIELDS = [
    "date", "resident_id", "resident_name", "occupied", "service_type",
    "leave_type", "leave_day_number", "ancc_class", "exclusion_reason",
]
CARE_EPISODE_FIELDS = [
    "date", "resident_id", "resident_name", "care_type", "care_category",
    "staff_id", "staff_name", "role", "source_role", "start_time",
    "end_time", "minutes",
]

DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y",
    "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%m/%d/%Y",
]
TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%H%M"]
DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S",
]

TRUE_WORDS = {"true", "1", "yes", "y", "t", "direct", "direct care"}
FALSE_WORDS = {"false", "0", "no", "n", "f", "indirect", "non-direct", "admin"}


class MappingError(Exception):
    pass


@dataclass
class TargetResult:
    kind: str
    sheet: str
    records: list[dict]
    row_errors: list[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_filtered: int = 0
    evidence_type: str | None = None
    evidence_basis: str | None = None


# ---------------------------------------------------------------- parsing


def parse_date_value(s: str, fmt: str | None = None) -> date:
    s = s.strip()
    formats = ([fmt] if fmt else []) + DATE_FORMATS + DATETIME_FORMATS
    for f in formats:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    # Excel serial dates occasionally survive as plain numbers
    if re.fullmatch(r"\d{5}", s):
        return date(1899, 12, 30) + __import__("datetime").timedelta(days=int(s))
    raise ValueError(f"unparseable date: {s!r}")


def parse_time_value(s: str, fmt: str | None = None) -> time:
    s = s.strip()
    formats = ([fmt] if fmt else []) + TIME_FORMATS + DATETIME_FORMATS
    for f in formats:
        try:
            return datetime.strptime(s, f).time()
        except ValueError:
            continue
    raise ValueError(f"unparseable time: {s!r}")


def parse_number(s: str) -> float:
    s = s.strip().replace(",", "")
    m = re.fullmatch(r"(\d+):(\d{2})", s)  # "7:30" as hours:minutes duration
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    return float(s)


def normalize_role(raw: str) -> str:
    s = raw.strip().lower()
    if not s:
        return ""
    tokens = set(re.findall(r"[a-z]+", s))
    if "rn" in tokens or "regist" in s or "nurse practitioner" in s:
        return "RN"
    if tokens & {"en", "een"} or "enrolled" in s:
        return "EN"
    if tokens & {"pca", "pcw", "ain", "cs"} or any(
        w in s for w in ("assistant", "aide", "carer", "care worker", "personal care", "support")
    ):
        return "PCW"
    return raw.strip().upper()


def parse_bool(s: str) -> bool | None:
    v = s.strip().lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return None


# ---------------------------------------------------------------- executor


def _resolve_column(row: dict, headers: list[str], column: str) -> str:
    if column in row:
        return row[column]
    # case-insensitive fallback so the spec survives minor header drift
    low = column.strip().lower()
    for h in headers:
        if h.strip().lower() == low:
            return row[h]
    raise MappingError(f"column not found: {column!r}")


def _raw_field(fs: dict, row: dict, headers: list[str]) -> str:
    source = fs.get("source", "column")
    if source == "constant":
        raw = fs.get("value", "")
        raw = "" if raw is None else str(raw)
    elif source == "combine":
        parts = [
            _resolve_column(row, headers, c).strip() for c in fs.get("columns", [])
        ]
        raw = fs.get("separator", " ").join(p for p in parts if p)
    else:
        raw = _resolve_column(row, headers, fs.get("column", ""))

    return (raw or "").strip()


def _extract_field(fs: dict, row: dict, headers: list[str]):
    raw = _raw_field(fs, row, headers)

    vm = fs.get("value_map")
    if vm:
        key = raw.lower()
        if key in vm:
            return vm[key]

    if raw == "":
        return fs.get("default")

    parse = fs.get("parse", "text")
    if parse == "date":
        return parse_date_value(raw, fs.get("format"))
    if parse == "time":
        return parse_time_value(raw, fs.get("format"))
    if parse == "datetime_date":
        return parse_date_value(raw, fs.get("format"))
    if parse == "datetime_time":
        return parse_time_value(raw, fs.get("format"))
    if parse == "number":
        n = parse_number(raw)
        return n * fs.get("multiply", 1)
    if parse == "boolean":
        b = parse_bool(raw)
        return fs.get("default", True) if b is None else b

    if fs.get("normalize") == "role":
        return normalize_role(raw)
    return raw


def _passes_filter(target: dict, row: dict, headers: list[str]) -> bool:
    rf = target.get("row_filter")
    if not rf:
        return True
    try:
        val = _resolve_column(row, headers, rf.get("column", "")).strip().lower()
    except MappingError:
        return True
    include = rf.get("include_values")
    exclude = rf.get("exclude_values")
    if include is not None:
        return val in [v.lower() for v in include]
    if exclude is not None:
        return val not in [v.lower() for v in exclude]
    return True


def _pick_sheet(target: dict, sheets: list[Sheet]) -> Sheet:
    want = target.get("sheet")
    if want:
        for s in sheets:
            if s.name == want:
                return s
        for s in sheets:
            if s.name.strip().lower() == str(want).strip().lower():
                return s
        raise MappingError(f"sheet not found: {want!r}")
    return sheets[0]


def run_target(target: dict, sheets: list[Sheet]) -> TargetResult:
    kind = target.get("kind")
    if kind not in ("shifts", "staff", "residents", "resident_days", "care_episodes"):
        raise MappingError(f"unknown target kind: {kind!r}")
    sheet = _pick_sheet(target, sheets)
    fields_spec = target.get("fields", {})
    allowed = {
        "shifts": SHIFT_FIELDS,
        "staff": STAFF_FIELDS,
        "residents": RESIDENT_FIELDS,
        "resident_days": RESIDENT_DAY_FIELDS,
        "care_episodes": CARE_EPISODE_FIELDS,
    }[kind]

    result = TargetResult(kind=kind, sheet=sheet.name, records=[])
    for idx, row in enumerate(sheet.rows, start=sheet.header_row_index + 2):
        result.rows_seen += 1
        if not _passes_filter(target, row, sheet.headers):
            result.rows_filtered += 1
            continue
        record, err = {}, None
        for name, fs in fields_spec.items():
            if name not in allowed:
                continue
            try:
                record[name] = _extract_field(fs, row, sheet.headers)
                # Normalisation must never destroy the evidence used by the
                # eligibility classifier. Preserve the exact source title.
                if name == "role":
                    record["source_role"] = _raw_field(fs, row, sheet.headers)
            except (ValueError, MappingError) as e:
                err = f"Row {idx}: {name}: {e}"
                break
        if err:
            result.row_errors.append(err)
            continue
        problem = _check_record(kind, record)
        if problem:
            result.row_errors.append(f"Row {idx}: {problem}")
            continue
        record["_source_row"] = idx  # audit lineage back to the file
        result.records.append(record)
    return result


def _check_record(kind: str, r: dict) -> str | None:
    if kind == "shifts":
        if not r.get("staff_id"):
            return "staff_id is blank"
        if not isinstance(r.get("date"), date):
            return "date missing"
        has_times = isinstance(r.get("start_time"), time) and isinstance(r.get("end_time"), time)
        has_minutes = isinstance(r.get("minutes"), (int, float)) and r["minutes"] > 0
        if not has_times and not has_minutes:
            return "no start/end times and no duration"
    elif kind == "staff":
        if not r.get("staff_id"):
            return "staff_id is blank"
    elif kind == "residents":
        if not r.get("resident_id"):
            return "resident_id is blank"
        if not r.get("name"):
            return "name is blank"
    elif kind == "resident_days":
        if not r.get("resident_id"):
            return "resident_id is blank"
        if not isinstance(r.get("date"), date):
            return "date missing"
    elif kind == "care_episodes":
        if not r.get("resident_id"):
            return "resident_id is blank"
        if not isinstance(r.get("date"), date):
            return "date missing"
        has_times = isinstance(r.get("start_time"), time) and isinstance(r.get("end_time"), time)
        has_minutes = isinstance(r.get("minutes"), (int, float)) and r["minutes"] > 0
        if not has_times and not has_minutes:
            return "no start/end times and no duration"
    return None


def run_spec(spec: dict, sheets: list[Sheet]) -> list[TargetResult]:
    targets = spec.get("targets", [])
    if not targets:
        raise MappingError("spec has no targets")
    return [run_target(t, sheets) for t in targets]


def validate_results(results: list[TargetResult]) -> list[str]:
    """Quality gate applied to a freshly generated spec before we store it."""
    problems = []
    for r in results:
        usable = r.rows_seen - r.rows_filtered
        if usable == 0:
            problems.append(f"{r.kind}: every row was filtered out")
            continue
        if len(r.records) == 0:
            sample = "; ".join(r.row_errors[:3])
            problems.append(f"{r.kind}: extracted 0 valid rows ({sample})")
        elif len(r.records) / usable < 0.7:
            sample = "; ".join(r.row_errors[:3])
            problems.append(
                f"{r.kind}: only {len(r.records)}/{usable} rows parsed ({sample})"
            )
    return problems


def apply_header_overrides(spec: dict, sheets: list) -> list:
    """Re-read any sheet the spec says was headered on the wrong row.

    Header detection runs before the analyzer sees anything, so this is how a
    wrong guess gets corrected rather than silently mapped.
    """
    from carelog.ingestion.reader import rebuild_with_header

    overrides = {}
    for target in spec.get("targets", []):
        row = target.get("header_row")
        if isinstance(row, int):
            overrides[target.get("sheet")] = row
    if not overrides:
        return sheets

    out = []
    for sheet in sheets:
        row = overrides.get(sheet.name, overrides.get(None))
        out.append(rebuild_with_header(sheet, row) if isinstance(row, int) else sheet)
    return out
