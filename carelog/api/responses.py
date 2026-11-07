"""Explicit API serialization. Never expose ORM credentials or storage internals."""

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import urlsplit

from flask import current_app, get_flashed_messages, jsonify, request, session
from flask_wtf.csrf import generate_csrf
from werkzeug.datastructures import MultiDict

MODEL_FIELDS = {
    "Organization": "id name created_at timezone",
    "User": "id organization_id email name role is_active is_superuser created_at last_login_at must_change_password mfa_enabled mfa_enrolled_at",
    "Facility": "id organization_id name ancc_target rn_target target_history",
    "FacilityTarget": "id quarter_start ancc_target rn_target source approved_by_user_id approved_at",
    "Resident": "id resident_id name ancc_class admitted_date discharged_date",
    "ResidentDay": "id resident_id resident_name date occupied service_type leave_type leave_day_number ancc_class exclusion_reason source_row source_file source_sheet source_page",
    "Staff": "id staff_id name role source_role eligibility_status eligibility_reason eligible_from eligible_to approved_by_user_id approved_at employment_type classification registration_number registration_expiry",
    "Shift": "id staff_id date start_time end_time is_direct_care break_minutes duration_minutes coverage_known is_agency evidence_type labour_cost import_receipt_id source_row source_file source_sheet source_page",
    "CareEpisode": "id resident_id resident_name date care_type care_category staff_id staff_name source_role start_time end_time minutes import_receipt_id source_row source_file source_sheet source_page",
    "ImportReceipt": "id facility_id imported_at residents_imported residents_skipped shifts_imported shifts_skipped resident_days_imported care_episodes_imported evidence_type first_shift_date last_shift_date imported_by calc_version",
    "FormatMapping": "id kinds source_filename created_at fingerprint",
    "IntegrationConfig": "id platform enabled last_sync_at last_result config_json",
    "AuditLog": "id organization_id user_email action entity entity_id detail created_at",
    "TrustedDevice": "id created_at last_used_at expires_at revoked_at",
}


def serialize(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize(v) for v in value]
    fields = MODEL_FIELDS.get(type(value).__name__)
    if fields:
        return {key: serialize(getattr(value, key, None)) for key in fields.split()}
    if is_dataclass(value):
        return serialize(asdict(value))
    if hasattr(value, "_mapping"):
        return serialize(dict(value._mapping))
    raise TypeError(f"No public serializer for {type(value).__name__}")


def form_data():
    if request.is_json:
        data = request.get_json(silent=True)
        return MultiDict(data) if isinstance(data, dict) else MultiDict()
    return request.form


def frontend_path(url):
    parts = urlsplit(url or "/")
    if (
        parts.scheme
        or parts.netloc
        or not parts.path.startswith("/")
        or parts.path.startswith("//")
        or "\\" in parts.path
    ):
        return "/"
    path = (
        parts.path[4:]
        if parts.path == "/api" or parts.path.startswith("/api/")
        else parts.path
    )
    return (path or "/") + (("?" + parts.query) if parts.query else "")


def navigate(url):
    return jsonify(navigate=frontend_path(url), csrf_token=generate_csrf())


def context():
    from carelog import auth
    from carelog.models import Organization, db
    from carelog.services.workspace import (
        current_facility,
        org_facilities,
        workspace_context,
    )

    user = auth.current_user()
    active = bool(user and (not user.is_superuser or session.get("acting_org_id")))
    facility = current_facility() if active else None
    org_id = auth.current_organization_id() if active else None
    permissions = (
        sorted(
            {
                p
                for role in auth.ROLES.values()
                for p in role["permissions"]
                if auth.has_permission(p)
            }
        )
        if user
        else []
    )
    return serialize(
        {
            "user": user,
            "organization": db.session.get(Organization, org_id) if org_id else None,
            "acting_org_id": session.get("acting_org_id")
            if user and user.is_superuser
            else None,
            "permissions": permissions,
            "facilities": org_facilities() if active else [],
            "facility": facility,
            "workspace": workspace_context(facility) if active else {},
            "contacts": {
                key.removesuffix("_EMAIL").lower(): current_app.config[key]
                for key in current_app.config
                if key.endswith("_EMAIL")
            },
            "connection_encrypted": request.is_secure,
        }
    )


def resource(view, **data):
    return jsonify(
        view=view,
        data=serialize(data),
        context=context(),
        csrf_token=generate_csrf(),
        messages=get_flashed_messages(),
    )
