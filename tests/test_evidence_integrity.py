from datetime import date, time, timedelta

import pytest

from carelog.domain.care_minutes import denominator_issues, resident_day_is_occupied
from carelog.domain.compliance import quarterly_tests, rn_coverage, forecast_quarter
from carelog.domain.eligibility import classify
from carelog.ingestion.mapping import MappingError, parse_date_value
from carelog.ingestion.pipeline import ImportOutcome, _import_shifts
from carelog.ingestion.quality import validate_shifts
from carelog.models import FacilityTarget, Resident, ResidentDay, Shift, Staff, db
from conftest import add_resident_days


def worker(facility, role="RN"):
    member = Staff(
        facility_id=facility.id,
        staff_id="W1",
        name="Worker",
        role=role,
        eligibility_status="approved",
        registration_number="RN1",
        registration_expiry=date(2030, 1, 1),
    )
    db.session.add(member)
    db.session.flush()
    return member


@pytest.mark.parametrize("index,threshold", [(0, 470), (1, 423), (2, 470)])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_all_three_verdicts_compare_exact_minutes(
    app, facility, index, threshold, delta
):
    with app.app_context():
        facility.ancc_target = facility.rn_target = 47
        target = FacilityTarget.query.filter_by(
            facility_id=facility.id, quarter_start=date(2026, 7, 1)
        ).one()
        target.ancc_target = target.rn_target = 47
        day = date(2026, 8, 1)
        add_resident_days(facility.id, day, 10)
        member = worker(facility)
        db.session.add(
            Shift(
                facility_id=facility.id,
                staff_id=member.id,
                date=day,
                start_time=time(8),
                end_time=time(16),
                duration_minutes=threshold + delta,
                evidence_type="worked",
            )
        )
        db.session.commit()
        test = quarterly_tests(facility, day, day)["tests"][index]
        assert test["passed"] is (delta >= 0)
        assert test["shortfall_minutes"] == max(-delta, 0)


def test_display_rounding_cannot_erase_a_shortfall(app, facility):
    with app.app_context():
        facility.ancc_target = 200
        FacilityTarget.query.filter_by(
            facility_id=facility.id, quarter_start=date(2026, 7, 1)
        ).one().ancc_target = 200
        day = date(2026, 8, 1)
        add_resident_days(facility.id, day, 25)
        member = worker(facility)
        for duration in (1000, 1000, 1000, 1000, 999):
            db.session.add(
                Shift(
                    facility_id=facility.id,
                    staff_id=member.id,
                    date=day,
                    start_time=time(0),
                    end_time=time(1),
                    duration_minutes=duration,
                    evidence_type="worked",
                )
            )
        db.session.commit()
        result = quarterly_tests(facility, day, day)["tests"][0]
        assert result["achieved"] == 200
        assert result["passed"] is False
        assert result["shortfall_minutes"] == 1


def test_partial_census_blocks_a_passing_verdict(app, facility):
    with app.app_context():
        day = date(2026, 8, 1)
        add_resident_days(facility.id, day, 1)
        db.session.add(
            Resident(
                facility_id=facility.id,
                resident_id="MISSING",
                name="Resident",
                admitted_date=date(2026, 1, 1),
            )
        )
        member = worker(facility)
        db.session.add(
            Shift(
                facility_id=facility.id,
                staff_id=member.id,
                date=day,
                start_time=time(0),
                end_time=time(23),
                evidence_type="worked",
            )
        )
        db.session.commit()
        result = quarterly_tests(facility, day, day)
        assert result["passed"] is None
        assert result["provisional"] is True
        assert "omits" in result["limitations"][0]
        assert denominator_issues(facility.id, day, day + timedelta(days=1))


def test_ambiguous_resident_days_are_excluded():
    for service, leave, number in [
        ("mystery", None, None),
        ("permanent", "hospital", None),
        ("permanent", "unknown", None),
    ]:
        assert not resident_day_is_occupied(
            ResidentDay(
                occupied=True,
                service_type=service,
                leave_type=leave,
                leave_day_number=number,
            )
        )


def test_imports_preserve_other_dates_and_reimport_idempotently(app, facility):
    with app.app_context():
        for month in (8, 9, 8):
            outcome = ImportOutcome("worked.csv", "x", True, {}, [])
            _import_shifts(
                facility,
                [
                    {
                        "staff_id": "W1",
                        "role": "PCW",
                        "date": date(2026, month, 1),
                        "start_time": time(8),
                        "end_time": time(16),
                    }
                ],
                outcome,
                "worked",
            )
            db.session.commit()
        assert Shift.query.count() == 2


@pytest.mark.parametrize(
    "change",
    [
        {"break_minutes": -120},
        {"end_time": time(8)},
        {"date": date.today() + timedelta(days=1)},
    ],
)
def test_bad_worked_rows_are_rejected(change):
    row = {
        "staff_id": "W1",
        "date": date(2026, 8, 1),
        "start_time": time(8),
        "end_time": time(16),
    }
    with pytest.raises(MappingError):
        validate_shifts([{**row, **change}])


def test_duplicate_and_overlapping_rows_are_rejected():
    row = {
        "staff_id": "W1",
        "date": date(2026, 8, 1),
        "start_time": time(8),
        "end_time": time(16),
    }
    for second in [row, {**row, "start_time": time(15)}]:
        with pytest.raises(MappingError):
            validate_shifts([row, second])


def test_midnight_gap_is_reportable_as_one_continuous_absence(app, facility):
    with app.app_context():
        day = date(2026, 9, 30)
        member = worker(facility)
        for current, start, end in [
            (day, time(0), time(23, 45)),
            (day + timedelta(days=1), time(0, 15), time(23, 59)),
        ]:
            db.session.add(
                Shift(
                    facility_id=facility.id,
                    staff_id=member.id,
                    date=current,
                    start_time=start,
                    end_time=end,
                    evidence_type="worked",
                )
            )
        db.session.commit()
        assert rn_coverage(facility.id, day)["gap_intervals"][-1]["reportable"] is True
        assert (
            rn_coverage(facility.id, day + timedelta(days=1))["gap_intervals"][0][
                "reportable"
            ]
            is True
        )


def test_duration_only_time_never_proves_rn_coverage(app, facility):
    with app.app_context():
        member = worker(facility)
        day = date(2026, 8, 1)
        db.session.add(
            Shift(
                facility_id=facility.id,
                staff_id=member.id,
                date=day,
                start_time=time(0),
                end_time=time(0),
                duration_minutes=480,
                coverage_known=False,
                evidence_type="worked",
            )
        )
        db.session.commit()
        assert rn_coverage(facility.id, day)["covered_minutes"] == 0
        assert rn_coverage(facility.id, day)["coverage_known"] is False
        from carelog.domain.reports import compute_quarter_stats

        report = compute_quarter_stats(facility.id, 2026, 1)
        august = next(row for row in report["monthly_rn_coverage"] if row["month"] == 8)
        assert august["coverage_pct"] is None
        assert "unavailable" in august["basis"]


def test_ambiguous_dates_and_non_direct_titles_need_review():
    with pytest.raises(ValueError, match="ambiguous"):
        parse_date_value("04/05/2026")
    assert parse_date_value("04/05/2026", "%d/%m/%Y") == date(2026, 5, 4)
    for title in ("Non-RN carer", "Clinical Nurse Educator"):
        assert classify(title)[1] == "pending"


def test_agency_scenarios_remove_the_observed_nursing_roles(app, facility):
    with app.app_context():
        day = date(2026, 8, 1)
        add_resident_days(facility.id, day, 1)
        member = worker(facility)
        db.session.add(
            Shift(
                facility_id=facility.id,
                staff_id=member.id,
                date=day,
                start_time=time(8),
                end_time=time(16),
                is_agency=True,
                evidence_type="worked",
            )
        )
        db.session.commit()
        baseline = forecast_quarter(facility, day)
        scenario = forecast_quarter(facility, day, {"agency_removed": True})
        combined = forecast_quarter(
            facility, day, {"agency_removed": True, "rn_shifts_removed": 1}
        )
        assert scenario["projected_rn_avg"] < baseline["projected_rn_avg"]
        assert scenario["projected_rn_en_avg"] == scenario["projected_rn_avg"]
        assert scenario["projected_avg"] == combined["projected_avg"]


def test_a_quarter_without_target_evidence_cannot_pass(app, facility):
    with app.app_context():
        day = date(2025, 10, 1)
        add_resident_days(facility.id, day, 1)
        result = quarterly_tests(facility, day, day)
        assert result["passed"] is None
        assert any("target evidence" in reason for reason in result["limitations"])


def test_an_ineligible_source_role_cannot_be_overridden_by_a_care_flag(app, facility):
    from carelog.domain.eligibility import counts_toward_care

    with app.app_context():
        member = worker(facility, "PCW")
        member.source_role = "Allied Health Assistant"
        assert counts_toward_care(member, date(2026, 8, 1)) is False


def test_source_role_changes_cannot_reclassify_old_worked_time(app, facility):
    with app.app_context():
        outcome = ImportOutcome("worked.csv", "x", True, {}, [])
        record = {
            "staff_id": "W1",
            "role": "PCW",
            "source_role": "Personal Care Worker",
            "date": date(2026, 8, 1),
            "start_time": time(8),
            "end_time": time(16),
        }
        _import_shifts(facility, [record], outcome, "worked")
        db.session.commit()
        with pytest.raises(MappingError, match="source role changed"):
            _import_shifts(
                facility,
                [
                    {
                        **record,
                        "role": "RN",
                        "source_role": "Registered Nurse",
                        "date": date(2026, 9, 1),
                    }
                ],
                outcome,
                "worked",
            )
