from flask import (
    abort,
    flash,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.database import _schema_drift
from carelog.domain.compliance import (
    CALC_VERSION,
)
from carelog.ingestion import jobs as import_jobs
from carelog.ingestion.analyzer import ai_ready, resolve_model
from carelog.models import (
    AuditLog,
    Facility,
    ImportReceipt,
    IntegrationConfig,
    Organization,
    Resident,
    Shift,
    User,
    db,
)
from carelog.services.workspace import _storage
from carelog.storage import StorageError


def _platform_org_summary(org):
    facilities = Facility.query.filter_by(organization_id=org.id).all()
    facility_ids = [f.id for f in facilities]
    latest = (
        ImportReceipt.query.filter_by(organization_id=org.id)
        .order_by(ImportReceipt.imported_at.desc())
        .first()
    )
    return {
        "org": org,
        "users": User.query.filter_by(
            organization_id=org.id, is_active=True, is_superuser=False
        ).count(),
        "facilities": len(facility_ids),
        "shifts": (
            Shift.query.filter(Shift.facility_id.in_(facility_ids)).count()
            if facility_ids
            else 0
        ),
        "residents": (
            Resident.query.filter(
                Resident.facility_id.in_(facility_ids),
                Resident.discharged_date.is_(None),
            ).count()
            if facility_ids
            else 0
        ),
        "last_import": latest.imported_at if latest else None,
        "admins": User.query.filter_by(
            organization_id=org.id,
            role="administrator",
            is_active=True,
            is_superuser=False,
        ).all(),
    }


@bp.route("/platform")
@auth.superuser_required
def platform_console():
    """Cross-organization view. This is the only place in the application
    that deliberately reads across the tenant boundary."""
    rows = [
        _platform_org_summary(org)
        for org in Organization.query.order_by(Organization.name).all()
    ]
    return resource(
        "platform",
        rows=rows,
        total_users=User.query.filter_by(is_active=True, is_superuser=False).count(),
        total_facilities=Facility.query.count(),
        recent_entries=AuditLog.query.order_by(AuditLog.created_at.desc())
        .limit(8)
        .all(),
    )


@bp.route("/platform/organizations/<int:organization_id>")
@auth.superuser_required
def platform_organization(organization_id):
    org = db.session.get(Organization, organization_id)
    if org is None:
        abort(404)
    facilities = (
        Facility.query.filter_by(organization_id=org.id).order_by(Facility.name).all()
    )
    facility_rows = []
    for facility in facilities:
        latest = (
            Shift.query.filter_by(facility_id=facility.id)
            .order_by(Shift.date.desc())
            .first()
        )
        facility_rows.append(
            {
                "facility": facility,
                "residents": Resident.query.filter_by(
                    facility_id=facility.id, discharged_date=None
                ).count(),
                "shifts": Shift.query.filter_by(facility_id=facility.id).count(),
                "latest": latest.date if latest else None,
            }
        )
    return resource(
        "platform_organization",
        summary=_platform_org_summary(org),
        facility_rows=facility_rows,
        users=User.query.filter_by(organization_id=org.id, is_superuser=False)
        .order_by(User.is_active.desc(), User.email)
        .all(),
        imports=(
            ImportReceipt.query.filter_by(organization_id=org.id)
            .order_by(ImportReceipt.imported_at.desc())
            .limit(10)
            .all()
        ),
        integrations=IntegrationConfig.query.filter_by(organization_id=org.id)
        .order_by(IntegrationConfig.platform)
        .all(),
        recent_entries=(
            AuditLog.query.filter_by(organization_id=org.id)
            .order_by(AuditLog.created_at.desc())
            .limit(12)
            .all()
        ),
    )


@bp.route("/platform/organizations/<int:organization_id>/update", methods=["POST"])
@auth.superuser_required
def platform_update_organization(organization_id):
    org = db.session.get(Organization, organization_id)
    if org is None:
        abort(404)
    name = (form_data().get("name") or "").strip()
    timezone = (form_data().get("timezone") or "").strip()
    if not name:
        flash("Organisation name is required.")
    elif Organization.query.filter(
        db.func.lower(Organization.name) == name.lower(),
        Organization.id != org.id,
    ).first():
        flash(f"An organisation called {name} already exists.")
    else:
        org.name = name
        if timezone:
            org.timezone = timezone
        auth.record("platform_org_updated", "organization", org.id, name)
        db.session.commit()
        flash(f"{org.name} updated.")
    return navigate(url_for("api.platform_organization", organization_id=org.id))


@bp.route("/platform/activity")
@auth.superuser_required
def platform_activity():
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(1000).all()
    organizations = {o.id: o for o in Organization.query.all()}
    return resource("platform_activity", entries=entries, organizations=organizations)


@bp.route("/platform/system")
@auth.superuser_required
def platform_system():
    try:
        storage_status = _storage().describe()
    except StorageError as error:
        storage_status = {"backend": "unavailable", "error": str(error)}
    return resource(
        "platform_system",
        database=db.engine.url.render_as_string(hide_password=True),
        schema_drift=_schema_drift(),
        storage_status=storage_status,
        worker_mode=import_jobs.worker_mode(),
        mapping_service_ready=ai_ready(),
        mapping_model=resolve_model(),
        calc_version=CALC_VERSION,
    )


@bp.route("/platform/organizations/create", methods=["POST"])
@auth.superuser_required
def platform_create_org():
    name = (form_data().get("name") or "").strip()
    email = (form_data().get("admin_email") or "").strip().lower()
    admin_name = (form_data().get("admin_name") or "").strip()
    password = form_data().get("password") or ""

    if not name:
        flash("Organisation name is required.")
    elif Organization.query.filter(
        db.func.lower(Organization.name) == name.lower()
    ).first():
        flash(f"An organisation called {name} already exists.")
    elif not email or "@" not in email:
        flash("A valid administrator email is required.")
    elif User.query.filter(db.func.lower(User.email) == email).first():
        flash("A user with that email already exists.")
    elif problem := auth.password_problem(password, password):
        flash(problem)
    else:
        org = Organization(name=name)
        db.session.add(org)
        db.session.flush()
        user = auth.create_user(
            organization_id=org.id,
            email=email,
            name=admin_name,
            role="administrator",
            password=password,
            must_change_password=True,
        )
        db.session.flush()
        auth.record(
            "platform_org_created",
            "organization",
            org.id,
            f"{name} with administrator {email}",
        )
        db.session.commit()
        flash(
            f"{name} created with {user.email} as its administrator. "
            "They must change the password at first sign-in."
        )
    return navigate(url_for("api.platform_console"))


@bp.route("/platform/act", methods=["POST"])
@auth.superuser_required
def platform_act():
    organization_id = form_data().get("organization_id", type=int)
    if organization_id is None:
        abort(400)
    org = auth.act_as_organization(organization_id)
    db.session.commit()
    flash(f"Support workspace opened for {org.name}.")
    return navigate(url_for("api.index"))


@bp.route("/platform/act/stop", methods=["POST"])
@auth.superuser_required
def platform_stop_acting():
    auth.act_as_organization(None)
    db.session.commit()
    flash("Returned to platform administration.")
    return navigate(url_for("api.platform_console"))
