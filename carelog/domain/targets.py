"""Quarterly AN-ACC target reconciliation under Aged Care Rules 2025 s 176-20.

The provider's configured target remains the official value used for testing.
This module independently derives the expected target from the resident-day
classification ledger for the statutory reference period and raises a mismatch.

Daily amounts reflect the Aged Care Amendment (Care Minutes) Rules 2025,
registered 12 December 2025 and in force during 2026.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from carelog.domain.care_minutes import resident_day_is_occupied
from carelog.models import FacilityTarget, ResidentDay, db


DAILY_AMOUNTS = {
    "class 1": (268, 51), "class 2": (128, 27), "class 3": (178, 36),
    "class 4": (150, 32), "class 5": (185, 41), "class 6": (176, 37),
    "class 7": (215, 46), "class 8": (232, 47), "class 9": (214, 44),
    "class 10": (229, 44), "class 11": (253, 48), "class 12": (247, 47),
    "class 13": (268, 51),
    "respite class 1": (176, 37), "respite class 2": (223, 48),
    "respite class 3": (262, 51),
}

# Department guide, March 2026, table 3: allocations applying from 1 October 2025.
# https://www.health.gov.au/sites/default/files/2026-03/care-minutes-and-24-7-registered-nurse-requirements-guides-for-registered-providers-of-residential-care-homes.pdf
DAILY_AMOUNTS_EFFECTIVE_FROM = date(2025, 10, 1)


def target_for_quarter(facility, period_start):
    start = date(period_start.year, ((period_start.month - 1) // 3) * 3 + 1, 1)
    return FacilityTarget.query.filter_by(facility_id=facility.id, quarter_start=start).first()


@dataclass
class PeriodFacility:
    id: int
    organization_id: int | None
    name: str
    ancc_target: float | None
    rn_target: float | None


def facility_for_period(facility, day):
    target = target_for_quarter(facility, day)
    return PeriodFacility(facility.id, facility.organization_id, facility.name,
                          target.ancc_target if target else None, target.rn_target if target else None)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    return date(year, month_zero + 1, 1)


def reference_period(quarter_start: date) -> tuple[date, date, date]:
    start = _add_months(date(quarter_start.year, quarter_start.month, 1), -4)
    end = _add_months(start, 3) - timedelta(days=1)
    calculation_day = _add_months(date(quarter_start.year, quarter_start.month, 1), -1).replace(day=15)
    return start, end, calculation_day


def normalize_classification(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (value or "").strip().lower().replace("-", " "))
    match = re.fullmatch(r"(respite )?class\s*(\d+)", text)
    if not match:
        return None
    key = f"{'respite ' if match.group(1) else ''}class {int(match.group(2))}"
    return key if key in DAILY_AMOUNTS else None


def reconcile_configured_targets(facility, quarter_start: date) -> dict:
    configured = target_for_quarter(facility, quarter_start)
    care_target = configured.ancc_target if configured else None
    rn_target = configured.rn_target if configured else None
    start, end, calculation_day = reference_period(quarter_start)
    rows = ResidentDay.query.filter(
        ResidentDay.facility_id == facility.id,
        ResidentDay.date >= start,
        ResidentDay.date <= end,
    ).all()
    ledger_dates = db.session.query(ResidentDay.date).filter(
        ResidentDay.facility_id == facility.id,
        ResidentDay.date >= start,
        ResidentDay.date <= end,
    ).distinct().count()
    expected_dates = (end - start).days + 1
    recognized = [row for row in rows if resident_day_is_occupied(row)]
    missing = [row for row in recognized if normalize_classification(row.ancc_class) is None]

    combined_sum = rn_sum = 0
    for row in recognized:
        key = normalize_classification(row.ancc_class)
        if key:
            combined, rn = DAILY_AMOUNTS[key]
            combined_sum += combined
            rn_sum += rn

    complete = bool(recognized) and not missing and ledger_dates == expected_dates and quarter_start >= DAILY_AMOUNTS_EFFECTIVE_FROM
    derived_care = round(combined_sum / len(recognized), 2) if complete else None
    derived_rn = round(rn_sum / len(recognized), 2) if complete else None
    care_match = complete and care_target is not None and abs(derived_care - care_target) < 0.01
    rn_match = complete and rn_target is not None and abs(derived_rn - rn_target) < 0.01
    return {
        "reference_start": start,
        "reference_end": end,
        "calculation_day": calculation_day,
        "recognized_days": len(recognized),
        "ledger_dates": ledger_dates,
        "expected_dates": expected_dates,
        "missing_classification_days": len(missing),
        "complete": complete,
        "derived_care_target": derived_care,
        "derived_rn_target": derived_rn,
        "configured_care_target": care_target,
        "configured_rn_target": rn_target,
        "target_source": configured.source if configured else None,
        "target_quarter": configured.quarter_start if configured else None,
        "allocation_effective_from": DAILY_AMOUNTS_EFFECTIVE_FROM,
        "care_match": care_match,
        "rn_match": rn_match,
        "matched": care_match and rn_match,
    }
