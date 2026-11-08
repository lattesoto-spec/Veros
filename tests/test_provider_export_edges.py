"""Messy provider exports must be reviewable without losing valid evidence."""

import copy
import json
from datetime import date, time

import pytest

from carelog.domain.compliance import detect_gaps, forecast_quarter
from carelog.ingestion import jobs
from carelog.models import AuditLog, ImportJob, QuarterClosure, ResidentDay, Shift, Staff, db
from tests.test_corrections import SPEC, approve_everyone, home, run_import, timesheet  # noqa: F401
from tests.test_honest_results import closure_form
from tests.test_paid_is_not_worked import prepare_file
from tests.test_source_questions import answer, confirm, prepare
from tests.test_ui_boundaries import web_app  # noqa: F401


def mixed_statuses():
    lines = timesheet(*[(str(i), f"2026-08-0{i}") for i in range(1, 6)]).decode().splitlines()
    return ("\n".join([lines[0]] + [line.rsplit(",", 1)[0] + "," + status
            for line, status in zip(lines[1:], ["Approved", "Worked", "Approved", "Worked", "Rejected"])]) + "\n").encode()


def choose_statuses(job, client, values):
    hold = next(h for h in json.loads(job.review_json)["impact"]["holds"] if h["kind"] == "status")
    return client.post(f"/api/import/{job.id}/resolve-hold", data={
        "kind": "status", "file": hold["file"], "sheet": hold["sheet"],
        "values": values, "basis": "Verified both approval codes against the synthetic export specification",
    })


def test_multiple_worked_statuses_are_combined_with_or_and_remembered(home):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec["targets"][0].pop("row_filter")
    job, client = prepare(home, mixed_statuses(), spec=spec)
    assert json.loads(job.review_json)["impact"]["shifts_imported"] == 4
    assert choose_statuses(job, client, ["Approved", "Worked"]).status_code == 200
    jobs.run_job(job.id)
    review = json.loads(job.review_json)
    assert review["impact"]["holds"] == []
    filters = review["outcomes"][0]["spec"]["targets"][0]["status_filters"]
    assert len(filters) == 1 and set(filters[0]["include_values"]) == {"Approved", "Worked"}
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 4


def test_new_status_reopens_review_even_when_an_old_filter_would_drop_every_row(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    data = timesheet(("A", "2026-08-02")).replace(b",Approved\n", b",Worked\n")
    job_id = prepare_file(app, ids, facility, "next_timesheet.csv", data)
    job = db.session.get(ImportJob, job_id)
    _, client = prepare(home, timesheet(("B", "2026-08-03")))
    assert job.status == "review", job.error
    hold = next(h for h in json.loads(job.review_json)["impact"]["holds"] if h["kind"] == "status")
    assert set(hold["options"]) == {"Approved", "Worked"}
    assert hold["counts"] == {"Approved": 0, "Worked": 1}
    assert client.post(f"/api/import/{job.id}/confirm").status_code == 409
    assert choose_statuses(job, client, ["Approved", "Worked"]).status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 2


@pytest.mark.parametrize("comment", ["pending payroll query resolved", "void previous note", "cancelled meeting attended afterwards"])
def test_status_words_inside_comments_do_not_drop_worked_rows(home, comment):  # noqa: F811
    original = timesheet(("A", "2026-08-01"), ("B", "2026-08-02")).decode().splitlines()
    data = (original[0] + ",Comments\n" + original[1] + ",\n" + original[2] + "," + comment + "\n").encode()
    job, client = prepare(home, data)
    assert job.status == "review", job.error
    assert json.loads(job.review_json)["impact"]["holds"] == []
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 2


def test_a_categorical_comment_can_be_reviewed_as_not_a_status(home):  # noqa: F811
    original = timesheet(("A", "2026-08-01"), ("B", "2026-08-02")).decode().splitlines()
    data = (original[0] + ",Comments\n" + original[1] + ",\n" + original[2] + ",Rejected\n").encode()
    job, client = prepare(home, data)
    assert job.status == "review", job.error
    hold = next(h for h in json.loads(job.review_json)["impact"]["holds"] if h["kind"] == "status")
    assert hold["can_ignore"] is True
    assert answer(job, client, "status", "not_status").status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 2
    assert AuditLog.query.filter_by(action="import_hold_resolved").count() == 1


def test_verified_name_alias_preserves_identity_and_does_not_link_another_person(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")).replace(b"A Name", b"Jane Smith"))
    approve_everyone(facility)
    member = Staff.query.one()
    old_approval = member.approved_at
    data = timesheet(("A", "2026-08-02")).replace(b"A Name", b'"SMITH, Jane"')
    job, client = prepare(home, data)
    assert job.status == "failed" and "name-format" in job.error
    endpoint = f"/api/eligibility/{member.id}/name-alias"
    assert client.post(endpoint, data={"alias": "Bob Smith", "basis": "Another worker"}).status_code == 400
    assert client.post(endpoint, data={"alias": "SMITH, Jane"}).status_code == 400
    assert client.post(endpoint, data={"alias": "SMITH, Jane", "basis": "Same employee and registration verified"}).status_code == 200
    corrected, _ = prepare(home, data)
    assert corrected.status == "review", corrected.error
    confirm(corrected, client)
    assert (member.name, member.registration_number, member.approved_at) == ("Jane Smith", "NMW0001", old_approval)
    assert Staff.query.count() == 1 and Shift.query.filter_by(evidence_type="worked").count() == 2
    actions = {row["action"] for row in client.get("/api/audit").json["data"]["decisions"]}
    assert "staff_name_alias_recorded" in actions
    foreign_member = Staff(facility_id=ids["other_facility"], staff_id="A", name="Jane Smith", role="RN")
    db.session.add(foreign_member)
    db.session.commit()
    foreign = client.post(f"/api/eligibility/{foreign_member.id}/name-alias", data={"alias": "SMITH, Jane", "basis": "x"})
    assert foreign.status_code == 404


def test_ended_quarter_has_no_forecast_and_assesses_its_missing_tail(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _: date(2026, 11, 7))
    run_import(app, ids, facility, timesheet(("A", "2026-08-25")))
    _, client = prepare(home, timesheet(("B", "2026-08-26")))
    data = client.get("/api/compliance").json["data"]
    assert data["forecast"] is None
    assert "2026-08-26" in data["tests"]["missing_worked_dates"]
    assert "2026-09-30" in data["tests"]["missing_worked_dates"]
    assert data["assessment_end"] == "2026-09-30" and data["tests"]["passed"] is None
    assert client.get("/api/scenarios").json["data"]["base"] is None
    assert client.post("/api/scenarios", data={"occupancy_delta": "1"}).status_code == 400


def test_closure_requires_missing_date_acknowledgement_and_a_current_snapshot(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr("carelog.services.closure.facility_today", lambda _: date(2026, 11, 7))
    run_import(app, ids, facility, timesheet(("A", "2026-08-25")))
    _, client = prepare(home, timesheet(("B", "2026-08-26")))
    approve_everyone(facility)  # Pending staff minutes do not evidence a date.
    form = closure_form(client, "Board draft")
    preview = client.get("/api/reports/closure-preview?q=2026-Q1").json["data"]["preview"]
    assert len(preview["missing_worked_dates"]) == 91 and "2026-09-30" in preview["missing_worked_dates"]
    without_ack = {k: v for k, v in form.items() if k != "acknowledge_limitations"}
    assert client.post("/api/reports/close", data=without_ack).status_code == 409
    assert QuarterClosure.query.count() == 0
    member = Staff.query.one()
    member.eligibility_reason = "A later review changed the evidence state"
    db.session.commit()
    assert client.post("/api/reports/close", data=form).status_code == 409
    assert QuarterClosure.query.count() == 0
    assert client.post("/api/reports/close", data=closure_form(client, "Reviewed provisional board draft")).status_code == 200
    closure = QuarterClosure.query.one()
    figures = json.loads(closure.figures_json)
    assert figures["assessment_end"] == "2026-09-30" and figures["limitations_acknowledged"] is True


def test_current_quarter_forecast_does_not_project_already_elapsed_missing_dates(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _: date(2026, 8, 5))
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)  # Only countable time evidences a date.
    forecast = forecast_quarter(facility, date(2026, 8, 1))
    assert forecast["missing_elapsed_days"] == 3
    assert forecast["days_remaining"] == 57
    assert forecast["provisional"] is True and forecast["on_track"] is None


def test_alert_precision_and_ended_period_wording_are_consistent(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _facility: date(2026, 7, 1))
    ResidentDay.query.filter_by(facility_id=facility.id).delete()
    for index in range(10):
        db.session.add(ResidentDay(facility_id=facility.id, resident_id=f"R{index}", date=date(2026, 7, 1), occupied=True, service_type="permanent"))
    nurses = Staff(facility_id=facility.id, staff_id="RN", name="Nurse", role="RN", source_role="Registered Nurse", eligibility_status="approved", registration_number="SYNTHETIC", registration_expiry=date(2027, 12, 31))
    db.session.add(nurses)
    db.session.flush()
    db.session.add(Shift(facility_id=facility.id, staff_id=nurses.id, date=date(2026, 7, 1), start_time=time(0), end_time=time(0), duration_minutes=395, coverage_known=False, evidence_type="worked"))
    for index, minutes in enumerate((480, 480, 480, 311)):
        member = Staff(facility_id=facility.id, staff_id=f"P{index}", name=f"Carer {index}", role="PCW", source_role="Personal Care Worker", eligibility_status="approved")
        db.session.add(member)
        db.session.flush()
        db.session.add(Shift(facility_id=facility.id, staff_id=member.id, date=date(2026, 7, 1), start_time=time(0), end_time=time(0), duration_minutes=minutes, coverage_known=False, evidence_type="worked"))
    db.session.commit()
    messages = [row["message"] for row in detect_gaps(facility, date(2026, 7, 1))]
    assert any("214.6 of 215.0" in message and "4 total minutes short" in message for message in messages)
    assert any("39.5 of 39.6" in message and "1 total minutes short" in message for message in messages)
    assert all("this quarter" not in message for message in messages)
