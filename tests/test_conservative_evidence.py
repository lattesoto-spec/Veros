from datetime import date, time

import pytest

from carelog.domain.compliance import quarterly_tests, evidence_summary, day_breakdown, detect_gaps
from carelog.domain.eligibility import classify
from carelog.ingestion.evidence import classify_shift_evidence
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.pipeline import ImportOutcome, _import_resident_days
from carelog.ingestion.reader import Sheet
from carelog.models import ResidentDay, Shift, Staff, FacilityTarget, db


def test_mapped_actual_times_required_in_mixed_or_planned_sources():
    headers = ["Scheduled Start", "Scheduled Finish", "Actual Start", "Actual Finish", "Pay Code"]
    sheet = Sheet(name="Time", headers=headers, rows=[])
    def target(start, end):
        return {"fields": {"start_time": {"column": start}, "end_time": {"column": end}}}
    actual = target("Actual Start", "Actual Finish")
    planned = target("Scheduled Start", "Scheduled Finish")
    assert classify_shift_evidence("timesheet.csv", sheet, planned)[0] == "rostered"
    assert classify_shift_evidence("roster.csv", sheet, actual)[0] == "worked"
    assert classify_shift_evidence("roster.csv", sheet, planned)[0] == "rostered"
    assert classify_shift_evidence("payroll_budget_forecast.csv", sheet, actual)[0] == "rostered"
    scheduled = Sheet(name="Time", headers=["Scheduled Start", "Scheduled Finish", "Pay Code"], rows=[])
    assert classify_shift_evidence("timesheet.csv", scheduled, planned)[0] == "rostered"
    forecast = Sheet(name="Time", headers=["Hours", "Labour Cost"], rows=[])
    assert classify_shift_evidence("payroll_budget_forecast.csv", forecast)[0] == "rostered"


@pytest.mark.parametrize("title", [
    "Non-clinical RN", "Former RN", "Assistant RN", "Support RN", "RN Manager",
    "RN Quality Lead", "RN - Infection Control Coordinator", "Nurse Unit Manager",
    "CNM", "Clinical Nurse Manager", "Registered Nurse Coordinator", "Graduate Nurse",
])
def test_qualified_or_ambiguous_titles_need_human_review(title):
    assert classify(title)[1] == "pending"


@pytest.mark.parametrize("title,bucket", [
    ("RN", "RN"), ("Registered Nurse", "RN"), ("Clinical Nurse", "RN"),
    ("Enrolled Nurse", "EN"), ("Endorsed Enrolled Nurse", "EN"),
    ("Assistant in Nursing", "PCA"), ("Personal Care Worker", "PCA"),
])
def test_explicit_care_titles_remain_eligible(title, bucket):
    assert classify(title)[:2] == (bucket, "approved")


def test_pending_other_minutes_reconcile_and_withhold_verdict(app, facility):
    day = date(2026, 7, 1)
    with app.app_context():
        member = Staff(facility_id=facility.id, staff_id="G", name="Graduate", role="OTHER", source_role="Graduate Nurse", eligibility_status="pending")
        db.session.add(member)
        db.session.flush()
        db.session.add_all([
            ResidentDay(facility_id=facility.id, resident_id="R", date=day, occupied=True, service_type="permanent"),
            Shift(facility_id=facility.id, staff_id=member.id, date=day, start_time=time(8), end_time=time(16), evidence_type="worked"),
        ])
        db.session.commit()
        totals = day_breakdown(facility.id, day)
        summary = evidence_summary(facility.id, day, day)
        result = quarterly_tests(facility, day, day)
        assert totals["excluded_minutes"] == summary["worked_withheld_minutes"] == 480
        assert totals["ineligible_minutes"] == 0
        assert result["passed"] is None and result["on_track"] is None
        assert any("480" in issue for issue in result["limitations"])


@pytest.mark.parametrize("records,match", [
    ([{"occupied": False}, {"occupied": True}], "Duplicate"),
    ([{"occupied": "No"}], "explicit boolean"),
    ([{"occupied": True, "exclusion_reason": "Private resident"}], "exclusion reason"),
])
def test_census_rejects_conflicts_and_truthy_strings_before_writes(app, facility, records, match):
    with app.app_context():
        rows = [dict(resident_id="R", date=date(2026, 7, 1), **rec) for rec in records]
        with pytest.raises(MappingError, match=match):
            _import_resident_days(facility, rows, ImportOutcome("census.csv", "x", False, {}, []))
        assert ResidentDay.query.count() == 0


def test_partial_period_and_zero_occupancy_never_yield_quarterly_pass(app, facility):
    with app.app_context():
        member = Staff(facility_id=facility.id, staff_id="RN", name="Nurse", role="RN", eligibility_status="approved", registration_number="SYNTHETIC", registration_expiry=date(2030, 1, 1))
        db.session.add(member)
        db.session.flush()
        for number, occupied in [(1, True), (2, False)]:
            db.session.add(ResidentDay(facility_id=facility.id, resident_id="R", date=date(2026, 7, number), occupied=occupied, service_type="permanent"))
            db.session.add(Shift(facility_id=facility.id, staff_id=member.id, date=date(2026, 7, number), start_time=time(8), end_time=time(16), evidence_type="worked"))
        db.session.commit()
        partial = quarterly_tests(facility, date(2026, 7, 1), date(2026, 7, 1))
        assert partial["passed"] is None and partial["on_track"] is True
        assert not partial["complete_quarter"]
        conflict = quarterly_tests(facility, date(2026, 7, 1), date(2026, 7, 2))
        assert conflict["passed"] is None and conflict["on_track"] is None
        assert any("zero-occupied" in issue for issue in conflict["limitations"])


def test_alerts_do_not_round_away_four_minute_shortfall(app, facility):
    with app.app_context():
        day = date(2026, 7, 1)
        FacilityTarget.query.filter_by(facility_id=facility.id, quarter_start=day).one().ancc_target = 200
        member = Staff(facility_id=facility.id, staff_id="RN", name="Nurse", role="RN", eligibility_status="approved", registration_number="SYNTHETIC", registration_expiry=date(2030, 1, 1))
        db.session.add(member)
        db.session.flush()
        for i in range(100):
            db.session.add(ResidentDay(facility_id=facility.id, resident_id=str(i), date=day, occupied=True, service_type="permanent"))
        # Durations are synthetic calculation fixtures, not importable shifts.
        db.session.add(Shift(facility_id=facility.id, staff_id=member.id, date=day, start_time=time(0), end_time=time(1), duration_minutes=19996, evidence_type="worked"))
        db.session.commit()
        assert any("below its AN-ACC target" in alert["message"] for alert in detect_gaps(facility, day))


def test_complete_quarter_can_receive_a_definitive_verdict(app, facility):
    from datetime import timedelta
    with app.app_context():
        member = Staff(facility_id=facility.id, staff_id="RN", name="Nurse", role="RN", eligibility_status="approved", registration_number="SYNTHETIC", registration_expiry=date(2030, 1, 1))
        db.session.add(member)
        db.session.flush()
        day = date(2026, 7, 1)
        while day <= date(2026, 9, 30):
            db.session.add(ResidentDay(facility_id=facility.id, resident_id="R", date=day, occupied=True, service_type="permanent"))
            db.session.add(Shift(facility_id=facility.id, staff_id=member.id, date=day, start_time=time(8), end_time=time(16), evidence_type="worked"))
            day += timedelta(days=1)
        db.session.commit()
        result = quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))
        assert result["complete_quarter"] and result["passed"] is True
        assert not result["limitations"]


def test_missing_target_is_null_and_daily_status_ignores_legacy_targets(app, facility):
    from carelog.domain.care_minutes import daily_stats
    with app.app_context():
        FacilityTarget.query.filter_by(facility_id=facility.id).delete()
        day = date(2026, 7, 1)
        db.session.add(ResidentDay(facility_id=facility.id, resident_id="R", date=day, occupied=True, service_type="permanent"))
        db.session.commit()
        assert all(t["target"] is None for t in quarterly_tests(facility, day, day)["tests"])
        assert daily_stats(facility.id, day, 0, 0)["status"] == "unavailable"
