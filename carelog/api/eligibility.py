from datetime import date, datetime, timedelta

from flask import (
    abort,
    flash,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.monitoring import _latest_data_date
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_user,
    require,
)
from carelog.domain import eligibility as el
from carelog.domain.compliance import (
    range_breakdown,
    worked_minutes,
)
from carelog.models import (
    Staff,
    Shift,
    db,
)
from carelog.services.workspace import current_facility


@bp.route("/eligibility")
@require("view_audit")
def eligibility_queue():
    """Staff whose minutes are being withheld until someone confirms them."""
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    rows = (
        Staff.query.filter_by(facility_id=facility.id)
        .order_by(Staff.eligibility_status, Staff.name)
        .all()
    )
    buckets = {"pending": [], "excluded": [], "approved": [], "blocked": []}
    latest_day = _latest_data_date(facility)
    for st in rows:
        bucket = (
            "blocked"
            if st.eligibility_status == el.APPROVED
            and not el.counts_toward_care(st, latest_day)
            else st.eligibility_status
        )
        buckets.setdefault(bucket, []).append(st)
    withheld = sum(
        b["excluded_minutes"]
        for b in range_breakdown(
            facility.id,
            _latest_data_date(facility) - timedelta(days=29),
            _latest_data_date(facility),
        )
    )
    withheld_by_staff = {}
    for shift, member in (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility.id,
            Shift.date >= latest_day - timedelta(days=29),
            Shift.date <= latest_day,
            Shift.evidence_type == "worked",
            Shift.is_direct_care.is_(True),
        )
        .all()
    ):
        if not el.counts_toward_care(member, shift.date):
            withheld_by_staff[member.id] = withheld_by_staff.get(
                member.id, 0
            ) + worked_minutes(shift)
    return resource(
        "eligibility",
        facility=facility,
        buckets=buckets,
        withheld=withheld,
        withheld_by_staff=withheld_by_staff,
        period_start=latest_day - timedelta(days=29),
        period_end=latest_day,
        registration_problems={
            staff.id: el.registration_problem(staff, latest_day)
            or (
                "Source role or approved effective dates exclude this staff member on the included date."
                if staff.eligibility_status == el.APPROVED
                and not el.counts_toward_care(staff, latest_day)
                else None
            )
            for staff in rows
        },
    )


@bp.post("/eligibility/bulk")
@require("manage_facility")
def bulk_eligibility():
    facility = current_facility()
    if not facility:
        abort(404)
    data = form_data()
    try:
        ids = {int(value) for value in data.getlist("staff_ids")}
        if not ids or len(ids) > 100:
            raise ValueError("Choose between 1 and 100 staff records.")
        start = (
            date.fromisoformat(data["eligible_from"])
            if data.get("eligible_from")
            else None
        )
        end = (
            date.fromisoformat(data["eligible_to"]) if data.get("eligible_to") else None
        )
        if start and end and start > end:
            raise ValueError("Eligible-to must be on or after eligible-from.")
    except (TypeError, ValueError) as error:
        return {"error": str(error)}, 400
    decision, reason = data.get("decision"), (data.get("reason") or "").strip()
    if decision not in {el.APPROVED, el.EXCLUDED, el.PENDING} or not reason:
        return {"error": "Choose a decision and record its basis."}, 400
    if decision == el.APPROVED and start is None:
        return {"error": "Bulk approval requires an explicit eligible-from date."}, 400
    from sqlalchemy import select
    from carelog.models import Facility

    db.session.execute(
        select(Facility).where(Facility.id == facility.id).with_for_update()
    )
    members = Staff.query.filter(
        Staff.facility_id == facility.id, Staff.id.in_(ids)
    ).all()
    if len(members) != len(ids):
        abort(404)
    for member in members:
        if decision == el.APPROVED and (
            el.bucket_for(member.role) not in el.ELIGIBLE_BUCKETS
            or el.classify(member.source_role or member.role)[1] == el.EXCLUDED
            or el.registration_problem(member, start)
        ):
            return {
                "error": f"{member.name} needs an individual role or registration review. No bulk decisions were saved."
            }, 400
    actor = current_user()
    for member in members:
        member.eligibility_status, member.eligibility_reason = decision, reason
        member.eligible_from, member.eligible_to = start, end
        member.approved_at, member.approved_by_user_id = datetime.utcnow(), actor.id
        auth.record(
            "eligibility_decided",
            "staff",
            member.id,
            f"Bulk review: {member.name} -> {member.role}/{decision}; {start} to {end}; basis: {reason}",
        )
    db.session.commit()
    return navigate(url_for("api.eligibility_queue"))


@bp.route("/eligibility/<int:staff_id>", methods=["POST"])
@require("manage_facility")
def eligibility_decide(staff_id):
    facility = current_facility()
    if facility:
        from carelog.models import Facility

        db.session.query(Facility).filter_by(id=facility.id).with_for_update().first()
    member = (
        Staff.query.filter_by(id=staff_id, facility_id=facility.id).first()
        if facility
        else None
    )
    if member is None:
        abort(404)
    decision = (form_data().get("decision") or "").strip()
    if decision not in (el.APPROVED, el.EXCLUDED, el.PENDING):
        abort(400)

    mapped_role = (
        (form_data().get("mapped_role") or member.role or "OTHER").strip().upper()
    )
    if mapped_role not in ("RN", "EN", "PCW", "PCA", "AIN", "OTHER"):
        abort(400)
    if mapped_role in ("PCA", "AIN"):
        mapped_role = "PCW"
    if decision == el.APPROVED and (
        el.bucket_for(mapped_role) not in el.ELIGIBLE_BUCKETS
        or el.classify(member.source_role or mapped_role)[1] == el.EXCLUDED
    ):
        return {
            "error": "This source role is ineligible. Correct the source evidence rather than overriding it with a direct-care decision."
        }, 400

    def optional_date(field):
        value = (form_data().get(field) or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            abort(400)

    eligible_from, eligible_to = (
        optional_date("eligible_from"),
        optional_date("eligible_to"),
    )
    if eligible_from and eligible_to and eligible_from > eligible_to:
        return {"error": "Eligible-to must be on or after eligible-from."}, 400
    member.eligibility_status = decision
    member.role = mapped_role
    reason = (form_data().get("reason") or "").strip()
    if decision in (el.APPROVED, el.EXCLUDED) and not reason:
        flash("Record a basis for an approval or exclusion.")
        return navigate(url_for("api.eligibility_queue"))
    member.eligibility_reason = reason or f"left {decision} by an administrator"
    member.registration_number = (
        form_data().get("registration_number") or ""
    ).strip() or member.registration_number
    member.registration_expiry = optional_date("registration_expiry")
    member.eligible_from = eligible_from
    member.eligible_to = eligible_to
    member.employment_type = (
        form_data().get("employment_type") or ""
    ).strip() or member.employment_type

    problem = el.registration_problem(member, member.eligible_from or date.today())
    if decision == el.APPROVED and problem:
        flash(f"Cannot approve: {problem}.")
        return navigate(url_for("api.eligibility_queue"))
    actor = current_user()
    member.approved_by_user_id = actor.id if actor else None
    member.approved_at = datetime.utcnow()
    auth.record(
        "eligibility_decided",
        "staff",
        member.id,
        f"{member.name} ({member.source_role or member.role}) -> {member.role}/{decision}: "
        f"{member.eligibility_reason}",
    )
    db.session.commit()
    flash(f"{member.name} marked {decision}. Care minutes recalculated on next view.")
    return navigate(url_for("api.eligibility_queue"))
