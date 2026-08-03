"""File inspector: infer what kind of data each column holds.

The inferred types are surfaced to the analyzer as hints (so it picks the
right parse mode) and shown in the import summary so users can sanity-check
what was detected.
"""

import re

from .mapping import DATE_FORMATS, DATETIME_FORMATS, TIME_FORMATS, parse_bool
from .reader import Sheet

_SAMPLE = 50


def _classify(value: str) -> str:
    from datetime import datetime

    v = value.strip()
    if not v:
        return "empty"
    if parse_bool(v) is not None:
        return "boolean"
    try:
        float(v.replace(",", ""))
        return "number"
    except ValueError:
        pass
    for f in DATETIME_FORMATS:
        try:
            datetime.strptime(v, f)
            return "datetime"
        except ValueError:
            continue
    for f in DATE_FORMATS:
        try:
            datetime.strptime(v, f)
            return "date"
        except ValueError:
            continue
    for f in TIME_FORMATS:
        try:
            datetime.strptime(v, f)
            return "time"
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v):
        return "time"
    return "text"


def infer_column_types(sheet: Sheet) -> dict:
    """header -> dominant type across a sample of rows ('mixed' if unclear)."""
    out = {}
    sample = sheet.rows[:_SAMPLE]
    for h in sheet.headers:
        counts: dict[str, int] = {}
        for row in sample:
            t = _classify(row.get(h, ""))
            counts[t] = counts.get(t, 0) + 1
        non_empty = {t: c for t, c in counts.items() if t != "empty"}
        if not non_empty:
            out[h] = "empty"
            continue
        top, top_count = max(non_empty.items(), key=lambda kv: kv[1])
        total = sum(non_empty.values())
        out[h] = top if top_count / total >= 0.8 else "mixed"
    return out
