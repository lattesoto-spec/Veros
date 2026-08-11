"""Authentication, roles and the tenant boundary.

Two rules hold the multi-tenancy together:

1. Nothing is reachable without a session (`login_required` runs as a
   before_request hook, so a new route is protected by default rather than by
   remembering to decorate it).
2. Care data is only ever read through `current_organization_id()`. Routes that
   take an id from the URL must additionally call `owned_or_404()` — a foreign
   id in a URL is the easiest way to leak another customer's data.
"""

import base64
import functools
import hashlib
import hmac
import io
import os
import secrets
import time
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from cryptography.fernet import Fernet, InvalidToken
from flask_wtf.csrf import CSRFError, CSRFProtect
import pyotp
import qrcode
import qrcode.image.svg
from werkzeug.security import check_password_hash, generate_password_hash

from carelog.models import (
    AuditLog,
    AuthRateLimit,
    MfaRecoveryCode,
    Organization,
    TrustedDevice,
    User,
    db,
)

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
PUBLIC_ENDPOINTS = {
    "auth.login", "auth.logout", "auth.change_password", "auth.mfa_setup",
    "auth.mfa_challenge", "auth.mfa_recovery", "auth.mfa_complete",
    "auth.forgot_password", "auth.privacy",
    "stylesheet", "brand_asset", "static", "import_run", "healthz",
}

PREAUTH_LIFETIME = timedelta(minutes=10)
TRUSTED_DEVICE_LIFETIME = timedelta(days=30)
TRUSTED_DEVICE_COOKIE = "caremin_trusted_device"
REMEMBERED_EMAIL_COOKIE = "caremin_remembered_email"
REMEMBERED_EMAIL_LIFETIME = timedelta(days=90)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_BLOCK = timedelta(minutes=15)
LOGIN_PAIR_LIMIT = 5
LOGIN_IP_LIMIT = 20
MFA_ATTEMPT_LIMIT = 5
RECOVERY_CODE_COUNT = 10

csrf = CSRFProtect()


def role_label(role: str) -> str:
    return ROLES.get(role, {}).get("label", role)


# -------------------------------------------------------------- current user


def current_user() -> User | None:
    uid = session.get("user_id")
    if not uid:
        return None
    user = db.session.get(User, uid)
    # Cookies issued before MFA rollout intentionally have no auth_version and
    # are rejected. This is also the revocation check for password/MFA changes.
    if user is None or not user.is_active or \
            session.get("auth_version") != user.auth_version:
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


def client_ip() -> str:
    """Best available client address for audit and coarse throttling.

    Vercel owns the forwarded header in production. Self-hosted deployments
    use the socket peer so a caller cannot choose their own throttle bucket.
    """
    if os.environ.get("VERCEL"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def record_auth(action: str, user: User | None = None, detail: str = None):
    """Record an authentication event without requiring a full session."""
    db.session.add(AuditLog(
        organization_id=user.organization_id if user else None,
        user_id=user.id if user else None,
        user_email=user.email if user else None,
        action=action,
        entity="user",
        entity_id=str(user.id) if user else None,
        detail=detail,
        ip_address=client_ip(),
    ))


def resolve_mfa_encryption_key(secret_key: str) -> str:
    """Return the dedicated production key or a stable local-only key."""
    configured = (os.environ.get("MFA_ENCRYPTION_KEY") or "").strip()
    if configured:
        return configured
    digest = hashlib.sha256(f"caremin-local-mfa:{secret_key}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def valid_mfa_encryption_key(value: str) -> bool:
    try:
        Fernet(value.encode("ascii"))
        return True
    except (TypeError, ValueError):
        return False


def _fernet() -> Fernet:
    key = current_app.config.get("MFA_ENCRYPTION_KEY", "")
    if isinstance(key, str):
        key = key.encode("ascii")
    return Fernet(key)


def _encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def _decrypt_secret(user: User) -> str:
    if not user.mfa_secret_encrypted:
        raise InvalidToken
    return _fernet().decrypt(user.mfa_secret_encrypted.encode("ascii")).decode("ascii")


def _safe_next(value: str | None) -> str:
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") else url_for("index")


def _remembered_email_fernet() -> Fernet:
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    key = hashlib.sha256(b"caremin-remembered-email:" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _remembered_email() -> str:
    token = request.cookies.get(REMEMBERED_EMAIL_COOKIE, "")
    if not token:
        return ""
    try:
        email = _remembered_email_fernet().decrypt(
            token.encode("ascii"), ttl=int(REMEMBERED_EMAIL_LIFETIME.total_seconds())
        ).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        return ""
    return email if 3 <= len(email) <= 254 and "@" in email else ""


def _apply_remembered_email(response, email: str, remember: bool):
    response = make_response(response)
    if remember:
        encrypted = _remembered_email_fernet().encrypt(email.encode("utf-8")).decode("ascii")
        response.set_cookie(
            REMEMBERED_EMAIL_COOKIE,
            encrypted,
            max_age=int(REMEMBERED_EMAIL_LIFETIME.total_seconds()),
            httponly=True,
            secure=current_app.config["SESSION_COOKIE_SECURE"],
            samesite="Lax",
            path="/login",
        )
    else:
        response.delete_cookie(REMEMBERED_EMAIL_COOKIE, path="/login")
    return response


def _start_preauth(user: User, next_url: str):
    session.clear()
    session["preauth_user_id"] = user.id
    session["preauth_auth_version"] = user.auth_version
    session["preauth_expires_at"] = int(time.time() + PREAUTH_LIFETIME.total_seconds())
    session["preauth_next"] = _safe_next(next_url)
    session["preauth_attempts"] = 0


def _clear_preauth():
    for key in (
        "preauth_user_id", "preauth_auth_version", "preauth_expires_at",
        "preauth_next", "preauth_attempts", "preauth_remember_device",
        "mfa_enrollment_verified",
    ):
        session.pop(key, None)


def _preauth_user() -> User | None:
    user_id = session.get("preauth_user_id")
    expires = session.get("preauth_expires_at", 0)
    if not user_id:
        return None
    if time.time() > expires:
        _clear_preauth()
        return None
    user = db.session.get(User, user_id)
    if user is None or not user.is_active or \
            session.get("preauth_auth_version") != user.auth_version:
        _clear_preauth()
        return None
    return user


def _increment_preauth_failure(user: User, action: str):
    session["preauth_attempts"] = int(session.get("preauth_attempts", 0)) + 1
    record_auth(action, user)
    db.session.commit()
    if session["preauth_attempts"] >= MFA_ATTEMPT_LIMIT:
        session.clear()
        return True
    return False


def _prepare_mfa_enrollment(user: User):
    if not user.mfa_secret_encrypted:
        user.mfa_secret_encrypted = _encrypt_secret(pyotp.random_base32())
        db.session.commit()


def _totp_counter(user: User, submitted: str) -> int | None:
    code = "".join(ch for ch in submitted if ch.isdigit())
    if len(code) != 6:
        return None
    try:
        totp = pyotp.TOTP(_decrypt_secret(user))
    except (InvalidToken, ValueError):
        current_app.logger.error("MFA secret for user %s cannot be decrypted", user.id)
        return None
    current_counter = int(time.time()) // totp.interval
    for counter in range(current_counter - 1, current_counter + 2):
        if user.mfa_last_totp_counter is not None and counter <= user.mfa_last_totp_counter:
            continue
        if pyotp.utils.strings_equal(totp.at(counter * totp.interval), code):
            return counter
    return None


def _provisioning_uri(user: User, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="CareMin")


def _qr_data_uri(uri: str) -> str:
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _new_recovery_codes(user: User) -> list[str]:
    MfaRecoveryCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    plaintext = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(10).upper()
        code = "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))
        normalized = code.replace("-", "")
        db.session.add(MfaRecoveryCode(
            user_id=user.id,
            code_prefix=normalized[:8],
            code_hash=generate_password_hash(normalized),
        ))
        plaintext.append(code)
    return plaintext


def _use_recovery_code(user: User, submitted: str) -> bool:
    normalized = "".join(ch for ch in submitted.upper() if ch.isalnum())
    if len(normalized) != 20:
        return False
    candidates = MfaRecoveryCode.query.filter_by(
        user_id=user.id, code_prefix=normalized[:8], used_at=None
    ).all()
    for row in candidates:
        if check_password_hash(row.code_hash, normalized):
            row.used_at = datetime.utcnow()
            return True
    return False


def revoke_trusted_devices(user: User):
    TrustedDevice.query.filter_by(user_id=user.id, revoked_at=None).update(
        {"revoked_at": datetime.utcnow()}, synchronize_session=False
    )


def reset_mfa(user: User):
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_enrolled_at = None
    user.mfa_last_totp_counter = None
    user.auth_version += 1
    MfaRecoveryCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    revoke_trusted_devices(user)


def _trusted_device(user: User) -> TrustedDevice | None:
    value = request.cookies.get(TRUSTED_DEVICE_COOKIE, "")
    if value.count(".") != 1:
        return None
    selector, validator = value.split(".", 1)
    device = TrustedDevice.query.filter_by(selector=selector, user_id=user.id).first()
    now = datetime.utcnow()
    if device is None or device.revoked_at or device.expires_at <= now or \
            device.auth_version != user.auth_version:
        return None
    digest = hashlib.sha256(validator.encode("ascii", "ignore")).hexdigest()
    if not hmac.compare_digest(device.token_hash, digest):
        return None
    device.last_used_at = now
    return device


def _issue_trusted_device(response, user: User):
    selector = secrets.token_urlsafe(18)
    validator = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db.session.add(TrustedDevice(
        user_id=user.id,
        selector=selector,
        token_hash=hashlib.sha256(validator.encode("ascii")).hexdigest(),
        auth_version=user.auth_version,
        created_at=now,
        last_used_at=now,
        expires_at=now + TRUSTED_DEVICE_LIFETIME,
    ))
    response.set_cookie(
        TRUSTED_DEVICE_COOKIE,
        f"{selector}.{validator}",
        max_age=int(TRUSTED_DEVICE_LIFETIME.total_seconds()),
        httponly=True,
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        samesite="Lax",
        path="/",
    )


def _rate_key(kind: str, value: str) -> str:
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _rate_buckets(email: str) -> tuple[tuple[str, int], tuple[str, int]]:
    ip = client_ip()
    return (
        (_rate_key("pair", f"{email}|{ip}"), LOGIN_PAIR_LIMIT),
        (_rate_key("ip", ip), LOGIN_IP_LIMIT),
    )


def _blocked(buckets: tuple[tuple[str, int], ...]) -> bool:
    now = datetime.utcnow()
    for key, _ in buckets:
        bucket = db.session.get(AuthRateLimit, key)
        if bucket and bucket.blocked_until and bucket.blocked_until > now:
            return True
    return False


def _register_login_failure(buckets: tuple[tuple[str, int], ...]):
    now = datetime.utcnow()
    for key, limit in buckets:
        bucket = db.session.get(AuthRateLimit, key)
        if bucket is None:
            bucket = AuthRateLimit(key_hash=key, failures=0, window_started_at=now)
            db.session.add(bucket)
        elif now - bucket.window_started_at >= LOGIN_WINDOW:
            bucket.failures = 0
            bucket.window_started_at = now
            bucket.blocked_until = None
        bucket.failures += 1
        bucket.updated_at = now
        if bucket.failures >= limit:
            bucket.blocked_until = now + LOGIN_BLOCK


def _clear_pair_rate_limit(email: str):
    pair_key = _rate_buckets(email)[0][0]
    bucket = db.session.get(AuthRateLimit, pair_key)
    if bucket:
        db.session.delete(bucket)


def _finalize_login(user: User, method: str, remember_device: bool = False):
    destination = _safe_next(session.get("preauth_next"))
    session.clear()
    session["user_id"] = user.id
    session["auth_version"] = user.auth_version
    session.permanent = True
    user.last_login_at = datetime.utcnow()
    record_auth("login", user, f"second_step={method}")
    response = make_response(redirect(destination))
    if remember_device:
        _issue_trusted_device(response, user)
    db.session.commit()
    return response


# ------------------------------------------------------------------ routes

bp = Blueprint("auth", __name__)


@bp.route("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")


@bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user():
            return redirect(url_for("index"))
        remembered = _remembered_email()
        return render_template(
            "login.html", email=remembered, remember_email=bool(remembered)
        )

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    remember_email = request.form.get("remember_email") == "on"
    buckets = _rate_buckets(email)
    if _blocked(buckets):
        record_auth("login_throttled", detail=f"email={email}")
        db.session.commit()
        return render_template(
            "login.html",
            error="Too many sign-in attempts. Wait 15 minutes and try again.",
            email=email,
            remember_email=remember_email,
        ), 429

    user = User.query.filter(db.func.lower(User.email) == email).first()
    password_hash = user.password_hash if user else current_app.config["DUMMY_PASSWORD_HASH"]
    password_valid = check_password_hash(password_hash, password)
    if user is None or not user.is_active or not password_valid:
        _register_login_failure(buckets)
        db.session.add(AuditLog(
            action="login_failed", entity="user", entity_id=email,
            ip_address=client_ip(),
        ))
        db.session.commit()
        return render_template(
            "login.html", error="Email or password is incorrect.", email=email,
            remember_email=remember_email,
        ), 401

    _clear_pair_rate_limit(email)
    _start_preauth(user, request.args.get("next"))
    record_auth("password_verified", user)
    db.session.commit()

    if user.must_change_password:
        flash("Please choose a new password.")
        return _apply_remembered_email(
            redirect(url_for("auth.change_password")), email, remember_email
        )
    if not user.mfa_enabled:
        _prepare_mfa_enrollment(user)
        return _apply_remembered_email(
            redirect(url_for("auth.mfa_setup")), email, remember_email
        )
    if _trusted_device(user):
        return _apply_remembered_email(
            _finalize_login(user, "trusted_device"), email, remember_email
        )
    return _apply_remembered_email(
        redirect(url_for("auth.mfa_challenge")), email, remember_email
    )


@bp.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user:
        device = _trusted_device(user)
        if device:
            device.revoked_at = datetime.utcnow()
        record("logout")
        db.session.commit()
    session.clear()
    response = make_response(redirect(url_for("auth.login")))
    response.delete_cookie(TRUSTED_DEVICE_COOKIE, path="/")
    return response


@bp.route("/account/password", methods=["GET", "POST"])
def change_password():
    authenticated = current_user()
    preauth = _preauth_user() if authenticated is None else None
    user = authenticated or preauth
    forced = bool(preauth and preauth.must_change_password and authenticated is None)
    if user is None or (authenticated is None and not forced):
        return redirect(url_for("auth.login"))
    if request.method == "GET":
        return render_template("change_password.html", forced=forced)

    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    if not forced and not check_password_hash(user.password_hash, current):
        return render_template(
            "change_password.html", error="Current password is incorrect.", forced=forced
        ), 400
    problem = password_problem(new, confirm)
    if problem:
        return render_template("change_password.html", error=problem, forced=forced), 400

    user.password_hash = generate_password_hash(new)
    user.must_change_password = False
    user.auth_version += 1
    revoke_trusted_devices(user)
    record_auth("password_changed", user)
    db.session.commit()

    if forced:
        session["preauth_auth_version"] = user.auth_version
        if not user.mfa_enabled:
            _prepare_mfa_enrollment(user)
            flash("Password updated. Set up two-step verification to continue.")
            return redirect(url_for("auth.mfa_setup"))
        flash("Password updated. Enter your verification code to continue.")
        return redirect(url_for("auth.mfa_challenge"))

    session.clear()
    response = make_response(redirect(url_for("auth.login")))
    response.delete_cookie(TRUSTED_DEVICE_COOKIE, path="/")
    flash("Password updated. Sign in again on each device.")
    return response


@bp.route("/mfa/setup", methods=["GET", "POST"])
def mfa_setup():
    user = _preauth_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if user.mfa_enabled:
        return redirect(url_for("auth.mfa_challenge"))
    try:
        secret = _decrypt_secret(user)
    except (InvalidToken, ValueError):
        abort(503)
    provisioning_uri = _provisioning_uri(user, secret)

    error = None
    if request.method == "POST":
        counter = _totp_counter(user, request.form.get("code") or "")
        if counter is None:
            exhausted = _increment_preauth_failure(user, "mfa_enrollment_failed")
            if exhausted:
                flash("Too many incorrect codes. Sign in and start again.")
                return redirect(url_for("auth.login"))
            error = "That verification code is incorrect. Check the time on your device and try again."
        else:
            user.mfa_enabled = True
            user.mfa_enrolled_at = datetime.utcnow()
            user.mfa_last_totp_counter = counter
            codes = _new_recovery_codes(user)
            session["mfa_enrollment_verified"] = True
            session["preauth_remember_device"] = request.form.get("remember_device") == "on"
            record_auth("mfa_enrolled", user)
            db.session.commit()
            return render_template("mfa_recovery_codes.html", recovery_codes=codes)

    return render_template(
        "mfa_setup.html",
        error=error,
        secret=secret,
        authenticator_uri=provisioning_uri,
        qr_data_uri=_qr_data_uri(provisioning_uri),
    ), (400 if error else 200)


@bp.route("/mfa/complete", methods=["POST"])
def mfa_complete():
    user = _preauth_user()
    if user is None or not user.mfa_enabled or not session.get("mfa_enrollment_verified"):
        return redirect(url_for("auth.login"))
    return _finalize_login(
        user, "totp_enrollment", bool(session.get("preauth_remember_device"))
    )


@bp.route("/mfa/challenge", methods=["GET", "POST"])
def mfa_challenge():
    user = _preauth_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if not user.mfa_enabled:
        return redirect(url_for("auth.mfa_setup"))
    error = None
    if request.method == "POST":
        counter = _totp_counter(user, request.form.get("code") or "")
        if counter is None:
            exhausted = _increment_preauth_failure(user, "mfa_challenge_failed")
            if exhausted:
                flash("Too many incorrect codes. Sign in and try again.")
                return redirect(url_for("auth.login"))
            error = "That verification code is incorrect or has already been used."
        else:
            user.mfa_last_totp_counter = counter
            return _finalize_login(
                user, "totp", request.form.get("remember_device") == "on"
            )
    return render_template("mfa_challenge.html", error=error), (400 if error else 200)


@bp.route("/mfa/recovery", methods=["GET", "POST"])
def mfa_recovery():
    user = _preauth_user()
    if user is None or not user.mfa_enabled:
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        if not _use_recovery_code(user, request.form.get("recovery_code") or ""):
            exhausted = _increment_preauth_failure(user, "mfa_recovery_failed")
            if exhausted:
                flash("Too many incorrect codes. Sign in and try again.")
                return redirect(url_for("auth.login"))
            error = "That recovery code is incorrect or has already been used."
        else:
            record_auth("mfa_recovery_code_used", user)
            return _finalize_login(
                user, "recovery_code", request.form.get("remember_device") == "on"
            )
    return render_template("mfa_recovery.html", error=error), (400 if error else 200)


@bp.route("/account/security")
def account_security():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    return render_template(
        "account_security.html",
        recovery_codes_remaining=MfaRecoveryCode.query.filter_by(
            user_id=user.id, used_at=None
        ).count(),
        trusted_devices=TrustedDevice.query.filter_by(
            user_id=user.id, revoked_at=None
        ).filter(TrustedDevice.expires_at > datetime.utcnow()).order_by(
            TrustedDevice.last_used_at.desc()
        ).all(),
    )


@bp.route("/account/security/recovery-codes", methods=["POST"])
def regenerate_recovery_codes():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    password = request.form.get("current_password") or ""
    counter = _totp_counter(user, request.form.get("code") or "")
    if not check_password_hash(user.password_hash, password) or counter is None:
        return render_template(
            "account_security.html",
            error="Your password or verification code is incorrect.",
            recovery_codes_remaining=MfaRecoveryCode.query.filter_by(
                user_id=user.id, used_at=None
            ).count(),
            trusted_devices=[],
        ), 400
    user.mfa_last_totp_counter = counter
    codes = _new_recovery_codes(user)
    record("mfa_recovery_codes_regenerated", "user", user.id)
    db.session.commit()
    return render_template(
        "mfa_recovery_codes.html", recovery_codes=codes, account_action=True
    )


@bp.route("/account/security/trusted-devices/revoke", methods=["POST"])
def revoke_devices():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    revoke_trusted_devices(user)
    record("trusted_devices_revoked", "user", user.id)
    db.session.commit()
    response = make_response(redirect(url_for("auth.account_security")))
    response.delete_cookie(TRUSTED_DEVICE_COOKIE, path="/")
    flash("Trusted devices revoked.")
    return response


@bp.route("/account/security/reset-mfa", methods=["POST"])
def reset_own_mfa():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    password = request.form.get("current_password") or ""
    counter = _totp_counter(user, request.form.get("code") or "")
    if not check_password_hash(user.password_hash, password) or counter is None:
        flash("Your password or verification code is incorrect.")
        return redirect(url_for("auth.account_security"))
    reset_mfa(user)
    record_auth("mfa_reset_by_user", user)
    db.session.commit()
    session.clear()
    response = make_response(redirect(url_for("auth.login")))
    response.delete_cookie(TRUSTED_DEVICE_COOKIE, path="/")
    flash("Two-step verification was reset. Sign in to set it up again.")
    return response


def password_problem(new: str, confirm: str) -> str | None:
    if len(new) < 12:
        return "Password must be at least 12 characters."
    if new != confirm:
        return "The two passwords do not match."
    return None


# ------------------------------------------------------- app wiring helpers


def init_app(app):
    """Attach the login gate and expose auth helpers to templates."""
    app.config.setdefault(
        "DUMMY_PASSWORD_HASH", generate_password_hash(secrets.token_urlsafe(32))
    )
    csrf.init_app(app)
    app.register_blueprint(bp)

    @app.errorhandler(CSRFError)
    def _csrf_error(error):
        if request.accept_mimetypes.best == "application/json":
            return {"error": "invalid or expired request token"}, 400
        return render_template(
            "csrf_error.html",
            message="This form expired or came from another site. Go back, reload the page and try again.",
        ), 400

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
        )
        return response

    @app.before_request
    def _require_login():
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("debug_") or \
                request.path == url_for("auth.logout"):
            return None
        user = current_user()
        if user is None:
            if request.accept_mimetypes.best == "application/json" or endpoint.endswith("_json"):
                return {"error": "authentication required"}, 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if user.is_superuser and not session.get("acting_org_id"):
            platform_endpoint = endpoint.startswith("platform_") or endpoint in {
                "auth.change_password", "auth.account_security",
                "auth.regenerate_recovery_codes", "auth.revoke_devices",
                "auth.reset_own_mfa", "auth.logout", "healthz", "stylesheet", "brand_asset",
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
            "connection_encrypted": bool(
                request.is_secure or current_app.config.get("SESSION_COOKIE_SECURE")
            ),
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
