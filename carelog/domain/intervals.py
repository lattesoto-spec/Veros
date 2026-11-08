"""Elapsed time from provider-local clocks, without guessing a DST fold."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("unrecognised source clock timezone; choose the source timezone before importing") from None


def _instant(local, zone):
    candidates = set()
    for fold in (0, 1):
        utc = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(utc)
    if len(candidates) != 1:
        raise ValueError("ambiguous or nonexistent local clock time at a daylight-saving change; supply verified duration-only evidence for these rows")
    return candidates.pop()


def elapsed_minutes(day, start, end, zone_name):
    zone = _zone(zone_name)
    left, right = datetime.combine(day, start), datetime.combine(day, end)
    if right < left:
        right += timedelta(days=1)
    return (_instant(right, zone) - _instant(left, zone)).total_seconds() / 60


def clock_change_day(day, zone_name):
    zone = _zone(zone_name)
    start = datetime.combine(day, time()).replace(tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time()).replace(tzinfo=zone)
    return start.utcoffset() != end.utcoffset()
