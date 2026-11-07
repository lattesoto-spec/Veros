"""Unfamiliar files: nothing personal leaves, and a person can always finish."""
import json
from datetime import date

import pytest

from carelog import auth
from carelog.ingestion import jobs, pipeline
from carelog.ingestion.analyzer import AnalyzerError, _structure_payload
from carelog.ingestion.manual import ALWAYS_TRUE, build_spec
from carelog.ingestion.masking import mask_header, mask_problem, mask_value
from carelog.ingestion.reader import Sheet, read_upload
from carelog.models import AuditLog, Facility, FormatMapping, ImportJob, Organization, Shift, db
from carelog.storage import LocalStorage
from tests.test_ui_boundaries import sign_in_as, web_app  # noqa: F401

BANNERED = (
    "Banksia House timesheet,,,,,\n"
    "Printed by Jane Smith,,,,,\n"
    "Emp,Who,Position,Day,In,Out\n"
    "E1,Jane Smith,Registered Nurse,12/08/2026,07:00,15:00\n"
    "E2,Omar Haddad,Enrolled Nurse,13/08/2026,07:00,15:00\n"
).encode()


def mapping_payload(header_row=2):
    return {
        "bannered.csv": [{
            "sheet": "CSV",
            "kind": "shifts",
            "header_row": header_row,
            "date_format": "%d/%m/%Y",
            "fields": {
                "staff_id": "Emp", "staff_name": "Who", "role": "Position", "date": "Day",
                "start_time": "In", "end_time": "Out", "is_direct_care": ALWAYS_TRUE,
            },
        }],
    }


@pytest.fixture()
def uploaded(web_app, tmp_path, monkeypatch):  # noqa: F811
    app, ids = web_app
    app.config["UPLOADS_DIR"] = str(tmp_path)
    monkeypatch.setenv("IMPORT_WORKER", "thread")
    monkeypatch.setattr(jobs, "dispatch_job", lambda *args: None)
    monkeypatch.setattr(jobs, "_progress", lambda *args: None)
    home = Facility.query.filter_by(organization_id=ids["customer"]).one()
    job_id = jobs.start_job(app, home.id, [("bannered.csv", BANNERED)], LocalStorage(str(tmp_path)),
                            organization_id=ids["customer"], user_id=ids["admin"])
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    return app, ids, job_id, client, monkeypatch


def sheet_name():
    return read_upload("bannered.csv", BANNERED)[0].name


def payload(header_row=2):
    data = mapping_payload(header_row)
    data["bannered.csv"][0]["sheet"] = sheet_name()
    return {"mapping": json.dumps(data)}


def test_only_structure_reaches_the_external_service():
    sheet = Sheet(
        name="Time",
        headers=["Employee Number", "Resident Name", "Ahpra", "Status", "Jane Smith"],
        rows=[{"Employee Number": "E10293", "Resident Name": "Margaret O'Brien", "Ahpra": "NMW0001234567",
               "Status": "Approved", "Jane Smith": "Fall in bathroom"}],
        raw_rows=[["Banksia House", "", "", "", ""],
                  ["Employee Number", "Resident Name", "Ahpra", "Status", "Jane Smith"],
                  ["E10293", "Margaret O'Brien", "NMW0001234567", "Approved", "Fall in bathroom"]],
        header_row_index=1,
    )
    sent = _structure_payload([sheet])
    for private in ("Margaret", "O'Brien", "E10293", "NMW0001234567", "Fall in bathroom", "Jane Smith", "Banksia House"):
        assert private not in sent, private
    assert "Employee Number" in sent and "Resident Name" in sent and "Approved" in sent


def test_masking_keeps_shape_for_parsing_decisions():
    assert mask_value("12/08/2026") == "99/99/9999"
    assert mask_value("Yes") == "Yes"
    assert mask_value("Jane Smith") == "Aaaa Aaaaa"
    assert mask_header("Staff Name") == "Staff Name"
    assert mask_header("Emp No") == "Emp No"
    assert mask_problem("Row 4: date: unparseable date: 'Jane Smith'") == "Row 4: date: unparseable date: [value]"


def test_external_mapping_is_off_by_default_and_the_job_waits_for_a_person(uploaded):
    app, ids, job_id, client, monkeypatch = uploaded
    def never(*args, **kwargs):
        raise AssertionError("no external call without opt-in")
    monkeypatch.setattr(pipeline, "generate_mapping_spec", never)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    jobs.run_job(job_id)
    assert db.session.get(ImportJob, job_id).status == "mapping"
    assert AuditLog.query.filter_by(action="external_disclosure").count() == 0
    assert Shift.query.count() == 0


def test_opted_in_disclosure_is_recorded_even_when_the_service_fails(uploaded):
    app, ids, job_id, client, monkeypatch = uploaded
    db.session.get(Organization, ids["customer"]).ai_mapping_enabled = True
    db.session.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    def fails(sheets, filename, on_disclosure=None):
        on_disclosure("synthetic-model")
        raise AnalyzerError("service down")
    monkeypatch.setattr(pipeline, "generate_mapping_spec", fails)
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "mapping" and "Choose its columns" in job.error
    record = AuditLog.query.filter_by(action="external_disclosure").one()
    assert "no data values disclosed" in record.detail and record.organization_id == ids["customer"]


def test_reviewer_corrects_the_header_and_finishes_without_any_ai(uploaded):
    app, ids, job_id, client, _ = uploaded
    jobs.run_job(job_id)
    view = client.get(f"/api/import/{job_id}/mapping").json
    assert view["view"] == "import_mapping"
    sheet = view["data"]["files"][0]["sheets"][0]
    assert sheet["grid"][1]["cells"][0] == "Printed by Jane Smith"
    assert sheet["candidates"][2]["headers"][:2] == ["Emp", "Who"]
    assert sheet["candidates"][2]["examples"]["Who"] == "Jane Smith"
    assert view["data"]["catalogue"]["kinds"]["shifts"]["fields"][0]["name"] == "staff_id"

    assert client.post(f"/api/import/{job_id}/mapping", data=payload()).status_code == 200
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "review", job.error
    status = client.get(f"/api/import/status/{job_id}").json["data"]["job"]
    columns = {c["field"]: c for c in status["review"]["files"][0]["columns"]}
    assert columns["start_time"]["source"] == "In"
    assert "Fixed value" in columns["is_direct_care"]["source"]
    assert Shift.query.count() == 0

    assert client.post(f"/api/import/{job_id}/confirm").status_code == 200
    jobs.run_job(job_id)
    assert db.session.get(ImportJob, job_id).status == "done"
    assert sorted(s.date for s in Shift.query.all()) == [date(2026, 8, 12), date(2026, 8, 13)]
    assert FormatMapping.query.filter_by(organization_id=ids["customer"]).count() == 1
    assert AuditLog.query.filter_by(action="import_mapping_saved").count() == 1


def test_mapping_can_be_corrected_from_the_review_screen(uploaded):
    app, ids, job_id, client, _ = uploaded
    jobs.run_job(job_id)
    client.post(f"/api/import/{job_id}/mapping", data=payload())
    jobs.run_job(job_id)
    view = client.get(f"/api/import/{job_id}/mapping").json["data"]["files"][0]
    assert view["known"] is True
    mapping = view["sheets"][0]["mapping"]
    assert mapping["fields"]["start_time"] == "In" and mapping["fields"]["is_direct_care"] == ALWAYS_TRUE
    assert mapping["date_format"] == "%d/%m/%Y" and mapping["header_row"] == 2
    assert client.post(f"/api/import/{job_id}/mapping", data=payload()).status_code == 200
    assert db.session.get(ImportJob, job_id).status == "queued"


def test_invalid_mapping_is_refused_with_the_reason_and_nothing_moves(uploaded):
    app, ids, job_id, client, _ = uploaded
    jobs.run_job(job_id)
    bad = mapping_payload()
    bad["bannered.csv"][0].update(sheet=sheet_name(), date_format="")
    response = client.post(f"/api/import/{job_id}/mapping", data={"mapping": json.dumps(bad)})
    assert response.status_code == 400
    assert "ambiguous date" in response.json["error"]
    assert db.session.get(ImportJob, job_id).status == "mapping"


def test_mapping_is_tenant_scoped_and_needs_record_access(uploaded):
    app, ids, job_id, client, _ = uploaded
    jobs.run_job(job_id)
    outsider = app.test_client()
    sign_in_as(outsider, ids["outsider"])
    assert outsider.get(f"/api/import/{job_id}/mapping").status_code == 404
    user = db.session.get(auth.User, ids["admin"])
    user.role = "clinical_manager"
    db.session.commit()
    assert client.get(f"/api/import/{job_id}/mapping").status_code == 403


@pytest.mark.parametrize("change,message", [
    ({"kind": "unknown"}, "choose what this sheet contains"),
    ({"fields": {"staff_id": "Emp"}}, "choose a column"),
    ({"fields": {"staff_id": "Emp", "staff_name": "Missing", "role": "Position", "date": "Day",
                 "start_time": "In", "end_time": "Out", "is_direct_care": ALWAYS_TRUE}}, "not in the chosen header row"),
    ({"filter_column": "Position"}, "needs both"),
])
def test_build_spec_names_each_problem(change, message):
    sheets = read_upload("bannered.csv", BANNERED)
    mapping = {**mapping_payload()["bannered.csv"][0], "sheet": sheets[0].name, **change}
    spec, problems = build_spec([mapping], sheets)
    assert spec is None and any(message in problem for problem in problems)


def test_shift_mapping_needs_a_complete_time_or_duration():
    sheets = read_upload("bannered.csv", BANNERED)
    fields = {"staff_id": "Emp", "role": "Position", "date": "Day", "is_direct_care": ALWAYS_TRUE}
    base = {"sheet": sheets[0].name, "kind": "shifts", "header_row": 2, "date_format": "%d/%m/%Y"}
    assert any("start and finish" in p for p in build_spec([{**base, "fields": fields}], sheets)[1])
    assert any("both the start" in p for p in build_spec([{**base, "fields": {**fields, "start_time": "In"}}], sheets)[1])


def test_only_administrators_decide_external_mapping_and_it_is_audited(web_app):  # noqa: F811
    app, ids = web_app
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    assert client.get("/api/settings").json["data"]["organization"]["ai_mapping_enabled"] is False
    assert client.post("/api/settings/ai-mapping", data={"enabled": "1"}).status_code == 400
    assert client.post("/api/settings/ai-mapping", data={"enabled": "1", "basis": "Privacy review 2026-11"}).status_code == 200
    assert db.session.get(Organization, ids["customer"]).ai_mapping_enabled is True
    assert "Privacy review 2026-11" in AuditLog.query.filter_by(action="ai_mapping_changed").one().detail
    user = db.session.get(auth.User, ids["admin"])
    user.role = "facility_manager"
    db.session.commit()
    assert client.post("/api/settings/ai-mapping", data={"enabled": "0", "basis": "x"}).status_code == 403
