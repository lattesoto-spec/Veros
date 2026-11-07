"""Explicit duration units, always normalised to minutes."""

import re
from decimal import Decimal, InvalidOperation

UNITS = ("minutes", "hours", "h:mm")


def duration_minutes(raw, unit):
    if unit not in UNITS:
        raise ValueError("Choose minutes, hours or h:mm for this duration column")
    value = str(raw).strip()
    if ":" in value:
        match = re.fullmatch(r"(\d+):([0-5]\d)(?::([0-5]\d(?:\.\d+)?))?", value)
        if not match:
            raise ValueError("Use h:mm or h:mm:ss with minutes and seconds below 60")
        hours, minutes, seconds = match.groups()
        return float(Decimal(hours) * 60 + Decimal(minutes) + Decimal(seconds or 0) / 60)
    if unit == "h:mm":
        raise ValueError("This column was declared h:mm; numeric durations need minutes or hours")
    if "," in value and not re.fullmatch(r"[+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
        raise ValueError("Ambiguous numeric duration: use a decimal point, not a decimal comma")
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise ValueError("Duration must be a finite non-negative number") from None
    if not number.is_finite() or number < 0:
        raise ValueError("Duration must be a finite non-negative number")
    return float(number * (60 if unit == "hours" else 1))


def needs_unit_review(rule):
    # Even a whole number can mean hours or minutes. Every column needs a
    # declared unit, independent of its current values or the source's name.
    return rule.get("unit") not in UNITS and rule.get("source") != "constant"
