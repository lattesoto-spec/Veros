"""Offline checks for the fixed product-demo workbook."""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from carelog.domain.compliance import quarterly_tests, rn_coverage
from carelog.ingestion.evidence import classify_shift_evidence
from carelog.ingestion.fingerprint import fingerprint
from carelog.ingestion.mapping import MappingError, run_spec, validate_results
from carelog.ingestion.pipeline import MAPPING_SPEC_VERSION, get_or_create_mapping, ingest_file
from carelog.ingestion.presets import matching_spec
from carelog.ingestion.reader import Sheet, read_upload
from carelog.models import FormatMapping, db


WORKBOOK = (
    Path(__file__).resolve().parents[1]
    / "sample_data/demo_video/caremin_demo_actual_worked_export.xlsx"
)
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 8, 25)
GAP_DAY = PERIOD_END


def _minutes(record):
    start = datetime.combine(record["date"], record["start_time"])
    finish = datetime.combine(record["date"], record["end_time"])
    if finish <= start:
        finish += timedelta(days=1)
    return int((finish - start).total_seconds() // 60) - int(
        record.get("break_minutes") or 0
    )


def test_demo_workbook_maps_and_tells_the_expected_story():
    sheets = read_upload(WORKBOOK.name, WORKBOOK.read_bytes())
    spec = matching_spec(sheets)
    assert spec is not None

    results = run_spec(spec, sheets)
    assert not validate_results(results)
    by_kind = {result.kind: result for result in results}
    assert set(by_kind) == {"residents", "resident_days", "staff", "shifts"}
    assert len(by_kind["residents"].records) == 42
    assert len(by_kind["resident_days"].records) == 6216
    assert len(by_kind["staff"].records) == 42
    assert len(by_kind["shifts"].records) == 1117
    assert by_kind["shifts"].rows_filtered == 1
    assert not any(result.row_errors for result in results)

    shift_sheet = next(sheet for sheet in sheets if sheet.name == "Approved Time Entries")
    evidence_type, basis = classify_shift_evidence(WORKBOOK.name, shift_sheet)
    assert evidence_type == "worked"
    assert "actual start/finish" in basis

    current_resident_days = [
        row for row in by_kind["resident_days"].records
        if PERIOD_START <= row["date"] <= PERIOD_END and row["occupied"]
    ]
    assert len(current_resident_days) == 2352
    assert {row["date"] for row in current_resident_days} == set(
        PERIOD_START + timedelta(days=offset)
        for offset in range((PERIOD_END - PERIOD_START).days + 1)
    )

    current_shifts = [
        row for row in by_kind["shifts"].records
        if PERIOD_START <= row["date"] <= PERIOD_END and row["is_direct_care"]
    ]
    minutes_by_day = defaultdict(int)
    role_minutes = defaultdict(int)
    for row in current_shifts:
        worked = _minutes(row)
        minutes_by_day[row["date"]] += worked
        role_minutes[row["role"]] += worked

    bed_days = len(current_resident_days)
    total_result = round(sum(minutes_by_day.values()) / bed_days, 1)
    rn_result = round(role_minutes["RN"] / bed_days, 1)
    rn_en_result = round((role_minutes["RN"] + role_minutes["EN"]) / bed_days, 1)
    assert total_result == 213.5
    assert rn_result == 42.8
    assert rn_en_result == 64.2
    assert total_result < 215
    assert rn_result >= 44 * 0.9
    assert rn_en_result >= 44

    daily_results = {
        day: round(minutes / 42, 1) for day, minutes in minutes_by_day.items()
    }
    assert any(result >= 215 for result in daily_results.values())
    assert any(result < 215 for result in daily_results.values())
    assert daily_results[GAP_DAY] == 200.7

    gap_day_rn = [
        row for row in current_shifts if row["date"] == GAP_DAY and row["role"] == "RN"
    ]
    previous_night = [
        row for row in current_shifts
        if row["date"] == GAP_DAY - timedelta(days=1)
        and row["role"] == "RN"
        and row["start_time"].hour == 23
        and row["end_time"].hour == 7
    ]
    assert previous_night
    assert min(row["start_time"] for row in gap_day_rn) == datetime.strptime(
        "08:00", "%H:%M"
    ).time()
    assert any(row["start_time"].hour == 23 for row in gap_day_rn)


@pytest.mark.parametrize(
    ("kind", "column", "field", "value"),
    [
        ("resident_days", "Occupied Bed Day", "occupied", "unknown"),
        ("resident_days", "Occupied Bed Day", "occupied", ""),
        ("shifts", "Direct Care", "is_direct_care", "unknown"),
        ("shifts", "Direct Care", "is_direct_care", ""),
    ],
)
def test_demo_preset_rejects_uncertain_counting_fields(kind, column, field, value):
    sheets = read_upload(WORKBOOK.name, WORKBOOK.read_bytes())
    spec = matching_spec(sheets)
    target = next(item for item in spec["targets"] if item["kind"] == kind)
    source = next(sheet for sheet in sheets if sheet.name == target["sheet"])
    row = dict(source.rows[0])
    row[column] = value
    sheet = Sheet(name=source.name, headers=source.headers, rows=[row])

    with pytest.raises(MappingError, match=field):
        run_spec({"targets": [target]}, [sheet])


def test_demo_workbook_produces_the_expected_reported_result(app, facility):
    with app.app_context():
        outcome = ingest_file(facility, WORKBOOK.name, WORKBOOK.read_bytes())
        assert not outcome.row_errors
        db.session.commit()

        result = quarterly_tests(facility, PERIOD_START, PERIOD_END)
        assert result["totals"]["bed_days"] == 2352
        assert [(test["achieved"], test["on_track"]) for test in result["tests"]] == [
            (213.5, False), (42.8, True), (64.2, True),
        ]
        assert result["target_reconciliation"]["matched"] is True
        assert any(
            gap["start_minute"] == 420 and gap["end_minute"] == 480
            and gap["reportable"]
            for gap in rn_coverage(facility.id, GAP_DAY)["gap_intervals"]
        )


def test_cached_demo_preset_refreshes_strict_validation(app, facility):
    sheets = read_upload(WORKBOOK.name, WORKBOOK.read_bytes())
    current = matching_spec(sheets)
    old = json.loads(json.dumps(current))
    for target in old["targets"]:
        target.pop("fail_on_row_error", None)
        for field in target["fields"].values():
            field.pop("required", None)
    old["schema_version"] = MAPPING_SPEC_VERSION

    with app.app_context():
        cached = FormatMapping(
            organization_id=facility.organization_id,
            fingerprint=fingerprint(sheets),
            spec_json=json.dumps(old),
            source_filename=WORKBOOK.name,
            kinds="residents,resident_days,staff,shifts",
        )
        db.session.add(cached)
        db.session.commit()

        refreshed, reused, usage = get_or_create_mapping(
            sheets, WORKBOOK.name, organization_id=facility.organization_id
        )
        assert refreshed.id == cached.id
        assert reused is False
        assert usage["method"] == "built-in exact mapping"
        assert json.loads(refreshed.spec_json)["targets"] == current["targets"]

