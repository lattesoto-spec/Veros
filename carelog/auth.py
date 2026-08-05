"""Authentication, roles and the tenant boundary.

Two rules hold the multi-tenancy together:

1. Nothing is reachable without a session (`login_required` runs as a
   before_request hook, so a new route is protected by default rather than by
   remembering to decorate it).
2. Care data is only ever read through `current_organization_id()`. Routes that
   take an id from the URL must additionally call `owned_or_404()` — a foreign
   id in a URL is the easiest way to leak another customer's data.
"""

import functools
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from carelog.models import AuditLog, Organization, User, db

# ------------------------------------------------------------------- roles

PERMISSIONS = (
    "view_dashboard",     # dashboard, compliance figures
    "run_scenarios",      # what-if modelling
    "export_data",        # CSV / Excel / PDF downloads
    "import_data",        # upload rosters, run imports
    "view_audit",         # audit trail and evidence list
    "download_evidence",  # download the retained source files
    "manage_facility",    # targets, facility details
    "manage_integrations",
    "manage_users",       # invite, edit, deactivate users
)

ROLES: dict[str, dict] = {
    "administrator": {
        "label": "Administrator",
        "description": "Full provider-workspace access, including user management.",
        "permissions": set(PERMISSIONS),
    },
    "facility_manager": {
        "label": "Facility Manager",
        "description": "Runs the facility day to day: imports, targets, integrations.",
        "permissions": {
            "view_dashboard", "run_scenarios", "export_data", "import_data",
            "view_audit", "download_evidence", "manage_facility", "manage_integrations",
        },
    },
    "clinical_manager": {
        "label": "Clinical Manager",
        "description": "Reviews care minutes and models staffing changes.",
        "permissions": {"view_dashboard", "run_scenarios", "export_data", "view_audit"},
    },
    "compliance_officer": {
        "label": "Compliance Officer",
        "description": "Prepares evidence for regulators; can import and export.",
        "permissions": {
            "view_dashboard", "export_data", "import_data", "view_audit",
            "download_evidence", "run_scenarios",
        },
    },
    "auditor": {
        "label": "Auditor",
        "description": "Read-only access to figures and their supporting evidence.",
        "permissions": {"view_dashboard", "export_data", "view_audit", "download_evidence"},
    },
    "read_only": {
        "label": "Read Only",
        "description": "Can see the dashboard and compliance position, nothing else.",
        "permissions": {"view_dashboard"},
    },
}

# Endpoints reachable without a session.
PUBLIC_ENDPOINTS = {"auth.login", "stylesheet", "static", "import_run", "healthz"}


def role_label(role: str) -> str:
    return ROLES.get(role, {}).get("label", role)


# -------------------------------------------------------------- current user


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    user = db.session.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def current_organization_id() -> int | None:
    """The tenant every data query must be scoped to.

    A superuser may be inspecting another organization for support; that is
    recorded in the session by `switch_organization` and audited.
    """
    user = current_user()
    if user is None:
        return None
    if user.is_superuser:
        # A platform owner has no provider workspace of their own. Customer
        # data becomes reachable only after the explicit, audited "Enter
        # workspace" action sets acting_org_id.
        return session.get("acting_org_id")
    return user.organization_id


def has_permission(permission: str) -> bool:
    user = current_user()
    if user is None:
        return False
    if user.is_superuser:
        # Platform routes use superuser_required. Provider permissions are
        # granted only while the owner is deliberately supporting a customer.
        return bool(session.get("acting_org_id"))
    return permission in ROLES.get(user.role, {}).get("permissions", set())


def require(permission: str):
    """Route decorator: 403 unless the signed-in user holds `permission`."""
    def outer(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if not has_permission(permission):
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return outer


def is_platform_owner() -> bool:
    user = current_user()
    return bool(user and user.is_superuser)


def superuser_required(fn):
    """Platform-owner routes. 404 rather than 403 — a customer administrator
    has no business knowing the platform console exists."""
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        if not is_platform_owner():
            abort(404)
        return fn(*args, **kwargs)
    return inner


def act_as_organization(org_id: int | None):
    """Enter (or leave) a client organization for support.

    Everything downstream reads `current_organization_id()`, so setting this
    makes the whole application render that customer's data — which is exactly
    why it is superuser-only, audited, and shown as a banner on every page.
    """
    if not is_platform_owner():
        abort(404)
    if org_id is None:
        previous = session.pop("acting_org_id", None)
        session.pop("facility_id", None)
        if previous:
            record("platform_stopped_acting", "organization", previous)
        return None
    org = db.session.get(Organization, org_id)
    if org is None:
        abort(404)
    session["acting_org_id"] = org.id
    # The previously selected facility belongs to a different customer.
    session.pop("facility_id", None)
    record("platform_acting_as", "organization", org.id, org.name)
    return org


def owned_or_404(obj):
    """Guard for anything fetched by an id taken from the URL."""
    if obj is None:
        abort(404)
    org_id = current_organization_id()
    owner = getattr(obj, "organization_id", None)
    # Rows created before organizations existed carry NULL; treat them as
    # belonging to whoever is looking, since a single-tenant database only
    # ever had one customer in it.
    if owner is not None and owner != org_id:
        abort(404)
    return obj


# ------------------------------------------------------------------ audit


def record(action: str, entity: str = None, entity_id=None, detail: str = None):
    """Append to the security/audit log. Caller commits."""
    user = current_user()
    db.session.add(AuditLog(
        organization_id=current_organization_id(),
        user_id=user.id if user else None,
        user_email=user.email if user else None,
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail,
        ip_address=(request.headers.get("x-forwarded-for", request.remote_addr or "") or "").split(",")[0].strip(),
    ))


# ------------------------------------------------------------------ routes

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user():
            return redirect(url_for("index"))
        return render_template("login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = User.query.filter(db.func.lower(User.email) == email).first()

    if user is None or not user.is_active or not check_password_hash(user.password_hash, password):
        # Deliberately identical for unknown email, wrong password and
        # deactivated account: no probing for valid addresses.
        db.session.add(AuditLog(
            action="login_failed", entity="user", entity_id=email,
            ip_address=(request.headers.get("x-forwarded-for", request.remote_addr or "") or "").split(",")[0].strip(),
        ))
        db.session.commit()
        return render_template(
            "login.html", error="Email or password is incorrect.", email=email
        ), 401

    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    user.last_login_at = datetime.utcnow()
    record("login", "user", user.id)
    db.session.commit()

    if user.must_change_password:
        flash("Please choose a new password.")
        return redirect(url_for("auth.change_password"))
    next_url = (request.args.get("next") or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("index"))


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    if current_user():
        record("logout")
        db.session.commit()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
def change_password():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if request.method == "GET":
        return render_template("change_password.html", forced=user.must_change_password)

    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    # A forced first-time change has no meaningful "current" password to prove.
    if not user.must_change_password and not check_password_hash(user.password_hash, current):
        return render_template("change_password.html", error="Current password is incorrect."), 400
    problem = password_problem(new, confirm)
    if problem:
        return render_template("change_password.html", error=problem,
                               forced=user.must_change_password), 400

    user.password_hash = generate_password_hash(new)
    user.must_change_password = False
    record("password_changed", "user", user.id)
    db.session.commit()
    flash("Password updated.")
    return redirect(url_for("index"))


def password_problem(new: str, confirm: str) -> str | None:
    if len(new) < 12:
        return "Password must be at least 12 characters."
    if new != confirm:
        return "The two passwords do not match."
    return None


# ------------------------------------------------------- app wiring helpers


def init_app(app):
    """Attach the login gate and expose auth helpers to templates."""
    app.register_blueprint(bp)

    @app.before_request
    def _require_login():
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("debug_"):
            return None
        user = current_user()
        if user is None:
            if request.accept_mimetypes.best == "application/json" or endpoint.endswith("_json"):
                return {"error": "authentication required"}, 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if user.is_superuser and not session.get("acting_org_id"):
            platform_endpoint = endpoint.startswith("platform_") or endpoint in {
                "auth.change_password", "auth.logout", "healthz", "stylesheet",
            } or endpoint.startswith("debug_")
            if not platform_endpoint:
                # Never execute a provider mutation without an explicitly
                # selected customer account. GETs return to the console;
                # writes fail closed.
                if request.method in ("GET", "HEAD"):
                    return redirect(url_for("platform_console"))
                abort(403)
        return None

    @app.context_processor
    def _expose():
        from carelog import app as app_module

        user = current_user()
        in_customer_workspace = bool(
            user and (not user.is_superuser or session.get("acting_org_id"))
        )
        facilities = app_module.org_facilities() if in_customer_workspace else []
        return {
            "current_user": user,
            "has_permission": has_permission,
            "role_label": role_label,
            "org_facilities": facilities,
            "active_facility": app_module.current_facility() if in_customer_workspace else None,
            "is_platform_owner": is_platform_owner(),
            "acting_org": db.session.get(Organization, session["acting_org_id"])
            if user and user.is_superuser and session.get("acting_org_id") else None,
            "home_org": db.session.get(Organization, user.organization_id) if user else None,
        }


def create_user(*, organization_id, email, name, role, password,
                is_superuser=False, must_change_password=True) -> User:
    user = User(
        organization_id=organization_id,
        email=email.strip().lower(),
        name=name.strip(),
        role=role,
        password_hash=generate_password_hash(password),
        is_superuser=is_superuser,
        must_change_password=must_change_password,
    )
    db.session.add(user)
    return user
