"""Deterministic mappings for common, unambiguous evidence layouts.

These are deliberately exact header-set matches. They avoid an AI call for
obvious formats without turning fuzzy guessing into silent regulated data.
Anything that does not match precisely still goes through the analyzer.
"""


def matching_spec(sheets) -> dict | None:
    targets = []
    for sheet in sheets:
        headers = set(sheet.headers)
        if {
            "Date", "Staff ID", "Staff Name", "Role", "Shift Start", "Shift End",
            "Unpaid Break (min)", "Counts Toward Care Minutes",
        }.issubset(headers):
            targets.append({
                "kind": "shifts", "sheet": sheet.name,
                "row_filter": {"column": "Counts Toward Care Minutes", "include_values": ["y"]},
                "fields": {
                    "staff_id": {"column": "Staff ID"},
                    "staff_name": {"column": "Staff Name"},
                    "role": {"column": "Role", "normalize": "role"},
                    "date": {"column": "Date", "parse": "date"},
                    "start_time": {"column": "Shift Start", "parse": "time"},
                    "end_time": {"column": "Shift End", "parse": "time"},
                    "break_minutes": {"column": "Unpaid Break (min)", "parse": "number"},
                    "is_direct_care": {"column": "Counts Toward Care Minutes", "parse": "boolean"},
                },
            })
        elif {
            "Date", "Resident ID", "Resident Name", "Care Type", "Care Category",
            "Staff Name", "Start Time", "End Time", "Duration (min)",
        }.issubset(headers):
            targets.append({
                "kind": "care_episodes", "sheet": sheet.name,
                "fields": {
                    "date": {"column": "Date", "parse": "date"},
                    "resident_id": {"column": "Resident ID"},
                    "resident_name": {"column": "Resident Name"},
                    "care_type": {"column": "Care Type"},
                    "role": {"column": "Care Category", "normalize": "role"},
                    "staff_name": {"column": "Staff Name"},
                    "start_time": {"column": "Start Time", "parse": "time"},
                    "end_time": {"column": "End Time", "parse": "time"},
                    "minutes": {"column": "Duration (min)", "parse": "number"},
                },
            })
        elif {
            "Date", "Resident ID", "Resident Name", "RN Minutes Delivered",
            "Total Care Minutes Delivered",
        }.issubset(headers):
            targets.append({
                "kind": "resident_days", "sheet": sheet.name,
                "fields": {
                    "date": {"column": "Date", "parse": "date"},
                    "resident_id": {"column": "Resident ID"},
                    "resident_name": {"column": "Resident Name"},
                    "occupied": {"source": "constant", "value": True},
                    "service_type": {"source": "constant", "value": "an_acc"},
                },
            })
    if not targets:
        return None
    return {"targets": targets, "reasoning": "exact built-in evidence layout"}
