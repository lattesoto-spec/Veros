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
            "Resident ID", "Resident Name", "AN-ACC Class", "Admission Date",
            "Discharge Date", "Status", "Room", "Wing",
        }.issubset(headers):
            targets.append({
                "kind": "residents", "sheet": sheet.name,
                "fail_on_row_error": True,
                "fields": {
                    "resident_id": {"column": "Resident ID"},
                    "name": {"column": "Resident Name"},
                    "ancc_class": {"column": "AN-ACC Class"},
                    "admitted_date": {"column": "Admission Date", "parse": "date"},
                    "discharged_date": {"column": "Discharge Date", "parse": "date"},
                },
            })
        elif {
            "Census Date", "Resident ID", "Resident Name", "Occupied Bed Day",
            "Service Type", "Leave Type", "Leave Day", "AN-ACC Class",
            "Exclusion Reason",
        }.issubset(headers):
            targets.append({
                "kind": "resident_days", "sheet": sheet.name,
                "fail_on_row_error": True,
                "fields": {
                    "date": {"column": "Census Date", "parse": "date"},
                    "resident_id": {"column": "Resident ID"},
                    "resident_name": {"column": "Resident Name"},
                    "occupied": {"column": "Occupied Bed Day", "parse": "boolean", "required": True},
                    "service_type": {"column": "Service Type"},
                    "leave_type": {"column": "Leave Type"},
                    "leave_day_number": {"column": "Leave Day", "parse": "number"},
                    "ancc_class": {"column": "AN-ACC Class"},
                    "exclusion_reason": {"column": "Exclusion Reason"},
                },
            })
        elif {
            "Employee ID", "Employee Name", "Position Title", "Employment Type",
            "AHPRA Registration", "Registration Expiry", "Status", "Home Facility",
        }.issubset(headers):
            targets.append({
                "kind": "staff", "sheet": sheet.name,
                "fail_on_row_error": True,
                "row_filter": {"column": "Status", "include_values": ["active"]},
                "fields": {
                    "staff_id": {"column": "Employee ID"},
                    "staff_name": {"column": "Employee Name"},
                    "role": {"column": "Position Title", "normalize": "role"},
                    "employment_type": {"column": "Employment Type"},
                    "registration_number": {"column": "AHPRA Registration"},
                    "registration_expiry": {"column": "Registration Expiry", "parse": "date"},
                },
            })
        elif {
            "Source Record", "Employee ID", "Employee Name", "Position Title",
            "Work Date", "Actual Start", "Actual Finish", "Unpaid Break (min)",
            "Direct Care", "Agency", "Approval Status", "Labour Cost", "Comment",
        }.issubset(headers):
            targets.append({
                "kind": "shifts", "sheet": sheet.name,
                "fail_on_row_error": True,
                "row_filter": {"column": "Approval Status", "include_values": ["approved"]},
                "fields": {
                    "staff_id": {"column": "Employee ID"},
                    "staff_name": {"column": "Employee Name"},
                    "role": {"column": "Position Title", "normalize": "role"},
                    "date": {"column": "Work Date", "parse": "date"},
                    "start_time": {"column": "Actual Start", "parse": "time"},
                    "end_time": {"column": "Actual Finish", "parse": "time"},
                    "break_minutes": {"column": "Unpaid Break (min)", "parse": "number"},
                    "is_direct_care": {"column": "Direct Care", "parse": "boolean", "required": True},
                    "is_agency": {"column": "Agency", "parse": "boolean", "required": True},
                    "labour_cost": {"column": "Labour Cost", "parse": "number"},
                },
            })
        elif {
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
