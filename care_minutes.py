from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_

from models import Resident, Shift, Staff, db


def _minutes_between(start: time, end: time) -> int:
    s = datetime.combine(date.today(), start)
    e = datetime.combine(date.today(), end)
    if e < s:
        e += timedelta(days=1)
    return int((e - s).total_seconds() // 60)


def active_residents_on(facility_id: int, day: date) -> int:
    return db.session.query(Resident).filter(
        Resident.facility_id == facility_id,
        Resident.admitted_date <= day,
        or_(Resident.discharged_date == None, Resident.discharged_date > day),  # noqa: E711
    ).count()


def daily_stats(facility_id: int, day: date, ancc_target: float, rn_target: float) -> dict:
    residents = active_residents_on(facility_id, day)

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

    total_minutes = sum(_minutes_between(s.start_time, s.end_time) for s, _ in shifts)
    rn_minutes = sum(
        _minutes_between(s.start_time, s.end_time) for s, st in shifts if st.role == "RN"
    )

    per_res = total_minutes / residents if residents else 0
    rn_per_res = rn_minutes / residents if residents else 0

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
    rows_with_data = [r for r in rows if r["active_residents"] > 0 and r["total_minutes"] > 0]
    if not rows_with_data:
        return {"care_per_resident": 0, "rn_per_resident": 0, "days": 0}
    care = sum(r["care_per_resident"] for r in rows_with_data) / len(rows_with_data)
    rn = sum(r["rn_per_resident"] for r in rows_with_data) / len(rows_with_data)
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
