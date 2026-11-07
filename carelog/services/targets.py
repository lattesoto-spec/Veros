"""Validate configured facility targets before changing persisted state."""

from math import isfinite
from datetime import date, datetime

from carelog.models import FacilityTarget, db


def parse_targets(data):
    try:
        care = float(data.get("ancc_target", ""))
        rn = float(data.get("rn_target", ""))
    except (TypeError, ValueError):
        raise ValueError("Enter both verified care and RN targets as numbers.") from None
    if not all(isfinite(value) and value > 0 for value in (care, rn)):
        raise ValueError("Care and RN targets must be finite numbers greater than zero.")
    if rn > care:
        raise ValueError("The RN reference target cannot exceed the total care target.")
    return care, rn


def target_details(data):
    care, rn = parse_targets(data)
    try:
        start = date.fromisoformat(data.get("target_quarter", ""))
    except (TypeError, ValueError):
        raise ValueError("Choose the target quarter start date (1 January, April, July or October).") from None
    if start.day != 1 or start.month not in (1, 4, 7, 10):
        raise ValueError("Targets must start on the first day of a quarter.")
    source = (data.get("target_source") or "").strip()
    if not source:
        raise ValueError("Record the target source, such as the provider's published target notice.")
    return care, rn, start, source


def save_target(facility, data, actor):
    from carelog import auth

    care, rn, start, source = target_details(data)
    existing = FacilityTarget.query.filter_by(facility_id=facility.id, quarter_start=start).first()
    correction = (data.get("target_correction") or "").strip()
    if existing and (existing.ancc_target, existing.rn_target, existing.source) != (care, rn, source) and not correction:
        raise ValueError("Changing an existing quarter requires a correction reason. Other quarters are preserved.")
    previous = f"{existing.ancc_target}/{existing.rn_target}; source {existing.source}" if existing else "not configured"
    row = existing or FacilityTarget(facility_id=facility.id, quarter_start=start)
    row.ancc_target, row.rn_target, row.source = care, rn, source
    row.approved_by_user_id = actor.id if actor else None
    row.approved_at = datetime.utcnow()
    db.session.add(row)
    facility.ancc_target, facility.rn_target = care, rn
    auth.record("target_corrected" if existing else "target_approved", "facility", facility.id,
                f"Quarter {start}: {previous} -> {care}/{rn}; source {source}; basis {correction or 'initial approval'}")
    return row
