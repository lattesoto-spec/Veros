from flask import (
    abort,
    flash,
    request,
    session,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_organization_id,
    has_permission,
    require,
)
from carelog.domain import eligibility as el
from carelog.domain.care_minutes import (
    quarter_bounds,
)
from carelog.domain.compliance import (
    quarterly_tests,
)
from carelog.models import (
    CareEpisode,
    Facility,
    ImportReceipt,
    Organization,
    Resident,
    ResidentDay,
    Shift,
    Staff,
    db,
)
from carelog.services.workspace import (
    _confirmed,
    _delete_facility_data,
    _purge_evidence,
    current_facility,
    org_facilities,
    set_active_facility,
)


@bp.route("/settings", methods=["GET", "POST"])
@require("manage_facility")
def settings():
    facility = current_facility()

    if request.method == "POST":
        if not has_permission("manage_facility"):
            abort(403)
        name = (form_data().get("facility_name") or "").strip()
        if not name:
            flash("Facility name is required.")
            return navigate(url_for("api.settings"))
        if not facility:
            facility = Facility(name=name, organization_id=current_organization_id())
            db.session.add(facility)
        facility.name = name
        try:
            facility.ancc_target = float(
                form_data().get("ancc_target") or facility.ancc_target or 215
            )
            facility.rn_target = float(
                form_data().get("rn_target") or facility.rn_target or 44
            )
        except ValueError:
            flash("Targets must be numbers.")
            return navigate(url_for("api.settings"))
        auth.record(
            "facility_updated",
            "facility",
            facility.id,
            f"targets {facility.ancc_target}/{facility.rn_target}",
        )
        db.session.commit()
        flash("Facility settings saved.")
        return navigate(url_for("api.settings"))

    return resource(
        "settings",
        facility=facility,
        organization=db.session.get(Organization, current_organization_id()),
    )


@bp.route("/facility", methods=["GET"])
@require("view_dashboard")
def facility_view():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    residents = (
        Resident.query.filter_by(facility_id=facility.id).order_by(Resident.name).all()
    )
    staff = (
        Staff.query.filter_by(facility_id=facility.id)
        .order_by(Staff.role, Staff.name)
        .all()
    )
    return resource(
        "facility",
        facility=facility,
        residents=residents,
        staff=staff,
        resident_day_count=ResidentDay.query.filter_by(facility_id=facility.id).count(),
        care_episode_count=CareEpisode.query.filter_by(facility_id=facility.id).count(),
    )


@bp.route("/facility/targets", methods=["POST"])
@require("manage_facility")
def update_targets():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    try:
        facility.ancc_target = float(
            form_data().get("ancc_target") or facility.ancc_target
        )
        facility.rn_target = float(form_data().get("rn_target") or facility.rn_target)
        db.session.commit()
    except ValueError:
        flash("Targets must be numbers.")
    return navigate(url_for("api.dashboard"))


@bp.route("/clear", methods=["POST"])
@require("manage_facility")
def clear():
    """Delete this organization's care data, including retained evidence."""
    if not _confirmed(request):
        flash('Type "delete" to confirm. Nothing was deleted.')
        return navigate(url_for("api.settings"))

    org_id = current_organization_id()
    facilities = Facility.query.filter_by(organization_id=org_id).all()
    facility_ids = [f.id for f in facilities]
    receipts = (
        ImportReceipt.query.filter(ImportReceipt.facility_id.in_(facility_ids)).all()
        if facility_ids
        else []
    )
    files = _purge_evidence(receipts)
    if facility_ids:
        _delete_facility_data(facility_ids)
        Facility.query.filter(Facility.id.in_(facility_ids)).delete(
            synchronize_session=False
        )
    auth.record(
        "data_cleared",
        "organization",
        org_id,
        f"{len(facility_ids)} facility/facilities, {files} evidence file(s)",
    )
    db.session.commit()
    flash(
        f"Deleted all care data for your organization "
        f"({len(facility_ids)} facility/facilities, {files} evidence file(s))."
    )
    return navigate(url_for("api.universal_import"))


@bp.route("/facilities")
@require("view_dashboard")
def facilities_view():
    facilities = org_facilities()
    active = current_facility()
    summary = {}
    for f in facilities:
        latest = (
            Shift.query.filter_by(facility_id=f.id, evidence_type="worked")
            .order_by(Shift.date.desc())
            .first()
        )
        period_status = None
        if latest:
            period_start, _ = quarter_bounds(latest.date)
            result = quarterly_tests(f, period_start, latest.date)
            if result["totals"]["bed_days"]:
                period_status = result["passed"]
        summary[f.id] = {
            "shifts": Shift.query.filter_by(facility_id=f.id).count(),
            "residents": Resident.query.filter_by(
                facility_id=f.id, discharged_date=None
            ).count(),
            "latest": latest.date if latest else None,
            "period_status": period_status,
            "open_issues": Staff.query.filter_by(
                facility_id=f.id, eligibility_status=el.PENDING
            ).count(),
        }
    return resource(
        "facilities",
        facilities=facilities,
        active=active,
        summary=summary,
        organization=db.session.get(Organization, current_organization_id()),
    )


@bp.route("/facilities/switch", methods=["POST"])
@require("view_dashboard")
def facility_switch():
    facility = set_active_facility(form_data().get("facility_id", type=int))
    if facility is None:
        abort(404)
    flash(f"Now viewing {facility.name}.")
    return navigate(form_data().get("next") or url_for("api.dashboard"))


@bp.route("/facilities/create", methods=["POST"])
@require("manage_facility")
def facility_create():
    name = (form_data().get("name") or "").strip()
    if not name:
        flash("Facility name is required.")
        return navigate(url_for("api.facilities_view"))
    existing = Facility.query.filter_by(
        organization_id=current_organization_id(), name=name
    ).first()
    if existing:
        flash(f"{name} already exists.")
        return navigate(url_for("api.facilities_view"))
    facility = Facility(name=name, organization_id=current_organization_id())
    try:
        facility.ancc_target = float(form_data().get("ancc_target") or 215)
        facility.rn_target = float(form_data().get("rn_target") or 44)
    except ValueError:
        flash("Targets must be numbers.")
        return navigate(url_for("api.facilities_view"))
    db.session.add(facility)
    db.session.flush()
    auth.record("facility_created", "facility", facility.id, name)
    db.session.commit()
    session["facility_id"] = facility.id
    flash(f"{name} created and selected.")
    return navigate(url_for("api.facilities_view"))


@bp.route("/facilities/<int:facility_id>/update", methods=["POST"])
@require("manage_facility")
def facility_update(facility_id):
    facility = Facility.query.filter_by(
        id=facility_id, organization_id=current_organization_id()
    ).first()
    if facility is None:
        abort(404)
    name = (form_data().get("name") or "").strip()
    if name:
        facility.name = name
    try:
        facility.ancc_target = float(
            form_data().get("ancc_target") or facility.ancc_target
        )
        facility.rn_target = float(form_data().get("rn_target") or facility.rn_target)
    except ValueError:
        flash("Targets must be numbers.")
        return navigate(url_for("api.facilities_view"))
    auth.record(
        "facility_updated",
        "facility",
        facility.id,
        f"{facility.name} targets {facility.ancc_target}/{facility.rn_target}",
    )
    db.session.commit()
    flash(f"{facility.name} updated.")
    return navigate(url_for("api.facilities_view"))


@bp.route("/facilities/<int:facility_id>/delete", methods=["POST"])
@require("manage_facility")
def facility_delete(facility_id):
    facility = Facility.query.filter_by(
        id=facility_id, organization_id=current_organization_id()
    ).first()
    if facility is None:
        abort(404)
    if not _confirmed(request):
        flash('Type "delete" to confirm. Nothing was deleted.')
        return navigate(url_for("api.facilities_view"))

    name = facility.name
    files = _purge_evidence(
        ImportReceipt.query.filter_by(facility_id=facility.id).all()
    )
    _delete_facility_data([facility.id])
    db.session.delete(facility)
    auth.record(
        "facility_deleted", "facility", facility_id, f"{name}, {files} evidence file(s)"
    )
    db.session.commit()
    session.pop("facility_id", None)
    flash(f"{name} deleted, along with its care data and {files} evidence file(s).")
    return navigate(url_for("api.facilities_view"))
