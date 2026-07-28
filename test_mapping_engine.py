"""Offline regression test for the mapping engine (no API key needed).

Runs hand-written mapping specs — the same language the AI analyzer emits —
over the 5 heterogeneous fixtures and asserts all of them normalize to the
same direct-care and RN minutes. This pins down the deterministic engine;
test_ingestion.py additionally exercises the AI spec generation.

Run:  python test_mapping_engine.py
"""

import os
import sys
from collections import defaultdict
from datetime import date, datetime, time

HERE = os.path.dirname(os.path.abspath(__file__))

from ingestion.fingerprint import fingerprint
from ingestion.mapping import run_spec, validate_results
from ingestion.reader import read_upload

SPECS = {
    "format1_simple.csv": {"targets": [{
        "kind": "shifts",
        "fields": {
            "staff_id": {"column": "staff_id"},
            "staff_name": {"column": "staff_name"},
            "role": {"column": "role", "normalize": "role"},
            "date": {"column": "date", "parse": "date"},
            "start_time": {"column": "start_time", "parse": "time"},
            "end_time": {"column": "end_time", "parse": "time"},
            "is_direct_care": {"column": "is_direct_care", "parse": "boolean", "default": True},
        },
    }]},
    "format2_humanforce.csv": {"targets": [{
        "kind": "shifts",
        "row_filter": {"column": "Record Type", "include_values": ["shift"]},
        "fields": {
            "staff_id": {"column": "Employee Code"},
            "staff_name": {"source": "combine", "columns": ["First Name", "Surname"]},
            "role": {"column": "Classification", "normalize": "role"},
            "date": {"column": "Shift Date", "parse": "date", "format": "%d/%m/%Y"},
            "start_time": {"column": "Start", "parse": "time"},
            "end_time": {"column": "Finish", "parse": "time"},
            "is_direct_care": {
                "column": "Cost Centre",
                "value_map": {"residential care": True, "corporate services": False},
                "default": True,
            },
        },
    }]},
    "format3_alayacare.xlsx": {"targets": [{
        "kind": "shifts",
        "sheet": "Visits",
        "fields": {
            "staff_id": {"column": "Employee ID"},
            "staff_name": {"column": "Employee Name"},
            "role": {"column": "Position", "normalize": "role"},
            "date": {"column": "Visit Start", "parse": "datetime_date"},
            "start_time": {"column": "Visit Start", "parse": "datetime_time"},
            "end_time": {"column": "Visit End", "parse": "datetime_time"},
            "is_direct_care": {
                "column": "Service Type",
                "value_map": {"direct care": True, "administration": False},
                "default": True,
            },
        },
    }]},
    "format4_payroll_hours.csv": {"targets": [{
        "kind": "shifts",
        "fields": {
            "staff_id": {"column": "EmpNo"},
            "staff_name": {"column": "Employee"},
            "role": {"column": "Position Title", "normalize": "role"},
            "date": {"column": "WorkDate", "parse": "date", "format": "%d.%m.%Y"},
            "minutes": {"column": "HoursWorked", "parse": "number", "multiply": 60},
            "is_direct_care": {
                "column": "PayCategory",
                "value_map": {"direct care hours": True, "non-care hours": False},
                "default": True,
            },
        },
    }]},
    "format5_roster.txt": {"targets": [{
        "kind": "shifts",
        "fields": {
            "staff_id": {"column": "Ref"},
            "staff_name": {"column": "Worker"},
            "role": {"column": "Grade", "normalize": "role"},
            "date": {"column": "Date Worked", "parse": "date", "format": "%d %b %Y"},
            "start_time": {"column": "Start Time", "parse": "time", "format": "%H%M"},
            "end_time": {"column": "Finish Time", "parse": "time", "format": "%H%M"},
            "is_direct_care": {
                "column": "Dept",
                "value_map": {"care": True, "admin": False},
                "default": True,
            },
        },
    }]},
    "residents_clients.xlsx": {"targets": [{
        "kind": "residents",
        "sheet": "Clients",
        "fields": {
            "resident_id": {"column": "Client Ref"},
            "name": {"source": "combine", "columns": ["Given Name", "Family Name"]},
            "ancc_class": {"column": "AN-ACC Class"},
            "admitted_date": {"column": "Admission Date", "parse": "date", "format": "%d/%m/%Y"},
            "discharged_date": {"column": "Departure Date", "parse": "date"},
        },
    }]},
}


def shift_minutes(rec) -> int:
    if isinstance(rec.get("minutes"), (int, float)):
        return int(rec["minutes"])
    s = datetime.combine(date.today(), rec["start_time"])
    e = datetime.combine(date.today(), rec["end_time"])
    if e < s:
        e = e.replace(day=e.day + 1)
    return int((e - s).total_seconds() // 60)


def main():
    fixture_dir = os.path.join(HERE, "sample_data/test_data")
    totals = {}
    fingerprints = set()

    for name, spec in SPECS.items():
        with open(os.path.join(fixture_dir, name), "rb") as f:
            sheets = read_upload(name, f.read())
        fp = fingerprint(sheets)
        assert fp not in fingerprints, f"fingerprint collision for {name}"
        fingerprints.add(fp)

        results = run_spec(spec, sheets)
        problems = validate_results(results)
        assert not problems, f"{name}: {problems}"

        r = results[0]
        if r.kind == "residents":
            assert len(r.records) == 30, f"{name}: expected 30 residents, got {len(r.records)}"
            print(f"{name}: 30 residents OK (fingerprint {fp[:12]})")
            continue

        per_day = defaultdict(lambda: [0, 0])  # date -> [direct_minutes, rn_minutes]
        for rec in r.records:
            if not rec.get("is_direct_care", True):
                continue
            m = shift_minutes(rec)
            per_day[rec["date"]][0] += m
            if rec.get("role") == "RN":
                per_day[rec["date"]][1] += m
        totals[name] = {d: tuple(v) for d, v in sorted(per_day.items())}
        print(f"{name}: {len(r.records)} shifts, {r.rows_filtered} filtered, "
              f"{len(r.row_errors)} errors (fingerprint {fp[:12]})")
        for d, (dm, rm) in totals[name].items():
            print(f"   {d}: direct={dm}m rn={rm}m")

    unique = {tuple(sorted(t.items())) for t in totals.values()}
    assert len(unique) == 1, f"formats disagree: {totals}"
    grand = sum(dm for t in list(totals.values())[:1] for dm, _ in t.values())
    print(f"\nPASS — all 5 formats produced identical totals ({grand} direct-care minutes over 3 days).")


if __name__ == "__main__":
    main()
