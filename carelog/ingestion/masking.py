"""What may leave CareMin when an external service helps read a file layout.

Only structure is disclosed. Data cells become character shapes ("Jane Smith"
becomes "Aaaa Aaaaa", "04/08/2026" becomes "99/99/9999"), so a resident name,
staff name, Ahpra number or care note is never sent. Low-risk category words
stay readable because value maps and row filters depend on them.
"""

import re

SAFE_VALUES = {
    "", "yes", "no", "y", "n", "true", "false", "t", "f", "0", "1",
    "approved", "pending", "rejected", "submitted", "draft", "cancelled",
    "active", "inactive", "completed", "published",
    "direct", "indirect", "direct care", "admin",
    "agency", "casual", "permanent", "contractor", "employee", "part-time", "full-time",
    "ordinary", "overtime", "leave", "sick", "sick leave", "annual leave", "training",
    "rn", "en", "een", "pcw", "pca", "ain", "am", "pm",
    "occupied", "hospital", "social", "respite", "permanent resident", "private",
}

HEADER_WORDS = {
    "actual", "address", "dept", "desc", "emp", "no", "num", "qty", "ref", "admission", "admitted", "agency", "ahpra", "allowance", "an",
    "acc", "an-acc", "ancc", "approval", "approved", "award", "base", "bed", "break",
    "care", "category", "class", "classification", "client", "clock", "code", "comment",
    "comments", "cost", "centre", "center", "date", "day", "days", "delivered", "department",
    "direct", "discharge", "discharged", "duration", "employee", "employment", "end",
    "enrolled", "episode", "exclusion", "expiry", "facility", "family", "finish", "first",
    "full", "given", "gross", "home", "hours", "hrs", "id", "identifier", "in", "job",
    "labour", "last", "leave", "level", "line", "location", "meal", "member", "mins",
    "minutes", "name", "nurse", "number", "occupied", "of", "out", "paid", "pay", "payroll",
    "period", "person", "personal", "planned", "position", "punch", "rate", "reason",
    "record", "registered", "registration", "resident", "role", "room", "roster", "rostered",
    "rule", "scheduled", "service", "shift", "site", "source", "staff", "start", "status",
    "surname", "team", "time", "title", "total", "type", "unit", "unpaid", "wing", "work",
    "worked", "worker",
}

_MAX_SHAPE = 24
_CAPITALISED = re.compile(r"[A-Z][a-z'’-]+$")


def _shape(text: str) -> str:
    out = []
    for char in text[:_MAX_SHAPE]:
        if char.isdigit():
            out.append("9")
        elif char.isalpha():
            out.append("A" if char.isupper() else "a")
        else:
            out.append(char)
    return "".join(out)


def mask_value(value) -> str:
    text = "" if value is None else str(value).strip()
    return text if text.lower() in SAFE_VALUES else _shape(text)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_][^\W\d_'’-]*", text)


def _looks_like_a_name(text: str) -> bool:
    """Two adjacent capitalised words neither of which a column heading uses."""
    words = text.split()
    return any(
        _CAPITALISED.match(left)
        and _CAPITALISED.match(right)
        and left.lower() not in HEADER_WORDS
        and right.lower() not in HEADER_WORDS
        for left, right in zip(words, words[1:])
    )


def mask_header(value) -> str:
    text = "" if value is None else str(value).strip()
    if len(text.split()) > 6 or _looks_like_a_name(text) or any(c.isdigit() for c in text.replace("AN-ACC", "")):
        return _shape(text)
    return text


def header_like(cells) -> bool:
    """A row of column names, wherever the header detector thought they were."""
    filled = [str(cell).strip() for cell in cells if str(cell or "").strip()]
    if not filled or any(any(c.isdigit() for c in cell) for cell in filled):
        return False
    known = sum(
        1 for cell in filled
        if any(word.lower() in HEADER_WORDS for word in _words(cell))
    )
    return known / len(filled) >= 0.6


def mask_row(cells) -> list[str]:
    """Judge each row by its content. A detected header may be the first record."""
    keep_text = header_like(cells)
    return [mask_header(cell) if keep_text else mask_value(cell) for cell in cells]


def mask_problem(text: str) -> str:
    """Validation feedback to the model must not quote source values."""
    return re.sub(r"(['\"]).*?\1", "[value]", text)
