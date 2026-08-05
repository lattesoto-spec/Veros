"""LLM format analysis.

Sends the *structure* of an upload (headers + a handful of sample rows) to
Claude and gets back a declarative mapping spec for the mapping engine.
Called only when a format fingerprint has never been seen before; the
resulting spec is stored and reused for all future uploads of that format.
"""

import json
import os

import anthropic
import httpx

from .mapping import apply_header_overrides, run_spec, validate_results
from .reader import Sheet

# Haiku keeps per-format learning cheap; the validate-and-retry loop below
# catches its occasional misses. Both the key and the model can be set on the
# platform environment.
DEFAULT_MODEL = "claude-haiku-4-5"
SAMPLE_ROWS = 8


def resolve_api_key() -> str | None:
    """Deployment configuration, not application data.

    The key used to be storable in the database from the Settings page, which
    meant a platform credential sat in tenant data and any administrator could
    read or replace it. It now comes only from the environment.
    """
    return os.environ.get("ANTHROPIC_API_KEY")


def resolve_model() -> str:
    return os.environ.get("CAREMIN_ANALYZER_MODEL") or DEFAULT_MODEL


def ai_ready() -> bool:
    return bool(resolve_api_key())


class AnalyzerError(Exception):
    pass


SYSTEM_PROMPT = """You are the format-analysis engine of an aged-care Care Minutes reporting product.
Providers upload staff rosters / timesheets, resident-day ledgers and delivered-care logs exported from arbitrary systems
(Humanforce, AlayaCare, ShiftCare, custom payroll, hand-made spreadsheets, JSON API extracts,
SQL database dumps). Your job is to look at the STRUCTURE of an upload and produce a declarative
mapping spec that a deterministic engine executes to normalize the data. You map columns; you
never transcribe data.

Uploads are always presented to you as sheets of columns, whatever the original file was:
a JSON export becomes one sheet per array of records, with nested objects flattened into
dotted column names ("employee.code"); a SQL dump or database file becomes one sheet per
table, named after the table. Treat these exactly like spreadsheet columns.

Normalized target schemas:
- kind "shifts": staff_id (required), staff_name, role, date (required),
  start_time, end_time, minutes (duration in minutes), break_minutes
  (unpaid break duration in minutes, subtracted from care time),
  is_direct_care, is_agency (worker supplied by an agency), labour_cost
  (the direct-care labour cost attributable to that row, if present).
  Each record needs EITHER start_time+end_time OR minutes.
- kind "staff": one row per worker from an employee directory or credential
  register: staff_id (required), staff_name, role, employment_type,
  classification (award/enterprise-agreement classification),
  registration_number and registration_expiry. Map Ahpra registration evidence
  whenever the source provides it. A staff directory is separate from shifts;
  never invent worked time from an employee master list.
- kind "residents": resident_id (required), name (required), ancc_class,
  admitted_date, discharged_date.
- kind "resident_days": one row per resident per date: date (required),
  resident_id (required), resident_name, occupied (default true), service_type,
  leave_type, leave_day_number (consecutive hospital-leave day), ancc_class,
  exclusion_reason. A daily resident summary belongs
  here; do not turn repeated daily rows into duplicate resident records.
  service_type/funding must distinguish AN-ACC permanent or respite care from
  private residents and programs such as Transition Care when the file provides
  that information. Hospital leave days 1-28 remain occupied; day 29 onward is
  excluded, so map a consecutive leave-day number when available.
- kind "care_episodes": resident-level delivered care: date (required),
  resident_id (required), resident_name, care_type, care_category, staff_id,
  staff_name, role, start_time, end_time, minutes. Each record needs either
  start_time+end_time or minutes. Care episodes are reconciliation evidence and
  must never be mapped as staff shifts.

Field spec language (per normalized field):
- source: "column" (default) with "column"; "combine" with "columns" + optional "separator";
  or "constant" with "value".
- parse: "date", "time", "datetime_date" (date part of a datetime column),
  "datetime_time" (time part), "number", "boolean", or omit for text.
- format: optional strptime format for date/time parsing (the engine also tries common formats).
- multiply: for parse "number" — e.g. a column of decimal hours needs multiply 60 to become minutes.
- value_map: list of {"from": raw_value_lowercase, "to": mapped_value} pairs applied before parsing.
- normalize: "role" applies built-in role normalization (Registered Nurse->RN, Enrolled->EN,
  care assistants->PCW). Prefer normalize "role" for role columns; add value_map only for
  values the heuristic would miss.
- default: value used when the cell is empty (or boolean unrecognized).

Per target:
- sheet: sheet name to read (omit for single-sheet files).
- header_row: 0-based index into grid_preview, set ONLY when detected_header_row
  is wrong. Exports often begin with a merged title banner and a subtitle, which
  can be mistaken for the header; the real header is the row whose cells are
  distinct short labels and which is followed by consistent data. When you set
  this, name columns as they appear in THAT row.
- row_filter: optional {"column", "include_values" | "exclude_values"} to drop non-shift rows
  (leave, sick, unfilled, totals). Values are compared lowercased.

Rules:
- Only emit targets actually present in the file. A roster file usually has only "shifts";
  an employee directory or credential sheet uses "staff";
  a resident master list uses "residents"; a resident-by-day summary uses
  "resident_days"; a log of individual services uses "care_episodes". One
  workbook can have several targets on different sheets. Ignore derived
  facility summary and notes sheets when the underlying detail sheet exists.
- staff_id: if there is no explicit ID column, use the best stable identifier available
  (e.g. combine name columns). Same for resident_id.
- is_direct_care: if a column distinguishes direct care from admin/leave, map it; otherwise
  use a constant true. Rows for leave, sick leave, training, unfilled shifts, and totals
  should be excluded via row_filter where possible.
- break_minutes: map a break/meal-break column when present (use multiply 60 if it is in
  hours). Omit the field when there is no break column.
- is_agency: map a column that flags agency/contractor workers (value_map to booleans);
  omit when there is no such signal.
- Dates in Australian exports are usually day-first when ambiguous.
- role: staff classification (RN/EN/PCW etc.), not a department name, if both exist.
- Each sheet's reported column_types (inferred from the data) are hints for choosing parse modes."""


def _structure_payload(sheets: list[Sheet]) -> str:
    from .inspect import infer_column_types

    doc = []
    for s in sheets:
        doc.append({
            "sheet": s.name,
            "detected_header_row": s.header_row_index,
            "headers": s.headers,
            "column_types": infer_column_types(s),
            "row_count": len(s.rows),
            "sample_rows": s.rows[:SAMPLE_ROWS],
            "notes": s.notes,
            # The unprocessed top of the sheet. `headers` above is a guess made
            # from this; if the guess is wrong, say so via header_row.
            "grid_preview": [
                {"row": i, "cells": cells} for i, cells in enumerate(s.preview)
            ],
        })
    return json.dumps(doc, indent=2, ensure_ascii=False, default=str)


def _extract_json(text: str) -> dict:
    """Parse the response as JSON; tolerate prose or code fences around it."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _clean_spec(spec: dict) -> dict:
    """Strip nulls and convert value_map pair-lists into the dicts the engine uses."""
    if not isinstance(spec, dict):
        raise ValueError("mapping spec must be an object")
    raw_targets = spec.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ValueError("mapping spec targets must be a list")

    def fields_as_object(raw):
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, list):
            raise ValueError("target fields must be an object")

        # Some model responses express fields as a named list despite the
        # requested object shape. Convert only unambiguous variants; anything
        # else is sent back through the validation/retry loop.
        converted = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each field-list item must be an object")
            name = next((item.get(k) for k in (
                "name", "field", "target_field", "normalized_field"
            ) if item.get(k)), None)
            if name:
                field_spec = {k: v for k, v in item.items() if k not in {
                    "name", "field", "target_field", "normalized_field"
                }}
            elif len(item) == 1:
                name, field_spec = next(iter(item.items()))
            else:
                raise ValueError("field-list item has no unambiguous field name")
            if not isinstance(field_spec, dict):
                raise ValueError(f"field {name!r} must contain an object")
            if name in converted:
                raise ValueError(f"duplicate field {name!r}")
            converted[str(name)] = field_spec
        return converted

    targets = []
    for t in raw_targets:
        if not isinstance(t, dict) or not t.get("kind"):
            raise ValueError("each target must be an object with a kind")
        ct = {"kind": t["kind"]}
        if t.get("sheet"):
            ct["sheet"] = t["sheet"]
        if isinstance(t.get("header_row"), int):
            ct["header_row"] = t["header_row"]
        rf = t.get("row_filter")
        if rf and not isinstance(rf, dict):
            raise ValueError("row_filter must be an object")
        if rf and rf.get("column"):
            crf = {"column": rf["column"]}
            if rf.get("include_values"):
                crf["include_values"] = rf["include_values"]
            elif rf.get("exclude_values"):
                crf["exclude_values"] = rf["exclude_values"]
            if len(crf) > 1:
                ct["row_filter"] = crf
        fields = {}
        for name, fs in fields_as_object(t.get("fields")).items():
            if fs is None:
                continue
            if not isinstance(fs, dict):
                raise ValueError(f"field {name!r} must contain an object")
            cf = {k: v for k, v in fs.items() if v is not None}
            if not cf:
                continue
            if "value_map" in cf:
                value_map = cf["value_map"]
                if isinstance(value_map, dict):
                    cf["value_map"] = {
                        str(k).strip().lower(): v for k, v in value_map.items()
                    }
                elif isinstance(value_map, list):
                    cf["value_map"] = {
                        str(p["from"]).strip().lower(): p["to"]
                        for p in value_map
                        if isinstance(p, dict) and "from" in p and "to" in p
                    }
                    if len(cf["value_map"]) != len(value_map):
                        raise ValueError(f"field {name!r} has an invalid value_map")
                else:
                    raise ValueError(f"field {name!r} value_map must be an object or list")
            fields[name] = cf
        ct["fields"] = fields
        targets.append(ct)
    return {"targets": targets, "reasoning": spec.get("reasoning", "")}


def generate_mapping_spec(sheets: list[Sheet], filename: str = "") -> tuple[dict, dict]:
    """Ask Claude for a mapping spec; validate it against the actual file; retry
    once with error feedback. Returns (spec, usage_info)."""
    api_key = resolve_api_key()
    if not api_key:
        raise AnalyzerError(
            "The format-mapping service is not configured (ANTHROPIC_API_KEY is missing)."
        )
    # This runs in a background job, so it can afford to wait: generous read
    # timeout, and the request is streamed so a slow generation keeps bytes
    # flowing instead of tripping a read timeout.
    # local_address pins the outbound socket to IPv4: api.anthropic.com is
    # dual-stack, and on hosts with a broken IPv6 route (Fly machines) the
    # client otherwise burns the whole connect timeout on the AAAA address.
    timeout = httpx.Timeout(240.0, connect=10.0)
    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=timeout,
        max_retries=2,
        http_client=httpx.Client(
            timeout=timeout,
            transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2),
        ),
    )
    model = resolve_model()

    structure = _structure_payload(sheets)
    messages = [{
        "role": "user",
        "content": (
            f"File name: {filename or 'upload'}\n\n"
            f"Structure and sample rows:\n{structure}\n\n"
            "Produce the mapping spec."
        ),
    }]

    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    last_problems: list[str] = []
    for attempt in range(2):
        try:
            # One plain streaming request; _extract_json tolerates prose or
            # code fences around the spec, so no structured-output modes are
            # needed.
            with client.messages.stream(
                model=model,
                max_tokens=8192,
                system=SYSTEM_PROMPT + "\n\nRespond with ONLY the JSON spec object, no prose.",
                messages=messages,
            ) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise AnalyzerError(
                "Anthropic rejected the platform API credential."
            ) from e
        except anthropic.PermissionDeniedError as e:
            raise AnalyzerError(
                "The API key was accepted but lacks access. This usually means "
                "the Anthropic account has no credit. Check console.anthropic.com → Billing."
            ) from e
        except anthropic.RateLimitError as e:
            raise AnalyzerError(
                "Anthropic rate limit reached. Wait a minute and re-import."
            ) from e
        except anthropic.NotFoundError as e:
            raise AnalyzerError(
                f"Model '{model}' was not found for the platform account."
            ) from e
        except anthropic.APIConnectionError as e:
            raise AnalyzerError(
                "Could not reach the Anthropic API (network timeout). Try again; "
                "if it persists, check the server's outbound connectivity."
            ) from e
        except anthropic.APIStatusError as e:
            raise AnalyzerError(
                f"Anthropic API error ({e.status_code}): {getattr(e, 'message', e)}"
            ) from e
        usage["calls"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens

        if response.stop_reason == "refusal":
            raise AnalyzerError("The analysis model declined this request.")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            raw_spec = _extract_json(text)
        except json.JSONDecodeError as e:
            raise AnalyzerError(f"Model returned invalid JSON: {e}") from e

        try:
            spec = _clean_spec(raw_spec)
            results = run_spec(spec, apply_header_overrides(spec, sheets))
            problems = validate_results(results)
        except Exception as e:
            problems = [f"spec failed to execute: {e}"]

        if not problems:
            return spec, usage

        last_problems = problems
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": (
                "That spec failed validation against the real file:\n- "
                + "\n- ".join(problems)
                + "\nFix the spec and return the corrected version."
            ),
        })

    raise AnalyzerError(
        "Could not build a working parser for this format: " + "; ".join(last_problems)
    )
