import json
from datetime import date
from pathlib import Path

from carelog.domain.compliance import day_breakdown, rn_coverage
from carelog.ingestion.analyzer import _clean_spec
from carelog.ingestion.evidence import classify_shift_evidence
from carelog.ingestion.fingerprint import fingerprint
from carelog.ingestion.mapping import run_spec
from carelog.ingestion.pipeline import (
    MAPPING_SPEC_VERSION,
    ImportOutcome,
    _import_resident_days,
    _import_shifts,
    _import_staff,
    get_or_create_mapping,
)
from carelog.ingestion.reader import Sheet, read_upload
from carelog.models import FormatMapping, Staff, db


ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "sample_data" / "realistic_imports"


def test_evidence_classification_requires_structural_support():
    worked = Sheet(
        name="Approved Time Entries",
        headers=["Employee", "Work Date", "Actual Start", "Actual Finish", "Paid Hours", "Approval Status"],
        rows=[],
    )
    roster = Sheet(
        name="Published Roster",
        headers=["Employee", "Date", "Scheduled Start", "Scheduled Finish", "Roster Status"],
        rows=[],
    )
    ambiguous = Sheet(
        name="Shifts",
        headers=["Employee", "Date", "Start", "Finish"],
        rows=[],
    )

    assert classify_shift_evidence("payroll.xlsx", worked)[0] == "worked"
    assert classify_shift_evidence("july_roster.xlsx", roster)[0] == "rostered"
    assert classify_shift_evidence("shifts.xlsx", ambiguous)[0] == "unverified"


def test_model_field_lists_are_normalised_instead_of_crashing():
    cleaned = _clean_spec({
        "targets": [{
            "kind": "staff",
            "sheet": "Employee Directory",
            "fields": [
                {"name": "staff_id", "column": "Employee Number"},
                {"normalized_field": "registration_number", "column": "AHPRA Registration"},
                {"registration_expiry": {"column": "Registration Expiry", "parse": "date"}},
            ],
        }],
    })
    fields = cleaned["targets"][0]["fields"]
    assert fields["staff_id"]["column"] == "Employee Number"
    assert fields["registration_number"]["column"] == "AHPRA Registration"
    assert fields["registration_expiry"]["parse"] == "date"


def test_old_cached_mapping_is_refreshed(app, facility):
    sheet = Sheet(
        name="Shifts",
        headers=[
            "Date", "Staff ID", "Staff Name", "Role", "Shift Start", "Shift End",
            "Unpaid Break (min)", "Counts Toward Care Minutes",
        ],
        rows=[{
            "Date": "2026-08-01", "Staff ID": "S1", "Staff Name": "Worker",
            "Role": "PCW", "Shift Start": "07:00", "Shift End": "15:00",
            "Unpaid Break (min)": "30", "Counts Toward Care Minutes": "Y",
        }],
    )
    with app.app_context():
        old = FormatMapping(
            organization_id=facility.organization_id,
            fingerprint=fingerprint([sheet]),
            spec_json=json.dumps({"targets": []}),
            source_filename="old.csv",
            kinds="",
        )
        db.session.add(old)
        db.session.commit()

        mapping, reused, usage = get_or_create_mapping(
            [sheet], "new.csv", organization_id=facility.organization_id
        )
        refreshed = json.loads(mapping.spec_json)
        assert reused is False
        assert usage["method"] == "built-in exact mapping"
        assert refreshed["schema_version"] == MAPPING_SPEC_VERSION
        assert refreshed["targets"][0]["kind"] == "shifts"


def test_realistic_workbook_imports_credentials_and_countable_rn_minutes(app, facility):
    staffing_path = SAMPLES / "worked_staffing_july_2026.xlsx"
    census_path = SAMPLES / "resident_census_july_2026.xlsx"
    staffing_sheets = read_upload(staffing_path.name, staffing_path.read_bytes())
    census_sheets = read_upload(census_path.name, census_path.read_bytes())

    staffing_spec = {"targets": [
        {"kind": "staff", "sheet": "Employee Directory", "fields": {
            "staff_id": {"column": "Employee Number"},
            "staff_name": {"column": "Employee Display Name"},
            "role": {"column": "Position Description", "normalize": "role"},
            "employment_type": {"column": "Engagement Type"},
            "classification": {"column": "Position Description"},
            "registration_number": {"column": "AHPRA Registration"},
            "registration_expiry": {"column": "Registration Expiry", "parse": "date"},
        }},
        {"kind": "shifts", "sheet": "Approved Time Entries",
         "row_filter": {"column": "Approval Status", "include_values": ["approved"]},
         "fields": {
             "staff_id": {"column": "Payroll Emp #"},
             "staff_name": {"column": "Team Member"},
             "role": {"column": "Award / Position Description", "normalize": "role"},
             "date": {"column": "Work Date", "parse": "date"},
             "start_time": {"column": "Actual Start", "parse": "time"},
             "end_time": {"column": "Actual Finish", "parse": "time"},
             "break_minutes": {"column": "Unpaid Meal Break (hours)", "parse": "number", "multiply": 60},
             "is_direct_care": {"column": "Counts as direct care?", "parse": "boolean"},
             "is_agency": {"column": "Agency / Employee", "value_map": {
                 "agency": True, "permanent part-time": False, "permanent full-time": False,
             }},
             "labour_cost": {"column": "Labour Cost ex GST", "parse": "number"},
         }},
    ]}
    census_spec = {"targets": [{
        "kind": "resident_days", "sheet": "Daily Occupancy", "fields": {
            "date": {"column": "Census Date", "parse": "date"},
            "resident_id": {"column": "Client Ref"},
            "resident_name": {"column": "Resident Display Name"},
            "occupied": {"column": "Included in care-minute OBD?", "parse": "boolean"},
            "service_type": {"column": "Funding Stream"},
            "leave_type": {"column": "Leave Category"},
            "leave_day_number": {"column": "Consecutive Leave Day", "parse": "number"},
            "ancc_class": {"column": "AN-ACC Class"},
            "exclusion_reason": {"column": "Exclusion / Adjustment Reason"},
        },
    }]}

    staffing_results = run_spec(staffing_spec, staffing_sheets)
    census_result = run_spec(census_spec, census_sheets)[0]
    outcome = ImportOutcome(
        filename=staffing_path.name, fingerprint="test", mapping_reused=False,
        spec=staffing_spec, results=staffing_results,
    )
    last_day = date(2026, 7, 31)

    with app.app_context():
        _import_staff(facility, staffing_results[0].records, outcome)
        evidence_type, _ = classify_shift_evidence(staffing_path.name, staffing_sheets[2])
        _import_shifts(facility, staffing_results[1].records, outcome, evidence_type)
        _import_resident_days(
            facility,
            [r for r in census_result.records if r["date"] == last_day],
            outcome,
        )
        db.session.commit()

        rn = Staff.query.filter_by(facility_id=facility.id, role="RN").first()
        assert rn.registration_number
        assert rn.registration_expiry >= last_day
        breakdown = day_breakdown(facility.id, last_day)
        assert evidence_type == "worked"
        assert breakdown["residents"] == 45
        assert breakdown["rn_minutes"] == 2310
        assert breakdown["en_minutes"] == 900
        assert breakdown["total_minutes"] == 10950
        assert breakdown["rn_per_resident"] == 51.3
        assert rn_coverage(facility.id, last_day)["coverage_pct"] == 100.0
