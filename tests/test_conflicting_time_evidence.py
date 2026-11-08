"""Contradictory source evidence must reach a visible stop before committing."""
import copy
import json
from datetime import date

import pytest

from carelog.domain.compliance import forecast_quarter, quarterly_tests, worked_minutes
from carelog.ingestion import jobs
from carelog.ingestion.mapping import MappingError, run_spec
from carelog.ingestion.reader import read_upload
from carelog.models import Shift, ResidentDay, db
from tests.test_corrections import SPEC, home, approve_everyone  # noqa: F401
from tests.test_source_questions import prepare, answer, confirm
from tests.test_ui_boundaries import web_app  # noqa: F401


def clocks(start, end, paid='8', day='2026-08-01'):
    return ('Staff ID,Staff Name,Role,Work Date,Actual Start,Actual Finish,Paid Hours,Approval Status\n'
            f'A,A Name,Registered Nurse,{day},{start},{end},{paid},Approved\n').encode()


@pytest.mark.parametrize('start,end', [
    ('2026-08-03 08:00', '2026-08-03 16:00'),
    ('2026-08-01 08:00', '2026-08-02 20:00'),
    ('2026-08-01 20:00', '2026-08-01 08:00'),
    ('08:00', '2026-08-03 16:00'),
])
def test_conflicting_datetime_dates_fail_the_actual_job_without_rows(home, start, end):  # noqa: F811
    job, client = prepare(home, clocks(start, end))
    assert job.status == 'failed', job.error
    assert 'date' in job.error.lower() or '24 hours' in job.error
    assert Shift.query.count() == 0
    assert client.post(f'/api/import/{job.id}/confirm').status_code == 409


@pytest.mark.parametrize('start,end,day,paid,expected', [
    ('2026-08-01 08:00', '2026-08-01 16:00', '2026-08-01', '8', 480),
    ('2026-09-30 23:00', '2026-10-01 07:00', '2026-09-30', '8', 480),
    ('16:00', '24:00', '1-Aug-2026', '8', 480),
    ('01/08/2026 23:00', '02/08/2026 07:00', '01/08/2026', '8', 480),
])
def test_dates_survive_datetime_validation_and_24_hour_finish_is_midnight(home, start, end, day, paid, expected):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    if '/' in day:
        spec['targets'][0]['fields']['date']['format'] = 'day_first'
    job, client = prepare(home, clocks(start, end, paid, day), spec=spec)
    assert job.status == 'review', job.error
    confirm(job, client)
    shift = Shift.query.one()
    assert worked_minutes(shift) == expected
    if day == '2026-09-30':
        assert shift.date == date(2026, 9, 30) and shift.end_time.hour == 7


def test_24_hour_start_is_not_silently_moved_to_the_next_date(home):  # noqa: F811
    job, client = prepare(home, clocks('24:00', '08:00'))
    assert job.status == 'failed' and '24:00 is a finish' in job.error
    assert Shift.query.count() == 0


@pytest.mark.parametrize('start,end,paid,clocked', [('08:00', '16:00', '4', 480), ('20:00', '23:30', '8', 210), ('08:00', '16:00', '7.5', 480)])
def test_disagreeing_unmapped_paid_hours_require_a_bound_audited_review(home, start, end, paid, clocked):  # noqa: F811
    job, client = prepare(home, clocks(start, end, paid))
    assert job.status == 'review', job.error
    hold = next(h for h in json.loads(job.review_json)['impact']['holds'] if h['kind'] == 'time_reconciliation')
    assert hold['sample'][0]['clock_minutes'] == clocked
    assert hold['sample'][0]['reported_minutes'] == float(paid) * 60
    assert client.post(f'/api/import/{job.id}/confirm').status_code == 409
    assert Shift.query.count() == 0
    assert answer(job, client, 'time_reconciliation', 'clocks_verified').status_code == 200
    jobs.run_job(job.id)
    confirm(job, client)
    assert worked_minutes(Shift.query.one()) == clocked
    # A changed conflict cannot inherit the previous explanation.
    job, client = prepare(home, clocks(start, end, paid, '2026-08-02'))
    assert any(h['kind'] == 'time_reconciliation' for h in json.loads(job.review_json)['impact']['holds'])


def test_correct_break_removes_the_conflict_without_overriding_source_evidence(home):  # noqa: F811
    spec = copy.deepcopy(SPEC)
    spec['targets'][0]['fields']['break_minutes']['value'] = 30
    job, client = prepare(home, clocks('08:00', '16:00', '7.5'), spec=spec)
    assert json.loads(job.review_json)['impact']['holds'] == []
    confirm(job, client)
    assert worked_minutes(Shift.query.one()) == 450


def test_detect_mode_rejects_mixed_date_orders_across_the_file():
    data = clocks('08:00', '16:00', day='13/08/2026') + b'B,B Name,Registered Nurse,08/14/2026,08:00,16:00,8,Approved\n'
    sheets = read_upload('timesheet.csv', data)
    spec = copy.deepcopy(SPEC)
    spec['targets'][0]['sheet'] = sheets[0].name
    with pytest.raises(MappingError, match='Mixed day-first'):
        run_spec(spec, sheets)


def test_forecast_keeps_zero_occupancy_minutes_and_withholds_status(home, monkeypatch):  # noqa: F811
    app, ids, facility = home
    monkeypatch.setattr('carelog.domain.clock.facility_today', lambda _: date(2026, 8, 2))
    for day in ('2026-08-01', '2026-08-02'):
        job, client = prepare(home, clocks('08:00', '16:00', day=day))
        confirm(job, client)
    approve_everyone(facility)
    census = ResidentDay.query.filter_by(facility_id=facility.id, date=date(2026, 8, 1)).one()
    census.occupied = False
    db.session.commit()
    forecast = forecast_quarter(facility, date(2026, 8, 2))
    assert forecast['qtd_avg'] == 960
    assert forecast['provisional'] and forecast['on_track'] is None
    assert any('zero-occupancy' in text for text in forecast['limitations'])
    result = quarterly_tests(facility, date(2026, 8, 1), date(2026, 8, 2))
    assert result['totals']['total_minutes'] == 960
