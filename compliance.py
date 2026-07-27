"""Compliance engine: role-split care minutes, RN coverage, gap detection,
predictive compliance, and scenario planning.

CALC_VERSION stamps every derived number so reports and the audit page can
say which calculation logic produced them.
"""

from datetime import date, datetime, time, timedelta

from sqlalchemy import or_

from care_minutes import _minutes_between, active_residents_on, quarter_bounds
from models import Resident, Shift, Staff, db

CALC_VERSION = "2026.07.1"

# Canonical reporting buckets. The mapping engine normalizes to PCW; the
# government statement calls the bucket PCA — they are the same bucket.
ROLE_BUCKETS = {
    "RN": "RN",
    "EN": "EN", "EEN": "EN",
    "PCW": "PCA", "PCA": "PCA", "AIN": "PCA",
}


def bucket_role(role: str) -> str:
    return ROLE_BUCKETS.get((role or "").strip().upper(), "OTHER")


def worked_minutes(shift: Shift) -> int:
    return max(_minutes_between(shift.start_time, shift.end_time) - (shift.break_minutes or 0), 0)


# ------------------------------------------------------------- daily stats


def day_breakdown(facility_id: int, day: date) -> dict:
    """Per-role direct-care minutes for one day."""
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day,
            Shift.is_direct_care == True,  # noqa: E712
        )
        .all()
    )
    roles = {"RN": 0, "EN": 0, "PCA": 0, "OTHER": 0}
    agency = 0
    for s, st in shifts:
        m = worked_minutes(s)
        roles[bucket_role(st.role)] += m
        if s.is_agency:
            agency += m
    residents = active_residents_on(facility_id, day)
    total = sum(roles.values())
    return {
        "date": day,
        "residents": residents,
        "total_minutes": total,
        "rn_minutes": roles["RN"],
        "en_minutes": roles["EN"],
        "pca_minutes": roles["PCA"],
        "other_minutes": roles["OTHER"],
        "agency_minutes": agency,
        "care_per_resident": round(total / residents, 1) if residents else 0.0,
        "rn_per_resident": round(roles["RN"] / residents, 1) if residents else 0.0,
    }


def range_breakdown(facility_id: int, start: date, end: date) -> list[dict]:
    out = []
    d = start
    while d <= end:
        out.append(day_breakdown(facility_id, d))
        d += timedelta(days=1)
    return out


def monthly_breakdown(facility_id: int, start: date, end: date) -> list[dict]:
    """Aggregate range_breakdown rows into calendar months."""
    months: dict[tuple, dict] = {}
    for r in range_breakdown(facility_id, start, end):
        if r["residents"] == 0 and r["total_minutes"] == 0:
            continue
        key = (r["date"].year, r["date"].month)
        m = months.setdefault(key, {
            "year": key[0], "month": key[1], "days": 0, "bed_days": 0,
            "total_minutes": 0, "rn_minutes": 0, "en_minutes": 0,
            "pca_minutes": 0, "agency_minutes": 0,
        })
        m["days"] += 1
        m["bed_days"] += r["residents"]
        for k in ("total_minutes", "rn_minutes", "en_minutes", "pca_minutes", "agency_minutes"):
            m[k] += r[k]
    out = []
    for key in sorted(months):
        m = months[key]
        bd = m["bed_days"]
        m["care_per_bed_day"] = round(m["total_minutes"] / bd, 1) if bd else 0.0
        m["rn_per_bed_day"] = round(m["rn_minutes"] / bd, 1) if bd else 0.0
        m["label"] = date(m["year"], m["month"], 1).strftime("%B %Y")
        out.append(m)
    return out


# ------------------------------------------------------------- RN coverage


def rn_coverage(facility_id: int, day: date, ignore_gap_minutes: int = 30) -> dict:
    """Fraction of the 24h day with at least one RN on a direct-care shift.
    Gaps up to `ignore_gap_minutes` are ignored, matching the reporting rule."""
    shifts = (
        db.session.query(Shift)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day,
            Staff.role == "RN",
        )
        .all()
    )
    intervals = []
    for s in shifts:
        start = datetime.combine(day, s.start_time)
        end = datetime.combine(day, s.end_time)
        if end <= start:
            end += timedelta(days=1)
        intervals.append((start, min(end, datetime.combine(day + timedelta(days=1), time(0)))))
    # overnight shifts from the previous day spill into today
    prev = (
        db.session.query(Shift)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day - timedelta(days=1),
            Staff.role == "RN",
        )
        .all()
    )
    day_start = datetime.combine(day, time(0))
    for s in prev:
        if s.end_time <= s.start_time:  # crossed midnight
            intervals.append((day_start, datetime.combine(day, s.end_time)))

    intervals.sort()
    merged = []
    for iv in intervals:
        if merged and iv[0] <= merged[-1][1] + timedelta(minutes=ignore_gap_minutes):
            merged[-1] = (merged[-1][0], max(merged[-1][1], iv[1]))
        else:
            merged.append(list(iv) if isinstance(iv, tuple) else iv)
            merged[-1] = (iv[0], iv[1])
    covered = sum((e - s).total_seconds() / 60 for s, e in merged)
    covered = min(covered, 1440)
    return {
        "covered_minutes": int(covered),
        "coverage_pct": round(covered / 1440 * 100, 1),
        "full_coverage": covered >= 1440,
        "gaps": max(len(merged) - 1, 0) if merged else (1 if not intervals else 0),
    }


# ------------------------------------------------------------- gap detection


def detect_gaps(facility, today: date) -> list[dict]:
    """Alert list: [{severity: 'high'|'medium'|'info', message: str}]."""
    alerts = []
    fid = facility.id

    # Quarter-to-date position
    fc = forecast_quarter(facility, today)
    if fc and fc["qtd_days"] > 0:
        if fc["qtd_avg"] < facility.ancc_target:
            alerts.append({
                "severity": "high",
                "message": (f"Facility is below its AN-ACC target: averaging "
                            f"{fc['qtd_avg']:.0f} of {facility.ancc_target:.0f} "
                            f"mins/resident/day this quarter."),
            })
        elif fc["projected_avg"] < facility.ancc_target:
            when = (f"in {fc['days_until_breach']} day(s)" if fc["days_until_breach"]
                    else "by quarter end")
            alerts.append({
                "severity": "high",
                "message": (f"Predicted breach: at current staffing the quarter average "
                            f"falls below target {when} "
                            f"(projected {fc['projected_avg']:.0f} vs target "
                            f"{facility.ancc_target:.0f})."),
            })
        if fc["qtd_rn_avg"] < facility.rn_target:
            alerts.append({
                "severity": "high",
                "message": (f"RN minutes below target: averaging {fc['qtd_rn_avg']:.0f} "
                            f"of {facility.rn_target:.0f} RN mins/resident/day this quarter."),
            })

    # Tomorrow's rostered RN shortfall (only if the roster extends that far)
    tomorrow = today + timedelta(days=1)
    has_future = db.session.query(Shift.id).filter(
        Shift.facility_id == fid, Shift.date >= tomorrow
    ).first() is not None
    if has_future:
        b = day_breakdown(fid, tomorrow)
        residents = b["residents"] or active_residents_on(fid, today)
        rn_needed = facility.rn_target * residents
        if b["rn_minutes"] < rn_needed:
            alerts.append({
                "severity": "high",
                "message": (f"Tomorrow you're short {rn_needed - b['rn_minutes']:.0f} RN "
                            f"minutes ({b['rn_minutes']:.0f} rostered vs "
                            f"{rn_needed:.0f} needed for {residents} residents)."),
            })
        care_needed = facility.ancc_target * residents
        if b["total_minutes"] < care_needed:
            alerts.append({
                "severity": "medium",
                "message": (f"Tomorrow's roster is {care_needed - b['total_minutes']:.0f} "
                            f"care minutes below target "
                            f"({b['total_minutes']:.0f} of {care_needed:.0f})."),
            })

    # 24/7 RN coverage today
    cov = rn_coverage(fid, today)
    if not cov["full_coverage"] and cov["covered_minutes"] > 0:
        alerts.append({
            "severity": "medium",
            "message": (f"RN 24/7 coverage today is {cov['coverage_pct']}% "
                        f"({1440 - cov['covered_minutes']} uncovered minutes)."),
        })
    elif cov["covered_minutes"] == 0:
        alerts.append({"severity": "high", "message": "No RN rostered today."})

    if not alerts:
        alerts.append({"severity": "info", "message": "No compliance risks detected."})
    return alerts


# ------------------------------------------------------------- forecasting


def _trailing_baseline(facility_id: int, today: date, days: int = 14) -> dict | None:
    """Average daily totals over the trailing window — the 'typical day'."""
    rows = [r for r in range_breakdown(facility_id, today - timedelta(days=days - 1), today)
            if r["residents"] > 0 and r["total_minutes"] > 0]
    if not rows:
        return None
    n = len(rows)
    return {
        "total_minutes": sum(r["total_minutes"] for r in rows) / n,
        "rn_minutes": sum(r["rn_minutes"] for r in rows) / n,
        "agency_minutes": sum(r["agency_minutes"] for r in rows) / n,
        "residents": sum(r["residents"] for r in rows) / n,
    }


def forecast_quarter(facility, today: date, adjustments: dict | None = None) -> dict | None:
    """Project the quarter-end average. `adjustments` (scenario planning):
      rn_shifts_removed: int   — 8h RN shifts removed per future day
      agency_removed: bool     — all agency minutes disappear from future days
      occupancy_delta: int     — residents added (+) or discharged (−) go-forward
    """
    q_start, _ = quarter_bounds(today)
    q_end = _calendar_quarter_end(today)
    qtd = [r for r in range_breakdown(facility.id, q_start, today)
           if r["residents"] > 0 and r["total_minutes"] > 0]
    baseline = _trailing_baseline(facility.id, today)
    if not qtd or not baseline:
        return None

    n = len(qtd)
    qtd_avg = sum(r["care_per_resident"] for r in qtd) / n
    qtd_rn_avg = sum(r["rn_per_resident"] for r in qtd) / n

    adj = adjustments or {}
    fut_total = baseline["total_minutes"]
    fut_rn = baseline["rn_minutes"]
    fut_res = baseline["residents"]
    if adj.get("rn_shifts_removed"):
        removed = adj["rn_shifts_removed"] * 8 * 60
        fut_total -= removed
        fut_rn -= removed
    if adj.get("agency_removed"):
        fut_total -= baseline["agency_minutes"]
    if adj.get("occupancy_delta"):
        fut_res += adj["occupancy_delta"]
    fut_total = max(fut_total, 0)
    fut_rn = max(min(fut_rn, fut_total), 0)
    fut_res = max(fut_res, 1)

    fut_per_res = fut_total / fut_res
    fut_rn_per_res = fut_rn / fut_res

    m = max((q_end - today).days, 0)
    projected = (qtd_avg * n + fut_per_res * m) / (n + m) if (n + m) else qtd_avg
    projected_rn = (qtd_rn_avg * n + fut_rn_per_res * m) / (n + m) if (n + m) else qtd_rn_avg

    days_until_breach = None
    target = facility.ancc_target
    if qtd_avg >= target and fut_per_res < target:
        for k in range(1, m + 1):
            if (qtd_avg * n + fut_per_res * k) / (n + k) < target:
                days_until_breach = k
                break

    return {
        "calc_version": CALC_VERSION,
        "q_start": q_start, "q_end": q_end,
        "qtd_days": n, "days_remaining": m,
        "qtd_avg": round(qtd_avg, 1), "qtd_rn_avg": round(qtd_rn_avg, 1),
        "future_per_resident": round(fut_per_res, 1),
        "future_rn_per_resident": round(fut_rn_per_res, 1),
        "projected_avg": round(projected, 1),
        "projected_rn_avg": round(projected_rn, 1),
        "days_until_breach": days_until_breach,
        "on_track": projected >= target,
        "rn_on_track": projected_rn >= facility.rn_target,
        "baseline_residents": round(fut_res, 1),
    }


def _calendar_quarter_end(d: date) -> date:
    q = (d.month - 1) // 3
    end_month = q * 3 + 3
    if end_month == 12:
        return date(d.year, 12, 31)
    return date(d.year, end_month + 1, 1) - timedelta(days=1)


def compliance_pct(facility, today: date) -> float | None:
    """QTD average as a percentage of target — the headline dashboard number."""
    fc = forecast_quarter(facility, today)
    if not fc or not facility.ancc_target:
        return None
    return round(fc["qtd_avg"] / facility.ancc_target * 100, 1)
