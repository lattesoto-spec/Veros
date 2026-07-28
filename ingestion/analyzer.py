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

from .mapping import run_spec, validate_results
from .reader import Sheet

# Haiku keeps per-format learning cheap; the validate-and-retry loop below
# catches its occasional misses. Both the key and the model can be set on the
# Settings page (stored in the DB) or via environment variables.
DEFAULT_MODEL = "claude-haiku-4-5"
SAMPLE_ROWS = 8


def _setting(key: str):
    try:
        from models import get_setting
        return get_setting(key)
    except Exception:  # outside app context (e.g. offline tests)
        return None


def resolve_api_key() -> str | None:
    return _setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def resolve_model() -> str:
    return (
        _setting("analyzer_model")
        or os.environ.get("CAREMIN_ANALYZER_MODEL")
        or DEFAULT_MODEL
    )


def ai_ready() -> bool:
    return bool(resolve_api_key())


class AnalyzerError(Exception):
    pass


SYSTEM_PROMPT = """You are the format-analysis engine of an aged-care Care Minutes reporting product.
Providers upload staff rosters / timesheets and resident lists exported from arbitrary systems
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
  is_direct_care, is_agency (worker supplied by an agency).
  Each record needs EITHER start_time+end_time OR minutes.
- kind "residents": resident_id (required), name (required), ancc_class,
  admitted_date, discharged_date.

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
- row_filter: optional {"column", "include_values" | "exclude_values"} to drop non-shift rows
  (leave, sick, unfilled, totals). Values are compared lowercased.

Rules:
- Only emit targets actually present in the file. A roster file usually has only "shifts";
  a resident list only "residents"; one workbook can have both on different sheets.
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
            "headers": s.headers,
            "column_types": infer_column_types(s),
            "row_count": len(s.rows),
            "sample_rows": s.rows[:SAMPLE_ROWS],
            "notes": s.notes,
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
    targets = []
    for t in spec.get("targets", []):
        ct = {"kind": t["kind"]}
        if t.get("sheet"):
            ct["sheet"] = t["sheet"]
        rf = t.get("row_filter")
        if rf and rf.get("column"):
            crf = {"column": rf["column"]}
            if rf.get("include_values"):
                crf["include_values"] = rf["include_values"]
            elif rf.get("exclude_values"):
                crf["exclude_values"] = rf["exclude_values"]
            if len(crf) > 1:
                ct["row_filter"] = crf
        fields = {}
        for name, fs in (t.get("fields") or {}).items():
            if fs is None:
                continue
            cf = {k: v for k, v in fs.items() if v is not None}
            if not cf:
                continue
            if "value_map" in cf:
                cf["value_map"] = {
                    str(p["from"]).strip().lower(): p["to"] for p in cf["value_map"]
                }
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
            "No Anthropic API key configured — add one on the Settings page (or set "
            "ANTHROPIC_API_KEY) so new file formats can be learned. Already-learned "
            "formats still import without it."
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
                "Anthropic rejected the API key — check it on the Settings page "
                "(keys start with sk-ant- and are shown once at creation)."
            ) from e
        except anthropic.PermissionDeniedError as e:
            raise AnalyzerError(
                "The API key was accepted but lacks access — most often this means "
                "the Anthropic account has no credit. Check console.anthropic.com → Billing."
            ) from e
        except anthropic.RateLimitError as e:
            raise AnalyzerError(
                "Anthropic rate limit hit — wait a minute and re-import."
            ) from e
        except anthropic.NotFoundError as e:
            raise AnalyzerError(
                f"Model '{model}' was not found for this account — reset the "
                "analysis model to the default on the Settings page."
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

        spec = _clean_spec(raw_spec)
        try:
            results = run_spec(spec, sheets)
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
