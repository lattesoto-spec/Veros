import re

import pyotp
import pytest
from cryptography.fernet import Fernet

from carelog import auth
from carelog.models import (
    AuthRateLimit,
    MfaRecoveryCode,
    Organization,
    TrustedDevice,
    User,
    db,
)


@pytest.fixture()
def auth_app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("AUTO_INIT_DB", "1")
    monkeypatch.setenv("SECRET_KEY", "authentication-test-secret")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    from carelog.app import create_app

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    with app.app_context():
        db.create_all()
        org = Organization(name="Test Provider")
        other_org = Organization(name="Other Provider")
        db.session.add_all([org, other_org])
        db.session.flush()
        user = auth.create_user(
            organization_id=org.id,
            email="user@example.com",
            name="Test User",
            role="administrator",
            password="Password123!",
            must_change_password=False,
        )
        forced = auth.create_user(
            organization_id=org.id,
            email="new@example.com",
            name="New User",
            role="read_only",
            password="Temporary123!",
            must_change_password=True,
        )
        outsider = auth.create_user(
            organization_id=other_org.id,
            email="outside@example.com",
            name="Outside User",
            role="administrator",
            password="Password123!",
            must_change_password=False,
        )
        db.session.commit()
        ids = {"user": user.id, "forced": forced.id, "outsider": outsider.id}
    yield app, ids
    with app.app_context():
        db.session.remove()
        db.drop_all()


def csrf_token(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data[:500]
    return match.group(1).decode("utf-8")


def password_step(
    client, email="user@example.com", password="Password123!", next_path=None,
    remember_email=False,
):
    page = client.get("/login")
    path = "/login" + (f"?next={next_path}" if next_path else "")
    data = {"csrf_token": csrf_token(page), "email": email, "password": password}
    if remember_email:
        data["remember_email"] = "on"
    return client.post(
        path,
        data=data,
    )


def enable_mfa(app, user_id, secret=None):
    secret = secret or pyotp.random_base32()
    with app.app_context():
        user = db.session.get(User, user_id)
        user.mfa_secret_encrypted = auth._encrypt_secret(secret)
        user.mfa_enabled = True
        user.mfa_enrolled_at = auth.datetime.utcnow()
        user.mfa_last_totp_counter = None
        db.session.commit()
    return secret


def test_mandatory_enrollment_stages_session_and_hashes_recovery_codes(auth_app):
    app, ids = auth_app
    client = app.test_client()

    response = password_step(client, next_path="/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/mfa/setup")
    with client.session_transaction() as state:
        assert "user_id" not in state
        assert state["preauth_user_id"] == ids["user"]
        assert state["preauth_next"] == "/dashboard"

    setup = client.get("/mfa/setup")
    assert setup.status_code == 200
    assert b'href="otpauth://totp/CareMin:' in setup.data
    assert b"play.google.com/store/apps/details?id=com.google.android.apps.authenticator2" in setup.data
    assert b"apps.apple.com/au/app/google-authenticator/id388497605" in setup.data
    assert b'referrerpolicy="no-referrer"' in setup.data
    with app.app_context():
        user = db.session.get(User, ids["user"])
        secret = auth._decrypt_secret(user)
        assert secret not in user.mfa_secret_encrypted

    enrolled = client.post(
        "/mfa/setup",
        data={
            "csrf_token": csrf_token(setup),
            "code": pyotp.TOTP(secret).now(),
            "remember_device": "on",
        },
    )
    assert enrolled.status_code == 200, enrolled.data.decode("utf-8")
    codes = re.findall(rb"[0-9A-F]{5}(?:-[0-9A-F]{5}){3}", enrolled.data)
    assert len(codes) == 10
    with app.app_context():
        rows = MfaRecoveryCode.query.filter_by(user_id=ids["user"]).all()
        assert len(rows) == 10
        assert all(codes[0].decode().replace("-", "") not in row.code_hash for row in rows)

    completed = client.post(
        "/mfa/complete", data={"csrf_token": csrf_token(enrolled)}
    )
    assert completed.status_code == 302
    assert completed.headers["Location"] == "/dashboard"
    assert auth.TRUSTED_DEVICE_COOKIE in completed.headers.get("Set-Cookie", "")
    with client.session_transaction() as state:
        assert state["user_id"] == ids["user"]
        assert "preauth_user_id" not in state


def test_forced_password_change_never_grants_workspace_access(auth_app):
    app, ids = auth_app
    client = app.test_client()
    response = password_step(client, "new@example.com", "Temporary123!")
    assert response.headers["Location"].endswith("/account/password")
    assert client.get("/dashboard").headers["Location"].startswith("/login")

    page = client.get("/account/password")
    changed = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_token(page),
            "new_password": "A-New-Password-123!",
            "confirm_password": "A-New-Password-123!",
        },
    )
    assert changed.headers["Location"].endswith("/mfa/setup")
    with client.session_transaction() as state:
        assert "user_id" not in state
        assert state["preauth_auth_version"] == 2
    with app.app_context():
        user = db.session.get(User, ids["forced"])
        assert not user.must_change_password
        assert user.auth_version == 2


def test_totp_login_can_trust_device_but_still_requires_password(auth_app):
    app, ids = auth_app
    secret = enable_mfa(app, ids["user"])
    client = app.test_client()

    assert password_step(client).headers["Location"].endswith("/mfa/challenge")
    challenge = client.get("/mfa/challenge")
    verified = client.post(
        "/mfa/challenge",
        data={
            "csrf_token": csrf_token(challenge),
            "code": pyotp.TOTP(secret).now(),
            "remember_device": "on",
        },
    )
    assert verified.headers["Location"] == "/"
    with app.app_context():
        assert TrustedDevice.query.filter_by(user_id=ids["user"], revoked_at=None).count() == 1

    # Simulate expiry of the 12-hour Flask session while retaining the separate
    # trusted-device cookie. Password is still required; only TOTP is skipped.
    with client.session_transaction() as state:
        state.clear()
    trusted_login = password_step(client)
    assert trusted_login.headers["Location"] == "/"
    with client.session_transaction() as state:
        assert state["user_id"] == ids["user"]


def test_recovery_code_is_single_use(auth_app):
    app, ids = auth_app
    enable_mfa(app, ids["user"])
    with app.app_context():
        user = db.session.get(User, ids["user"])
        recovery_code = auth._new_recovery_codes(user)[0]
        db.session.commit()

    client = app.test_client()
    password_step(client)
    page = client.get("/mfa/recovery")
    accepted = client.post(
        "/mfa/recovery",
        data={"csrf_token": csrf_token(page), "recovery_code": recovery_code},
    )
    assert accepted.headers["Location"] == "/"

    with client.session_transaction() as state:
        state.clear()
    password_step(client)
    page = client.get("/mfa/recovery")
    rejected = client.post(
        "/mfa/recovery",
        data={"csrf_token": csrf_token(page), "recovery_code": recovery_code},
    )
    assert rejected.status_code == 400
    assert b"already been used" in rejected.data


def test_csrf_logout_method_and_legacy_session_are_rejected(auth_app):
    app, ids = auth_app
    client = app.test_client()
    assert client.post(
        "/login", data={"email": "user@example.com", "password": "Password123!"}
    ).status_code == 400
    assert client.get("/logout").status_code == 405

    with client.session_transaction() as state:
        state.clear()
        state["user_id"] = ids["user"]
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")

    login_page = client.get("/login")
    assert login_page.headers["X-Content-Type-Options"] == "nosniff"
    assert login_page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in login_page.headers["Content-Security-Policy"]

    with client.session_transaction() as state:
        state.clear()
        state["user_id"] = ids["user"]
        state["auth_version"] = 1
    client.get("/mfa/challenge")
    with client.session_transaction() as state:
        assert state["user_id"] == ids["user"]


def test_password_failures_are_database_throttled(auth_app):
    app, _ = auth_app
    client = app.test_client()
    page = client.get("/login")
    token = csrf_token(page)
    for _ in range(5):
        response = client.post(
            "/login",
            data={"csrf_token": token, "email": "user@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/login",
        data={"csrf_token": token, "email": "user@example.com", "password": "wrong"},
    )
    assert blocked.status_code == 429
    with app.app_context():
        assert AuthRateLimit.query.filter(AuthRateLimit.blocked_until.isnot(None)).count() >= 1


def test_admin_mfa_reset_is_tenant_scoped_and_revokes_target_sessions(auth_app):
    app, ids = auth_app
    enable_mfa(app, ids["forced"])
    enable_mfa(app, ids["outsider"])
    client = app.test_client()
    with client.session_transaction() as state:
        state["user_id"] = ids["user"]
        state["auth_version"] = 1

    page = client.get("/admin/users")
    token = csrf_token(page)
    reset = client.post(
        f"/admin/users/{ids['forced']}/update",
        data={"csrf_token": token, "action": "reset_mfa"},
    )
    assert reset.status_code == 302
    with app.app_context():
        target = db.session.get(User, ids["forced"])
        assert not target.mfa_enabled
        assert target.mfa_secret_encrypted is None
        assert target.auth_version == 2

    foreign = client.post(
        f"/admin/users/{ids['outsider']}/update",
        data={"csrf_token": token, "action": "reset_mfa"},
    )
    assert foreign.status_code == 404


def test_remembered_email_is_encrypted_opt_in_and_removable(auth_app):
    app, _ = auth_app
    client = app.test_client()

    response = password_step(client, remember_email=True)
    cookie_header = response.headers.get("Set-Cookie", "")
    assert auth.REMEMBERED_EMAIL_COOKIE in cookie_header
    assert "user@example.com" not in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header

    with client.session_transaction() as state:
        state.clear()
    remembered = client.get("/login")
    assert b'value="user@example.com"' in remembered.data
    assert b'name="remember_email" checked' in remembered.data

    cleared = client.post(
        "/login",
        data={
            "csrf_token": csrf_token(remembered),
            "email": "user@example.com",
            "password": "Password123!",
        },
    )
    assert f"{auth.REMEMBERED_EMAIL_COOKIE}=;" in cleared.headers.get("Set-Cookie", "")
    with client.session_transaction() as state:
        state.clear()
    assert b'value="user@example.com"' not in client.get("/login").data


def test_forgot_password_privacy_and_https_copy_are_public(auth_app):
    app, _ = auth_app
    client = app.test_client()

    forgot = client.get("/forgot-password")
    assert forgot.status_code == 200
    assert b"CareMin does not send password-reset emails yet" in forgot.data
    assert b"organisation administrator" in forgot.data

    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert b"Privacy information" in privacy.data
    assert b"remembered-email cookie" in privacy.data

    secure_login = client.get("/login", base_url="https://caremin.test")
    assert b"Your connection is encrypted" in secure_login.data
    assert b"Review our privacy information" in secure_login.data


def test_admin_can_issue_confirmed_temporary_password(auth_app):
    app, ids = auth_app
    client = app.test_client()
    with client.session_transaction() as state:
        state["user_id"] = ids["user"]
        state["auth_version"] = 1

    page = client.get("/admin/users")
    response = client.post(
        f"/admin/users/{ids['forced']}/update",
        data={
            "csrf_token": csrf_token(page),
            "action": "reset_password",
            "password": "Replacement123!",
            "confirm_password": "Replacement123!",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        user = db.session.get(User, ids["forced"])
        assert auth.check_password_hash(user.password_hash, "Replacement123!")
        assert user.must_change_password
        assert user.auth_version == 2
