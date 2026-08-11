"""Compliance engine: role-split care minutes, RN coverage, gap detection,
predictive compliance, and scenario planning.

CALC_VERSION stamps every derived number so reports and the audit page can
say which calculation logic produced them.
"""

from datetime import date, datetime, time, timedelta


from carelog.domain.care_minutes import _minutes_between, active_residents_on, quarter_bounds
from carelog.domain import eligibility
from carelog.models import CareEpisode, ResidentDay, Shift, Staff, db

CALC_VERSION = "2026.08.3"

# Canonical reporting buckets. The mapping engine normalizes to PCW; the
# government statement calls the bucket PCA — they are the same bucket.
ROLE_BUCKETS = eligibility.BUCKETS


def bucket_role(role: str) -> str:
    return eligibility.bucket_for(role)


def worked_minutes(shift: Shift) -> int:
    return max(_minutes_between(shift.start_time, shift.end_time) - (shift.break_minutes or 0), 0)


# ------------------------------------------------------------- daily stats


def day_breakdown(facility_id: int, day: date, evidence_type: str = "worked") -> dict:
    """Per-role direct-care minutes for one day."""
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day,
            Shift.is_direct_care == True,  # noqa: E712
            Shift.evidence_type == evidence_type,
        )
        .all()
    )
    roles = {"RN": 0, "EN": 0, "PCA": 0}
    agency = 0
    excluded = 0            # eligible-looking rows whose staff is not approved
    ineligible = 0          # roles that can never count
    for s, st in shifts:
        m = worked_minutes(s)
        if eligibility.counts_toward_care(st, day):
            roles[bucket_role(st.role)] += m
            if s.is_agency:
                agency += m
        elif bucket_role(st.role) in eligibility.ELIGIBLE_BUCKETS:
            excluded += m   # right role, unapproved — surfaced as an exception
        else:
            ineligible += m
    residents = active_residents_on(facility_id, day)
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
        "other_minutes": ineligible,   # retained for existing report templates
        "agency_minutes": agency,
        "evidence_type": evidence_type,
        "care_per_resident": round(total / residents, 1) if residents else 0.0,
        "rn_per_resident": round(roles["RN"] / residents, 1) if residents else 0.0,
    }


def range_breakdown(facility_id: int, start: date, end: date,
                    evidence_type: str = "worked") -> list[dict]:
    out = []
    d = start
    while d <= end:
        out.append(day_breakdown(facility_id, d, evidence_type=evidence_type))
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


def rn_coverage(facility_id: int, day: date, ignore_gap_minutes: int = 30,
                evidence_type: str = "worked") -> dict:
    """Fraction of the 24h day with at least one RN on duty.

    Only gaps *shorter than* `ignore_gap_minutes` are disregarded: a gap of
    exactly 30 minutes is reportable, so the comparison must be strict.
    """
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day,
            Shift.evidence_type == evidence_type,
        )
        .all()
    )
    intervals = []
    for s, st in shifts:
        if eligibility.bucket_for(st.role) != "RN" or not eligibility.counts_toward_care(st, day):
            continue
        start = datetime.combine(day, s.start_time)
        end = datetime.combine(day, s.end_time)
        if end <= start:
            end += timedelta(days=1)
        intervals.append((start, min(end, datetime.combine(day + timedelta(days=1), time(0)))))
    # overnight shifts from the previous day spill into today
    prev = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.date == day - timedelta(days=1),
            Shift.evidence_type == evidence_type,
        )
        .all()
    )
    day_start = datetime.combine(day, time(0))
    for s, st in prev:
        if eligibility.bucket_for(st.role) != "RN" or not eligibility.counts_toward_care(st, day):
            continue
        if s.end_time <= s.start_time:  # crossed midnight
            intervals.append((day_start, datetime.combine(day, s.end_time)))

    intervals.sort()
    merged = []
    for iv in intervals:
        if merged and iv[0] < merged[-1][1] + timedelta(minutes=ignore_gap_minutes):
            merged[-1] = (merged[-1][0], max(merged[-1][1], iv[1]))
        else:
            merged.append(list(iv) if isinstance(iv, tuple) else iv)
            merged[-1] = (iv[0], iv[1])
    covered = sum((e - s).total_seconds() / 60 for s, e in merged)
    covered = min(covered, 1440)
    # Presentation metadata for the dedicated coverage view. These values are
    # derived from the exact merged intervals used above, so the UI never
    # recreates RN eligibility or gap logic independently.
    day_end = datetime.combine(day + timedelta(days=1), time(0))
    timeline = []
    for start, end in merged:
        start_minute = max(int((start - day_start).total_seconds() / 60), 0)
        end_minute = min(int((end - day_start).total_seconds() / 60), 1440)
        timeline.append({
            "start_minute": start_minute,
            "end_minute": end_minute,
            "left_pct": round(start_minute / 1440 * 100, 4),
            "width_pct": round(max(end_minute - start_minute, 0) / 1440 * 100, 4),
        })
    gap_intervals = []
    cursor = day_start
    for start, end in merged:
        if start > cursor:
            minutes = int((start - cursor).total_seconds() / 60)
            gap_intervals.append({
                "start_minute": int((cursor - day_start).total_seconds() / 60),
                "end_minute": int((start - day_start).total_seconds() / 60),
                "minutes": minutes,
                "reportable": minutes >= ignore_gap_minutes,
            })
        cursor = max(cursor, end)
    if cursor < day_end:
        minutes = int((day_end - cursor).total_seconds() / 60)
        gap_intervals.append({
            "start_minute": int((cursor - day_start).total_seconds() / 60),
            "end_minute": 1440,
            "minutes": minutes,
            "reportable": minutes >= ignore_gap_minutes,
        })
    return {
        "covered_minutes": int(covered),
        "coverage_pct": round(covered / 1440 * 100, 1),
        "full_coverage": covered >= 1440,
        "gaps": max(len(merged) - 1, 0) if merged else (1 if not intervals else 0),
        "timeline": timeline,
        "gap_intervals": gap_intervals,
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
        if fc["qtd_rn_avg"] < facility.rn_target * RN_ONLY_FRACTION:
            alerts.append({
                "severity": "high",
                "message": (f"RN minutes below target: averaging {fc['qtd_rn_avg']:.0f} "
                            f"of {facility.rn_target * RN_ONLY_FRACTION:.1f} required "
                            f"RN-only mins/bed-day this quarter."),
            })
        if fc["qtd_rn_en_avg"] < facility.rn_target:
            alerts.append({
                "severity": "high",
                "message": (f"RN + EN minutes below target: averaging "
                            f"{fc['qtd_rn_en_avg']:.0f} of {facility.rn_target:.0f} "
                            f"mins/bed-day this quarter."),
            })

    # Tomorrow's rostered RN shortfall (only if the roster extends that far)
    tomorrow = today + timedelta(days=1)
    has_future = db.session.query(Shift.id).filter(
        Shift.facility_id == fid, Shift.date >= tomorrow
    ).first() is not None
    if has_future:
        has_roster = db.session.query(Shift.id).filter_by(
            facility_id=fid, date=tomorrow, evidence_type="rostered"
        ).first() is not None
        b = day_breakdown(fid, tomorrow, evidence_type="rostered" if has_roster else "worked")
        residents = b["residents"] or active_residents_on(fid, today)
        rn_needed = facility.rn_target * RN_ONLY_FRACTION * residents
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
        "en_minutes": sum(r["en_minutes"] for r in rows) / n,
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
           if r["residents"] > 0]
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
    projected_bed_days = qtd_bed_days + fut_res * m
    projected = (qtd_total + fut_total * m) / projected_bed_days
    projected_rn = (qtd_rn + fut_rn * m) / projected_bed_days
    projected_rn_en = (qtd_rn + qtd_en + (fut_rn + fut_en) * m) / projected_bed_days

    days_until_breach = None
    target = facility.ancc_target
    if qtd_avg >= target and fut_per_res < target:
        for k in range(1, m + 1):
            if (qtd_total + fut_total * k) / (qtd_bed_days + fut_res * k) < target:
                days_until_breach = k
                break

    return {
        "calc_version": CALC_VERSION,
        "q_start": q_start, "q_end": q_end,
        "qtd_days": n, "days_remaining": m,
        "qtd_avg": round(qtd_avg, 1), "qtd_rn_avg": round(qtd_rn_avg, 1),
        "qtd_rn_en_avg": round(qtd_rn_en_avg, 1),
        "future_per_resident": round(fut_per_res, 1),
        "future_rn_per_resident": round(fut_rn_per_res, 1),
        "projected_avg": round(projected, 1),
        "projected_rn_avg": round(projected_rn, 1),
        "projected_rn_en_avg": round(projected_rn_en, 1),
        "days_until_breach": days_until_breach,
        "on_track": projected >= target,
        "rn_on_track": (
            projected_rn >= facility.rn_target * RN_ONLY_FRACTION
            and projected_rn_en >= facility.rn_target
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
    if not fc or not facility.ancc_target:
        return None
    return round(fc["qtd_avg"] / facility.ancc_target * 100, 1)


# ------------------------------------------------- legislated quarterly tests

# A mainstream residential home must pass all three each quarter.
RN_ONLY_FRACTION = 0.90      # RN-only minutes vs the RN target
RN_PLUS_EN_FRACTION = 1.00   # RN + EN minutes vs the RN target


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
    care_target = facility.ancc_target or 0
    rn_target = facility.rn_target or 0

    def result(key, label, achieved, target, basis):
        return {
            "key": key,
            "label": label,
            "achieved": achieved,
            "target": round(target, 1),
            "basis": basis,
            "passed": achieved >= target if target else None,
            "shortfall": round(max(target - achieved, 0), 1),
            "shortfall_minutes": round(max(target - achieved, 0) * bd),
        }

    tests = [
        result("total_care", "Total care minutes",
               per_bed_day(t["total_minutes"], bd), care_target,
               "RN + EN + eligible PCW/AIN minutes per occupied bed day"),
        result("rn_only", "Registered nurse minutes",
               per_bed_day(t["rn_minutes"], bd), rn_target * RN_ONLY_FRACTION,
               f"RN-only minutes per bed day, against {RN_ONLY_FRACTION:.0%} of the RN target"),
        result("rn_plus_en", "Registered + enrolled nurse minutes",
               per_bed_day(t["rn_en_minutes"], bd), rn_target * RN_PLUS_EN_FRACTION,
               f"RN + EN minutes per bed day, against {RN_PLUS_EN_FRACTION:.0%} of the RN target"),
    ]
    decided = [x for x in tests if x["passed"] is not None]
    from carelog.domain.targets import reconcile_configured_targets

    return {
        "totals": t,
        "tests": tests,
        "passed": all(x["passed"] for x in decided) if decided else None,
        "failed": [x for x in decided if not x["passed"]],
        "calc_version": CALC_VERSION,
        "target_reconciliation": reconcile_configured_targets(facility, start),
    }


def evidence_summary(facility_id: int, start: date, end: date) -> dict:
    """Reconcile evidence streams without mixing them into the statutory total."""
    shift_rows = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(Shift.facility_id == facility_id, Shift.date >= start, Shift.date <= end)
        .all()
    )
    by_type = {"worked": 0, "rostered": 0, "unverified": 0}
    withheld_by_type = {"worked": 0, "rostered": 0, "unverified": 0}
    for shift, staff in shift_rows:
        kind = shift.evidence_type or "unverified"
        by_type.setdefault(kind, 0)
        if shift.is_direct_care and eligibility.counts_toward_care(staff, shift.date):
            by_type[kind] += worked_minutes(shift)
        elif shift.is_direct_care:
            withheld_by_type.setdefault(kind, 0)
            withheld_by_type[kind] += worked_minutes(shift)

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
    ledger_unverified = ledger_query.filter(
        (ResidentDay.service_type == None) | (ResidentDay.service_type == "")  # noqa: E711
    ).count()
    ledger_dates = db.session.query(ResidentDay.date).filter(
        ResidentDay.facility_id == facility_id,
        ResidentDay.date >= start,
        ResidentDay.date <= end,
    ).distinct().count()
    expected_dates = (end - start).days + 1
    worked = by_type.get("worked", 0)
    return {
        "worked_minutes": worked,
        "rostered_minutes": by_type.get("rostered", 0),
        "unverified_minutes": by_type.get("unverified", 0),
        "worked_withheld_minutes": withheld_by_type.get("worked", 0),
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
        "resident_day_ledger_complete": ledger_dates == expected_dates,
    }
