import importlib
from datetime import date, time

import pytest

from carelog import auth
from carelog.models import Facility, Organization, ResidentDay, Shift, Staff, db


@pytest.fixture()
def web_app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("AUTO_INIT_DB", "1")
    monkeypatch.setenv("SECRET_KEY", "ui-boundary-test")
    module = importlib.import_module("carelog.app")
    app = module.create_app({"TESTING": True})
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


def test_api_errors_and_login_are_json(web_app):
    app, ids = web_app
    client = app.test_client()
    page = client.get("/api/login")
    assert page.json["view"] == "login"
    failed = client.post("/api/login", data={"email": "admin@example.com", "password": "wrong"})
    assert failed.status_code == 401
    assert failed.json["data"]["email"] == "admin@example.com"
    sign_in_as(client, ids["admin"])
    missing = client.get("/api/not-a-page")
    assert missing.status_code == 404
    assert missing.is_json


def test_login_rejects_external_redirects(web_app):
    app, _ = web_app
    client = app.test_client()
    response = client.post(
        "/api/login?next=https://example.net/steal",
        data={"email": "admin@example.com", "password": "Password123!"},
    )
    assert response.status_code == 200
    assert response.json["navigate"].endswith("/mfa/setup")
    with client.session_transaction() as session:
        assert session["preauth_next"] == "/api/"


def test_platform_owner_defaults_to_platform_only_ui(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["owner"])

    for path in ("/api/", "/api/dashboard", "/api/compliance", "/api/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json["navigate"].endswith("/platform")

    for path in (
        "/api/platform", f"/api/platform/organizations/{ids['customer']}",
        "/api/platform/activity", "/api/platform/system",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Dashboard" not in response.data
        assert b"Compliance" not in response.data


def test_support_workspace_requires_explicit_account_entry(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["owner"])

    entered = client.post("/api/platform/act", data={"organization_id": ids["customer"]})
    assert entered.status_code == 200
    workspace = client.get("/api/import")
    assert workspace.status_code == 200
    assert workspace.json["context"]["acting_org_id"] == ids["customer"]

    client.post("/api/platform/act/stop")
    assert client.get("/api/import").json["navigate"].endswith("/platform")


def test_customer_ui_hides_platform_internals_and_cross_tenant_controls(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    forbidden_copy = (
        b"anthropic", b"claude", b"ai connection", b"learned parser",
        b"mapping spec", b"fingerprint",
    )
    for path in ("/api/settings", "/api/import", "/api/audit"):
        response = client.get(path)
        assert response.status_code == 200
        for phrase in forbidden_copy:
            assert phrase not in response.data.lower()

    assert client.get("/api/platform").status_code == 404
    assert client.post(
        f"/api/platform/organizations/{ids['other']}/update",
        data={"name": "Changed by customer", "timezone": "Australia/Sydney"},
    ).status_code == 404
    assert client.get("/api/admin/security-log").status_code == 404
    assert client.post(
        f"/api/admin/users/{ids['owner']}/update", data={"action": "deactivate"}
    ).status_code == 404
    assert client.post(
        f"/api/admin/users/{ids['outsider']}/update", data={"action": "deactivate"}
    ).status_code == 404
    assert client.post(
        f"/api/facilities/{ids['other_facility']}/update", data={"name": "Changed"}
    ).status_code == 404

    users_page = client.get("/api/admin/users")
    assert users_page.status_code == 200
    assert b"owner@example.com" not in users_page.data


def test_import_classifies_evidence_without_asking_the_user(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    page = client.get("/api/import")
    assert page.status_code == 200
    assert page.json["view"] == "universal_import"
    assert "evidence_type" not in page.json["data"]
    assert client.get("/api/import/guide").json["view"] == "import_guide"

    sample = client.get("/api/samples/worked_staffing_july_2026.xlsx")
    assert sample.status_code == 200
    assert sample.content_type.startswith("application/")


def test_redesigned_provider_workspace_routes_render_with_sparse_data(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    for path in (
        "/api/dashboard", "/api/compliance", "/api/rn-coverage", "/api/scenarios",
        "/api/reports", "/api/audit", "/api/eligibility", "/api/facility",
        "/api/facilities", "/api/integrations", "/api/admin/users", "/api/settings",
        "/api/import/guide",
    ):
        response = client.get(path)
        assert response.status_code == 200, path

    dashboard = client.get("/api/dashboard").json["data"]
    assert dashboard["coverage_available"] is False
    assert dashboard["tests"]["totals"]["bed_days"] == 0
    coverage = client.get("/api/rn-coverage").json["data"]
    assert coverage["available_days"] == 0
    assert coverage["period_pct"] is None
    assert client.get("/api/rn-coverage?month=not-a-month").status_code == 200
    assert client.get("/api/facilities").json["data"]["summary"]


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
    for path in ("/api/dashboard", "/api/compliance", "/api/rn-coverage?month=2026-08", "/api/facilities"):
        response = client.get(path)
        assert response.status_code == 200, path

    assert client.get("/api/dashboard").json["data"]["tests"]["passed"] is True
    assert client.get("/api/rn-coverage?month=2026-08").json["data"]["available_days"] == 2
    summaries = client.get("/api/facilities").json["data"]["summary"]
    assert next(iter(summaries.values()))["period_status"] is True


@pytest.mark.parametrize("invalid", ["", "nan", "inf", "-1", "0", "not a number"])
def test_invalid_targets_never_change_facility_state(web_app, invalid):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    with app.app_context():
        home_id = Facility.query.filter_by(organization_id=ids["customer"]).one().id
    for route in ("/api/settings", "/api/facility/targets", f"/api/facilities/{home_id}/update"):
        response = client.post(route, data={
            "facility_name": "Changed", "name": "Changed", "ancc_target": "220", "rn_target": invalid,
        })
        assert response.status_code == 400
        with app.app_context():
            home = db.session.get(Facility, home_id)
            assert (home.name, home.ancc_target, home.rn_target) == ("Sunrise Home", 215, 44)
    response = client.post("/api/facilities/create", data={"name": "Invalid Home", "ancc_target": "220", "rn_target": invalid})
    assert response.status_code == 400
    with app.app_context():
        assert Facility.query.filter_by(name="Invalid Home").count() == 0


def test_targets_require_explicit_values_and_record_valid_updates(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post("/api/facilities/create", data={"name": "No targets"}).status_code == 400
    assert client.post("/api/facility/targets", data={"ancc_target": "30", "rn_target": "44"}).status_code == 400
    assert client.post("/api/facility/targets", data={"ancc_target": "220.5", "rn_target": "45"}).status_code == 200
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        assert (home.ancc_target, home.rn_target) == (220.5, 45)
