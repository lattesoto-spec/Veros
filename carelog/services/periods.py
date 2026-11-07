"""Bound and validate explicit reporting ranges; never silently replace bad dates."""

import re
from datetime import date, timedelta


def reporting_range(values, latest, max_days=366):
    def parse(name, fallback):
        raw = values.get(name, "")
        if not raw:
            return fallback
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise ValueError(f"{name.capitalize()} must use YYYY-MM-DD.")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError(f"{name.capitalize()} is not a valid calendar date.") from None

    end = parse("end", latest)
    start = parse("start", end - timedelta(days=89))
    if start > end:
        raise ValueError("End must be on or after start.")
    if (end - start).days >= max_days:
        raise ValueError(f"Choose a period of no more than {max_days} days.")
    return start, end
