"""Data-quality checks on normalized shift records.

Run after mapping, before import. Produces human-readable warnings — the
import still proceeds (a few odd rows shouldn't block a whole roster), but
the user is told exactly what looks wrong.
"""

from datetime import date, datetime, time, timedelta

KNOWN_ROLES = {"RN", "EN", "PCW", "PCA", "AIN", "ADMIN", "OTHER", ""}
MAX_WARNINGS_PER_KIND = 10


def _span(rec) -> tuple[datetime, datetime] | None:
    s, e = rec.get("start_time"), rec.get("end_time")
    if not (isinstance(s, time) and isinstance(e, time)):
        return None
    start = datetime.combine(rec["date"], s)
    end = datetime.combine(rec["date"], e)
    if end <= start:
        end += timedelta(days=1)  # overnight
    return start, end


def _minutes(rec) -> float | None:
    span = _span(rec)
    if span:
        gross = (span[1] - span[0]).total_seconds() / 60
    elif isinstance(rec.get("minutes"), (int, float)):
        gross = rec["minutes"]
    else:
        return None
    return gross - (rec.get("break_minutes") or 0)


def check_shifts(records: list[dict], today: date | None = None) -> list[str]:
    today = today or date.today()
    warnings: dict[str, list[str]] = {}

    def warn(kind: str, msg: str):
        warnings.setdefault(kind, []).append(msg)

    seen = set()
    by_staff_day: dict[tuple, list] = {}
    unknown_roles = set()

    for rec in records:
        d = rec["date"]
        sid = rec["staff_id"]

        if d.year < 2000 or d > today + timedelta(days=730):
            warn("impossible_date", f"{sid} on {d}: date looks impossible")

        mins = _minutes(rec)
        if mins is not None:
            if mins <= 0:
                warn("negative_hours", f"{sid} on {d}: non-positive worked time ({mins:.0f}m after breaks)")
            elif mins > 16 * 60:
                warn("long_shift", f"{sid} on {d}: unusually long shift ({mins / 60:.1f}h)")

        key = (sid, d, rec.get("start_time"), rec.get("end_time"), rec.get("minutes"))
        if key in seen:
            warn("duplicate_shift", f"{sid} on {d}: duplicate shift row")
        seen.add(key)

        span = _span(rec)
        if span:
            by_staff_day.setdefault(sid, []).append(span)

        role = rec.get("role") or ""
        if role not in KNOWN_ROLES:
            unknown_roles.add(role)

    for sid, spans in by_staff_day.items():
        spans.sort()
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            if s2 < e1:
                warn("overlapping_shifts",
                     f"{sid}: overlapping shifts ({s1:%d %b %H:%M}-{e1:%H:%M} and {s2:%H:%M}-{e2:%H:%M})")

    if unknown_roles:
        warn("unknown_classification",
             "Unrecognized role value(s): " + ", ".join(sorted(unknown_roles)[:8])
             + " — counted toward total care minutes but not RN/EN/PCW splits")

    out = []
    for kind, msgs in warnings.items():
        out.extend(msgs[:MAX_WARNINGS_PER_KIND])
        if len(msgs) > MAX_WARNINGS_PER_KIND:
            out.append(f"… and {len(msgs) - MAX_WARNINGS_PER_KIND} more {kind} warning(s)")
    return out
