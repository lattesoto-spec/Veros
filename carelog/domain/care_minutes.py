from datetime import date, datetime, time, timedelta

from sqlalchemy import or_

from carelog.models import Resident, ResidentDay, db


def _minutes_between(start: time, end: time) -> int:
    s = datetime.combine(date(2000, 1, 1), start)
    e = datetime.combine(date(2000, 1, 1), end)
    if e < s:
        e += timedelta(days=1)
    return int((e - s).total_seconds() // 60)


def active_residents_on(facility_id: int, day: date) -> int:
    ledger = ResidentDay.query.filter_by(facility_id=facility_id, date=day)
    if ledger.first() is not None:
        return sum(1 for row in ledger.all() if resident_day_is_occupied(row))
    return (
        db.session.query(Resident)
        .filter(
            Resident.facility_id == facility_id,
            Resident.admitted_date <= day,
            or_(Resident.discharged_date == None, Resident.discharged_date > day),  # noqa: E711
        )
        .count()
    )


def resident_day_is_occupied(row: ResidentDay) -> bool:
    """Apply the QFR occupied-bed-day exclusions to one ledger row."""
    if not row.occupied:
        return False
    service = (
        (row.service_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    )
    if resident_day_problem(row):
        return False
    if service in {
        "private",
        "private_resident",
        "transition_care",
        "transition_care_program",
        "tcp",
        "other_program",
        "non_an_acc",
        "private_/_non_subsidised",
        "transition_care_programme",
    }:
        return False
    leave = (row.leave_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if (
        leave in {"hospital", "hospital_leave", "hospital_transition"}
        and row.leave_day_number is not None
        and row.leave_day_number >= 29
    ):
        return False
    return True


def resident_day_problem(row) -> str | None:
    if row.occupied and (row.exclusion_reason or "").strip():
        return "occupied resident-day conflicts with an exclusion reason"
    service = (
        (row.service_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    )
    if service not in {
        "permanent",
        "respite",
        "an_acc",
        "an_acc_permanent",
        "an_acc_respite",
        "residential",
        "private",
        "private_resident",
        "transition_care",
        "transition_care_program",
        "transition_care_programme",
        "private_/_non_subsidised",
        "tcp",
        "other_program",
        "non_an_acc",
    }:
        return "missing or unrecognised resident service type"
    leave = (row.leave_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if leave not in {
        "",
        "none",
        "no",
        "hospital",
        "hospital_leave",
        "hospital_transition",
        "social",
        "social_leave",
    }:
        return "unrecognised leave type"
    if leave in {"hospital", "hospital_leave", "hospital_transition"} and (
        row.leave_day_number is None or row.leave_day_number < 1
    ):
        return "hospital leave needs a positive consecutive leave day"
    return None


def denominator_issues(facility_id, start, end):
    """Reconcile complete census identities, not merely distinct dates."""
    rows = ResidentDay.query.filter(
        ResidentDay.facility_id == facility_id,
        ResidentDay.date >= start - timedelta(days=1),
        ResidentDay.date <= end,
    ).all()
    residents = Resident.query.filter_by(facility_id=facility_id).all()
    by_day = {}
    for row in rows:
        by_day.setdefault(row.date, []).append(row)
    issues = []
    day = start
    while day <= end:
        ledger = by_day.get(day, [])
        if not ledger:
            issues.append(f"{day}: no verified resident-day census")
        else:
            ids = {row.resident_id for row in ledger}
            expected = {
                r.resident_id
                for r in residents
                if r.admitted_date
                and r.admitted_date <= day
                and (r.discharged_date is None or r.discharged_date > day)
            }
            missing = expected - ids
            if missing:
                issues.append(f"{day}: census omits {len(missing)} active resident(s)")
            previous = {r.resident_id for r in by_day.get(day - timedelta(days=1), [])}
            discharged = {
                r.resident_id
                for r in residents
                if r.discharged_date and r.discharged_date <= day
            }
            if previous - ids - discharged and (residents or len(ids) < len(previous)):
                issues.append(
                    f"{day}: census identities dropped without recorded discharge"
                )
            invalid = sum(bool(resident_day_problem(row)) for row in ledger)
            if invalid:
                issues.append(
                    f"{day}: {invalid} resident-day row(s) have unverified service or leave evidence"
                )
        day += timedelta(days=1)
    return issues


def _stats_from_breakdown(breakdown, ancc_target, rn_target):
    residents = breakdown["residents"]
    total_minutes = breakdown["total_minutes"]
    rn_minutes = breakdown["rn_minutes"]
    per_res = breakdown["care_per_resident"]
    rn_per_res = breakdown["rn_per_resident"]

    return {
        "date": breakdown["date"],
        "active_residents": residents,
        "total_minutes": total_minutes,
        "rn_minutes": rn_minutes,
        "care_per_resident": round(per_res, 1),
        "rn_per_resident": round(rn_per_res, 1),
        "status": status_for(total_minutes / residents, ancc_target) if residents else "unavailable",
        "rn_status": status_for(rn_minutes / residents, rn_target * 0.9 if rn_target else None) if residents else "unavailable",
    }


def daily_stats(
    facility_id: int, day: date, ancc_target: float, rn_target: float
) -> dict:
    from carelog.domain.compliance import day_breakdown

    from carelog.models import FacilityTarget
    target = FacilityTarget.query.filter_by(facility_id=facility_id, quarter_start=quarter_bounds(day)[0]).first()
    return _stats_from_breakdown(day_breakdown(facility_id, day),
                                target.ancc_target if target else None,
                                target.rn_target if target else None)


def status_for(value: float, target: float) -> str:
    if target is None or target <= 0:
        return "unavailable"
    gap_pct = (value - target) / target
    if value >= target:
        return "on_track"
    if gap_pct >= -0.05:
        return "at_risk"
    return "behind"


def range_stats(
    facility_id: int, start: date, end: date, ancc_target: float, rn_target: float
) -> list[dict]:
    from carelog.domain.compliance import range_breakdown

    from carelog.models import FacilityTarget
    targets = {t.quarter_start: t for t in FacilityTarget.query.filter(
        FacilityTarget.facility_id == facility_id,
        FacilityTarget.quarter_start >= quarter_bounds(start)[0],
        FacilityTarget.quarter_start <= quarter_bounds(end)[0],
    ).all()}
    out = []
    for row in range_breakdown(facility_id, start, end):
        target = targets.get(quarter_bounds(row["date"])[0])
        out.append(_stats_from_breakdown(row, target.ancc_target if target else None,
                                       target.rn_target if target else None))
    return out


def average(rows: list[dict]) -> dict:
    rows_with_data = [r for r in rows if r["active_residents"] > 0]
    if not rows_with_data:
        return {"care_per_resident": 0, "rn_per_resident": 0, "days": 0}
    bed_days = sum(r["active_residents"] for r in rows_with_data)
    care = sum(r["total_minutes"] for r in rows_with_data) / bed_days
    rn = sum(r["rn_minutes"] for r in rows_with_data) / bed_days
    return {
        "care_per_resident": round(care, 1),
        "rn_per_resident": round(rn, 1),
        "days": len(rows_with_data),
    }


def quarter_bounds(today: date) -> tuple[date, date]:
    q = (today.month - 1) // 3
    start_month = q * 3 + 1
    start = date(today.year, start_month, 1)
    return start, today
