"""Correction workflow: nothing a provider fixes may hide or double count time."""
import json
from datetime import date, datetime, time

import pytest

from carelog.domain.compliance import quarter_totals
from carelog.domain.eligibility import counts_toward_care, registration_problem
from carelog.ingestion import jobs
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.pipeline import (
    ImportOutcome,
    _break_whole_minutes,
    _import_shifts,
    _whole_minutes,
    record_credential_gap,
)
from carelog.ingestion.quality import validate_shifts
from carelog.models import (
    AuditLog,
    Facility,
    ImportJob,
    ImportReceipt,
    ResidentDay,
    Shift,
    Staff,
    db,
)
from carelog.storage import LocalStorage
from tests.test_ui_boundaries import sign_in_as, web_app  # noqa: F401

HEADER = "Staff ID,Staff Name,Role,Work Date,Actual Start,Actual Finish,Paid Hours,Approval Status\n"


def timesheet(*rows):
    lines = [
        f"{staff},{staff} Name,Registered Nurse,{day},08:00,16:00,8,Approved\n"
        for staff, day in rows
    ]
    return (HEADER + "".join(lines)).encode()


SPEC = {
    "targets": [{
        "kind": "shifts",
        "sheet": "CSV",
        "fail_on_row_error": True,
        "row_filter": {"column": "Approval Status", "include_values": ["Approved"]},
        "break_basis": "Synthetic intervals contain no unpaid breaks.",
        "fields": {
            "staff_id": {"column": "Staff ID"},
            "staff_name": {"column": "Staff Name"},
            "role": {"column": "Role", "normalize": "role"},
            "date": {"column": "Work Date", "parse": "date"},
            "start_time": {"column": "Actual Start", "parse": "time"},
            "end_time": {"column": "Actual Finish", "parse": "time"},
            "break_minutes": {"source": "constant", "value": 0, "parse": "number"},
        },
    }],
    "reasoning": "test mapping",
}


@pytest.fixture()
def home(web_app, tmp_path, monkeypatch):  # noqa: F811
    app, ids = web_app
    app.config["UPLOADS_DIR"] = str(tmp_path)
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    monkeypatch.setattr(jobs, "dispatch_job", lambda *args: None)
    # In-memory SQLite shares one connection, so a separate progress session
    # would commit the care transaction. PostgreSQL tests cover real isolation.
    monkeypatch.setattr(jobs, "_progress", lambda *args: None)
    facility = Facility.query.filter_by(organization_id=ids["customer"]).one()
    for day in range(1, 11):
        db.session.add(ResidentDay(
            facility_id=facility.id, resident_id="R1", date=date(2026, 8, day),
            occupied=True, service_type="permanent",
        ))
    db.session.commit()
    return app, ids, facility


def run_import(app, ids, facility, data, name="timesheet.csv", replace_period=False):
    storage = LocalStorage(app.config["UPLOADS_DIR"])
    job_id = jobs.start_job(app, facility.id, [(name, data)], storage,
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    job.manual_specs_json = json.dumps({name: {**SPEC, "targets": [{**SPEC["targets"][0], "sheet": sheet_name(name, data)}]}})
    db.session.commit()
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "review", job.error
    review = json.loads(job.review_json)
    job.status, job.approved_at, job.replace_period = "queued", datetime.utcnow(), replace_period
    db.session.commit()
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "done", job.error
    return job, review


def sheet_name(name, data):
    from carelog.ingestion.reader import read_upload
    return read_upload(name, data)[0].name


def approve_everyone(facility):
    for member in Staff.query.filter_by(facility_id=facility.id):
        member.eligibility_status = "approved"
        member.approved_at = datetime(2026, 8, 1)
        member.eligible_from = date(2026, 1, 1)
        member.registration_number = "NMW0001"
        member.registration_expiry = date(2027, 6, 30)
    db.session.commit()


def worked_total(facility):
    return quarter_totals(facility.id, date(2026, 7, 1), date(2026, 9, 30))["total_minutes"]


def test_corrected_export_retires_removed_row_only_when_period_replacement_is_chosen(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-02"), ("C", "2026-08-03")))
    approve_everyone(facility)
    assert worked_total(facility) == 3 * 480

    # A corrected file omits B, who did not attend. Without the explicit choice
    # the row stays, and review must say so.
    corrected = timesheet(("A", "2026-08-01"), ("C", "2026-08-03"))
    _, review = run_import(app, ids, facility, corrected, name="timesheet2.csv")
    assert review["impact"]["period_rows"] == 1
    assert review["impact"]["period_sample"][0]["minutes"] == 480
    assert worked_total(facility) == 3 * 480

    _, review = run_import(app, ids, facility, corrected, name="timesheet3.csv", replace_period=True)
    assert review["impact"]["period_rows"] == 1
    assert worked_total(facility) == 2 * 480
    retained = Shift.query.filter_by(evidence_type="superseded", source_row=3).order_by(Shift.id).first()
    assert retained is not None and retained.prior_evidence_type == "worked"
    assert AuditLog.query.filter_by(action="import_period_replaced").count() == 1


def test_review_shows_effect_on_all_three_tests_before_anything_changes(home):
    app, ids, facility = home
    first, _ = run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    before_rows = Shift.query.filter(Shift.evidence_type == "worked").count()
    _, review = run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("A", "2026-08-02")), name="more.csv")
    effect = review["effect"][0]
    assert effect["start"] == "2026-07-01"
    assert {t["key"] for t in effect["before"]["tests"]} == {"total_care", "rn_only", "rn_plus_en"}
    care = {t["key"]: t for t in effect["after"]["tests"]}["total_care"]
    was = {t["key"]: t for t in effect["before"]["tests"]}["total_care"]
    assert care["achieved"] > was["achieved"]
    assert Shift.query.filter(Shift.evidence_type == "worked").count() == before_rows + 1


def test_deleting_an_import_restores_what_it_replaced(home):
    app, ids, facility = home
    original = [("A", "2026-08-01"), ("B", "2026-08-02"), ("C", "2026-08-03")]
    run_import(app, ids, facility, timesheet(*original))
    approve_everyone(facility)
    job, _ = run_import(app, ids, facility, timesheet(original[0], original[2]), name="redo.csv", replace_period=True)
    assert worked_total(facility) == 2 * 480

    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/audit/receipt/{job.receipt_id}/delete", data={"confirm": "delete", "reason": "Wrong file"}).status_code == 200
    assert worked_total(facility) == 3 * 480
    assert Shift.query.filter_by(evidence_type="superseded").count() == 0


def test_delete_does_not_restore_rows_newer_evidence_already_covers(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    middle, _ = run_import(app, ids, facility, timesheet(("A", "2026-08-01")), name="b.csv")
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")), name="c.csv")
    assert worked_total(facility) == 480

    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/audit/receipt/{middle.receipt_id}/delete", data={"confirm": "delete", "reason": "Wrong file"}).status_code == 200
    assert worked_total(facility) == 480


def test_single_row_void_is_audited_reversible_and_tenant_scoped(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-02")))
    approve_everyone(facility)
    row = Shift.query.filter_by(date=date(2026, 8, 2)).one()
    client = app.test_client()
    sign_in_as(client, ids["admin"])

    assert client.post(f"/api/audit/shift/{row.id}/void", data={}).status_code == 400
    assert client.post(f"/api/audit/shift/{row.id}/void", data={"reason": "No-show confirmed by roster manager"}).status_code == 200
    db.session.refresh(row)
    assert (row.evidence_type, row.prior_evidence_type) == ("voided", "worked")
    assert worked_total(facility) == 480
    records = client.get("/api/audit/records?start=2026-08-01&end=2026-08-10&evidence=voided").json["data"]["records"]
    assert records[0]["reason"] == "Voided: No-show confirmed by roster manager"
    assert client.get("/api/audit/records?start=2026-08-01&end=2026-08-10").json["data"]["total"] == 1

    outsider = app.test_client()
    sign_in_as(outsider, ids["outsider"])
    assert outsider.post(f"/api/audit/shift/{row.id}/restore", data={"reason": "x"}).status_code == 404

    assert client.post(f"/api/audit/shift/{row.id}/restore", data={"reason": "Timesheet found"}).status_code == 200
    assert worked_total(facility) == 2 * 480
    actions = [a.action for a in AuditLog.query.filter(AuditLog.action.like("shift_%")).order_by(AuditLog.id)]
    assert actions == ["shift_voided", "shift_restored"]


def test_restore_refuses_to_double_count_a_replacement_row(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    row = Shift.query.filter_by(evidence_type="worked").one()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    client.post(f"/api/audit/shift/{row.id}/void", data={"reason": "Wrong times"})
    # This correction changes clocks and their matching duration together.
    later = timesheet(("A", "2026-08-01")).replace(b"08:00,16:00,8", b"09:00,15:00,6")
    run_import(app, ids, facility, later, name="right.csv")
    assert client.post(f"/api/audit/shift/{row.id}/restore", data={"reason": "oops"}).status_code == 409
    assert worked_total(facility) == 360


def test_restore_applies_the_import_rule_to_duration_rows(home):
    app, ids, facility = home
    member = Staff(facility_id=facility.id, staff_id="D1", name="Dur", role="RN", source_role="Registered Nurse")
    db.session.add(member)
    db.session.flush()
    common = dict(facility_id=facility.id, staff_id=member.id, date=date(2026, 8, 1), start_time=time(0),
                  end_time=time(0), coverage_known=False)
    voided = Shift(**common, duration_minutes=480, evidence_type="voided", prior_evidence_type="worked")
    active = Shift(**common, duration_minutes=420, evidence_type="worked")
    db.session.add_all([voided, active])
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/audit/shift/{voided.id}/restore", data={"reason": "oops"}).status_code == 409
    assert voided.evidence_type == "voided"

    voided.pay_category, active.pay_category = "Overtime", "Ordinary"
    db.session.commit()
    assert client.post(f"/api/audit/shift/{voided.id}/restore", data={"reason": "Second line"}).status_code == 200


def test_a_cumulative_export_cannot_undo_a_void(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-02")))
    approve_everyone(facility)
    row = Shift.query.filter_by(date=date(2026, 8, 2)).one()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    client.post(f"/api/audit/shift/{row.id}/void", data={"reason": "No-show"})
    _, review = run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-02")), name="cumulative.csv")
    assert review["impact"]["void_matches"][0]["reason"] == "No-show"
    assert review["impact"]["shifts_imported"] == 1
    assert worked_total(facility) == 480


def test_role_change_opens_a_new_record_and_never_blocks_the_batch(home):
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-01")))
    approve_everyone(facility)
    changed = timesheet(("A", "2026-08-02"), ("B", "2026-08-02")).replace(
        b"A,A Name,Registered Nurse", b"A,A Name,RN Level 2 Educator")
    run_import(app, ids, facility, changed, name="later.csv")
    records = Staff.query.filter_by(staff_id="A").order_by(Staff.id).all()
    assert [m.source_role for m in records] == ["Registered Nurse", "RN Level 2 Educator"]
    assert records[1].registration_number == "NMW0001"
    assert records[0].eligibility_status == "approved"
    assert worked_total(facility) == 3 * 480
    assert Shift.query.filter_by(staff_id=records[1].id).count() == 1


def test_verified_source_runs_a_reviewed_replacement_and_is_remembered(home):
    app, ids, facility = home
    plain = b"Staff ID,Staff Name,Role,Day,Start,Finish\nA,A Name,Registered Nurse,2026-08-01,08:00,16:00\n"
    spec = {"targets": [{"kind": "shifts", "sheet": sheet_name("f.csv", plain), "fields": {
        "staff_id": {"column": "Staff ID"}, "role": {"column": "Role", "normalize": "role"},
        "date": {"column": "Day", "parse": "date"}, "start_time": {"column": "Start", "parse": "time"},
        "end_time": {"column": "Finish", "parse": "time"}}}], "reasoning": "test"}
    storage = LocalStorage(app.config["UPLOADS_DIR"])

    def prepare(name, data):
        job_id = jobs.start_job(app, facility.id, [(name, data)], storage,
                                organization_id=ids["customer"], user_id=ids["admin"])
        job = db.session.get(ImportJob, job_id)
        job.manual_specs_json = json.dumps({name: spec})
        db.session.commit()
        jobs.run_job(job_id)
        return job_id

    job_id = prepare("f.csv", plain)
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert json.loads(db.session.get(ImportJob, job_id).review_json)["outcomes"][0]["results"][0]["evidence_type"] == "unverified"
    assert client.post(f"/api/import/{job_id}/verify-source", data={}).status_code == 400
    assert client.post(f"/api/import/{job_id}/verify-source", data={"basis": "Checked against Kronos clock data"}).status_code == 200
    jobs.run_job(job_id)
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    result = review["outcomes"][0]["results"][0]
    assert result["evidence_type"] == "worked" and "Kronos" in result["evidence_basis"]
    assert AuditLog.query.filter_by(action="source_verified").count() == 1


def test_verify_source_refuses_a_roster(home):
    app, ids, facility = home
    roster = b"Staff ID,Role,Date,Scheduled Start,Scheduled Finish\nA,Registered Nurse,2026-08-01,08:00,16:00\n"
    name = "roster.csv"
    job_id = jobs.start_job(app, facility.id, [(name, roster)], LocalStorage(app.config["UPLOADS_DIR"]),
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    job.manual_specs_json = json.dumps({name: {"targets": [{"kind": "shifts", "sheet": sheet_name(name, roster), "fields": {
        "staff_id": {"column": "Staff ID"}, "role": {"column": "Role", "normalize": "role"},
        "date": {"column": "Date", "parse": "date"}, "start_time": {"column": "Scheduled Start", "parse": "time"},
        "end_time": {"column": "Scheduled Finish", "parse": "time"}}}], "reasoning": "t"}})
    db.session.commit()
    jobs.run_job(job_id)
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/import/{job_id}/verify-source", data={"basis": "x"}).status_code == 400


def test_retry_keeps_the_reviewers_mapping(home):
    app, ids, facility = home
    data = timesheet(("A", "2026-08-01"))
    storage = LocalStorage(app.config["UPLOADS_DIR"])
    job_id = jobs.start_job(app, facility.id, [("t.csv", data)], storage, organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    job.manual_specs_json = json.dumps({"t.csv": SPEC})
    job.status = "failed"
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    new_id = client.post(f"/api/import/{job_id}/retry").json["navigate"].rsplit("/", 1)[-1]
    assert json.loads(db.session.get(ImportJob, new_id).manual_specs_json) == {"t.csv": SPEC}


def test_reviewers_only_can_void(home):
    from carelog import auth
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    row = Shift.query.first()
    user = db.session.get(auth.User, ids["admin"])
    user.role = "auditor"
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.post(f"/api/audit/shift/{row.id}/void", data={"reason": "x"}).status_code == 403


def test_tampered_retained_file_is_refused_at_import_and_download(home, tmp_path):
    app, ids, facility = home
    storage = LocalStorage(app.config["UPLOADS_DIR"])
    data = timesheet(("A", "2026-08-01"))
    job_id = jobs.start_job(app, facility.id, [("t.csv", data)], storage,
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    assert json.loads(job.files_json)[0]["sha256"]
    storage.put(f"{job.storage_prefix}/t.csv", data + b"B,B,Registered Nurse,2026-08-02,08:00,16:00,8,Approved\n")
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "failed" and "no longer matches" in job.error
    assert Shift.query.count() == 0

    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get(f"/api/audit/job/{job_id}/file/t.csv").status_code == 502
    assert AuditLog.query.filter_by(action="evidence_integrity_failed").count() == 1


def test_receipt_records_source_hash(home):
    app, ids, facility = home
    data = timesheet(("A", "2026-08-01"))
    job, _ = run_import(app, ids, facility, data)
    receipt = db.session.get(ImportReceipt, job.receipt_id)
    assert json.loads(receipt.summary_json)[0]["sha256"]


def test_credential_renewal_never_rewrites_a_lapse(facility):
    member = Staff(facility_id=facility.id, staff_id="N1", name="Nurse", role="RN", source_role="RN",
                   eligibility_status="approved", approved_at=datetime(2026, 1, 1),
                   registration_number="NMW1", registration_expiry=date(2026, 6, 30))
    db.session.add(member)
    db.session.commit()

    record_credential_gap(member, date(2027, 5, 31), today=date(2026, 7, 20))
    member.registration_expiry = date(2027, 5, 31)

    assert counts_toward_care(member, date(2026, 6, 30))
    assert not counts_toward_care(member, date(2026, 7, 1))
    assert "not evidenced" in registration_problem(member, date(2026, 7, 19))
    assert counts_toward_care(member, date(2026, 7, 20))


def test_renewal_before_lapse_records_no_gap(facility):
    member = Staff(facility_id=facility.id, staff_id="N2", name="Nurse", role="RN",
                   registration_expiry=date(2026, 6, 30))
    record_credential_gap(member, date(2027, 5, 31), today=date(2026, 6, 20))
    record_credential_gap(member, date(2027, 5, 31), today=date(2026, 7, 1))
    assert member.credential_gaps_json is None


def test_reviewer_confirms_continuity_with_a_recorded_basis(web_app):  # noqa: F811
    app, ids = web_app
    home = Facility.query.filter_by(organization_id=ids["customer"]).one()
    member = Staff(facility_id=home.id, staff_id="N3", name="Nurse", role="RN", source_role="RN",
                   registration_number="NMW3", registration_expiry=date(2027, 5, 31),
                   credential_gaps_json=json.dumps([["2026-07-01", "2026-07-19"]]))
    db.session.add(member)
    db.session.commit()
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    form = {"decision": "approved", "mapped_role": "RN", "registration_number": "NMW3",
            "registration_expiry": "2027-05-31", "eligible_from": "2026-07-01",
            "registration_continuous": "1"}
    client.post(f"/api/eligibility/{member.id}", data=form)
    db.session.refresh(member)
    assert len(member.credential_gaps) == 1
    form["reason"] = "Ahpra register shows no lapse"
    assert client.post(f"/api/eligibility/{member.id}", data=form).status_code == 200
    db.session.refresh(member)
    assert member.credential_gaps == []
    assert AuditLog.query.filter_by(action="registration_continuity_confirmed").count() == 1


def test_payroll_lines_with_distinct_categories_are_both_kept(facility):
    rows = [{"staff_id": "P1", "staff_name": "Pay", "role": "RN", "date": date(2026, 8, 1),
             "minutes": 240, "pay_category": category, "_source_row": i}
            for i, category in enumerate(("Ordinary", "Overtime"))]
    validate_shifts(rows, today=date(2026, 8, 2))
    outcome = ImportOutcome(filename="pay.csv", fingerprint="f", mapping_reused=False, spec={}, results=[])
    outcome.receipt = ImportReceipt(facility_id=facility.id)
    db.session.add(outcome.receipt)
    db.session.flush()
    _import_shifts(facility, rows, outcome, "worked")
    assert sorted(s.duration_minutes for s in Shift.query.all()) == [240, 240]


def test_duration_lines_without_a_category_still_reject_as_duplicates():
    row = {"staff_id": "P1", "date": date(2026, 8, 1), "minutes": 240}
    with pytest.raises(MappingError, match="duplicate"):
        validate_shifts([dict(row), dict(row)], today=date(2026, 8, 2))


def test_duration_lines_cannot_exceed_a_day():
    rows = [{"staff_id": "P1", "date": date(2026, 8, 1), "minutes": 800, "pay_category": c}
            for c in ("Ordinary", "Overtime")]
    with pytest.raises(MappingError, match="exceed 24 hours"):
        validate_shifts(rows, today=date(2026, 8, 2))


def test_minute_precision_is_exact_and_conservative():
    assert _whole_minutes(4.1 * 60) == 246
    assert _whole_minutes(7.33 * 60) == 439
    assert _break_whole_minutes(0.7 * 60) == 42
    assert _break_whole_minutes(0.51) == 1
    assert _break_whole_minutes(None) == 0
