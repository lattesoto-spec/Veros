"""Every surface must read one quarter position: same verdict, same value label, same shortfall."""
import csv
import io
from datetime import date, time
from io import BytesIO

import pytest
from openpyxl import load_workbook

from carelog.domain.compliance import (
    compliance_pct, detect_gaps, forecast_quarter, monthly_breakdown, present_test, quarterly_tests,
)
from carelog.domain.exports import daily_csv, summary_xlsx
from carelog.domain.reports import _compliance_tests_table
from carelog.models import ResidentDay, Shift, db
from tests.test_honest_results import _fill, member, shift


def clock(monkeypatch, day):
    monkeypatch.setattr("carelog.domain.clock.facility_today", lambda _facility: day)


def xlsx_first_test_row(facility, start, end):
    sheet = load_workbook(BytesIO(summary_xlsx(facility, start, end)))["Compliance tests"]
    rows = list(sheet.iter_rows(values_only=True))
    return dict(zip(rows[0], rows[1]))


def alerts(facility, today):
    return [alert["message"] for alert in detect_gaps(facility, today)]


def test_clean_open_quarter_below_target_is_one_reading_everywhere(facility, monkeypatch):
    today = date(2026, 7, 3)
    clock(monkeypatch, today)
    _fill(facility, member(facility), date(2026, 7, 1), today, 10)
    tested = quarterly_tests(facility, date(2026, 7, 1), today)
    total = tested["tests"][0]
    assert total["passed"] is None and total["on_track"] is False
    shown = present_test(total)
    assert shown["shortfall_minutes"] == pytest.approx(total["shortfall_minutes"])
    assert shown["shortfall_label"].endswith("to date")
    # The gap register states the same deficit, scoped to the quarter to date.
    message = next(m for m in alerts(facility, today) if "below its AN-ACC target" in m)
    assert "quarter to date; not a quarter result" in message
    # The forecast is not provisional, so the percentage is shown.
    assert forecast_quarter(facility, today)["provisional"] is False
    assert compliance_pct(facility, today) is not None
    # Excel agrees with the presenter.
    row = xlsx_first_test_row(facility, date(2026, 7, 1), today)
    assert row["Shortfall total minutes"] == pytest.approx(total["shortfall_minutes"])


def test_pending_minutes_withhold_everywhere(facility, monkeypatch):
    today = date(2026, 7, 3)
    clock(monkeypatch, today)
    _fill(facility, member(facility), date(2026, 7, 1), today, 10)
    pending = member(facility, staff_id="N2", name="Pending", eligibility_status="pending", approved_at=None)
    for day in (1, 2, 3):
        shift(facility, pending, date(2026, 7, day), time(0), time(23, 59))
    db.session.commit()
    total = quarterly_tests(facility, date(2026, 7, 1), today)["tests"][0]
    assert total["passed"] is None and total["on_track"] is None
    assert present_test(total)["shortfall_label"].startswith("Not assessable")
    assert any("cannot be assessed" in m for m in alerts(facility, today))
    forecast = forecast_quarter(facility, today)
    assert forecast["provisional"] is True and forecast["on_track"] is None and forecast["rn_on_track"] is None
    assert any("withheld pending eligibility" in limit for limit in forecast["limitations"])
    assert compliance_pct(facility, today) is None
    assert xlsx_first_test_row(facility, date(2026, 7, 1), today)["Shortfall total minutes"] == "Not assessable"


def test_monthly_tables_count_every_calendar_date(facility):
    _fill(facility, member(facility), date(2026, 10, 1), date(2026, 10, 20), 3000)
    months = monthly_breakdown(facility.id, date(2026, 10, 1), date(2026, 10, 31))
    october = months[0]
    assert october["calendar_days"] == 31 and october["days"] == 20
    assert october["evidence_complete"] is False
    assert "20 of 31" in october["evidence_note"]


def test_daily_export_says_a_date_without_evidence_is_missing_not_zero(facility):
    _fill(facility, member(facility), date(2026, 10, 1), date(2026, 10, 2), 3000)
    rows = list(csv.DictReader(io.StringIO(daily_csv(facility, date(2026, 10, 1), date(2026, 10, 3)))))
    assert rows[0]["Countable worked time"] == "Yes"
    assert rows[2]["Countable worked time"].startswith("No - treat as missing")
    assert rows[2]["Census reconciled"].startswith("No")


def test_regulatory_pdf_heading_labels_diluted_per_bed_day_figures(facility):
    from carelog.domain.reports import _care_minutes_per_obd_table
    nurse = member(facility)
    from tests.test_honest_results import complete_quarter
    complete_quarter(facility, nurse)
    Shift.query.filter(Shift.date >= date(2026, 9, 1)).delete()
    db.session.commit()
    tests = quarterly_tests(facility, date(2026, 7, 1), date(2026, 9, 30))
    stats = {"tests": tests, "care_minutes_per_obd": {"RN": 1.0, "EN": 0.0, "PCA": 0.0},
             "total_care_minutes_per_obd": 1.0}
    heading = _care_minutes_per_obd_table(stats, facility)._cellvalues[0][1]
    assert "dates without evidence count as zero" in getattr(heading, "text", str(heading))
