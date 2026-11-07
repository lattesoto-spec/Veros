"""A half-hour break is thirty minutes in every supported source notation."""

import json
from datetime import date

import pytest

from carelog.domain.compliance import quarter_totals
from carelog.ingestion import jobs
from carelog.ingestion.durations import duration_minutes
from carelog.ingestion.manual import ALWAYS_TRUE, build_spec
from carelog.ingestion.mapping import _extract_field
from carelog.ingestion.reader import read_upload
from carelog.models import ImportJob, Shift, Staff, db
from tests.test_corrections import approve_everyone, home, run_import, timesheet  # noqa: F401
from tests.test_source_questions import answer, confirm, prepare
from tests.test_ui_boundaries import web_app  # noqa: F401


@pytest.mark.parametrize("raw,unit,expected", [
    ("0:30", "h:mm", 30), ("0:30", "minutes", 30), ("0:30", "hours", 30),
    ("00:30:00", "h:mm", 30), ("7:30", "minutes", 450),
    ("0.5", "hours", 30), ("30", "minutes", 30), ("4.1", "hours", 246),
])
def test_duration_notation_has_one_unambiguous_minute_value(raw, unit, expected):
    assert duration_minutes(raw, unit) == expected


@pytest.mark.parametrize("raw,unit", [("0:60", "h:mm"), ("0:30:60", "h:mm"), ("0.5", "h:mm"),
                                      ("-1", "minutes"), ("nan", "hours"), ("inf", "minutes"), ("0,5", "hours")])
def test_invalid_duration_notation_is_rejected(raw, unit):
    with pytest.raises(ValueError):
        duration_minutes(raw, unit)


def test_duration_unit_conversion_is_not_applied_twice():
    assert _extract_field({"column": "Break", "parse": "number", "unit": "hours", "multiply": 60},
                          {"Break": "0.5"}, ["Break"], minute_field=True) == 30
    with pytest.raises(ValueError, match="conflicts"):
        _extract_field({"column": "Break", "parse": "number", "unit": "minutes", "multiply": 60},
                       {"Break": "0.5"}, ["Break"], minute_field=True)


def manual_spec(data, unit):
    sheets = read_upload("actual_worked.csv", data)
    mapping = {"sheet": sheets[0].name, "header_row": 0, "kind": "shifts", "fields": {
        "staff_id": "Emp", "staff_name": "Person", "role": "Position", "date": "Day",
        "start_time": "In", "end_time": "Out", "break_minutes": "Pause", "is_direct_care": ALWAYS_TRUE,
    }, "units": {"break_minutes": unit}}
    spec, problems = build_spec([mapping], sheets)
    return spec, problems


@pytest.mark.parametrize("raw,unit", [("0:30", "h:mm"), ("0.5", "hours"), ("30", "minutes")])
def test_manual_mapping_keeps_480_minutes_from_a_510_minute_shift(home, raw, unit):  # noqa: F811
    data = ("Emp,Person,Position,Day,In,Out,Pause\n"
            f"A,A Name,Registered Nurse,2026-08-01,07:00,15:30,{raw}\n").encode()
    spec, problems = manual_spec(data, unit)
    assert not problems
    spec["targets"][0]["verified_source"] = "Synthetic actual clock evidence"
    job, client = prepare(home, data, name="actual_worked.csv", spec=spec)
    assert job.status == "review", job.error
    assert json.loads(job.review_json)["impact"]["holds"] == []
    confirm(job, client)
    assert Shift.query.one().break_minutes == 30
    app, ids, facility = home
    approve_everyone(facility)
    assert quarter_totals(facility.id, date(2026, 8, 1), date(2026, 8, 1))["total_minutes"] == 480


def test_a_manual_break_column_requires_a_unit():
    data = b"Emp,Person,Position,Day,In,Out,Pause\nA,A Name,RN,2026-08-01,07:00,15:30,0.5\n"
    spec, problems = manual_spec(data, "")
    assert spec is None and any("choose minutes, hours or h:mm" in problem for problem in problems)


def test_a_blank_break_is_not_silently_treated_as_zero():
    data = b"Emp,Person,Position,Day,In,Out,Pause\nA,A Name,RN,2026-08-01,07:00,15:30,\n"
    spec, problems = manual_spec(data, "minutes")
    assert spec is None and any("break evidence is blank" in problem for problem in problems)


@pytest.mark.parametrize("raw,expected", [("0.5", 30), ("1", 60), ("0:30", 30)])
def test_legacy_breaks_cannot_commit_without_unit_review(home, raw, expected):  # noqa: F811
    data = f"Emp,Person,Position,Day,In,Out,Pause\nA,A Name,RN,2026-08-01,07:00,15:30,{raw}\n".encode()
    spec, problems = manual_spec(data, "minutes")
    rule = spec["targets"][0]["fields"]["break_minutes"]
    rule["parse"] = "number"
    rule.pop("unit")
    spec["targets"][0]["verified_source"] = "Synthetic clock evidence"
    job, client = prepare(home, data, name="actual_worked.csv", spec=spec)
    assert job.status == "review", job.error
    assert client.post(f"/api/import/{job.id}/confirm").status_code == 409
    assert answer(job, client, "time_units", "hours").status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert Shift.query.one().break_minutes == expected


def test_partial_eligibility_decisions_preserve_credentials_and_record_review(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    member = Staff.query.one()
    _, client = prepare(home, timesheet(("B", "2026-08-02")))
    previous_expiry = member.registration_expiry
    assert client.post(f"/api/eligibility/{member.id}", data={"decision": "pending"}).status_code == 200
    assert member.registration_expiry == previous_expiry
    assert member.approved_at is None and member.approved_by_user_id is None
    assert member.reviewed_at is not None and member.reviewed_by_user_id == ids["admin"]
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    assert member.eligibility_status == "pending"  # importing cannot undo the review


def test_failed_approval_returns_400_and_preserves_previous_decision(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    member = Staff.query.one()
    _, client = prepare(home, timesheet(("B", "2026-08-02")))
    response = client.post(f"/api/eligibility/{member.id}", data={
        "decision": "approved", "eligible_from": "2026-08-01", "reason": "Synthetic review",
    })
    assert response.status_code == 400 and "Cannot approve" in response.json["error"]
    assert member.eligibility_status == "approved"  # original exact-title classification
    assert member.approved_at is None and member.reviewed_at is None
    assert member.eligible_from is None


def test_dashboard_identifies_an_ended_quarter_and_missing_current_evidence(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr("carelog.api.monitoring.facility_today", lambda _: date(2026, 11, 7))
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _: date(2026, 11, 7))
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    _, client = prepare(home, timesheet(("B", "2026-08-02")))
    data = client.get("/api/dashboard").json["data"]
    assert data["historical_period"] is True and data["current_q_start"] == "2026-10-01"
    assert data["tests"]["ended_quarter"] is True
    assert data["tests"]["totals"]["end"] == "2026-09-30"
    assert "2026-09-30" in data["tests"]["missing_worked_dates"]
    assert data["tests"]["passed"] is None
    run_import(app, ids, facility, timesheet(("A", "2026-11-01")))
    data = client.get("/api/dashboard").json["data"]
    assert data["historical_period"] is False and data["tests"]["ended_quarter"] is False
