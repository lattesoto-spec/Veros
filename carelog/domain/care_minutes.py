from datetime import date, datetime, time, timedelta

from sqlalchemy import or_

from carelog.models import Resident, ResidentDay, db


def _minutes_between(start: time, end: time) -> int:
    s = datetime.combine(date.today(), start)
    e = datetime.combine(date.today(), end)
    if e < s:
        e += timedelta(days=1)
    return int((e - s).total_seconds() // 60)


def active_residents_on(facility_id: int, day: date) -> int:
    ledger = ResidentDay.query.filter_by(facility_id=facility_id, date=day)
    if ledger.first() is not None:
        return sum(1 for row in ledger.all() if resident_day_is_occupied(row))
    return db.session.query(Resident).filter(
        Resident.facility_id == facility_id,
        Resident.admitted_date <= day,
        or_(Resident.discharged_date == None, Resident.discharged_date > day),  # noqa: E711
    ).count()


def resident_day_is_occupied(row: ResidentDay) -> bool:
    """Apply the QFR occupied-bed-day exclusions to one ledger row."""
    if not row.occupied:
        return False
    service = (row.service_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if service in {
        "private", "private_resident", "transition_care", "transition_care_program",
        "tcp", "other_program", "non_an_acc",
    }:
        return False
    leave = (row.leave_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if leave in {"hospital", "hospital_leave", "hospital_transition"} and \
            row.leave_day_number is not None and row.leave_day_number >= 29:
        return False
    return True


def daily_stats(facility_id: int, day: date, ancc_target: float, rn_target: float) -> dict:
    from carelog.domain.compliance import day_breakdown

    breakdown = day_breakdown(facility_id, day)
    residents = breakdown["residents"]
    total_minutes = breakdown["total_minutes"]
    rn_minutes = breakdown["rn_minutes"]
    per_res = breakdown["care_per_resident"]
    rn_per_res = breakdown["rn_per_resident"]

    return {
        "date": day,
        "active_residents": residents,
        "total_minutes": total_minutes,
        "rn_minutes": rn_minutes,
        "care_per_resident": round(per_res, 1),
        "rn_per_resident": round(rn_per_res, 1),
        "status": status_for(per_res, ancc_target),
        "rn_status": status_for(rn_per_res, rn_target),
    }


def status_for(value: float, target: float) -> str:
    if target <= 0:
        return "on_track"
    gap_pct = (value - target) / target
    if value >= target:
        return "on_track"
    if gap_pct >= -0.05:
        return "at_risk"
    return "behind"


def range_stats(facility_id: int, start: date, end: date, ancc_target: float, rn_target: float) -> list[dict]:
    days = []
    d = start
    while d <= end:
        days.append(daily_stats(facility_id, d, ancc_target, rn_target))
        d += timedelta(days=1)
    return days


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
