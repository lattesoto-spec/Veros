"""Leave on a payroll sheet must never become care minutes."""
import json
from datetime import date, datetime

import pytest

from carelog.domain.compliance import quarter_totals
from carelog.ingestion import jobs
from carelog.ingestion.mapping import parse_date_value
from carelog.ingestion.paycodes import EXCLUDED, UNKNOWN, WORKED, pay_column, treatment
from carelog.ingestion.pipeline import ImportOutcome, _import_resident_days, restore_census_rows
from carelog.ingestion.presets import matching_spec
from carelog.ingestion.reader import Sheet, read_upload
from carelog.models import (
    AuditLog, Facility, ImportJob, ImportReceipt, PayCategoryRule, ResidentDay, Staff, db,
)
from carelog.storage import LocalStorage
from tests.test_ui_boundaries import sign_in_as, web_app  # noqa: F401

PAYROLL = (
    "Staff ID,Staff Name,Role,Work Date,Paid Hours,Pay Code\n"
    "A,A Name,Registered Nurse,2026-08-03,8,Ordinary\n"
    "A,A Name,Registered Nurse,2026-08-04,8,Annual Leave\n"
    "A,A Name,Registered Nurse,2026-08-05,8,Personal Leave\n"
    "A,A Name,Registered Nurse,2026-08-06,8,Workers Compensation\n"
    "A,A Name,Registered Nurse,2026-08-07,8,Mystery Code 7\n"
).encode()


def spec_without_pay_column(name, data):
    return {name: {"targets": [{
        "kind": "shifts",
        "sheet": read_upload(name, data)[0].name,
        "fields": {
            "staff_id": {"column": "Staff ID"},
            "role": {"column": "Role", "normalize": "role"},
            "date": {"column": "Work Date", "parse": "date"},
            "minutes": {"column": "Paid Hours", "parse": "number", "multiply": 60},
        },
    }], "reasoning": "test"}}


@pytest.fixture()
def prepared(web_app, tmp_path, monkeypatch):  # noqa: F811
    app, ids = web_app
    app.config["UPLOADS_DIR"] = str(tmp_path)
    monkeypatch.setattr(jobs, "dispatch_job", lambda *args: None)
    monkeypatch.setattr(jobs, "_progress", lambda *args: None)
    home = Facility.query.filter_by(organization_id=ids["customer"]).one()
    job_id = jobs.start_job(app, home.id, [("payroll_timesheet.csv", PAYROLL)], LocalStorage(str(tmp_path)),
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, job_id)
    job.manual_specs_json = json.dumps(spec_without_pay_column("payroll_timesheet.csv", PAYROLL))
    db.session.commit()
    jobs.run_job(job_id)
    client = app.test_client()
    sign_in_as(client, ids["admin"])
    return app, ids, job_id, client, home


def worked_minutes(home):
    for member in Staff.query.filter_by(facility_id=home.id):
        member.eligibility_status, member.approved_at = "approved", datetime(2026, 8, 1)
        member.eligible_from = date(2026, 1, 1)
        member.registration_number, member.registration_expiry = "NMW1", date(2027, 1, 1)
    db.session.commit()
    return quarter_totals(home.id, date(2026, 7, 1), date(2026, 9, 30))["total_minutes"]


def test_unmapped_pay_column_is_read_and_unknown_codes_block_confirmation(prepared):
    app, ids, job_id, client, home = prepared
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    categories = {c["category"]: c for c in review["outcomes"][0]["results"][0]["pay_categories"]}
    assert categories["Ordinary"]["treatment"] == WORKED
    assert categories["Annual Leave"]["treatment"] == EXCLUDED and categories["Annual Leave"]["minutes"] == 480
    assert categories["Workers Compensation"]["treatment"] == EXCLUDED
    assert categories["Mystery Code 7"]["treatment"] == UNKNOWN
    assert review["impact"]["pay_unknown"] == ["Mystery Code 7"]
    assert review["impact"]["shifts_imported"] == 1
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 409


def test_decision_is_audited_remembered_and_only_then_counts(prepared):
    app, ids, job_id, client, home = prepared
    form = {"decisions": json.dumps({"Mystery Code 7": "worked"})}
    assert client.post(f"/api/import/{job_id}/pay-categories", data=form).status_code == 400
    form["basis"] = "Payroll manager confirmed code 7 is worked broken-shift time"
    assert client.post(f"/api/import/{job_id}/pay-categories", data=form).status_code == 200
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "review" and json.loads(job.review_json)["impact"]["pay_unknown"] == []
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 200
    jobs.run_job(job_id)
    assert db.session.get(ImportJob, job_id).status == "done"
    assert worked_minutes(home) == 960  # Ordinary and the decided code; never the leave
    rule = PayCategoryRule.query.one()
    assert (rule.category, rule.treatment, rule.organization_id) == ("mystery code 7", "worked", ids["customer"])
    assert AuditLog.query.filter_by(action="pay_categories_decided").count() == 1


def test_provider_can_overrule_a_built_in_treatment_but_only_with_a_basis(prepared):
    app, ids, job_id, client, home = prepared
    form = {"decisions": json.dumps({"Mystery Code 7": "excluded", "Annual Leave": "worked"}), "basis": "Mislabelled in payroll"}
    assert client.post(f"/api/import/{job_id}/pay-categories", data=form).status_code == 200
    jobs.run_job(job_id)
    categories = json.loads(db.session.get(ImportJob, job_id).review_json)["outcomes"][0]["results"][0]["pay_categories"]
    assert {c["category"]: c["treatment"] for c in categories}["Annual Leave"] == WORKED


def test_unknown_categories_are_not_worked_by_default():
    assert treatment("Ordinary", {}) == WORKED
    assert treatment("ordinary  hours", {}) == WORKED
    assert treatment("Sick Leave", {}) == EXCLUDED
    assert treatment("Uniform Allowance", {}) == EXCLUDED
    assert treatment("Code 7", {}) == UNKNOWN
    assert treatment("", {}) == UNKNOWN
    assert treatment("code 7", {"code 7": WORKED}) == WORKED


def test_pay_columns_are_recognised():
    assert pay_column(["Staff", "Pay Code"]) == "Pay Code"
    assert pay_column(["Staff", "Earnings Type"]) == "Earnings Type"
    assert pay_column(["Staff", "Hours"]) is None


@pytest.mark.parametrize("value", ["03/04/2026", "03-04-2026", "03.04.2026", "03/04/2026 07:00", "03-04-26"])
def test_every_ambiguous_date_needs_a_declared_format(value):
    with pytest.raises(ValueError, match="ambiguous"):
        parse_date_value(value)
    assert parse_date_value(value.split(" ")[0], "%d/%m/%Y".replace("/", value[2]).replace("Y", "y" if value.endswith("-26") else "Y")) == date(2026, 4, 3)


@pytest.mark.parametrize("value,expected", [("31-08-2026", date(2026, 8, 31)), ("2026-04-03", date(2026, 4, 3))])
def test_unambiguous_dates_still_parse(value, expected):
    assert parse_date_value(value) == expected


def test_a_daily_care_summary_can_no_longer_assert_occupancy():
    headers = ["Date", "Resident ID", "Resident Name", "RN Minutes Delivered", "Total Care Minutes Delivered"]
    assert matching_spec([Sheet(name="Care", headers=headers, rows=[])]) is None


def test_analyzer_cannot_fix_occupancy_to_a_constant():
    from carelog.ingestion.analyzer import _clean_spec
    with pytest.raises(ValueError, match="never a fixed value"):
        _clean_spec({"targets": [{"kind": "resident_days", "fields": {"occupied": {"source": "constant", "value": True}}}]})


def census_row(**values):
    return {"resident_id": "R1", "date": date(2026, 8, 1), "occupied": True, **values}


def test_census_change_is_listed_kept_and_undone_with_its_import(facility):
    first, second = ImportReceipt(facility_id=facility.id), ImportReceipt(facility_id=facility.id)
    db.session.add_all([first, second])
    db.session.flush()
    outcome = ImportOutcome("a", "f", False, {}, [], receipt=first)
    _import_resident_days(facility, [census_row(service_type="private", ancc_class="Class 5", occupied=False, exclusion_reason="Private")], outcome)
    db.session.flush()

    later = ImportOutcome("b", "f", False, {}, [], receipt=second)
    _import_resident_days(facility, [census_row(service_type="an_acc")], later)
    db.session.flush()
    row = ResidentDay.query.one()
    assert later.census_changed == 1
    changed = {c["field"]: c for c in later.census_changes[0]["changes"]}
    assert changed["occupied"]["from"] is False and changed["service_type"]["to"] == "an_acc"
    # A blank incoming field cannot erase what was verified.
    assert row.ancc_class == "Class 5" and row.exclusion_reason == "Private"

    assert restore_census_rows(second.id) == (1, 0)
    db.session.flush()
    assert (row.occupied, row.service_type, row.import_receipt_id) == (False, "private", first.id)
    assert restore_census_rows(first.id) == (0, 1)
