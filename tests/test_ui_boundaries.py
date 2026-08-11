import importlib
from datetime import date, time
from pathlib import Path

import pytest

from carelog import auth
from carelog.models import Facility, Organization, ResidentDay, Shift, Staff, db


@pytest.fixture()
def web_app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("AUTO_INIT_DB", "1")
    monkeypatch.setenv("SECRET_KEY", "ui-boundary-test")
    module = importlib.import_module("carelog.app")
    app = module.create_app()
    app.config["TESTING"] = True
    # This legacy boundary suite posts directly. Dedicated authentication tests
    # exercise the production CSRF middleware with real form tokens.
    app.config["WTF_CSRF_ENABLED"] = False
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
        session["auth_version"] = 1


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


def test_recovery_codes_use_a_non_clipping_layout():
    stylesheet = (Path(__file__).parents[1] / "public" / "style.css").read_text()
    assert "grid-template-columns: minmax(0, 1fr);" in stylesheet
    assert "overflow-wrap: anywhere;" in stylesheet


def test_login_rejects_external_redirects(web_app):
    app, _ = web_app
    client = app.test_client()
    response = client.post(
        "/login?next=https://example.net/steal",
        data={"email": "admin@example.com", "password": "Password123!"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/mfa/setup")
    with client.session_transaction() as session:
        assert session["preauth_next"] == "/"


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


def test_import_classifies_evidence_without_asking_the_user(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    page = client.get("/import")
    assert page.status_code == 200
    assert b'name="evidence_type"' not in page.data
    assert b"CareMin classifies shift evidence automatically" in page.data
    assert b"worked_staffing_july_2026.xlsx" in page.data

    sample = client.get("/samples/worked_staffing_july_2026.xlsx")
    assert sample.status_code == 200
    assert sample.content_type.startswith("application/")


def test_redesigned_provider_workspace_routes_render_with_sparse_data(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    for path in (
        "/dashboard", "/compliance", "/rn-coverage", "/scenarios",
        "/reports", "/audit", "/eligibility", "/facility",
        "/facilities", "/integrations", "/admin/users", "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200, path

    dashboard = client.get("/dashboard")
    assert b"Attention required" in dashboard.data
    assert b"Care minutes" in dashboard.data
    assert b"RN coverage" in dashboard.data
    assert b"Reports" in dashboard.data
    assert b"RN coverage has no actual-worked source" in dashboard.data

    compliance = client.get("/compliance")
    assert b"No occupied bed days are available" in compliance.data

    coverage = client.get("/rn-coverage")
    assert b"No actual-worked RN shift evidence is available" in coverage.data
    assert b"Not available" in coverage.data
    assert b'name="month"' in coverage.data
    assert client.get("/rn-coverage?month=not-a-month").status_code == 200

    reports = client.get("/reports")
    assert b"Report register" in reports.data
    assert b"Output qualification" in reports.data

    facilities = client.get("/facilities")
    assert b"On track" not in facilities.data


def test_redesigned_reporting_routes_render_calculated_data(web_app):
    app, ids = web_app
    with app.app_context():
        facility = Facility.query.filter_by(organization_id=ids["customer"]).one()
        rn = Staff(
            facility_id=facility.id, staff_id="RN-1", name="Registered Nurse",
            role="RN", source_role="Registered Nurse", eligibility_status="approved",
            registration_number="AHPRA-RN-1", registration_expiry=date(2027, 8, 1),
        )
        pcw = Staff(
            facility_id=facility.id, staff_id="PCW-1", name="Care Worker",
            role="PCW", source_role="Personal Care Worker",
            eligibility_status="approved",
        )
        db.session.add_all([rn, pcw])
        db.session.flush()
        for day in (date(2026, 8, 1), date(2026, 8, 2)):
            db.session.add(ResidentDay(
                facility_id=facility.id, resident_id=f"R-{day}",
                resident_name="Resident", date=day, occupied=True,
                service_type="permanent",
            ))
            for start, end in ((time(0), time(12)), (time(12), time(0))):
                db.session.add(Shift(
                    facility_id=facility.id, staff_id=rn.id, date=day,
                    start_time=start, end_time=end, break_minutes=0,
                    is_direct_care=True, evidence_type="worked",
                ))
            db.session.add(Shift(
                facility_id=facility.id, staff_id=pcw.id, date=day,
                start_time=time(8), end_time=time(16), break_minutes=0,
                is_direct_care=True, evidence_type="worked",
            ))
        db.session.commit()

    client = app.test_client()
    sign_in_as(client, ids["admin"])
    for path in ("/dashboard", "/compliance", "/rn-coverage?month=2026-08", "/facilities"):
        response = client.get(path)
        assert response.status_code == 200, path

    assert b"All defined tests pass" in client.get("/compliance").data
    assert b"100.0%" in client.get("/rn-coverage?month=2026-08").data
    assert b"On track" in client.get("/facilities").data
