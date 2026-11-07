"""Source meaning and identity must be resolved before any evidence commits."""

import copy
import json
from datetime import date, datetime, time

import pytest

from carelog.domain.eligibility import classify
from carelog.domain.compliance import detect_gaps
from carelog.ingestion import jobs
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.pipeline import ImportOutcome, _import_staff
from carelog.ingestion.reader import read_upload
from carelog.models import ImportJob, ImportReceipt, Shift, Staff, db
from carelog.storage import LocalStorage
from tests.test_corrections import SPEC, approve_everyone, home, run_import, timesheet  # noqa: F401
from tests.test_ui_boundaries import sign_in_as, web_app  # noqa: F401


def prepare(context, data, name="timesheet.csv", spec=None):
    app, ids, facility = context
    job_id = jobs.start_job(app, facility.id, [(name, data)], LocalStorage(app.config["UPLOADS_DIR"]),
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    mapping = copy.deepcopy(spec or SPEC)
    mapping["targets"][0]["sheet"] = read_upload(name, data)[0].name
    job.manual_specs_json = json.dumps({name: mapping})
    db.session.commit()
    jobs.run_job(job_id)
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    return db.session.get(ImportJob, job_id), client


def answer(job, client, kind, value):
    hold = next(h for h in json.loads(job.review_json)["impact"]["holds"] if h["kind"] == kind)
    return client.post(f"/api/import/{job.id}/resolve-hold", data={
        "kind": kind, "file": hold["file"], "sheet": hold["sheet"],
        "field": hold.get("field", ""),
        "value": value, "basis": "Checked the synthetic source's meaning",
    })


def confirm(job, client):
    assert client.post(f"/api/import/{job.id}/confirm").status_code == 200
    jobs.run_job(job.id)
    assert db.session.get(ImportJob, job.id).status == "done"


def test_status_column_cannot_be_bypassed_and_rejected_lines_never_count(home):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec["targets"][0].pop("row_filter")
    data = timesheet(*[(str(i), f"2026-08-0{i}") for i in range(1, 6)])
    lines = data.decode().splitlines()
    data = ("\n".join([lines[0]] + [line.rsplit(",", 1)[0] + "," + status
            for line, status in zip(lines[1:], ["Approved", "Rejected", "Pending", "Cancelled", "No Show"])]) + "\n").encode()
    job, client = prepare(home, data, spec=spec)
    assert job.status == "review", job.error
    review = json.loads(job.review_json)
    assert review["impact"]["shifts_imported"] == 1
    assert review["impact"]["holds"][0]["kind"] == "status"
    assert client.post(f"/api/import/{job.id}/confirm").status_code == 409
    assert answer(job, client, "status", "all").status_code == 400
    assert answer(job, client, "status", "Rejected").status_code == 400
    assert answer(job, client, "status", "Approved").status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 1
    assert Staff.query.one().staff_id == "1"


def test_status_correction_retires_previously_worked_time(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01"), ("B", "2026-08-02")))
    corrected = timesheet(("A", "2026-08-01"), ("B", "2026-08-02")).replace(
        b"B,B Name,Registered Nurse,2026-08-02,08:00,16:00,8,Approved",
        b"B,B Name,Registered Nurse,2026-08-02,08:00,16:00,8,Cancelled")
    job, client = prepare(home, corrected)
    assert json.loads(job.review_json)["impact"]["shifts_replaced"] == 2
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 1
    assert Shift.query.filter_by(evidence_type="superseded").count() == 2


@pytest.mark.parametrize("next_status", ["", "Open", "Pending"])
def test_new_or_blank_statuses_reopen_a_remembered_every_status_attestation(home, next_status):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec["targets"][0].pop("row_filter")
    job, client = prepare(home, timesheet(("A", "2026-08-01")), spec=spec)
    assert answer(job, client, "status", "all").status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    data = timesheet(("A", "2026-08-02"), ("B", "2026-08-02"))
    lines = data.decode().splitlines()
    lines[1] = lines[1].rsplit(",", 1)[0] + "," + next_status
    app, ids, facility = home
    name = "next_timesheet.csv"
    job_id = jobs.start_job(app, facility.id, [(name, ("\n".join(lines) + "\n").encode())],
                            LocalStorage(app.config["UPLOADS_DIR"]), organization_id=ids["customer"], user_id=ids["admin"])
    jobs.run_job(job_id)
    next_job = db.session.get(ImportJob, job_id)
    assert next_job.status == "review", next_job.error
    assert any(h["kind"] == "status" for h in json.loads(next_job.review_json)["impact"]["holds"])
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 409
    assert Shift.query.filter_by(evidence_type="worked").count() == 1


def test_missing_break_decision_blocks_every_mapping_path_and_worker(home):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec["targets"][0]["fields"].pop("break_minutes")
    job, client = prepare(home, timesheet(("A", "2026-08-01")), spec=spec)
    assert client.post(f"/api/import/{job.id}/confirm").status_code == 409
    assert answer(job, client, "breaks", "none").status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert Shift.query.one().break_minutes == 0

    unreviewed, _ = prepare(home, timesheet(("B", "2026-08-02")), spec=spec)
    unreviewed.status, unreviewed.approved_at = "queued", datetime.utcnow()
    db.session.commit()
    jobs.run_job(unreviewed.id)
    assert db.session.get(ImportJob, unreviewed.id).status == "failed"
    assert Shift.query.filter_by(evidence_type="worked").count() == 1


def test_colliding_person_ids_do_not_replace_or_inherit_identity(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("1001", "2026-08-01")).replace(b"1001 Name", b"Alice"))
    approve_everyone(facility)
    original = Staff.query.one()
    approval = original.approved_at
    data = timesheet(("1001", "2026-08-01")).replace(b"1001 Name", b"Bob").replace(b"08:00,16:00", b"16:00,23:00")
    job, client = prepare(home, data, name="agency_timesheet.csv")
    assert job.status == "failed" and "Alice" in job.error and "Bob" in job.error
    assert (original.name, original.approved_at, original.registration_number) == ("Alice", approval, "NMW0001")
    assert Shift.query.filter_by(evidence_type="worked").count() == 1
    assert ImportReceipt.query.count() == 1
    assert LocalStorage(app.config["UPLOADS_DIR"]).get(f"{job.storage_prefix}/agency_timesheet.csv") == data


def test_registration_collision_is_refused_without_rewriting_credentials(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    with pytest.raises(MappingError, match="different Ahpra"):
        _import_staff(facility, [{"staff_id": "A", "staff_name": "A Name", "registration_number": "OTHER"}],
                      ImportOutcome("directory.csv", "x", False, {}, []))
    assert Staff.query.one().registration_number == "NMW0001"


def test_unmapped_identity_columns_cannot_hide_a_collision(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    data = timesheet(("A", "2026-08-02")).replace(b"Approval Status", b"Approval Status,AHPRA Registration").replace(b",Approved\n", b",Approved,DIFFERENT\n")
    spec = copy.deepcopy(SPEC)
    spec["targets"][0]["fields"].pop("staff_name")
    job, _ = prepare(home, data, spec=spec)
    assert job.status == "failed" and "different Ahpra" in job.error
    assert Shift.query.filter_by(evidence_type="worked").count() == 1


def test_diagnostics_do_not_echo_proxy_credentials(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setenv("DEBUG_TOKEN", "synthetic-debug-token")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-password@proxy.example")
    monkeypatch.setattr("carelog.ingestion.analyzer.resolve_api_key", lambda: None)

    def blocked(*args, **kwargs):
        raise OSError("proxy-user:proxy-password@proxy.example")

    monkeypatch.setattr("socket.getaddrinfo", blocked)
    monkeypatch.setattr("httpx.Client", blocked)
    client = app.test_client()
    assert client.get("/api/debug/network", headers={"x-debug-token": "incorrect"}).status_code == 404
    response = client.get("/api/debug/network", headers={"x-debug-token": "synthetic-debug-token"})
    assert response.status_code == 200
    assert response.json["env"]["HTTPS_PROXY"] == "configured"
    assert b"proxy-password" not in response.data and b"proxy-user" not in response.data


def test_different_layouts_preserve_other_sources_and_refuse_overlaps(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    source = timesheet(("A", "2026-08-01")).replace(b"Approval Status", b"Approval Status,Other").replace(b",Approved\n", b",Approved,agency\n")
    job, client = prepare(home, source.replace(b"08:00,16:00", b"16:00,23:00"), name="agency_timesheet.csv")
    assert job.status == "review", job.error
    assert json.loads(job.review_json)["impact"]["shifts_replaced"] == 0
    assert json.loads(job.review_json)["impact"]["period_rows"] == 0
    confirm(job, client)
    assert Shift.query.filter_by(evidence_type="worked").count() == 2
    # A third layout cannot silently replace either source, or double count it.
    job, _ = prepare(home, source.replace(b"Other", b"Third system"))
    assert job.status == "failed" and "overlapping staff time" in job.error
    assert Shift.query.filter_by(evidence_type="worked").count() == 2


def test_future_non_roster_data_does_not_invent_a_tomorrow_roster(home):  # noqa: F811
    app, ids, facility = home
    run_import(app, ids, facility, timesheet(("A", "2026-08-01")))
    approve_everyone(facility)
    staff = Staff.query.one()
    db.session.add(Shift(facility_id=facility.id, staff_id=staff.id, date=date(2026, 8, 3),
                         start_time=time(8), end_time=time(16), evidence_type="rostered"))
    db.session.add(Shift(facility_id=facility.id, staff_id=staff.id, date=date(2026, 8, 2),
                         start_time=time(8), end_time=time(16), evidence_type="unverified"))
    db.session.commit()
    assert not any("Roster on 2026-08-02" in alert["message"] for alert in detect_gaps(facility, date(2026, 8, 1)))


@pytest.mark.parametrize("title", ["PCW - Garden Wing", "RN Garden Court", "Registered Nurse - Rostered"])
def test_incidental_parts_of_role_words_are_reviewable(title):
    assert classify(title)[1] == "pending"


@pytest.mark.parametrize("title", ["Cleaner", "Cleaning", "Gardener", "Admin RN", "RN Night Driver", "Physiotherapist"])
def test_explicit_ineligible_occupations_stay_excluded(title):
    assert classify(title)[1] == "excluded"
