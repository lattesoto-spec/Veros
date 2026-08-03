"""Format fingerprinting.

A format is identified by the *structure* of the file — the normalized header
names in order, per sheet — not by its data. Two exports from the same system
in different months hash identically, so the stored mapping is reused and no
AI call is made.
"""

import hashlib
import re

from .reader import Sheet


def normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def sheet_signature(sheet: Sheet) -> str:
    return "|".join(normalize_header(h) for h in sheet.headers)


def fingerprint(sheets: list[Sheet]) -> str:
    # Sheet names are excluded on purpose: systems often name the sheet after
    # the export period ("April 2026"), which would defeat reuse.
    payload = "\n".join(sorted(sheet_signature(s) for s in sheets))
    return hashlib.sha256(payload.encode()).hexdigest()
