from datetime import date, datetime, time, timedelta

from carelog.domain.compliance import (
    day_breakdown,
    evidence_summary,
    forecast_quarter,
    quarterly_tests,
    rn_coverage,
)
from carelog.domain.eligibility import classify, counts_toward_care
from carelog.domain.reports import compute_quarter_stats
from carelog.ingestion.mapping import run_spec
from carelog.ingestion.pipeline import ImportOutcome, _import_shifts
from carelog.ingestion.reader import Sheet
from carelog.models import ImportReceipt, ResidentDay, Shift, Staff, db
from carelog.domain.care_minutes import active_residents_on
from carelog.domain.targets import reconcile_configured_targets, reference_period

from conftest import add_resident_days


def add_staff(facility_id, staff_id, role, *, status="approved", registration=True,
              source_role=None, manual=False):
    member = Staff(
        facility_id=facility_id,
        staff_id=staff_id,
        name=staff_id,
        role=role,
        source_role=source_role or role,
        eligibility_status=status,
        eligibility_reason="test evidence",
        registration_number=(f"AHPRA-{staff_id}" if registration and role in ("RN", "EN") else None),
        registration_expiry=(date.today() + timedelta(days=365)
                             if registration and role in ("RN", "EN") else None),
        approved_at=datetime.utcnow() if manual else None,
    )
    db.session.add(member)
    db.session.flush()
    return member


def add_shift(facility_id, staff, day, start, end, *, agency=False, cost=100.0,
              evidence_type="worked"):
    shift = Shift(
        facility_id=facility_id,
        staff_id=staff.id,
        date=day,
        start_time=start,
        end_time=end,
        break_minutes=0,
        is_direct_care=True,
        is_agency=agency,
        evidence_type=evidence_type,
        labour_cost=cost,
    )
    db.session.add(shift)
    return shift


def test_raw_role_survives_normalisation_and_is_classified_conservatively():
    sheet = Sheet(
        name="Shifts",
        headers=["ID", "Role", "Date", "Start", "End"],
        rows=[{"ID": "1", "Role": "Allied Health Assistant", "Date": "2026-08-01",
               "Start": "09:00", "End": "17:00"}],
    )
    spec = {"targets": [{"kind": "shifts", "fields": {
        "staff_id": {"column": "ID"},
        "role": {"column": "Role", "normalize": "role"},
        "date": {"column": "Date", "parse": "date"},
        "start_time": {"column": "Start", "parse": "time"},
        "end_time": {"column": "End", "parse": "time"},
    }}]}
    record = run_spec(spec, [sheet])[0].records[0]
    assert record["role"] == "PCW"
    assert record["source_role"] == "Allied Health Assistant"
    assert classify(record["source_role"])[1] == "excluded"


def test_manual_exclusion_survives_reimport(app, facility):
    with app.app_context():
        member = add_staff(facility.id, "S1", "RN", status="excluded", manual=True)
        db.session.commit()
        outcome = ImportOutcome(filename="x.csv", fingerprint="x", mapping_reused=True,
                                spec={}, results=[])
        _import_shifts(facility, [{
            "staff_id": "S1", "staff_name": "S1", "role": "RN",
            "source_role": "Registered Nurse", "date": date(2026, 8, 1),
            "start_time": time(7), "end_time": time(15), "_source_row": 2,
        }], outcome, evidence_type="worked")
        db.session.flush()
        assert db.session.get(Staff, member.id).eligibility_status == "excluded"


def test_weighted_quarter_result_not_mean_of_daily_ratios(app, facility):
    with app.app_context():
        facility.ancc_target = 220
        facility.rn_target = 0
        worker = add_staff(facility.id, "P1", "PCW")
        d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
        add_resident_days(facility.id, d1, 1)
        add_resident_days(facility.id, d2, 3)
        add_shift(facility.id, worker, d1, time(0), time(5))       # 300 / 1
        add_shift(facility.id, worker, d2, time(0), time(10))      # 600 / 3
        db.session.commit()

        result = quarterly_tests(facility, d1, d2)
        assert result["tests"][0]["achieved"] == 225.0
        assert result["tests"][0]["passed"] is True


def test_forecast_qtd_uses_weighted_numerator_and_bed_days(app, facility):
    with app.app_context():
        worker = add_staff(facility.id, "P1", "PCW")
        start = date(2026, 7, 1)
        for offset, residents, hours in ((0, 1, 5), (1, 3, 10)):
            day = start + timedelta(days=offset)
            add_resident_days(facility.id, day, residents)
            add_shift(facility.id, worker, day, time(0), time(hours))
        db.session.commit()
        forecast = forecast_quarter(facility, start + timedelta(days=1))
        assert forecast["qtd_avg"] == 225.0


def test_exact_30_minute_rn_gap_is_reportable_and_excluded_rn_does_not_fill_it(app, facility):
    with app.app_context():
        day = date(2026, 8, 1)
        rn = add_staff(facility.id, "RN1", "RN")
        excluded = add_staff(facility.id, "RN2", "RN", status="excluded")
        add_shift(facility.id, rn, day, time(0), time(12))
        add_shift(facility.id, rn, day, time(12, 30), time(0))
        add_shift(facility.id, excluded, day, time(12), time(12, 30))
        db.session.commit()
        coverage = rn_coverage(facility.id, day)
        assert coverage["covered_minutes"] == 1410
        assert coverage["full_coverage"] is False
        assert coverage["gap_intervals"] == [{
            "start_minute": 720,
            "end_minute": 750,
            "minutes": 30,
            "reportable": True,
        }]


def test_nurse_registration_needs_a_current_expiry(app, facility):
    with app.app_context():
        rn = add_staff(facility.id, "RN1", "RN")
        rn.registration_expiry = None
        assert counts_toward_care(rn, date(2026, 8, 1)) is False


def test_report_and_compliance_use_same_eligible_minutes_and_agency_split(app, facility):
    with app.app_context():
        day = date(2026, 7, 1)
        add_resident_days(facility.id, day, 1)
        rn = add_staff(facility.id, "RN1", "RN")
        pcw = add_staff(facility.id, "P1", "PCW")
        excluded = add_staff(facility.id, "X1", "PCW", status="excluded")
        add_shift(facility.id, rn, day, time(0), time(1), cost=80)
        add_shift(facility.id, pcw, day, time(1), time(3), agency=True, cost=120)
        add_shift(facility.id, excluded, day, time(3), time(5), cost=120)
        db.session.add(ImportReceipt(facility_id=facility.id, evidence_type="worked"))
        db.session.commit()

        official = day_breakdown(facility.id, day)
        report = compute_quarter_stats(facility.id, 2026, 1)
        assert official["total_minutes"] == 180
        assert report["worked_minutes"]["RN"] + report["worked_minutes"]["PCA"] == 180
        assert report["calculation_end"] == day
        assert report["total_care_minutes_per_obd"] == 180
        assert report["labour"]["PCA"]["agency_minutes"] == 120
        assert report["labour"]["PCA"]["agency_cost"] == 120


def test_evidence_streams_are_reconciled_not_added(app, facility):
    with app.app_context():
        day = date(2026, 8, 1)
        worker = add_staff(facility.id, "P1", "PCW")
        add_shift(facility.id, worker, day, time(0), time(2), evidence_type="worked")
        add_shift(facility.id, worker, day, time(0), time(3), evidence_type="rostered")
        db.session.commit()
        summary = evidence_summary(facility.id, day, day)
        assert summary["worked_minutes"] == 120
        assert summary["rostered_minutes"] == 180
        assert day_breakdown(facility.id, day)["total_minutes"] == 120


def test_resident_day_ledger_excludes_private_transition_and_hospital_day_29(app, facility):
    with app.app_context():
        day = date(2026, 8, 1)
        rows = [
            ResidentDay(facility_id=facility.id, resident_id="P", date=day,
                        occupied=True, service_type="permanent"),
            ResidentDay(facility_id=facility.id, resident_id="R", date=day,
                        occupied=True, service_type="respite"),
            ResidentDay(facility_id=facility.id, resident_id="H28", date=day,
                        occupied=True, service_type="an_acc", leave_type="hospital",
                        leave_day_number=28),
            ResidentDay(facility_id=facility.id, resident_id="H29", date=day,
                        occupied=True, service_type="an_acc", leave_type="hospital",
                        leave_day_number=29),
            ResidentDay(facility_id=facility.id, resident_id="PRIVATE", date=day,
                        occupied=True, service_type="private"),
            ResidentDay(facility_id=facility.id, resident_id="TCP", date=day,
                        occupied=True, service_type="transition care"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        assert active_residents_on(facility.id, day) == 3


def test_configured_targets_reconcile_to_reference_period_classifications(app, facility):
    with app.app_context():
        quarter_start = date(2026, 7, 1)
        start, end, calculation_day = reference_period(quarter_start)
        day = start
        while day <= end:
            db.session.add(ResidentDay(
                facility_id=facility.id, resident_id="R1", date=day,
                occupied=True, service_type="an_acc", ancc_class="Class 7",
            ))
            day += timedelta(days=1)
        db.session.commit()
        facility.ancc_target = 215
        facility.rn_target = 46
        result = reconcile_configured_targets(facility, quarter_start)
        assert calculation_day == date(2026, 6, 15)
        assert result["derived_care_target"] == 215
        assert result["derived_rn_target"] == 46
        assert result["matched"] is True
