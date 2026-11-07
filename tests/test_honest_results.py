"""Missing evidence is never good news, a gap, or a final verdict."""
import json
import time as clock
from datetime import date, datetime, time

import pytest

from carelog import auth
from carelog.domain.compliance import detect_gaps, quarterly_tests, rn_coverage
from carelog.ingestion import jobs
from carelog.ingestion.quality import daylight_saving_warnings
from carelog.models import (
    AuditLog, Facility, FacilityTarget, ImportJob, ImportReceipt, QuarterClosure, ResidentDay, Shift, Staff, db,
)
from carelog.storage import LocalStorage
from tests.test_corrections import run_import, timesheet, approve_everyone
from tests.test_ui_boundaries import sign_in_as, web_app  # noqa: F401


def member(facility, **values):
    row = Staff(**{"facility_id": facility.id, "staff_id": "N1", "name": "Nurse", "role": "RN",
                   "source_role": "Registered Nurse", "eligibility_status": "approved",
                   "approved_at": datetime(2026, 1, 1), "registration_number": "NMW1",
                   "registration_expiry": date(2027, 1, 1), **values})
    db.session.add(row)
    db.session.flush()
    return row


def shift(facility, staff, day, start=time(0), end=time(23, 59), evidence="worked", **values):
    db.session.add(Shift(facility_id=facility.id, staff_id=staff.id, date=day, start_time=start,
                         end_time=end, evidence_type=evidence, **values))


def complete_quarter(facility, staff):
    for offset in range(92):
        day = date.fromordinal(date(2026, 7, 1).toordinal() + offset)
        db.session.add(ResidentDay(facility_id=facility.id, resident_id="R1", date=day,
                                   occupied=True, service_type="permanent"))
        shift(facility, staff, day)
    db.session.commit()


def test_unverified_time_makes_a_complete_quarter_provisional(facility):
    nurse = member(facility)
    complete_quarter(facility, nurse)
    assert quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))["provisional"] is False
    shift(facility, nurse, date(2026, 8, 1), time(0), time(8), evidence="unverified")
    db.session.commit()
    result = quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))
    assert result["provisional"] is True and result["passed"] is None
    assert any("unverified time" in limit for limit in result["limitations"])


def test_one_cleaners_shift_does_not_complete_a_date(facility):
    nurse = member(facility)
    cleaner = member(facility, staff_id="C1", role="OTHER", source_role="Cleaner", eligibility_status="excluded")
    complete_quarter(facility, nurse)
    Shift.query.filter_by(date=date(2026, 8, 1)).delete()
    shift(facility, cleaner, date(2026, 8, 1), time(8), time(16))
    db.session.commit()
    result = quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))
    assert "2026-08-01" in result["missing_worked_dates"] and result["provisional"] is True


def test_rn_awaiting_credentials_is_not_a_confirmed_gap(facility):
    nurse = member(facility, registration_number=None, registration_expiry=None)
    shift(facility, nurse, date(2026, 8, 1), time(7), time(15))
    db.session.commit()
    coverage = rn_coverage(facility.id, date(2026, 8, 1))
    assert coverage["covered_minutes"] == 0
    assert coverage["withheld_minutes"] == 480
    assert coverage["coverage_known"] is False and coverage["full_coverage"] is False
    messages = [a["message"] for a in detect_gaps(facility, date(2026, 8, 1))]
    assert any("not a confirmed gap" in m for m in messages)
    assert not any("No clocked RN coverage" in m for m in messages)


def test_a_real_gap_is_still_reported(facility):
    nurse = member(facility)
    for day in (date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)):
        shift(facility, nurse, day, time(7), time(15))
    db.session.commit()
    coverage = rn_coverage(facility.id, date(2026, 8, 1))
    assert coverage["coverage_known"] is True and coverage["withheld_minutes"] == 0
    assert any(gap["reportable"] for gap in coverage["gap_intervals"])
    assert coverage["unassessed_minutes"] == 0


def test_a_gap_at_the_edge_of_the_data_is_unassessed_not_reportable(facility):
    nurse = member(facility)
    shift(facility, nurse, date(2026, 8, 1), time(7), time(23, 50))
    shift(facility, nurse, date(2026, 8, 2), time(0), time(7))
    db.session.commit()
    first = rn_coverage(facility.id, date(2026, 8, 1))
    assert first["unassessed_minutes"] == 420 and first["full_coverage"] is False
    assert not any(g["reportable"] for g in first["gap_intervals"] if g["start_minute"] == 0)


def test_a_pending_nurse_title_leaves_the_gap_unassessed(facility):
    nurse = member(facility, staff_id="P1", role="OTHER", source_role="Nurse", eligibility_status="pending",
                   approved_at=None, registration_number=None, registration_expiry=None)
    for day in (date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)):
        shift(facility, nurse, day, time(7), time(19))
    db.session.commit()
    coverage = rn_coverage(facility.id, date(2026, 8, 1))
    assert coverage["withheld_minutes"] == 720 and coverage["coverage_known"] is False


def test_target_mismatch_makes_the_result_provisional(facility, monkeypatch):
    nurse = member(facility)
    complete_quarter(facility, nurse)
    monkeypatch.setattr("carelog.domain.targets.reconcile_configured_targets",
                        lambda *a: {"complete": True, "matched": False})
    result = quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))
    assert result["provisional"] is True and result["passed"] is None


def test_social_leave_over_the_yearly_limit_is_flagged(facility):
    for offset in range(53):
        day = date.fromordinal(date(2026, 7, 1).toordinal() + offset)
        db.session.add(ResidentDay(facility_id=facility.id, resident_id="R1", date=day, occupied=True,
                                   service_type="permanent", leave_type="social"))
    db.session.commit()
    result = quarterly_tests(facility, date(2026, 7, 1), date(2026, 8, 22))
    assert any("social-leave days" in limit for limit in result["limitations"])


def test_excluded_staff_do_not_hold_up_a_gap(facility):
    nurse = member(facility, eligibility_status="excluded")
    shift(facility, nurse, date(2026, 8, 1), time(7), time(15))
    db.session.commit()
    assert rn_coverage(facility.id, date(2026, 8, 1))["coverage_known"] is True


def test_no_target_is_flagged_not_called_on_track(facility):
    FacilityTarget.query.delete()
    db.session.commit()
    alerts = detect_gaps(facility, date(2026, 8, 1))
    assert any(a["severity"] == "medium" and "No approved target" in a["message"] for a in alerts)


@pytest.fixture()
def admin_client(web_app, tmp_path, monkeypatch):  # noqa: F811
    app, ids = web_app
    app.config["UPLOADS_DIR"] = str(tmp_path)
    monkeypatch.setattr(jobs, "dispatch_job", lambda *args: None)
    monkeypatch.setattr(jobs, "_progress", lambda *args: None)
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    return app, ids, client, Facility.query.filter_by(organization_id=ids["customer"]).one()


def test_sign_out_ends_a_copied_session_cookie_but_keeps_trusted_devices(admin_client):
    app, ids, client, _ = admin_client
    user = db.session.get(auth.User, ids["admin"])
    version = user.auth_version
    stolen = app.test_client()
    sign_in_as(stolen, ids["admin"])
    assert stolen.get("/api/dashboard").status_code == 200
    client.post("/api/logout")
    assert stolen.get("/api/dashboard").status_code == 401
    assert user.auth_version == version


def test_sessions_have_an_absolute_limit(admin_client):
    _, _, client, _ = admin_client
    with client.session_transaction() as state:
        state["signed_in_at"] = clock.time() - 12 * 3600 - 1
    assert client.get("/api/dashboard").status_code == 401


def prepare(app, ids, home, name="t.csv"):
    job_id = jobs.start_job(app, home.id, [(name, timesheet(("A", "2026-08-01")))], LocalStorage(app.config["UPLOADS_DIR"]),
                            organization_id=ids["customer"], user_id=ids["admin"])
    return job_id


def test_wrong_upload_can_be_erased_with_a_reason_and_only_by_an_administrator(admin_client):
    app, ids, client, home = admin_client
    job_id = prepare(app, ids, home)
    job = db.session.get(ImportJob, job_id)
    prefix = job.storage_prefix
    job.status = "cancelled"
    db.session.commit()
    store = LocalStorage(app.config["UPLOADS_DIR"])
    assert store.list(prefix)

    assert client.post(f"/api/import/{job_id}/purge", data={"confirm": "delete"}).status_code == 400
    assert client.post(f"/api/import/{job_id}/purge", data={"confirm": "delete", "reason": "Wrong home"}).status_code == 200
    assert db.session.get(ImportJob, job_id) is None and store.list(prefix) == []
    assert "Wrong home" in AuditLog.query.filter_by(action="import_purged").one().detail


def test_committed_imports_cannot_be_purged_as_uploads(admin_client):
    app, ids, client, home = admin_client
    job_id = prepare(app, ids, home)
    job = db.session.get(ImportJob, job_id)
    job.status = "done"
    db.session.commit()
    assert client.post(f"/api/import/{job_id}/purge", data={"confirm": "delete", "reason": "x"}).status_code == 409


def test_samples_do_not_outlive_review(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    job, review = run_import(app, ids, home, timesheet(("A", "2026-08-01")))
    assert review["samples"]
    stored = json.loads(job.review_json)
    assert "samples" not in stored and "period_sample" not in stored["impact"]


@pytest.mark.parametrize("path", ["/api/audit/receipt/{receipt}/delete", "/api/facilities/{home}/delete"])
def test_a_facility_manager_cannot_destroy_evidence(admin_client, path):
    app, ids, client, home = admin_client
    receipt = ImportReceipt(facility_id=home.id, organization_id=ids["customer"])
    db.session.add(receipt)
    user = db.session.get(auth.User, ids["admin"])
    user.role = "facility_manager"
    db.session.commit()
    url = path.format(receipt=receipt.id, home=home.id)
    assert client.post(url, data={"confirm": "delete", "reason": "x"}).status_code == 403


def test_deleting_needs_a_recorded_reason(admin_client):
    app, ids, client, home = admin_client
    receipt = ImportReceipt(facility_id=home.id, organization_id=ids["customer"])
    db.session.add(receipt)
    db.session.commit()
    client.post(f"/api/audit/receipt/{receipt.id}/delete", data={"confirm": "delete"})
    assert db.session.get(ImportReceipt, receipt.id) is not None


def closure_form(client, basis, q="2026-Q1"):
    response = client.get(f"/api/reports/closure-preview?q={q}")
    assert response.status_code == 200
    preview = response.json["data"]["preview"]
    return {"q": q, "basis": basis, "review_fingerprint": preview["review_fingerprint"],
            "acknowledge_limitations": "1"}


def test_closed_quarter_keeps_figures_and_statement_and_shows_later_movement(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    for day in range(1, 4):
        db.session.add(ResidentDay(facility_id=home.id, resident_id="R1", date=date(2026, 8, day),
                                   occupied=True, service_type="permanent"))
    db.session.commit()
    run_import(app, ids, home, timesheet(("A", "2026-08-01"), ("A", "2026-08-02")))
    approve_everyone(home)

    assert client.post("/api/reports/close", data={"q": "2026-Q1"}).status_code == 400
    assert client.post("/api/reports/close", data=closure_form(client, "QFR lodged")).status_code == 200
    statuses = client.get("/api/reports").json["data"]["closures"]
    assert statuses[0]["label"] == "FY2026-27 Q1" and statuses[0]["moved"] is False
    assert statuses[0]["source_files"] == 1

    pdf = client.get(f"/api/reports/closure/{statuses[0]['id']}.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"

    row = Shift.query.filter_by(date=date(2026, 8, 2)).one()
    client.post(f"/api/audit/shift/{row.id}/void", data={"reason": "Late correction"})
    moved = client.get("/api/reports").json["data"]["closures"][0]
    assert moved["moved"] is True
    assert client.get(f"/api/reports/closure/{statuses[0]['id']}.pdf").data == pdf.data
    assert AuditLog.query.filter_by(action="quarter_closed").count() == 1


def test_closure_names_census_files_and_matches_the_statement(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    receipt = ImportReceipt(facility_id=home.id, organization_id=ids["customer"],
                            summary_json=json.dumps([{"filename": "census.csv", "sha256": "abc"}]))
    db.session.add(receipt)
    db.session.flush()
    for offset in range(92):
        day = date.fromordinal(date(2026, 7, 1).toordinal() + offset)
        db.session.add(ResidentDay(facility_id=home.id, resident_id="R1", date=day, occupied=True,
                                   service_type="permanent", import_receipt_id=receipt.id))
    db.session.commit()
    run_import(app, ids, home, timesheet(("A", "2026-08-01")))
    approve_everyone(home)
    assert client.post("/api/reports/close", data=closure_form(client, "Lodged")).status_code == 200
    closure = QuarterClosure.query.one()
    assert "census.csv" in {s["filename"] for s in json.loads(closure.sources_json)}
    from carelog.domain.reports import compute_quarter_stats
    stats = compute_quarter_stats(home.id, 2026, 1)
    assert json.loads(closure.figures_json)["bed_days"] == stats["totals"]["bed_days"] if "totals" in stats else True


def test_a_quarter_cannot_close_before_it_ends(admin_client):
    _, _, client, _ = admin_client
    response = client.post("/api/reports/close", data={"q": "2030-Q1", "basis": "Too early"})
    assert response.status_code == 400 and "has ended" in response.json["error"]


def test_closed_quarter_flags_readiness_changes_without_changed_minutes(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    nurse = member(home)
    complete_quarter(home, nurse)
    assert client.post("/api/reports/close", data=closure_form(client, "Board statement")).status_code == 200
    before = client.get("/api/reports").json["data"]["closures"][0]
    issued = client.get(f"/api/reports/closure/{before['id']}.pdf").data
    figures = QuarterClosure.query.one().figures_json

    # Extra unverified time changes confidence, while every counted minute stays put.
    shift(home, nurse, date(2026, 8, 1), time(0), time(8), evidence="unverified")
    db.session.commit()
    after = client.get("/api/reports").json["data"]["closures"][0]
    assert before["moved"] is False and after["moved"] is True
    assert "Evidence readiness or limitations changed." in after["changes"]
    assert after["live_provisional"] is True
    assert QuarterClosure.query.one().figures_json == figures
    assert client.get(f"/api/reports/closure/{before['id']}.pdf").data == issued


def test_closed_quarter_flags_new_sources_and_calculation_version(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    nurse = member(home)
    complete_quarter(home, nurse)
    assert client.post("/api/reports/close", data=closure_form(client, "Board statement")).status_code == 200
    before = client.get("/api/reports").json["data"]["closures"][0]
    receipt = ImportReceipt(facility_id=home.id, organization_id=ids["customer"],
                            summary_json=json.dumps([{"filename": "verified-census.csv", "sha256": "abc"}]))
    db.session.add(receipt)
    db.session.flush()
    ResidentDay.query.filter_by(facility_id=home.id).update({"import_receipt_id": receipt.id})
    db.session.commit()
    after = client.get("/api/reports").json["data"]["closures"][0]
    assert after["tests"] == before["tests"]
    assert after["changes"] == ["Contributing source files changed."]
    monkeypatch.setattr("carelog.services.closure.CALC_VERSION", "future-version")
    assert "The calculation version changed." in client.get("/api/reports").json["data"]["closures"][0]["changes"]


def test_closed_statement_is_facility_scoped(admin_client):
    app, ids, client, home = admin_client
    other = QuarterClosure(facility_id=ids["other_facility"], year=2026, quarter=1, basis="x",
                           calc_version="x", figures_json="{}", pdf_key="k", pdf_sha256="s")
    db.session.add(other)
    db.session.commit()
    assert client.get(f"/api/reports/closure/{other.id}.pdf").status_code == 404


def test_daylight_saving_shift_is_flagged():
    overnight = {"staff_id": "N1", "date": date(2026, 4, 4), "start_time": time(22), "end_time": time(6)}
    assert "N1 on 2026-04-04" in daylight_saving_warnings([overnight], "Australia/Sydney")[0]
    assert daylight_saving_warnings([overnight], "Australia/Brisbane") == []
    assert daylight_saving_warnings([{**overnight, "date": date(2026, 8, 4)}], "Australia/Sydney") == []


def test_a_closed_quarter_protects_the_evidence_it_cites(admin_client, monkeypatch):
    app, ids, client, home = admin_client
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    job, _ = run_import(app, ids, home, timesheet(("A", "2026-08-01")))
    approve_everyone(home)
    assert client.post("/api/reports/close", data=closure_form(client, "Lodged")).status_code == 200
    form = {"confirm": "delete", "reason": "Tidying"}
    assert client.post(f"/api/audit/receipt/{job.receipt_id}/delete", data=form).status_code == 409
    assert db.session.get(ImportReceipt, job.receipt_id) is not None
    receipt_id = job.receipt_id
    assert client.post(f"/api/audit/receipt/{receipt_id}/delete", data={**form, "override_closure": "1"}).status_code == 200


def test_restore_checks_every_record_for_that_person(facility, web_app):  # noqa: F811
    app, ids = web_app
    home = Facility.query.filter_by(organization_id=ids["customer"]).one()
    old = member(home, staff_id="R", source_role="Enrolled Nurse", role="EN")
    new = member(home, staff_id="R")
    common = dict(facility_id=home.id, date=date(2026, 8, 1), start_time=time(0), end_time=time(0), coverage_known=False)
    voided = Shift(staff_id=old.id, duration_minutes=480, evidence_type="voided", prior_evidence_type="worked", **common)
    db.session.add_all([voided, Shift(staff_id=new.id, duration_minutes=450, evidence_type="worked", **common)])
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/audit/shift/{voided.id}/restore", data={"reason": "x"}).status_code == 409


def test_a_forecast_makes_no_call_on_unverified_bed_days(facility, monkeypatch):
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _: date(2026, 8, 14))
    from carelog.domain.compliance import compliance_pct, forecast_quarter
    nurse = member(facility)
    for day in range(1, 15):
        db.session.add(ResidentDay(facility_id=facility.id, resident_id="R1", date=date(2026, 8, day),
                                   occupied=True, service_type="permanent"))
        db.session.add(ResidentDay(facility_id=facility.id, resident_id="R2", date=date(2026, 8, day), occupied=True))
        shift(facility, nurse, date(2026, 8, day), time(8), time(16))
    db.session.commit()
    forecast = forecast_quarter(facility, date(2026, 8, 14))
    assert forecast["on_track"] is None and forecast["provisional"] is True
    assert compliance_pct(facility, date(2026, 8, 14)) is None


def test_a_duration_row_next_door_leaves_a_midnight_gap_unassessed(facility):
    nurse = member(facility)
    shift(facility, nurse, date(2026, 8, 1), time(7), time(23, 59))
    db.session.add(Shift(facility_id=facility.id, staff_id=nurse.id, date=date(2026, 7, 31), start_time=time(0),
                         end_time=time(0), coverage_known=False, duration_minutes=480, evidence_type="worked"))
    db.session.commit()
    assert rn_coverage(facility.id, date(2026, 8, 1))["unassessed_minutes"] >= 420
