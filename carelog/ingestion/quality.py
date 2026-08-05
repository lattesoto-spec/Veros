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

    overlap_minutes = 0
    for sid, spans in by_staff_day.items():
        spans.sort()
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            if s2 < e1:
                overlap_minutes += int((min(e1, e2) - s2).total_seconds() // 60)
                warn("overlapping_shifts",
                     f"{sid}: overlapping shifts ({s1:%d %b %H:%M}-{e1:%H:%M} and {s2:%H:%M}-{e2:%H:%M})")
    if overlap_minutes:
        # One person cannot be in two places, so overlapping rows inflate the
        # worked total. Episode-level exports (one row per care activity) are
        # the usual source.
        warn("overlap_impact",
             f"Overlapping rows add roughly {overlap_minutes} double-counted minutes. "
             "If this file lists individual care episodes rather than shifts, the "
             "care-minute total will be overstated until the episodes are "
             "reconciled into worked time.")

    if unknown_roles:
        from carelog.domain.eligibility import classify

        excluded, pending = [], []
        for role in sorted(unknown_roles):
            _, status, _ = classify(role)
            (excluded if status == "excluded" else pending).append(role or "(blank)")
        if excluded:
            warn("ineligible_role",
                 "Not counted toward care minutes. These roles are not eligible "
                 "direct care: " + ", ".join(excluded[:8]))
        if pending:
            warn("unknown_classification",
                 "Held out of the figures until someone confirms them: "
                 + ", ".join(pending[:8])
                 + ". Decide on the Eligibility page. Unresolved roles are "
                   "excluded rather than assumed to count.")

    out = []
    for kind, msgs in warnings.items():
        out.extend(msgs[:MAX_WARNINGS_PER_KIND])
        if len(msgs) > MAX_WARNINGS_PER_KIND:
            out.append(f"{len(msgs) - MAX_WARNINGS_PER_KIND} additional {kind} warning(s)")
    return out
