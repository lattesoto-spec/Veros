"""Generate 7 differently-formatted exports of the SAME underlying roster.

Every file encodes the identical set of worked shifts (13-15 Apr 2026), so a
correct ingestion engine must produce identical care-minute totals from all
seven. Formats deliberately vary: delimiter, header names, date/time styles,
12h vs 24h, durations vs start/end, Excel with preamble rows, nested JSON,
SQL dumps, junk rows to filter, split name columns, role vocabularies.
"""

import csv
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# Canonical shifts: (staff_id, first, last, role, date, start, end, direct)
STAFF = {
    "S001": ("Alice", "Nguyen", "RN"),
    "S002": ("Ben", "Carter", "RN"),
    "S003": ("Carla", "Diaz", "EN"),
    "S004": ("David", "Okafor", "PCW"),
    "S005": ("Emma", "Fischer", "PCW"),
    "S006": ("Frank", "Gray", "PCW"),
    "S007": ("Grace", "Hall", "ADMIN"),
}
SHIFTS = []
for day in ("2026-04-13", "2026-04-14", "2026-04-15"):
    SHIFTS += [
        ("S001", day, "07:00", "15:00", True),
        ("S003", day, "07:00", "15:00", True),
        ("S004", day, "07:00", "13:30", True),
        ("S007", day, "09:00", "17:00", False),  # admin, not direct care
    ]
SHIFTS += [
    ("S002", "2026-04-13", "15:00", "23:00", True),
    ("S002", "2026-04-14", "15:00", "23:00", True),
    ("S005", "2026-04-13", "13:30", "21:00", True),
    ("S005", "2026-04-15", "13:30", "21:00", True),
    ("S006", "2026-04-14", "22:00", "23:30", True),
]

ROLE_WORDS = {
    "RN": ("Registered Nurse", "RN Level 1"),
    "EN": ("Enrolled Nurse", "EEN"),
    "PCW": ("Personal Care Assistant", "Care Worker Gr2"),
    "ADMIN": ("Administration Officer", "Admin"),
}


def _minutes(start, end):
    s = datetime.strptime(start, "%H:%M")
    e = datetime.strptime(end, "%H:%M")
    return (e - s).seconds // 60


def fmt1_simple():
    """Native-style clean CSV."""
    with open(os.path.join(HERE, "format1_simple.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["staff_id", "staff_name", "role", "date", "start_time", "end_time", "is_direct_care"])
        for sid, day, start, end, direct in SHIFTS:
            first, last, role = STAFF[sid]
            w.writerow([sid, f"{first} {last}", role, day, start, end, str(direct).lower()])


def fmt2_humanforce():
    """Semicolon-delimited, split names, 12h clock, DD/MM/YYYY, leave rows."""
    path = os.path.join(HERE, "format2_humanforce.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Record Type", "Employee Code", "Surname", "First Name",
                    "Classification", "Shift Date", "Start", "Finish", "Cost Centre"])
        for sid, day, start, end, direct in SHIFTS:
            first, last, role = STAFF[sid]
            d = datetime.strptime(day, "%Y-%m-%d").strftime("%d/%m/%Y")
            s12 = datetime.strptime(start, "%H:%M").strftime("%I:%M %p").lstrip("0")
            e12 = datetime.strptime(end, "%H:%M").strftime("%I:%M %p").lstrip("0")
            cc = "Residential Care" if direct else "Corporate Services"
            w.writerow(["Shift", sid.replace("S", "EMP-"), last, first,
                        ROLE_WORDS[role][0], d, s12, e12, cc])
        # junk rows a real export would contain
        w.writerow(["Leave", "EMP-001", "Nguyen", "Alice", ROLE_WORDS["RN"][0],
                    "16/04/2026", "", "", "Residential Care"])
        w.writerow(["Leave", "EMP-004", "Okafor", "David", ROLE_WORDS["PCW"][0],
                    "16/04/2026", "", "", "Residential Care"])


def fmt3_alayacare_xlsx():
    """Excel workbook with preamble rows and full datetimes."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Visits"
    ws.append(["AlayaCare — Shift Export"])
    ws.append(["Period: 13 Apr 2026 to 15 Apr 2026"])
    ws.append([])
    ws.append(["Employee ID", "Employee Name", "Position", "Visit Start", "Visit End", "Service Type"])
    for sid, day, start, end, direct in SHIFTS:
        first, last, role = STAFF[sid]
        ws.append([
            sid.replace("S", "AC"),
            f"{last}, {first}",
            ROLE_WORDS[role][1],
            f"{day} {start}:00",
            f"{day} {end}:00",
            "Direct Care" if direct else "Administration",
        ])
    wb.save(os.path.join(HERE, "format3_alayacare.xlsx"))


def fmt4_payroll_hours():
    """Duration-based payroll export: decimal hours, dotted dates, no times."""
    with open(os.path.join(HERE, "format4_payroll_hours.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["EmpNo", "Employee", "Position Title", "WorkDate", "PayCategory", "HoursWorked"])
        for sid, day, start, end, direct in SHIFTS:
            first, last, role = STAFF[sid]
            d = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
            hours = _minutes(start, end) / 60
            cat = "Direct Care Hours" if direct else "Non-Care Hours"
            w.writerow([sid.replace("S00", "9"), f"{first} {last}", ROLE_WORDS[role][0], d, cat, f"{hours:.2f}"])


def fmt5_tsv():
    """Tab-separated, reordered columns, '13 Apr 2026' dates, 0700 times."""
    with open(os.path.join(HERE, "format5_roster.txt"), "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["Date Worked", "Dept", "Finish Time", "Start Time", "Worker", "Grade", "Ref"])
        for sid, day, start, end, direct in SHIFTS:
            first, last, role = STAFF[sid]
            d = datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
            w.writerow([
                d,
                "Care" if direct else "Admin",
                end.replace(":", ""),
                start.replace(":", ""),
                f"{first} {last}",
                ROLE_WORDS[role][1],
                f"W-{sid[1:]}",
            ])


def fmt6_api_json():
    """Nested JSON API extract: dotted paths, ISO datetimes, ragged keys."""
    import json

    records = []
    for sid, day, start, end, direct in SHIFTS:
        first, last, role = STAFF[sid]
        rec = {
            "employee": {"code": sid.replace("S", "EMP"), "displayName": f"{first} {last}"},
            "classification": {"title": ROLE_WORDS[role][0]},
            "startsAt": f"{day}T{start}:00",
            "endsAt": f"{day}T{end}:00",
            "serviceType": "direct_care" if direct else "administration",
            "tags": ["rostered", "confirmed"],
        }
        if not direct:  # only some records carry this key
            rec["costCentre"] = "Corporate"
        records.append(rec)
    doc = {
        "exportedAt": "2026-04-16T02:00:00Z",
        "facility": "Test Facility",
        "shifts": records,
    }
    with open(os.path.join(HERE, "format6_api_extract.json"), "w") as f:
        json.dump(doc, f, indent=2)


def fmt7_sql_dump():
    """Database dump: CREATE TABLE + multi-row INSERTs, quoted identifiers,
    a schema-qualified table name, NULLs and leave rows to filter out."""
    lines = [
        "-- Roster database dump",
        "/*!40101 SET NAMES utf8 */;",
        "CREATE TABLE `roster_entries` (",
        "  `entry_id` int NOT NULL AUTO_INCREMENT,",
        "  `emp_ref` varchar(20) NOT NULL,",
        "  `emp_name` varchar(100) DEFAULT NULL,",
        "  `grade` varchar(50) DEFAULT NULL,",
        "  `duty_date` date DEFAULT NULL,",
        "  `commenced` time DEFAULT NULL,",
        "  `ceased` time DEFAULT NULL,",
        "  `entry_type` varchar(20) DEFAULT NULL,",
        "  PRIMARY KEY (`entry_id`),",
        "  KEY `idx_duty_date` (`duty_date`)",
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
    ]
    values = []
    for i, (sid, day, start, end, direct) in enumerate(SHIFTS, start=1):
        first, last, role = STAFF[sid]
        d = datetime.strptime(day, "%Y-%m-%d").strftime("%d/%m/%Y")
        entry = "CARE" if direct else "ADMIN"
        values.append(
            f"({i},'{sid.replace('S', 'R')}','{last}, {first}','{ROLE_WORDS[role][1]}',"
            f"'{d}','{start}:00','{end}:00','{entry}')"
        )
    # Non-shift rows a real dump carries; the mapping must filter them out.
    values.append(f"({len(SHIFTS) + 1},'R001','Nguyen, Alice','RN Level 1','16/04/2026',NULL,NULL,'LEAVE')")
    values.append(f"({len(SHIFTS) + 2},'R004','Okafor, David','Care Worker Gr2','16/04/2026',NULL,NULL,'LEAVE')")
    lines.append(
        "INSERT INTO `payroll`.`roster_entries` "
        "(`entry_id`,`emp_ref`,`emp_name`,`grade`,`duty_date`,`commenced`,`ceased`,`entry_type`) VALUES\n"
        + ",\n".join(values) + ";"
    )
    with open(os.path.join(HERE, "format7_dbdump.sql"), "w") as f:
        f.write("\n".join(lines) + "\n")


def residents_xlsx():
    """Resident list in a non-native shape."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Clients"
    ws.append(["Client Ref", "Given Name", "Family Name", "AN-ACC Class", "Admission Date", "Departure Date"])
    for i in range(1, 31):
        ws.append([f"C{i:03d}", f"Res{i}", f"Family{i}", f"{(i % 13) + 1:02d}", "01/03/2026", ""])
    wb.save(os.path.join(HERE, "residents_clients.xlsx"))


if __name__ == "__main__":
    fmt1_simple()
    fmt2_humanforce()
    fmt3_alayacare_xlsx()
    fmt4_payroll_hours()
    fmt5_tsv()
    fmt6_api_json()
    fmt7_sql_dump()
    residents_xlsx()
    direct = sum(_minutes(s, e) for _, _, s, e, d in SHIFTS if d)
    print(f"Wrote 8 fixture files. Canonical direct-care minutes across 3 days: {direct}")
