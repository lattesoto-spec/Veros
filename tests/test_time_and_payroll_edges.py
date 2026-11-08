"""Real elapsed clocks and non-worked payroll rows never guess care evidence."""
import copy
import json
from datetime import date, time

import pytest

from carelog.domain.care_minutes import denominator_issues
from carelog.domain.compliance import rn_coverage, worked_minutes, quarterly_tests
from carelog.domain.intervals import elapsed_minutes
from carelog.ingestion.manual import ALWAYS_TRUE, build_spec
from carelog.ingestion.mapping import parse_date_value
from carelog.ingestion.review import public_review
from carelog.models import Shift, Staff, db
from carelog.services.workspace import workspace_context
from tests.test_corrections import SPEC, approve_everyone, home  # noqa: F401
from tests.test_source_questions import prepare, confirm
from tests.test_ui_boundaries import web_app  # noqa: F401
from carelog.ingestion.reader import read_upload


@pytest.mark.parametrize('day,expected', [(date(2026, 10, 3), 450), (date(2026, 4, 4), 570)])
def test_overnight_elapsed_time_tracks_clock_change(day, expected):
    assert elapsed_minutes(day, time(23), time(7, 30), 'Australia/Sydney') == expected
    assert elapsed_minutes(day, time(23), time(7, 30), 'Australia/Brisbane') == 510


@pytest.mark.parametrize('day,clock', [(date(2026, 10, 4), time(2, 30)), (date(2026, 4, 5), time(2, 30))])
def test_nonexistent_or_repeated_clock_is_rejected(day, clock):
    with pytest.raises(ValueError, match='ambiguous or nonexistent'):
        elapsed_minutes(day, clock, time(7), 'Australia/Sydney')


def test_import_captures_elapsed_minutes_and_clock_change_coverage_is_unassessed(home):  # noqa: F811
    data = b'Staff ID,Staff Name,Role,Work Date,Actual Start,Actual Finish,Paid Hours,Approval Status\nA,A Name,Registered Nurse,2026-10-03,23:00,07:30,8.5,Approved\n'
    job, client = prepare(home, data)
    assert job.status == 'review', job.error
    confirm(job, client)
    shift = Shift.query.one()
    assert shift.timezone_name == 'Australia/Sydney'
    assert shift.duration_minutes == worked_minutes(shift) == 450
    assert shift.coverage_known
    app, ids, facility = home
    approve_everyone(facility)
    coverage = rn_coverage(facility.id, date(2026, 10, 4))
    assert not coverage['coverage_known']
    assert 'clock' in coverage['time_basis'].lower()


def test_legacy_ambiguous_clock_is_zero_and_explicitly_provisional(home):  # noqa: F811
    app, ids, facility = home
    staff = Staff(facility_id=facility.id, staff_id='A', name='A', role='PCW', source_role='PCW', eligibility_status='approved')
    db.session.add(staff)
    db.session.flush()
    shift = Shift(facility_id=facility.id, staff_id=staff.id, date=date(2026, 4, 5), start_time=time(2, 30), end_time=time(7), evidence_type='worked', is_direct_care=True)
    db.session.add(shift)
    db.session.commit()
    assert worked_minutes(shift) == 0
    result = quarterly_tests(facility, date(2026, 4, 1), date(2026, 4, 5))
    assert any('clock' in text.lower() and 'zero' in text.lower() for text in result['limitations'])


def payroll_spec(data):
    sheets = read_upload('actual_worked.csv', data)
    spec, problems = build_spec([{'sheet': sheets[0].name, 'header_row': 0, 'kind': 'shifts', 'date_format': 'day_first',
        'fields': {'staff_id': 'ID', 'staff_name': 'Name', 'role': 'Role', 'date': 'Date', 'start_time': 'Start', 'end_time': 'End',
                   'break_minutes': 'Break', 'pay_category': 'Pay Code', 'is_direct_care': ALWAYS_TRUE}, 'units': {'break_minutes': 'minutes'}}], sheets)
    return spec, problems


def test_leave_with_blank_clocks_and_breaks_reaches_review_as_excluded(home):  # noqa: F811
    data = b'ID,Name,Role,Date,Start,End,Break,Pay Code\nA,A Name,PCW,01/08/2026,07:00,15:30,30,Ordinary\nB,B Name,PCW,13/08/2026,,,,AL\n'
    spec, problems = payroll_spec(data)
    assert not problems
    spec['targets'][0]['verified_source'] = 'Synthetic actual worked export'
    job, client = prepare(home, data, name='actual_worked.csv', spec=spec)
    assert job.status == 'review', job.error
    review = json.loads(job.review_json)
    categories = review['outcomes'][0]['results'][0]['pay_categories']
    assert next(row for row in categories if row['category'] == 'AL')['minutes'] is None
    confirm(job, client)
    assert Shift.query.count() == 1
    assert worked_minutes(Shift.query.one()) == 480


def test_worked_pay_line_still_requires_break_evidence():
    data = b'ID,Name,Role,Date,Start,End,Break,Pay Code\nA,A Name,PCW,01/08/2026,07:00,15:30,,Ordinary\n'
    spec, problems = payroll_spec(data)
    assert spec is None and any('blank' in problem for problem in problems)


@pytest.mark.parametrize('value', ['01/08/2026', '01-08-2026', '01.08.2026', '01/08/2026 07:00', '2026-08-01'])
def test_explicit_file_date_convention_accepts_variants_without_guessing(value):
    assert parse_date_value(value, 'day_first') == date(2026, 8, 1)
    with pytest.raises(ValueError):
        parse_date_value('13/08/2026', 'month_first')


def test_missing_census_ranges_and_workspace_period_match_assessment(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    spec = copy.deepcopy(SPEC)
    data = b'Staff ID,Staff Name,Role,Work Date,Actual Start,Actual Finish,Paid Hours,Approval Status\nA,A Name,Registered Nurse,2026-08-01,08:00,16:00,8,Approved\n'
    job, client = prepare(home, data, spec=spec)
    confirm(job, client)
    monkeypatch.setattr('carelog.domain.clock.facility_today', lambda _: date(2026, 11, 7))
    assert workspace_context(facility)['period'] == '01 Jul to 30 Sep 2026'
    issues = denominator_issues(facility.id, date(2026, 9, 1), date(2026, 9, 30))
    assert issues == ['2026-09-01 to 2026-09-30: no verified resident-day census']


def test_staff_review_lists_private_before_and_after_and_clears_samples(home):  # noqa: F811
    data = b'ID,Name,Role,Expiry\nA,A Name,RN,2027-05-31\n'
    spec = {'targets': [{'kind': 'staff', 'sheet': 'staff', 'fields': {'staff_id': {'column': 'ID'}, 'staff_name': {'column': 'Name'},
        'role': {'column': 'Role', 'normalize': 'role'}, 'registration_expiry': {'column': 'Expiry', 'parse': 'date'}}}]}
    job, client = prepare(home, data, name='staff.csv', spec=spec)
    assert job.status == 'review', job.error
    review = json.loads(job.review_json)
    assert review['impact']['staff_new'] == 1
    assert Staff.query.count() == 0  # preview rolled back
    assert 'staff_changes' not in public_review(review)['impact']
    assert public_review(review, True)['impact']['staff_changes'][0]['changes']
    confirm(job, client)
    assert 'staff_changes' not in json.loads(job.review_json)['impact']
    next_data = data.replace(b'2027-05-31', b'2028-05-31')
    job, client = prepare(home, next_data, name='staff.csv', spec=spec)
    review = json.loads(job.review_json)
    assert review['impact']['staff_new'] == 0 and review['impact']['staff_updated'] == 1
    change = next(c for c in review['impact']['staff_changes'][0]['changes'] if c['field'] == 'registration_expiry')
    assert change == {'field': 'registration_expiry', 'before': '2027-05-31', 'after': '2028-05-31'}
    assert Staff.query.one().registration_expiry == date(2027, 5, 31)


def test_unknown_pay_with_blank_clocks_cannot_be_decided_into_worked_evidence(home):  # noqa: F811
    from carelog.ingestion import jobs
    data = b'ID,Name,Role,Date,Start,End,Break,Pay Code\nA,A Name,PCW,01/08/2026,07:00,15:30,30,Ordinary\nB,B Name,PCW,13/08/2026,,,,Code 7\n'
    spec, problems = payroll_spec(data)
    assert not problems
    spec['targets'][0]['verified_source'] = 'Synthetic actual worked export'
    job, client = prepare(home, data, name='actual_worked.csv', spec=spec)
    assert job.status == 'review', job.error
    assert json.loads(job.review_json)['impact']['pay_unknown'] == ['Code 7']
    assert client.post(f'/api/import/{job.id}/confirm').status_code == 409
    response = client.post(f'/api/import/{job.id}/pay-categories', data={'decisions': json.dumps({'Code 7': 'worked'}), 'basis': 'Synthetic probe'})
    assert response.status_code == 200
    jobs.run_job(job.id)
    db.session.refresh(job)
    assert job.status == 'failed' and 'blank' in job.error.lower(), job.error
    assert Shift.query.count() == 0


def test_mapping_source_timezone_changes_elapsed_basis_without_changing_provider(home):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec['targets'][0]['clock_timezone'] = 'Australia/Brisbane'
    data = b'Staff ID,Staff Name,Role,Work Date,Actual Start,Actual Finish,Paid Hours,Approval Status\nA,A Name,Registered Nurse,2026-10-03,23:00,07:30,8.5,Approved\n'
    job, client = prepare(home, data, spec=spec)
    assert 'Australia/Brisbane' in json.loads(job.review_json)['outcomes'][0]['results'][0]['evidence_basis']
    confirm(job, client)
    assert worked_minutes(Shift.query.one()) == 510
    assert Shift.query.one().timezone_name == 'Australia/Brisbane'
