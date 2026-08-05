import importlib
from pathlib import Path

import pytest

from carelog import auth
from carelog.models import Facility, Organization, db


@pytest.fixture()
def web_app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("AUTO_INIT_DB", "1")
    monkeypatch.setenv("SECRET_KEY", "ui-boundary-test")
    module = importlib.import_module("carelog.app")
    app = module.create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        customer = Organization(name="Sunrise Group")
        other = Organization(name="Other Group")
        db.session.add_all([customer, other])
        db.session.flush()
        owner = auth.create_user(
            organization_id=customer.id, email="owner@example.com",
            name="Platform Owner", role="administrator", password="Password123!",
            is_superuser=True, must_change_password=False,
        )
        admin = auth.create_user(
            organization_id=customer.id, email="admin@example.com",
            name="Customer Admin", role="administrator", password="Password123!",
            must_change_password=False,
        )
        outsider = auth.create_user(
            organization_id=other.id, email="other@example.com",
            name="Other Admin", role="administrator", password="Password123!",
            must_change_password=False,
        )
        db.session.add(Facility(organization_id=customer.id, name="Sunrise Home"))
        other_facility = Facility(organization_id=other.id, name="Other Home")
        db.session.add(other_facility)
        db.session.commit()
        ids = {
            "owner": owner.id, "admin": admin.id, "outsider": outsider.id,
            "customer": customer.id, "other": other.id,
            "other_facility": other_facility.id,
        }
        yield app, ids
        db.session.remove()
        db.drop_all()


def sign_in_as(client, user_id):
    with client.session_transaction() as session:
        session.clear()
        session["user_id"] = user_id


def test_login_is_branded_accessible_and_keeps_context_on_error(web_app):
    app, _ = web_app
    client = app.test_client()

    page = client.get("/login")
    assert page.status_code == 200
    assert b"Care-minute reporting in one controlled workspace" in page.data
    assert b"Work email" in page.data
    assert b'aria-controls="login-password"' in page.data
    assert b">Sign in</button>" in page.data
    assert b"Sign in securely" not in page.data
    assert b'/brand/full.png' in page.data
    assert b'/brand/favicon.png' in page.data

    icon = client.get("/brand/favicon.png")
    assert icon.status_code == 200
    assert icon.content_type == "image/png"

    failed = client.post(
        "/login", data={"email": "admin@example.com", "password": "wrong"}
    )
    assert failed.status_code == 401
    assert b'value="admin@example.com"' in failed.data
    assert b'role="alert"' in failed.data


def test_customer_facing_templates_avoid_rejected_copy():
    templates = Path(__file__).parents[1] / "carelog" / "templates"
    copy = "\n".join(path.read_text() for path in templates.glob("*.html"))
    for rejected in (
        "—", "–", "…", "What if", "Every number has evidence",
        "Sign in securely", "Care-minute evidence you can stand behind",
    ):
        assert rejected not in copy


def test_login_rejects_external_redirects(web_app):
    app, _ = web_app
    client = app.test_client()
    response = client.post(
        "/login?next=https://example.net/steal",
        data={"email": "admin@example.com", "password": "Password123!"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_platform_owner_defaults_to_platform_only_ui(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["owner"])

    for path in ("/", "/dashboard", "/compliance", "/settings"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/platform")

    for path in (
        "/platform", f"/platform/organizations/{ids['customer']}",
        "/platform/activity", "/platform/system",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Dashboard" not in response.data
        assert b"Compliance" not in response.data


def test_support_workspace_requires_explicit_account_entry(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["owner"])

    entered = client.post("/platform/act", data={"organization_id": ids["customer"]})
    assert entered.status_code == 302
    workspace = client.get("/import")
    assert workspace.status_code == 200
    assert b"Back to platform" in workspace.data

    client.post("/platform/act/stop")
    assert client.get("/import").headers["Location"].endswith("/platform")


def test_customer_ui_hides_platform_internals_and_cross_tenant_controls(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    forbidden_copy = (
        b"anthropic", b"claude", b"ai connection", b"learned parser",
        b"mapping spec", b"fingerprint",
    )
    for path in ("/settings", "/import", "/audit"):
        response = client.get(path)
        assert response.status_code == 200
        for phrase in forbidden_copy:
            assert phrase not in response.data.lower()

    assert client.get("/platform").status_code == 404
    assert client.post(
        f"/platform/organizations/{ids['other']}/update",
        data={"name": "Changed by customer", "timezone": "Australia/Sydney"},
    ).status_code == 404
    assert client.get("/admin/security-log").status_code == 404
    assert client.post(
        f"/admin/users/{ids['owner']}/update", data={"action": "deactivate"}
    ).status_code == 404
    assert client.post(
        f"/admin/users/{ids['outsider']}/update", data={"action": "deactivate"}
    ).status_code == 404
    assert client.post(
        f"/facilities/{ids['other_facility']}/update", data={"name": "Changed"}
    ).status_code == 404

    users_page = client.get("/admin/users")
    assert users_page.status_code == 200
    assert b"owner@example.com" not in users_page.data
