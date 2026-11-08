import importlib
import time as time_module
from datetime import date, time

import pytest

from carelog import auth
from carelog.models import (
    Facility,
    FacilityTarget,
    Organization,
    ResidentDay,
    Shift,
    Staff,
    db,
)


@pytest.fixture()
def web_app(monkeypatch):
    # These tests execute jobs explicitly; do not leave background workers
    # running after a fixture's database and monkeypatches are torn down.
    from carelog.ingestion import jobs
    monkeypatch.setattr(jobs, "dispatch_job", lambda *args: None)
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
            organization_id=customer.id,
            email="owner@example.com",
            name="Platform Owner",
            role="administrator",
            password="Password123!",
            is_superuser=True,
            must_change_password=False,
        )
        admin = auth.create_user(
            organization_id=customer.id,
            email="admin@example.com",
            name="Customer Admin",
            role="administrator",
            password="Password123!",
            must_change_password=False,
        )
        outsider = auth.create_user(
            organization_id=other.id,
            email="other@example.com",
            name="Other Admin",
            role="administrator",
            password="Password123!",
            must_change_password=False,
        )
        db.session.add(Facility(organization_id=customer.id, name="Sunrise Home"))
        db.session.flush()
        for home in Facility.query.filter_by(organization_id=customer.id).all():
            for month in (1, 4, 7, 10):
                db.session.add(
                    FacilityTarget(
                        facility_id=home.id,
                        quarter_start=date(2026, month, 1),
                        ancc_target=215,
                        rn_target=44,
                        source="Synthetic provider notice",
                    )
                )
        other_facility = Facility(organization_id=other.id, name="Other Home")
        db.session.add(other_facility)
        db.session.commit()
        ids = {
            "owner": owner.id,
            "admin": admin.id,
            "outsider": outsider.id,
            "customer": customer.id,
            "other": other.id,
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
        session["last_activity_at"] = time_module.time()


def test_api_errors_and_login_are_json(web_app):
    app, ids = web_app
    client = app.test_client()
    page = client.get("/api/login")
    assert page.json["view"] == "login"
    failed = client.post(
        "/api/login", data={"email": "admin@example.com", "password": "wrong"}
    )
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
        "/api/platform",
        f"/api/platform/organizations/{ids['customer']}",
        "/api/platform/activity",
        "/api/platform/system",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Dashboard" not in response.data
        assert b"Compliance" not in response.data


def test_support_workspace_requires_explicit_account_entry(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["owner"])

    entered = client.post(
        "/api/platform/act", data={"organization_id": ids["customer"]}
    )
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
        b"anthropic",
        b"claude",
        b"ai connection",
        b"learned parser",
        b"mapping spec",
        b"fingerprint",
    )
    for path in ("/api/settings", "/api/import", "/api/audit"):
        response = client.get(path)
        assert response.status_code == 200
        for phrase in forbidden_copy:
            assert phrase not in response.data.lower()

    assert client.get("/api/platform").status_code == 404
    assert (
        client.post(
            f"/api/platform/organizations/{ids['other']}/update",
            data={"name": "Changed by customer", "timezone": "Australia/Sydney"},
        ).status_code
        == 404
    )
    assert client.get("/api/admin/security-log").status_code == 404
    assert (
        client.post(
            f"/api/admin/users/{ids['owner']}/update", data={"action": "deactivate"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/admin/users/{ids['outsider']}/update", data={"action": "deactivate"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/facilities/{ids['other_facility']}/update", data={"name": "Changed"}
        ).status_code
        == 404
    )

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
        "/api/dashboard",
        "/api/compliance",
        "/api/rn-coverage",
        "/api/scenarios",
        "/api/reports",
        "/api/audit",
        "/api/eligibility",
        "/api/facility",
        "/api/facilities",
        "/api/integrations",
        "/api/admin/users",
        "/api/settings",
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
    assert client.get("/api/rn-coverage?month=not-a-month").status_code == 400
    assert client.get("/api/facilities").json["data"]["summary"]


def test_redesigned_reporting_routes_render_calculated_data(web_app):
    app, ids = web_app
    with app.app_context():
        facility = Facility.query.filter_by(organization_id=ids["customer"]).one()
        rn = Staff(
            facility_id=facility.id,
            staff_id="RN-1",
            name="Registered Nurse",
            role="RN",
            source_role="Registered Nurse",
            eligibility_status="approved",
            registration_number="AHPRA-RN-1",
            registration_expiry=date(2027, 8, 1),
        )
        pcw = Staff(
            facility_id=facility.id,
            staff_id="PCW-1",
            name="Care Worker",
            role="PCW",
            source_role="Personal Care Worker",
            eligibility_status="approved",
        )
        db.session.add_all([rn, pcw])
        db.session.flush()
        for day in (date(2026, 8, 1), date(2026, 8, 2)):
            db.session.add(
                ResidentDay(
                    facility_id=facility.id,
                    resident_id=f"R-{day}",
                    resident_name="Resident",
                    date=day,
                    occupied=True,
                    service_type="permanent",
                )
            )
            for start, end in ((time(0), time(12)), (time(12), time(0))):
                db.session.add(
                    Shift(
                        facility_id=facility.id,
                        staff_id=rn.id,
                        date=day,
                        start_time=start,
                        end_time=end,
                        break_minutes=0,
                        is_direct_care=True,
                        evidence_type="worked",
                    )
                )
            db.session.add(
                Shift(
                    facility_id=facility.id,
                    staff_id=pcw.id,
                    date=day,
                    start_time=time(8),
                    end_time=time(16),
                    break_minutes=0,
                    is_direct_care=True,
                    evidence_type="worked",
                )
            )
        db.session.commit()

    client = app.test_client()
    sign_in_as(client, ids["admin"])
    for path in (
        "/api/dashboard",
        "/api/compliance",
        "/api/rn-coverage?month=2026-08",
        "/api/facilities",
    ):
        response = client.get(path)
        assert response.status_code == 200, path

    # Only two August dates are present in a July-to-date period. The result
    # must now remain provisional rather than imply a verified quarterly pass.
    tests = client.get("/api/dashboard").json["data"]["tests"]
    assert tests["passed"] is None
    assert tests["provisional"] is True
    assert (
        client.get("/api/rn-coverage?month=2026-08").json["data"]["available_days"] == 2
    )
    summaries = client.get("/api/facilities").json["data"]["summary"]
    assert next(iter(summaries.values()))["period_status"] is None


@pytest.mark.parametrize("invalid", ["", "nan", "inf", "-1", "0", "not a number"])
def test_invalid_targets_never_change_facility_state(web_app, invalid):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    with app.app_context():
        home_id = Facility.query.filter_by(organization_id=ids["customer"]).one().id
    for route in (
        "/api/settings",
        "/api/facility/targets",
        f"/api/facilities/{home_id}/update",
    ):
        response = client.post(
            route,
            data={
                "facility_name": "Changed",
                "name": "Changed",
                "ancc_target": "220",
                "rn_target": invalid,
            },
        )
        assert response.status_code == 400
        with app.app_context():
            home = db.session.get(Facility, home_id)
            assert (home.name, home.ancc_target, home.rn_target) == (
                "Sunrise Home",
                215,
                44,
            )
    response = client.post(
        "/api/facilities/create",
        data={"name": "Invalid Home", "ancc_target": "220", "rn_target": invalid},
    )
    assert response.status_code == 400
    with app.app_context():
        assert Facility.query.filter_by(name="Invalid Home").count() == 0


def test_targets_require_explicit_values_and_record_valid_updates(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert (
        client.post("/api/facilities/create", data={"name": "No targets"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/facility/targets", data={"ancc_target": "30", "rn_target": "44"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/facility/targets",
            data={
                "ancc_target": "220.5",
                "rn_target": "45",
                "target_quarter": "2026-10-01",
                "target_source": "Corrected provider notice",
                "target_correction": "Updated notice received",
            },
        ).status_code
        == 200
    )
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        assert (home.ancc_target, home.rn_target) == (220.5, 45)


def test_read_only_workspace_never_exposes_identifiable_records(web_app):
    app, ids = web_app
    with app.app_context():
        user = db.session.get(auth.User, ids["admin"])
        user.role = "read_only"
        db.session.commit()
    # The fixture holds an outer app context; invalidate its identity map after
    # changing the role in the nested context used for setup.
    db.session.expire_all()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get("/api/facility").status_code == 403
    assert client.get("/api/eligibility").status_code == 403
    assert client.get("/api/audit").status_code == 403
    assert client.get("/api/facilities").status_code == 200
    assert client.get("/api/dashboard").status_code == 200


def test_url_connector_cannot_bypass_reviewed_imports(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get("/api/integrations").json["data"]["platforms"] == []
    response = client.post(
        "/api/integrations", data={"action": "sync", "platform": "url_fetch"}
    )
    assert response.status_code == 409
    assert "Imports" in response.json["error"]


def test_quarter_targets_preserve_history_and_require_correction_basis(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        home_id = home.id
        july = FacilityTarget.query.filter_by(
            facility_id=home_id, quarter_start=date(2026, 7, 1)
        ).one()
        historical = (july.ancc_target, july.rn_target, july.source)
    body = {
        "ancc_target": "225",
        "rn_target": "46",
        "target_quarter": "2026-10-01",
        "target_source": "Corrected Q2 target notice",
    }
    assert client.post("/api/facility/targets", data=body).status_code == 400
    assert (
        client.post(
            "/api/facility/targets",
            data={**body, "target_correction": "Replacement notice received"},
        ).status_code
        == 200
    )
    with app.app_context():
        july = FacilityTarget.query.filter_by(
            facility_id=home_id, quarter_start=date(2026, 7, 1)
        ).one()
        assert (july.ancc_target, july.rn_target, july.source) == historical
        october = FacilityTarget.query.filter_by(
            facility_id=home_id, quarter_start=date(2026, 10, 1)
        ).one()
        assert (october.ancc_target, october.rn_target) == (225, 46)
        assert october.approved_by_user_id == ids["admin"]


def test_source_trace_and_evidence_decisions_are_scoped_and_reversible(web_app):
    from carelog.models import ImportReceipt

    app, ids = web_app
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        member = Staff(
            facility_id=home.id,
            staff_id="P1",
            name="Care Worker",
            role="PCW",
            eligibility_status="approved",
        )
        receipt = ImportReceipt(
            facility_id=home.id,
            organization_id=ids["customer"],
            evidence_type="unverified",
            shifts_imported=1,
        )
        db.session.add_all([member, receipt])
        db.session.flush()
        receipt_id = receipt.id
        db.session.add(
            Shift(
                facility_id=home.id,
                staff_id=member.id,
                date=date(2026, 8, 1),
                start_time=time(8),
                end_time=time(16),
                evidence_type="unverified",
                import_receipt_id=receipt.id,
                source_file="source.csv",
                source_sheet="Worked",
                source_row=7,
            )
        )
        db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    trace = client.get("/api/audit/records?start=2026-08-01&end=2026-08-01")
    assert trace.status_code == 200
    row = trace.json["data"]["records"][0]
    assert row["shift"]["source_row"] == 7
    assert row["minutes"] == 480
    assert row["counted"] is False
    assert (
        client.post(
            f"/api/audit/receipt/{receipt_id}/evidence",
            data={"evidence_type": "worked"},
        ).status_code
        == 400
    )
    for evidence in ("worked", "unverified"):
        assert (
            client.post(
                f"/api/audit/receipt/{receipt_id}/evidence",
                data={
                    "evidence_type": evidence,
                    "reason": "Source verified against approved attendance export",
                },
            ).status_code
            == 200
        )
        with app.app_context():
            assert (
                Shift.query.filter_by(import_receipt_id=receipt_id).one().evidence_type
                == evidence
            )
    decisions = client.get("/api/audit").json["data"]["decisions"]
    assert any(row["action"] == "evidence_classified" for row in decisions)
    sign_in_as(client, ids["outsider"])
    assert (
        client.post(
            f"/api/audit/receipt/{receipt_id}/evidence",
            data={"evidence_type": "worked", "reason": "Not owned"},
        ).status_code
        == 404
    )
    assert (
        client.get("/api/audit/records?start=2026-08-01&end=2026-08-01").json["data"][
            "total"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "query",
    ["start=bad", "start=2026-08-02&end=2026-08-01", "start=2020-01-01&end=2026-08-01"],
)
def test_invalid_reporting_ranges_are_rejected_visibly(web_app, query):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    for route in (
        "/api/export/daily.csv",
        "/api/export/summary.xlsx",
        "/api/audit/records",
    ):
        assert client.get(f"{route}?{query}").status_code == 400


def test_bulk_eligibility_is_atomic_and_requires_effective_approval_dates(web_app):
    app, ids = web_app
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        members = [
            Staff(
                facility_id=home.id,
                staff_id="P1",
                name="Care Worker",
                role="PCW",
                source_role="Personal Care Worker",
                eligibility_status="pending",
            ),
            Staff(
                facility_id=home.id,
                staff_id="N1",
                name="Unverified Nurse",
                role="RN",
                source_role="Registered Nurse",
                eligibility_status="pending",
            ),
        ]
        db.session.add_all(members)
        db.session.commit()
        selected = [member.id for member in members]
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    body = {
        "staff_ids": selected,
        "decision": "approved",
        "reason": "Verified eligible source roles and effective assignment",
    }
    assert client.post("/api/eligibility/bulk", data=body).status_code == 400
    assert (
        client.post(
            "/api/eligibility/bulk", data={**body, "eligible_from": "2026-08-01"}
        ).status_code
        == 400
    )
    with app.app_context():
        assert {
            member.eligibility_status
            for member in Staff.query.filter(Staff.id.in_(selected))
        } == {"pending"}
    assert (
        client.post(
            "/api/eligibility/bulk", data={**body, "decision": "excluded"}
        ).status_code
        == 200
    )
    with app.app_context():
        assert {
            member.eligibility_status
            for member in Staff.query.filter(Staff.id.in_(selected))
        } == {"excluded"}


def test_evidence_owner_guard_denies_null_and_foreign_facility(web_app):
    from carelog.models import ImportJob
    app, ids = web_app
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        db.session.add_all([
            ImportJob(id="null-owner", facility_id=home.id, organization_id=None, status="failed"),
            ImportJob(id="foreign-home", facility_id=ids["other_facility"], organization_id=ids["customer"], status="failed"),
        ])
        db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get("/api/import/status/null-owner").status_code == 404
    assert client.get("/api/import/status/foreign-home").status_code == 404
    from carelog.services.ownership import reconcile_evidence_owners
    assert reconcile_evidence_owners() == {"repairable": 1, "quarantined": 0, "conflicts": 1}
    assert db.session.get(ImportJob, "null-owner").organization_id is None
    reconcile_evidence_owners(apply=True)
    assert client.get("/api/import/status/null-owner").status_code == 200
    assert client.get("/api/import/status/foreign-home").status_code == 404


def test_facility_delete_purges_failed_sources_and_preserves_db_on_storage_failure(web_app, monkeypatch, tmp_path):
    from carelog.models import ImportJob
    from carelog.storage import LocalStorage, StorageError
    app, ids = web_app
    storage = LocalStorage(str(tmp_path))
    monkeypatch.setattr("carelog.services.workspace._storage", lambda: storage)
    with app.app_context():
        home = Facility.query.filter_by(organization_id=ids["customer"]).one()
        home_id = home.id
        db.session.add(ImportJob(id="failed-source", facility_id=home.id, organization_id=ids["customer"], status="failed", storage_prefix="imports/job-failed-source"))
        db.session.commit()
    key = storage.put("imports/job-failed-source/private.csv", b"synthetic private evidence")
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    original_delete = storage.delete
    def unavailable(keys):
        raise StorageError("offline")
    monkeypatch.setattr(storage, "delete", unavailable)
    assert client.post(f"/api/facilities/{home_id}/delete", data={"confirm": "delete", "reason": "Closed home"}).status_code == 502
    with app.app_context():
        assert db.session.get(Facility, home_id) is not None
        assert db.session.get(ImportJob, "failed-source") is not None
    monkeypatch.setattr(storage, "delete", original_delete)
    assert client.post(f"/api/facilities/{home_id}/delete", data={"confirm": "delete", "reason": "Closed home"}).status_code == 200
    assert storage.list("imports/job-failed-source") == []
    with app.app_context():
        assert db.session.get(Facility, home_id) is None
        assert db.session.get(ImportJob, "failed-source") is None


def test_clinical_review_permission_and_admin_only_workspace_delete(web_app):
    app, ids = web_app
    # The fixture's application context is shared with test requests, so roles
    # change on that session rather than through a second, stale identity map.
    reviewer = db.session.get(auth.User, ids["admin"])
    reviewer.role = "clinical_manager"
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert "review_eligibility" in auth.ROLES["clinical_manager"]["permissions"]
    assert "review_eligibility" in auth.ROLES["compliance_officer"]["permissions"]
    assert client.post("/api/clear", data={"confirm": "delete"}).status_code == 403
    reviewer.role = "facility_manager"
    db.session.commit()
    assert client.post("/api/clear", data={"confirm": "delete"}).status_code == 403
    assert Facility.query.filter_by(organization_id=ids["customer"]).count() == 1


def test_session_expires_after_thirty_minutes_without_requests(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get("/api/dashboard").status_code == 200

    with client.session_transaction() as session:
        session["last_activity_at"] = time_module.time() - 1799
    assert client.get("/api/dashboard").status_code == 200

    with client.session_transaction() as session:
        session["last_activity_at"] = time_module.time() - 1801
    response = client.get("/api/dashboard")
    assert response.status_code == 401
    assert response.json["navigate"].startswith("/login")
    assert client.get("/api/dashboard").status_code == 401


def test_session_without_activity_timestamp_is_not_trusted(web_app):
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    with client.session_transaction() as session:
        del session["last_activity_at"]
    assert client.get("/api/dashboard").status_code == 401
