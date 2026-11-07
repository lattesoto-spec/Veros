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
            "minutes": {"column": "Paid Hours", "parse": "number", "multiply": 60, "unit": "hours"},
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


def test_known_leave_cannot_be_decided_into_worked_time(prepared):
    app, ids, job_id, client, home = prepared
    form = {"decisions": json.dumps({"Annual Leave": "worked"}), "basis": "Mislabelled in payroll"}
    assert client.post(f"/api/import/{job_id}/pay-categories", data=form).status_code == 400
    assert treatment("Annual Leave", {"annual leave": WORKED}) == EXCLUDED


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


@pytest.mark.parametrize("header", ["Payroll Category", "Earnings Rate", "Wage Category", "Pay Description"])
def test_leave_is_found_by_its_values_whatever_the_column_is_called(header):
    rows = [{"Name": "A", header: value} for value in ("Ordinary", "Annual Leave", "Personal Leave")]
    assert pay_column(["Name", header], rows, mapped={"Name"}) == header


def test_a_notes_column_or_role_is_not_mistaken_for_pay_categories():
    notes = [{"Notes": f"Covered sick leave for {i}"} for i in range(50)]
    assert pay_column(["Notes"], notes) is None
    roles = [{"Department": "Personal Care Worker"}]
    assert pay_column(["Department"], roles) is None
    assert treatment("Personal Care", {}) == UNKNOWN


def test_a_line_corrected_to_leave_replaces_the_worked_row(prepared):
    app, ids, job_id, client, home = prepared
    client.post(f"/api/import/{job_id}/pay-categories", data={
        "decisions": json.dumps({"Mystery Code 7": "excluded"}), "basis": "Allowance"})
    jobs.run_job(job_id)
    client.post(f"/api/import/{job_id}/confirm")
    jobs.run_job(job_id)
    assert worked_minutes(home) == 480

    corrected = PAYROLL.replace(b"2026-08-03,8,Ordinary", b"2026-08-03,8,Annual Leave").replace(b"Mystery Code 7", b"Ordinary")
    second = jobs.start_job(app, home.id, [("payroll_timesheet.csv", corrected)], LocalStorage(app.config["UPLOADS_DIR"]),
                            organization_id=ids["customer"], user_id=ids["admin"])
    job = db.session.get(ImportJob, second)
    job.manual_specs_json = json.dumps(spec_without_pay_column("payroll_timesheet.csv", corrected))
    db.session.commit()
    jobs.run_job(second)
    review = json.loads(db.session.get(ImportJob, second).review_json)
    assert review["impact"]["shifts_replaced"] == 1
    client.post(f"/api/import/{second}/confirm")
    jobs.run_job(second)
    assert worked_minutes(home) == 480  # The 7 August line, not the old 3 August one.


def test_deleting_a_duplicate_census_import_leaves_the_census_alone(facility):
    first, second = ImportReceipt(facility_id=facility.id), ImportReceipt(facility_id=facility.id)
    db.session.add_all([first, second])
    db.session.flush()
    rows = [census_row(service_type="permanent")]
    _import_resident_days(facility, rows, ImportOutcome("a", "f", False, {}, [], receipt=first))
    db.session.flush()
    duplicate = ImportOutcome("b", "f", False, {}, [], receipt=second)
    _import_resident_days(facility, rows, duplicate)
    db.session.flush()
    assert duplicate.census_changed == 0
    assert restore_census_rows(second.id) == (0, 0)
    db.session.flush()
    row = ResidentDay.query.one()
    assert row.import_receipt_id == first.id


def test_deleting_the_middle_import_of_three_never_revives_it(facility):
    receipts = [ImportReceipt(facility_id=facility.id) for _ in range(3)]
    db.session.add_all(receipts)
    db.session.flush()
    for receipt, kind in zip(receipts, ("permanent", "respite", "private")):
        _import_resident_days(facility, [census_row(service_type=kind)], ImportOutcome("a", "f", False, {}, [], receipt=receipt))
        db.session.flush()
    assert restore_census_rows(receipts[1].id) == (0, 0)
    db.session.flush()
    assert restore_census_rows(receipts[2].id) == (1, 0)
    db.session.flush()
    row = ResidentDay.query.one()
    assert (row.service_type, row.import_receipt_id) == ("permanent", receipts[0].id)


def test_census_without_a_service_type_warns_at_review(facility):
    outcome = ImportOutcome("a", "f", False, {}, [])
    _import_resident_days(facility, [census_row()], outcome)
    assert "will not count as occupied bed days" in outcome.warnings[0]


def test_mistyped_shift_times_are_refused():
    from carelog.ingestion.mapping import MappingError
    from carelog.ingestion.quality import validate_shifts
    from datetime import time
    row = {"staff_id": "A", "date": date(2026, 8, 1), "start_time": time(8), "end_time": time(7, 30)}
    with pytest.raises(MappingError, match="mistyped"):
        validate_shifts([row], today=date(2026, 8, 2))


def test_a_rostered_file_name_alone_does_not_decide_the_evidence_class():
    from carelog.ingestion.evidence import classify_shift_evidence
    sheet = Sheet(name="Sheet1", headers=["Staff", "Date", "Start", "Finish"], rows=[])
    assert classify_shift_evidence("roster_export.csv", sheet)[0] == "unverified"


def test_headerless_files_and_sheet_names_disclose_nothing():
    from carelog.ingestion.analyzer import _structure_payload
    cells = ["Nguyen Thi", "Papadopoulos", "12/08/2026"]
    sheet = Sheet(name="Eleni Papadopoulos", headers=cells, rows=[], raw_rows=[cells], header_row_index=0)
    sent = _structure_payload([sheet])
    for private in ("Nguyen", "Thi", "Papadopoulos", "Eleni"):
        assert private not in sent, private


def prepare_file(app, ids, home, name, data, spec=None):
    job_id = jobs.start_job(app, home.id, [(name, data)], LocalStorage(app.config["UPLOADS_DIR"]),
                            organization_id=ids["customer"], user_id=ids["admin"])
    if spec:
        job = db.session.get(ImportJob, job_id)
        job.manual_specs_json = json.dumps({name: spec})
        db.session.commit()
    jobs.run_job(job_id)
    return job_id


def confirmed(client, job_id):
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 200
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "done", job.error


def test_a_remembered_layout_survives_a_new_file_name(prepared):
    app, ids, _, client, home = prepared
    july = b"Staff ID,Role,Day,Start,Finish,Unpaid\nA,Registered Nurse,2026-07-01,08:00,16:00,30\n"
    spec = {"targets": [{"kind": "shifts", "sheet": read_upload("july.csv", july)[0].name, "fields": {
        "staff_id": {"column": "Staff ID"}, "role": {"column": "Role", "normalize": "role"},
        "date": {"column": "Day", "parse": "date"}, "start_time": {"column": "Start", "parse": "time"},
        "end_time": {"column": "Finish", "parse": "time"},
        "break_minutes": {"column": "Unpaid", "parse": "number", "unit": "minutes"}}}], "reasoning": "test"}
    confirmed(client, prepare_file(app, ids, home, "july.csv", july, spec))

    august = july.replace(b"2026-07-01", b"2026-08-01")
    second = prepare_file(app, ids, home, "august.csv", august)
    job = db.session.get(ImportJob, second)
    assert job.status == "review", job.error
    confirmed(client, second)
    assert Shift_count(home) == 2


def Shift_count(home):
    from carelog.models import Shift
    return Shift.query.filter_by(facility_id=home.id, evidence_type="unverified").count() + Shift.query.filter_by(facility_id=home.id, evidence_type="worked").count()


def test_a_sheet_that_cannot_be_identified_goes_to_a_person_not_a_failure():
    from carelog.ingestion.pipeline import MappingRequired, _rebind_sheets
    sheets = [Sheet(name=n, headers=["Staff"], rows=[]) for n in ("One", "Two")]
    spec = {"targets": [{"kind": "shifts", "sheet": "Old", "fields": {"staff_id": {"column": "Staff"}}}]}
    with pytest.raises(MappingRequired):
        _rebind_sheets(spec, sheets, "file.xlsx")


def test_a_target_with_no_sheet_binds_to_the_only_sheet():
    from carelog.ingestion.pipeline import _rebind_sheets
    spec = {"targets": [{"kind": "shifts", "fields": {"staff_id": {"column": "Staff"}}}]}
    _rebind_sheets(spec, [Sheet(name="Only", headers=["Staff"], rows=[])], "file.csv")
    assert spec["targets"][0]["sheet"] == "Only"


SHORT_CODES = (
    b"Staff ID,Role,Work Date,Paid Hours,Thing\n"
    b"A,Registered Nurse,2026-08-03,8,ORD\n"
    b"A,Registered Nurse,2026-08-04,8,XYZ\n"
)


def test_payroll_hours_with_unrecognised_codes_need_an_answer(prepared):
    app, ids, _, client, home = prepared
    spec = spec_without_pay_column("payroll.csv", SHORT_CODES)
    job_id = prepare_file(app, ids, home, "payroll.csv", SHORT_CODES, spec["payroll.csv"])
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    hold = review["impact"]["holds"][0]
    assert hold["kind"] == "pay_column" and hold["options"][0]["column"] == "Thing"
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 409

    form = {"kind": "pay_column", "file": hold["file"], "sheet": hold["sheet"], "value": "Thing", "basis": "Payroll export"}
    assert client.post(f"/api/import/{job_id}/resolve-hold", data=form).status_code == 200
    jobs.run_job(job_id)
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    assert review["impact"]["holds"] == []
    assert review["impact"]["pay_unknown"] == ["XYZ"]


def test_short_leave_codes_are_recognised_by_value():
    rows = [{"Code": value} for value in ("ORD", "AL", "PCL")]
    assert pay_column(["Code"], rows) == "Code"
    assert treatment("AL", {}) == EXCLUDED


def test_a_multi_site_export_cannot_load_into_one_home(prepared):
    app, ids, _, client, home = prepared
    data = (b"Staff ID,Role,Day,Start,Finish,Unpaid,Site\n"
            b"A,Registered Nurse,2026-08-03,08:00,16:00,0,Banksia\n"
            b"B,Registered Nurse,2026-08-03,08:00,16:00,0,Wattle\n")
    spec = {"targets": [{"kind": "shifts", "sheet": read_upload("all.csv", data)[0].name, "fields": {
        "staff_id": {"column": "Staff ID"}, "role": {"column": "Role", "normalize": "role"},
        "date": {"column": "Day", "parse": "date"}, "start_time": {"column": "Start", "parse": "time"},
        "end_time": {"column": "Finish", "parse": "time"},
        "break_minutes": {"column": "Unpaid", "parse": "number", "unit": "minutes"}}}], "reasoning": "test"}
    job_id = prepare_file(app, ids, home, "all.csv", data, spec)
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    hold = review["impact"]["holds"][0]
    assert (hold["kind"], hold["column"], hold["options"]) == ("site", "Site", ["Banksia", "Wattle"])
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 409
    form = {"kind": "site", "file": hold["file"], "sheet": hold["sheet"], "value": "Banksia", "basis": "This home"}
    assert client.post(f"/api/import/{job_id}/resolve-hold", data=form).status_code == 200
    jobs.run_job(job_id)
    review = json.loads(db.session.get(ImportJob, job_id).review_json)
    assert review["impact"]["holds"] == [] and review["impact"]["shifts_imported"] == 1


def test_two_pending_titles_never_share_a_human_decision(facility):
    from carelog.ingestion.pipeline import _record_for_role
    approved = Staff(facility_id=facility.id, staff_id="S", name="S", role="PCW", source_role="Care Assistant",
                     eligibility_status="approved", approved_at=datetime(2026, 1, 1))
    assert _record_for_role([approved], "Activities Assistant") is None
    assert _record_for_role([approved], "Care Assistant") is approved


def test_a_move_into_nursing_stays_pending_until_registration_is_confirmed(facility):
    from carelog.ingestion.pipeline import _add_staff_record, _staff_directory
    db.session.add(Staff(facility_id=facility.id, staff_id="S", name="S", role="EN", source_role="Enrolled Nurse",
                         eligibility_status="approved", approved_at=datetime(2026, 1, 1), registration_number="NMW1"))
    db.session.flush()
    record = _add_staff_record(facility, _staff_directory(facility), "S", "S", "RN", "Registered Nurse", "approved", "Exact title")
    assert record.eligibility_status == "pending" and record.registration_number == "NMW1"


def test_a_mapped_blank_clears_leave_but_an_unmapped_field_stays(facility):
    receipt = ImportReceipt(facility_id=facility.id)
    db.session.add(receipt)
    db.session.flush()
    _import_resident_days(facility, [census_row(service_type="permanent", leave_type="hospital", leave_day_number=30, ancc_class="Class 5")],
                          ImportOutcome("a", "f", False, {}, [], receipt=receipt))
    db.session.flush()
    later = ImportOutcome("b", "f", False, {}, [], receipt=ImportReceipt(facility_id=facility.id))
    db.session.add(later.receipt)
    db.session.flush()
    _import_resident_days(facility, [census_row(leave_type="", leave_day_number=None)], later)
    db.session.flush()
    row = ResidentDay.query.one()
    assert row.leave_type is None and row.leave_day_number is None
    assert row.ancc_class == "Class 5" and row.service_type == "permanent"
    assert {c["field"] for c in later.census_changes[0]["changes"]} == {"leave_type", "leave_day_number"}


def test_implausible_duration_lines_are_refused():
    from carelog.ingestion.mapping import MappingError
    from carelog.ingestion.quality import validate_shifts
    with pytest.raises(MappingError, match="on one line"):
        validate_shifts([{"staff_id": "A", "date": date(2026, 8, 1), "minutes": 1000}], today=date(2026, 8, 2))
