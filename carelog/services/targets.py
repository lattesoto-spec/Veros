"""Validate configured facility targets before changing persisted state."""

from math import isfinite


def parse_targets(data):
    try:
        care = float(data.get("ancc_target", ""))
        rn = float(data.get("rn_target", ""))
    except (TypeError, ValueError):
        raise ValueError("Enter both verified care and RN targets as numbers.") from None
    if not all(isfinite(value) and value > 0 for value in (care, rn)):
        raise ValueError("Care and RN targets must be finite numbers greater than zero.")
    if rn > care:
        raise ValueError("The RN reference target cannot exceed the total care target.")
    return care, rn
