"""Compliance engine: role-split care minutes, RN coverage, gap detection,
predictive compliance, and scenario planning.

CALC_VERSION stamps every derived number so reports and the audit page can
say which calculation logic produced them.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal


from carelog.domain.care_minutes import (
    _minutes_between,
    active_residents_on,
    quarter_bounds,
    resident_day_is_occupied,
)
from carelog.domain import eligibility
from carelog.models import CareEpisode, Resident, ResidentDay, Shift, Staff, db

CALC_VERSION = "2026.11.6"

# Canonical reporting buckets. The mapping engine normalizes to PCW; the
# government statement calls the bucket PCA — they are the same bucket.
ROLE_BUCKETS = eligibility.BUCKETS


def bucket_role(role: str) -> str:
    return eligibility.bucket_for(role)


def worked_minutes(shift: Shift) -> int:
    if shift.duration_minutes is not None:
        return max(shift.duration_minutes, 0)
    return max(
        _minutes_between(shift.start_time, shift.end_time) - (shift.break_minutes or 0),
        0,
    )


# ------------------------------------------------------------- daily stats


def _day_from_rows(day, shifts, ledger, residents_master, evidence_type):
    roles = {"RN": 0, "EN": 0, "PCA": 0}
    agency = 0
    agency_roles = {"RN": 0, "EN": 0, "PCA": 0}
    excluded = 0  # eligible-looking rows whose staff is not approved
    ineligible = 0  # roles that can never count
    for s, st in shifts:
        m = worked_minutes(s)
        if eligibility.counts_toward_care(st, day):
            roles[bucket_role(st.role)] += m
            if s.is_agency:
                agency += m
                agency_roles[bucket_role(st.role)] += m
        elif st.eligibility_status != eligibility.EXCLUDED and (
            st.eligibility_status == eligibility.PENDING
            or bucket_role(st.role) in eligibility.ELIGIBLE_BUCKETS
        ):
            excluded += m  # unresolved title, dates or credentials — visibly withheld
        else:
            ineligible += m
    residents = (
        sum(resident_day_is_occupied(row) for row in ledger)
        if ledger
        else sum(
            person.admitted_date is not None
            and person.admitted_date <= day
            and (person.discharged_date is None or person.discharged_date > day)
            for person in residents_master
        )
    )
    # Only eligible, approved minutes reach the total.
    total = sum(roles.values())
    return {
        "date": day,
        "residents": residents,
        "total_minutes": total,
        "rn_minutes": roles["RN"],
        "en_minutes": roles["EN"],
        "pca_minutes": roles["PCA"],
        "rn_en_minutes": roles["RN"] + roles["EN"],
        "excluded_minutes": excluded,
        "ineligible_minutes": ineligible,
        "other_minutes": ineligible,  # retained for existing report templates
        "agency_minutes": agency,
        "agency_rn_minutes": agency_roles["RN"],
        "agency_en_minutes": agency_roles["EN"],
        "evidence_type": evidence_type,
        "care_per_resident": round(total / residents, 1) if residents else 0.0,
        "rn_per_resident": round(roles["RN"] / residents, 1) if residents else 0.0,
    }


def day_breakdown(facility_id: int, day: date, evidence_type: str = "worked") -> dict:
    return range_breakdown(facility_id, day, day, evidence_type)[0]


def range_breakdown(
    facility_id: int, start: date, end: date, evidence_type: str = "worked"
) -> list[dict]:
    # Three bounded queries per period, independent of the number of dates.
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date >= start,
            Shift.date <= end,
            Shift.is_direct_care.is_(True),
            Shift.evidence_type == evidence_type,
        )
        .all()
    )
    ledger = ResidentDay.query.filter(
        ResidentDay.facility_id == facility_id,
        ResidentDay.date >= start,
        ResidentDay.date <= end,
    ).all()
    residents = Resident.query.filter_by(facility_id=facility_id).all()
    by_day, census = {}, {}
    for shift, staff in shifts:
        by_day.setdefault(shift.date, []).append((shift, staff))
    for row in ledger:
        census.setdefault(row.date, []).append(row)
    out, day = [], start
    while day <= end:
        out.append(
            _day_from_rows(
                day, by_day.get(day, []), census.get(day, []), residents, evidence_type
            )
        )
        day += timedelta(days=1)
    return out


def monthly_breakdown(facility_id: int, start: date, end: date) -> list[dict]:
    """Aggregate range_breakdown rows into calendar months."""
    months: dict[tuple, dict] = {}
    for r in range_breakdown(facility_id, start, end):
        if r["residents"] == 0 and r["total_minutes"] == 0:
            continue
        key = (r["date"].year, r["date"].month)
        m = months.setdefault(
            key,
            {
                "year": key[0],
                "month": key[1],
                "days": 0,
                "bed_days": 0,
                "total_minutes": 0,
                "rn_minutes": 0,
                "en_minutes": 0,
                "pca_minutes": 0,
                "agency_minutes": 0,
            },
        )
        m["days"] += 1
        m["bed_days"] += r["residents"]
        for k in (
            "total_minutes",
            "rn_minutes",
            "en_minutes",
            "pca_minutes",
            "agency_minutes",
        ):
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


def rn_coverage(
    facility_id: int,
    day: date,
    ignore_gap_minutes: int = 30,
    evidence_type: str = "worked",
) -> dict:
    """Continuous coverage around midnight; duration-only evidence proves no interval."""
    day_start = datetime.combine(day, time(0))
    day_end = day_start + timedelta(days=1)
    window_start, window_end = (
        day_start - timedelta(days=1),
        day_end + timedelta(days=1),
    )
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date >= day - timedelta(days=1),
            Shift.date <= day + timedelta(days=1),
            Shift.evidence_type == evidence_type,
        )
        .all()
    )
    intervals, unknown = [], False
    for shift, staff in shifts:
        if eligibility.bucket_for(staff.role) != "RN":
            continue
        if not shift.coverage_known or shift.start_time == shift.end_time:
            if shift.date == day:
                unknown = True
            continue
        start = datetime.combine(shift.date, shift.start_time)
        end = datetime.combine(shift.date, shift.end_time)
        if end < start:
            end += timedelta(days=1)
        # Effective dates and registration apply on each calendar date covered.
        cursor = start
        while cursor < end:
            boundary = min(
                end, datetime.combine(cursor.date() + timedelta(days=1), time(0))
            )
            if eligibility.counts_toward_care(staff, cursor.date()):
                intervals.append((max(cursor, window_start), min(boundary, window_end)))
            cursor = boundary
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    timeline = []
    covered = 0
    for start, end in merged:
        left, right = max(start, day_start), min(end, day_end)
        if right > left:
            lo, hi = (
                int((left - day_start).total_seconds() / 60),
                int((right - day_start).total_seconds() / 60),
            )
            covered += hi - lo
            timeline.append(
                {
                    "start_minute": lo,
                    "end_minute": hi,
                    "left_pct": round(lo / 1440 * 100, 4),
                    "width_pct": round((hi - lo) / 1440 * 100, 4),
                }
            )
    gaps = []
    cursor = window_start
    for start, end in [*merged, (window_end, window_end)]:
        left, right = max(cursor, day_start), min(start, day_end)
        if right > left:
            duration = int((start - cursor).total_seconds() / 60)
            gaps.append(
                {
                    "start_minute": int((left - day_start).total_seconds() / 60),
                    "end_minute": int((right - day_start).total_seconds() / 60),
                    "minutes": int((right - left).total_seconds() / 60),
                    "reportable": duration >= ignore_gap_minutes,
                }
            )
        cursor = max(cursor, end)
    return {
        "covered_minutes": covered,
        "coverage_pct": round(covered / 1440 * 100, 1),
        "full_coverage": not unknown and not any(g["reportable"] for g in gaps),
        "coverage_known": not unknown,
        "gaps": len(gaps),
        "timeline": timeline,
        "gap_intervals": gaps,
    }


# ------------------------------------------------------------- gap detection


def detect_gaps(facility, today: date) -> list[dict]:
    """Alert list: [{severity: 'high'|'medium'|'info', message: str}]."""
    alerts = []
    fid = facility.id
    from carelog.domain.targets import target_for_quarter

    approved = target_for_quarter(facility, today)
    care_target = approved.ancc_target if approved else 0
    rn_target = approved.rn_target if approved else 0

    # Quarter-to-date position
    fc = forecast_quarter(facility, today)
    if fc and fc["qtd_days"] > 0 and approved:
        exact = quarter_totals(fid, quarter_bounds(today)[0], today)
        bd = exact["bed_days"]
        if Decimal(exact["total_minutes"]) < Decimal(str(care_target)) * bd:
            alerts.append(
                {
                    "severity": "high",
                    "message": (
                        f"Facility is below its AN-ACC target: averaging "
                        f"{fc['qtd_avg']:.0f} of {care_target:.0f} "
                        f"mins/resident/day this quarter."
                    ),
                }
            )
        elif fc["projected_avg"] < care_target:
            when = (
                f"in {fc['days_until_breach']} day(s)"
                if fc["days_until_breach"]
                else "by quarter end"
            )
            alerts.append(
                {
                    "severity": "high",
                    "message": (
                        f"Predicted breach: at current staffing the quarter average "
                        f"falls below target {when} "
                        f"(projected {fc['projected_avg']:.0f} vs target "
                        f"{care_target:.0f})."
                    ),
                }
            )
        if Decimal(exact["rn_minutes"]) < Decimal(str(rn_target)) * Decimal("0.9") * bd:
            alerts.append(
                {
                    "severity": "high",
                    "message": (
                        f"RN minutes below target: averaging {fc['qtd_rn_avg']:.0f} "
                        f"of {rn_target * RN_ONLY_FRACTION:.1f} required "
                        f"RN-only mins/bed-day this quarter."
                    ),
                }
            )
        if Decimal(exact["rn_en_minutes"]) < Decimal(str(rn_target)) * bd:
            alerts.append(
                {
                    "severity": "high",
                    "message": (
                        f"RN + EN minutes below target: averaging "
                        f"{fc['qtd_rn_en_avg']:.0f} of {rn_target:.0f} "
                        f"mins/bed-day this quarter."
                    ),
                }
            )

    # Tomorrow's rostered RN shortfall (only if the roster extends that far)
    tomorrow = today + timedelta(days=1)
    has_future = (
        db.session.query(Shift.id)
        .filter(Shift.facility_id == fid, Shift.date >= tomorrow)
        .first()
        is not None
    )
    if has_future and approved:
        has_roster = (
            db.session.query(Shift.id)
            .filter_by(facility_id=fid, date=tomorrow, evidence_type="rostered")
            .first()
            is not None
        )
        b = day_breakdown(
            fid, tomorrow, evidence_type="rostered" if has_roster else "worked"
        )
        residents = b["residents"] or active_residents_on(fid, today)
        rn_needed = rn_target * RN_ONLY_FRACTION * residents
        if b["rn_minutes"] < rn_needed:
            alerts.append(
                {
                    "severity": "high",
                    "message": (
                        f"Roster on {tomorrow.isoformat()} is short {rn_needed - b['rn_minutes']:.0f} RN "
                        f"minutes ({b['rn_minutes']:.0f} rostered vs "
                        f"{rn_needed:.0f} needed for {residents} residents)."
                    ),
                }
            )
        care_needed = care_target * residents
        if b["total_minutes"] < care_needed:
            alerts.append(
                {
                    "severity": "medium",
                    "message": (
                        f"Roster on {tomorrow.isoformat()} is {care_needed - b['total_minutes']:.0f} "
                        f"care minutes below target "
                        f"({b['total_minutes']:.0f} of {care_needed:.0f})."
                    ),
                }
            )

    # 24/7 RN coverage today
    cov = rn_coverage(fid, today)
    if not cov["full_coverage"] and cov["covered_minutes"] > 0:
        alerts.append(
            {
                "severity": "medium",
                "message": (
                    f"RN coverage on {today.isoformat()} is {cov['coverage_pct']}% "
                    f"({1440 - cov['covered_minutes']} uncovered minutes)."
                ),
            }
        )
    elif cov["covered_minutes"] == 0:
        alerts.append(
            {
                "severity": "high",
                "message": f"No clocked RN coverage is evidenced on {today.isoformat()}.",
            }
        )

    if not alerts:
        alerts.append(
            {
                "severity": "info",
                "message": f"No additional risks detected by the available checks through {today.isoformat()}; review evidence limitations.",
            }
        )
    return alerts


# ------------------------------------------------------------- forecasting


def _trailing_baseline(facility_id: int, today: date, days: int = 14) -> dict | None:
    """Average daily totals over the trailing window — the 'typical day'."""
    rows = [
        r
        for r in range_breakdown(facility_id, today - timedelta(days=days - 1), today)
        if r["residents"] > 0
    ]
    if not rows:
        return None
    n = len(rows)
    return {
        "total_minutes": sum(r["total_minutes"] for r in rows) / n,
        "rn_minutes": sum(r["rn_minutes"] for r in rows) / n,
        "en_minutes": sum(r["en_minutes"] for r in rows) / n,
        "agency_minutes": sum(r["agency_minutes"] for r in rows) / n,
        "agency_rn_minutes": sum(r["agency_rn_minutes"] for r in rows) / n,
        "agency_en_minutes": sum(r["agency_en_minutes"] for r in rows) / n,
        "residents": sum(r["residents"] for r in rows) / n,
    }


def forecast_quarter(
    facility, today: date, adjustments: dict | None = None
) -> dict | None:
    """Project the quarter-end average. `adjustments` (scenario planning):
    rn_shifts_removed: int   — 8h RN shifts removed per future day
    agency_removed: bool     — all agency minutes disappear from future days
    occupancy_delta: int     — residents added (+) or discharged (−) go-forward
    """
    q_start, _ = quarter_bounds(today)
    from carelog.domain.targets import target_for_quarter

    configured = target_for_quarter(facility, today)
    if configured is None:
        return None
    q_end = _calendar_quarter_end(today)
    qtd = [
        r for r in range_breakdown(facility.id, q_start, today) if r["residents"] > 0
    ]
    baseline = _trailing_baseline(facility.id, today)
    if not qtd or not baseline:
        return None

    n = len(qtd)
    qtd_bed_days = sum(r["residents"] for r in qtd)
    qtd_total = sum(r["total_minutes"] for r in qtd)
    qtd_rn = sum(r["rn_minutes"] for r in qtd)
    qtd_en = sum(r["en_minutes"] for r in qtd)
    qtd_avg = qtd_total / qtd_bed_days
    qtd_rn_avg = qtd_rn / qtd_bed_days
    qtd_rn_en_avg = (qtd_rn + qtd_en) / qtd_bed_days

    adj = adjustments or {}
    fut_total = baseline["total_minutes"]
    fut_rn = baseline["rn_minutes"]
    fut_en = baseline["en_minutes"]
    fut_res = baseline["residents"]
    if adj.get("agency_removed"):
        fut_total -= baseline["agency_minutes"]
        fut_rn -= baseline["agency_rn_minutes"]
        fut_en -= baseline["agency_en_minutes"]
    if adj.get("rn_shifts_removed"):
        # Additional RN reductions apply after agency removal, bounded by the
        # remaining RN time so the same minutes cannot be removed twice.
        removed = min(adj["rn_shifts_removed"] * 8 * 60, fut_rn)
        fut_total -= removed
        fut_rn -= removed
    fut_res += adj.get("occupancy_delta", 0)
    if fut_res <= 0:
        raise ValueError("The scenario must retain at least one resident.")
    fut_total = max(fut_total, 0)
    fut_rn = max(fut_rn, 0)
    fut_en = max(fut_en, 0)

    fut_per_res = fut_total / fut_res
    fut_rn_per_res = fut_rn / fut_res

    m = max((q_end - today).days, 0)
    projected_bed_days = qtd_bed_days + fut_res * m
    projected = (qtd_total + fut_total * m) / projected_bed_days
    projected_rn = (qtd_rn + fut_rn * m) / projected_bed_days
    projected_rn_en = (qtd_rn + qtd_en + (fut_rn + fut_en) * m) / projected_bed_days

    days_until_breach = None
    target = configured.ancc_target
    if qtd_avg >= target and fut_per_res < target:
        for k in range(1, m + 1):
            if (qtd_total + fut_total * k) / (qtd_bed_days + fut_res * k) < target:
                days_until_breach = k
                break

    return {
        "calc_version": CALC_VERSION,
        "q_start": q_start,
        "q_end": q_end,
        "qtd_days": n,
        "days_remaining": m,
        "qtd_avg": round(qtd_avg, 1),
        "qtd_rn_avg": round(qtd_rn_avg, 1),
        "qtd_rn_en_avg": round(qtd_rn_en_avg, 1),
        "future_per_resident": round(fut_per_res, 1),
        "future_rn_per_resident": round(fut_rn_per_res, 1),
        "projected_avg": round(projected, 1),
        "projected_rn_avg": round(projected_rn, 1),
        "projected_rn_en_avg": round(projected_rn_en, 1),
        "days_until_breach": days_until_breach,
        "on_track": projected >= target,
        "rn_on_track": (
            projected_rn >= configured.rn_target * RN_ONLY_FRACTION
            and projected_rn_en >= configured.rn_target
        ),
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
    from carelog.domain.targets import target_for_quarter

    target = target_for_quarter(facility, today)
    if not fc or not target or not target.ancc_target:
        return None
    return round(fc["qtd_avg"] / target.ancc_target * 100, 1)


# ------------------------------------------------- legislated quarterly tests

# A mainstream residential home must pass all three each quarter.
RN_ONLY_FRACTION = 0.90  # RN-only minutes vs the RN target
RN_PLUS_EN_FRACTION = 1.00  # RN + EN minutes vs the RN target


def quarter_totals(facility_id: int, start: date, end: date) -> dict:
    """Eligible minutes and occupied bed days over a period.

    Care minutes are a weighted figure: total eligible worked minutes divided
    by occupied bed days. Averaging each day's per-resident ratio instead
    over-weights days with few residents and understates days with many, which
    is not the measure the Rules describe.
    """
    rows = range_breakdown(facility_id, start, end)
    bed_days = sum(r["residents"] for r in rows)
    return {
        "start": start,
        "end": end,
        "days": len(rows),
        "bed_days": bed_days,
        "total_minutes": sum(r["total_minutes"] for r in rows),
        "rn_minutes": sum(r["rn_minutes"] for r in rows),
        "en_minutes": sum(r["en_minutes"] for r in rows),
        "pca_minutes": sum(r["pca_minutes"] for r in rows),
        "rn_en_minutes": sum(r["rn_en_minutes"] for r in rows),
        "excluded_minutes": sum(r["excluded_minutes"] for r in rows),
        "ineligible_minutes": sum(r["ineligible_minutes"] for r in rows),
        "agency_minutes": sum(r["agency_minutes"] for r in rows),
        "zero_bed_day_worked_dates": [r["date"].isoformat() for r in rows if r["residents"] == 0 and r["total_minutes"] > 0],
    }


def per_bed_day(minutes: float, bed_days: int) -> float:
    return round(minutes / bed_days, 1) if bed_days else 0.0


def quarterly_tests(facility, start: date, end: date) -> dict:
    """The three tests a mainstream home must pass, each as its own result.

    A single pass/fail on total minutes hides the nursing requirements: a home
    can meet its total while failing either RN test, and each failure is
    separately reportable.
    """
    t = quarter_totals(facility.id, start, end)
    bd = t["bed_days"]
    from carelog.domain.targets import target_for_quarter

    configured = target_for_quarter(facility, start)
    care_target = configured.ancc_target if configured else 0
    rn_target = configured.rn_target if configured else 0

    from carelog.domain.care_minutes import denominator_issues

    limitations = denominator_issues(facility.id, start, end)
    if configured is None:
        limitations.append(
            "No approved target evidence is recorded for this quarter. Configure the quarter and source in Settings."
        )
    if quarter_bounds(start)[0] != quarter_bounds(end)[0]:
        limitations.append(
            "This date range crosses quarters; assess each quarter against its own approved targets."
        )
    if not bd:
        limitations.append("No occupied bed days are available.")

    if t["excluded_minutes"]:
        limitations.append(f"{t['excluded_minutes']} worked minutes are withheld pending eligibility, effective-date or registration review.")
    if t["zero_bed_day_worked_dates"]:
        limitations.append("Eligible worked minutes occur on zero-occupied-bed-day dates: " + ", ".join(t["zero_bed_day_worked_dates"]) + ". Reconcile their allocation before a verdict.")
    worked_dates = {row[0] for row in db.session.query(Shift.date).filter(
        Shift.facility_id == facility.id, Shift.evidence_type == "worked",
        Shift.date >= start, Shift.date <= end,
    ).distinct().all()}
    missing_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)
                     if start + timedelta(days=i) not in worked_dates]
    if missing_dates:
        limitations.append("No worked-time evidence on: " + ", ".join(d.isoformat() for d in missing_dates) + ". Verify source completeness; absence is not proof of zero time.")
    complete_quarter = start == quarter_bounds(start)[0] and end == _calendar_quarter_end(start)
    # Threshold comparisons remain useful during a quarter, but are not a
    # completed quarterly verdict. Evidence limitations withhold both.
    evidence_clear = not limitations
    if not complete_quarter:
        limitations.append("Quarter to date / partial period: a definitive quarterly verdict requires a complete calendar quarter.")

    def result(key, label, minutes, target, basis):
        target = Decimal(str(target))
        achieved_exact = Decimal(minutes) / Decimal(bd) if bd else Decimal(0)
        shortfall = max(target * bd - minutes, Decimal(0))
        return {
            "key": key,
            "label": label,
            "achieved": float(round(achieved_exact, 1)),
            "target": float(target) if configured else None,
            "basis": basis,
            "passed": minutes >= target * bd
            if target > 0 and bd and not limitations
            else None,
            "on_track": minutes >= target * bd if target > 0 and bd and evidence_clear else None,
            "shortfall": float(round(shortfall / bd, 3)) if bd and configured else None,
            "shortfall_minutes": float(shortfall) if configured else None,
            "provisional": bool(limitations),
        }

    tests = [
        result(
            "total_care",
            "Total care minutes",
            t["total_minutes"],
            care_target,
            "RN + EN + eligible PCW/AIN minutes per occupied bed day",
        ),
        result(
            "rn_only",
            "Registered nurse minutes",
            t["rn_minutes"],
            Decimal(str(rn_target)) * Decimal("0.9"),
            f"RN-only minutes per bed day, against {RN_ONLY_FRACTION:.0%} of the RN target",
        ),
        result(
            "rn_plus_en",
            "Registered + enrolled nurse minutes",
            t["rn_en_minutes"],
            rn_target,
            f"RN + EN minutes per bed day, against {RN_PLUS_EN_FRACTION:.0%} of the RN target",
        ),
    ]
    decided = [x for x in tests if x["passed"] is not None]
    from carelog.domain.targets import reconcile_configured_targets

    return {
        "totals": t,
        "tests": tests,
        "passed": all(x["passed"] for x in decided) if len(decided) == 3 else None,
        "failed": [x for x in decided if not x["passed"]],
        "calc_version": CALC_VERSION,
        "complete_quarter": complete_quarter,
        "period_label": "Quarter" if complete_quarter else "Quarter to date / partial period",
        "on_track": all(x["on_track"] for x in tests) if all(x["on_track"] is not None for x in tests) else None,
        "missing_worked_dates": [d.isoformat() for d in missing_dates],
        "limitations": limitations,
        "provisional": bool(limitations),
        "target_reconciliation": reconcile_configured_targets(facility, start),
    }


def evidence_summary(facility_id: int, start: date, end: date) -> dict:
    """Reconcile evidence streams without mixing them into the statutory total."""
    shift_rows = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id, Shift.date >= start, Shift.date <= end
        )
        .all()
    )
    by_type = {"worked": 0, "rostered": 0, "unverified": 0}
    ineligible_by_type = {"worked": 0, "rostered": 0, "unverified": 0}
    withheld_by_type = {"worked": 0, "rostered": 0, "unverified": 0}
    for shift, staff in shift_rows:
        kind = shift.evidence_type or "unverified"
        by_type.setdefault(kind, 0)
        if shift.is_direct_care and eligibility.counts_toward_care(staff, shift.date):
            by_type[kind] += worked_minutes(shift)
        elif shift.is_direct_care:
            unresolved = staff.eligibility_status != eligibility.EXCLUDED and (
                staff.eligibility_status == eligibility.PENDING or bucket_role(staff.role) in eligibility.ELIGIBLE_BUCKETS
            )
            destination = withheld_by_type if unresolved else ineligible_by_type
            destination.setdefault(kind, 0)
            destination[kind] += worked_minutes(shift)

    episodes = CareEpisode.query.filter(
        CareEpisode.facility_id == facility_id,
        CareEpisode.date >= start,
        CareEpisode.date <= end,
    ).all()
    delivered = sum(e.minutes or 0 for e in episodes)
    ledger_query = ResidentDay.query.filter(
        ResidentDay.facility_id == facility_id,
        ResidentDay.date >= start,
        ResidentDay.date <= end,
    )
    ledger_rows = ledger_query.count()
    from carelog.domain.care_minutes import denominator_issues, resident_day_problem

    ledger_unverified = sum(
        bool(resident_day_problem(row)) for row in ledger_query.all()
    )
    limitations = denominator_issues(facility_id, start, end)
    ledger_dates = (
        db.session.query(ResidentDay.date)
        .filter(
            ResidentDay.facility_id == facility_id,
            ResidentDay.date >= start,
            ResidentDay.date <= end,
        )
        .distinct()
        .count()
    )
    expected_dates = (end - start).days + 1
    worked = by_type.get("worked", 0)
    return {
        "worked_minutes": worked,
        "rostered_minutes": by_type.get("rostered", 0),
        "unverified_minutes": by_type.get("unverified", 0),
        "worked_withheld_minutes": withheld_by_type.get("worked", 0),
        "worked_ineligible_minutes": ineligible_by_type.get("worked", 0),
        "rostered_withheld_minutes": withheld_by_type.get("rostered", 0),
        "unverified_withheld_minutes": withheld_by_type.get("unverified", 0),
        "delivered_minutes": delivered,
        "delivered_variance": delivered - worked,
        "care_episode_count": len(episodes),
        "resident_day_rows": ledger_rows,
        "resident_day_unverified_rows": ledger_unverified,
        "resident_day_dates": ledger_dates,
        "resident_day_expected_dates": expected_dates,
        "uses_resident_day_ledger": ledger_rows > 0,
        "resident_day_ledger_complete": ledger_dates == expected_dates
        and not limitations,
        "denominator_issues": limitations,
    }
