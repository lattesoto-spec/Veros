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
)
from carelog.models import (
    Staff,
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
            and el.registration_problem(st, latest_day)
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
    return resource(
        "eligibility",
        facility=facility,
        buckets=buckets,
        withheld=withheld,
        registration_problems={
            staff.id: el.registration_problem(staff, latest_day) for staff in rows
        },
    )


@bp.route("/eligibility/<int:staff_id>", methods=["POST"])
@require("manage_facility")
def eligibility_decide(staff_id):
    facility = current_facility()
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

    def optional_date(field):
        value = (form_data().get(field) or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            abort(400)

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
    member.eligible_from = optional_date("eligible_from")
    member.eligible_to = optional_date("eligible_to")
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
