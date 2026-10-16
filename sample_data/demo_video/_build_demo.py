"""Generate the fixed CareMin walkthrough workbook.

The workbook is intentionally synthetic and deterministic.  It contains a
complete resident-day ledger, current staff credentials and actual worked-time
records.  The reporting period passes both nursing tests but falls just below
the overall care-minute target.  The latest included Tuesday also has a
one-hour RN coverage gap, backed by an explicit unfilled source row.

Run from the repository root:

    python -m sample_data.demo_video._build_demo
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


OUT = Path(__file__).resolve().parent
OUTPUT = OUT / "caremin_demo_actual_worked_export.xlsx"

FACILITY = "Banksia Grove Residential Care"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 8, 25)  # Tuesday, and the latest included date.
REFERENCE_START = date(2026, 3, 1)
REFERENCE_END = date(2026, 5, 31)
RN_GAP_DAY = PERIOD_END

CARE_TARGET = 215.0
RN_TARGET = 44.0

NAVY = "19324D"
TEAL = "167D86"
PALE_BLUE = "EAF0F6"
PALE_RED = "FBE4E6"
WHITE = "FFFFFF"
INK = "243341"
GRID = "D7DEE5"
THIN = Side(style="thin", color=GRID)

FIRST_NAMES = [
    "Amelia", "Noah", "Olivia", "Jack", "Isla", "William", "Mia", "Henry",
    "Charlotte", "Thomas", "Grace", "James", "Sophie", "Leo", "Ruby", "Lucas",
    "Chloe", "Ethan", "Zoe", "Samuel", "Matilda", "Alexander", "Ella", "Daniel",
    "Evie", "Liam", "Ava", "Benjamin", "Lucy", "Oliver", "Hannah", "Max",
    "Emily", "Joshua", "Georgia", "Oscar", "Lily", "Sebastian", "Claire", "Isaac",
    "Margaret", "Arthur", "Helen", "George", "Doreen", "Ronald", "Judith", "Peter",
    "Irene", "Kenneth", "Janet", "Graham", "Patricia", "Raymond", "Sylvia", "Colin",
    "Maureen", "Frank", "Valerie", "Barry", "Nora", "Albert", "Jean", "Keith",
]

LAST_NAMES = [
    "Nguyen", "Martin", "Thompson", "Brown", "Wilson", "Taylor", "Anderson", "Lee",
    "Harris", "Clark", "Walker", "Wright", "Roberts", "King", "Hall", "Young",
    "Allen", "Scott", "Green", "Baker", "Campbell", "Mitchell", "Carter", "Evans",
    "Collins", "Murphy", "Kelly", "Singh", "Patel", "Williams", "Jones", "Lewis",
    "Robinson", "Edwards", "Stewart", "Morris", "Cooper", "Cook", "Morgan", "Bell",
    "Reed", "Bailey", "Rivera", "Foster", "Ward", "Gray", "James", "Watson",
    "Brooks", "Wood", "Bennett", "Price", "Hughes", "Russell", "Griffin", "Diaz",
    "Hayes", "Myers", "Ford", "Hamilton", "Graham", "Sullivan", "Wallace", "Woods",
]

AN_ACC_AMOUNTS = {
    "Class 3": (178, 36),
    "Class 9": (214, 44),
    "Class 11": (253, 48),
}


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _title(ws, title: str, subtitle: str, width: int, note: str = ""):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    ws.cell(1, 1, title)
    ws.cell(1, 1).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(1, 1).font = Font(name="Aptos Display", size=17, bold=True, color=WHITE)
    ws.cell(1, 1).alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 29

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
    ws.cell(2, 1, subtitle)
    ws.cell(2, 1).fill = PatternFill("solid", fgColor=PALE_BLUE)
    ws.cell(2, 1).font = Font(name="Aptos", size=10, italic=True, color=INK)
    ws.row_dimensions[2].height = 20

    if note:
        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=width)
        ws.cell(3, 1, note)
        ws.cell(3, 1).font = Font(name="Aptos", size=9, color=INK)
    ws.sheet_view.showGridLines = False


def _table(ws, header_row: int, widths: list[int]):
    for cell in ws[header_row][:len(widths)]:
        cell.fill = PatternFill("solid", fgColor=TEAL)
        cell.font = Font(name="Aptos", size=10, bold=True, color=WHITE)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="medium", color=NAVY))
    ws.row_dimensions[header_row].height = 34
    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = (
        f"A{header_row}:{get_column_letter(len(widths))}{ws.max_row}"
    )
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    for row in ws.iter_rows(
        min_row=header_row + 1, max_row=ws.max_row, max_col=len(widths)
    ):
        for cell in row:
            cell.font = Font(name="Aptos", size=9, color=INK)
            cell.border = Border(bottom=THIN)
            cell.alignment = Alignment(vertical="top")
        if row[0].row % 2 == 0:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor="F7F9FB")


def _summary_sheet(ws, summary: dict):
    _title(
        ws,
        "Payroll, Workforce and Census Export",
        "Synthetic source-system extract for a CareMin product demonstration",
        5,
    )
    ws["A4"] = "Export field"
    ws["B4"] = "Value"
    for cell in ws[4][:2]:
        cell.fill = PatternFill("solid", fgColor=TEAL)
        cell.font = Font(bold=True, color=WHITE)
    rows = [
        ("Entity", "Banksia Grove Care Services Pty Ltd"),
        ("Facility", FACILITY),
        ("Worked-time period", f"{PERIOD_START:%d/%m/%Y} to {PERIOD_END:%d/%m/%Y}"),
        ("Generated", "27/08/2026 09:15 AEST"),
        ("Resident master records", summary["residents"]),
        ("Resident-day ledger rows", summary["resident_days"]),
        ("Active workforce records", summary["staff"]),
        ("Approved worked shifts", summary["approved_shifts"]),
        ("Unfilled / non-worked rows", summary["non_worked_rows"]),
        ("Source system", "WorkTrack + HarbourCare consolidated export"),
    ]
    for row_index, (label, value) in enumerate(rows, start=5):
        ws.cell(row_index, 1, label)
        ws.cell(row_index, 2, f"{value:,}" if isinstance(value, int) else value)
        ws.cell(row_index, 1).font = Font(bold=True, color=INK)
        ws.cell(row_index, 1).fill = PatternFill("solid", fgColor=PALE_BLUE)
        ws.cell(row_index, 1).border = Border(bottom=THIN)
        ws.cell(row_index, 2).font = Font(name="Aptos", size=10, color=INK)
        ws.cell(row_index, 2).alignment = Alignment(vertical="center", wrap_text=True)
        ws.cell(row_index, 2).border = Border(bottom=THIN)
        ws.row_dimensions[row_index].height = 23
    ws.column_dimensions["A"].width = 31
    ws.column_dimensions["B"].width = 48


def _people():
    residents = []
    # This precise mix independently reconciles to 215 total and 44 RN target
    # minutes: 1 x Class 3, 39 x Class 9 and 2 x Class 11.
    classes = ["Class 3"] + ["Class 11"] * 2 + ["Class 9"] * 39
    for index in range(42):
        residents.append({
            "id": f"BG-{1001 + index}",
            "name": f"{FIRST_NAMES[index]} {LAST_NAMES[(index * 5 + 3) % len(LAST_NAMES)]}",
            "an_acc_class": classes[index],
            "admitted": date(2023 + index % 3, 1 + index % 12, 1 + index % 24),
            "room": f"{101 + index}{'A' if index % 11 == 0 else ''}",
            "wing": ("Banksia", "Wattle", "Waratah")[index % 3],
        })
    return residents


def _workforce():
    specs = [
        ("RN", 8, "Registered Nurse", 58.40),
        ("EN", 6, "Enrolled Nurse", 45.20),
        ("PCW", 28, "Personal Care Worker", 35.10),
    ]
    workforce = []
    person_index = 0
    for role, count, title, rate in specs:
        for role_index in range(count):
            name_index = 42 + person_index
            workforce.append({
                "id": f"E{5001 + person_index}",
                "name": (
                    f"{FIRST_NAMES[name_index % len(FIRST_NAMES)]} "
                    f"{LAST_NAMES[(name_index * 7) % len(LAST_NAMES)]}"
                ),
                "role": role,
                "title": title,
                "employment": "Agency" if role == "PCW" and role_index >= 25 else "Employee",
                "rate": rate + (12.0 if role == "PCW" and role_index >= 25 else 0),
                "registration": f"NMW000{88001 + person_index}" if role in ("RN", "EN") else "",
                "registration_expiry": date(2027, 6 + role_index % 6, 28) if role in ("RN", "EN") else None,
            })
            person_index += 1
    return workforce


def _worked_rows(workforce):
    by_role = {
        role: [worker for worker in workforce if worker["role"] == role]
        for role in ("RN", "EN", "PCW")
    }
    rows = []
    source_number = 0

    def add(day, worker, start, finish, *, status="Approved", direct="Yes", comment=""):
        nonlocal source_number
        source_number += 1
        start_dt = datetime.combine(day, start)
        finish_dt = datetime.combine(day, finish)
        if finish_dt <= start_dt:
            finish_dt += timedelta(days=1)
        hours = (finish_dt - start_dt).total_seconds() / 3600
        break_minutes = 30 if status == "Approved" else 0
        paid_hours = max(hours - break_minutes / 60, 0) if status == "Approved" else 0
        cost = round(paid_hours * worker.get("rate", 0), 2)
        rows.append([
            f"WT-{day:%y%m%d}-{source_number:05d}", worker["id"], worker["name"],
            worker["title"], day, start, finish, break_minutes, direct,
            "Yes" if worker.get("employment") == "Agency" else "No",
            status, cost, comment,
        ])

    # The previous night's worked shift closes the midnight-to-07:00 boundary
    # on the first reporting day.  It is deliberately outside the numerator's
    # reporting period but is valid source evidence for 24-hour coverage.
    add(
        PERIOD_START - timedelta(days=1), by_role["RN"][4],
        time(23, 0), time(7, 0), comment="Night shift continuing into reporting period",
    )

    report_days = list(daterange(PERIOD_START, PERIOD_END))
    for day_index, day in enumerate(report_days):
        rn_team = by_role["RN"][:4] if day_index % 2 == 0 else by_role["RN"][4:]
        rn_windows = [
            (time(23, 0), time(7, 0)),
            (time(7, 0), time(15, 0)),
            (time(7, 0), time(15, 0)),
            (time(15, 0), time(23, 0)),
        ]
        if day == RN_GAP_DAY:
            rn_windows[1] = (time(8, 0), time(15, 0))
            rn_windows[2] = (time(8, 0), time(15, 0))
        for worker, (start, finish) in zip(rn_team, rn_windows):
            add(day, worker, start, finish)

        en_workers = by_role["EN"]
        add(day, en_workers[(day_index * 2) % len(en_workers)], time(7, 0), time(15, 0))
        add(day, en_workers[(day_index * 2 + 1) % len(en_workers)], time(15, 0), time(23, 0))

        # Tuesdays and alternating Saturdays are visibly below target. Thursdays
        # sit above target, leaving a believable mixed trend whose weighted
        # quarter-to-date result remains slightly below 215 minutes.
        if day.weekday() == 1 or (day.weekday() == 5 and day.day % 2 == 0):
            pcw_count = 13
        elif day.weekday() == 3:
            pcw_count = 15
        else:
            pcw_count = 14
        pcw_windows = (
            [(time(7, 0), time(15, 0))] * 7
            + [(time(15, 0), time(23, 0))] * 5
            + [(time(23, 0), time(7, 0))] * 3
        )
        pcw_workers = by_role["PCW"]
        start_index = (day_index * 11) % len(pcw_workers)
        selected = [pcw_workers[(start_index + offset) % len(pcw_workers)] for offset in range(pcw_count)]
        for worker, (start, finish) in zip(selected, pcw_windows[:pcw_count]):
            add(day, worker, start, finish)

        if day == RN_GAP_DAY:
            vacant = {
                "id": "VAC-RN-0825", "name": "Unfilled RN relief",
                "title": "Registered Nurse", "employment": "Unfilled", "rate": 0,
            }
            add(
                day, vacant, time(7, 0), time(8, 0), status="Not Worked", direct="No",
                comment="Agency replacement not secured; RN coverage gap escalated",
            )
    return rows


def _resident_sheets(wb, residents):
    master = wb.create_sheet("Resident Register")
    _title(
        master, f"{FACILITY} — Resident Register",
        "Resident administration master extract", 8,
        "All identifiers and names in this workbook are fictional.",
    )
    master.append([])
    master.append([
        "Resident ID", "Resident Name", "AN-ACC Class", "Admission Date",
        "Discharge Date", "Status", "Room", "Wing",
    ])
    for resident in residents:
        master.append([
            resident["id"], resident["name"], resident["an_acc_class"],
            resident["admitted"], None, "Active", resident["room"], resident["wing"],
        ])
    _table(master, 5, [16, 24, 16, 16, 16, 13, 12, 14])
    for row in range(6, master.max_row + 1):
        master.cell(row, 4).number_format = "dd/mm/yyyy"
        master.cell(row, 5).number_format = "dd/mm/yyyy"

    ledger = wb.create_sheet("Daily Occupancy")
    _title(
        ledger, f"{FACILITY} — Daily Occupancy Ledger",
        "Statutory reference period plus current quarter-to-date", 9,
        "One source row per resident per occupied bed day.",
    )
    ledger.append([])
    ledger.append([
        "Census Date", "Resident ID", "Resident Name", "Occupied Bed Day",
        "Service Type", "Leave Type", "Leave Day", "AN-ACC Class",
        "Exclusion Reason",
    ])
    ledger_days = list(daterange(REFERENCE_START, REFERENCE_END)) + list(
        daterange(PERIOD_START, PERIOD_END)
    )
    for day in ledger_days:
        for resident in residents:
            ledger.append([
                day, resident["id"], resident["name"], "Yes", "Permanent",
                "", "", resident["an_acc_class"], "",
            ])
    _table(ledger, 5, [15, 16, 24, 18, 16, 15, 12, 16, 24])
    for row in range(6, ledger.max_row + 1):
        ledger.cell(row, 1).number_format = "dd/mm/yyyy"
    return len(ledger_days) * len(residents)


def _workforce_sheets(wb, workforce, worked_rows):
    directory = wb.create_sheet("Employee Directory")
    _title(
        directory, f"{FACILITY} — Employee Directory",
        "Current direct-care workforce and Ahpra credential extract", 8,
        "Agency personnel are included where they appear in worked-time records.",
    )
    directory.append([])
    directory.append([
        "Employee ID", "Employee Name", "Position Title", "Employment Type",
        "AHPRA Registration", "Registration Expiry", "Status", "Home Facility",
    ])
    for worker in workforce:
        directory.append([
            worker["id"], worker["name"], worker["title"], worker["employment"],
            worker["registration"], worker["registration_expiry"], "Active", FACILITY,
        ])
    _table(directory, 5, [16, 24, 24, 18, 22, 19, 13, 31])
    for row in range(6, directory.max_row + 1):
        directory.cell(row, 6).number_format = "dd/mm/yyyy"

    shifts = wb.create_sheet("Approved Time Entries")
    _title(
        shifts, f"{FACILITY} — Actual Worked Time",
        f"Approved payroll lines | {PERIOD_START:%d %b %Y} to {PERIOD_END:%d %b %Y}",
        13,
        "Approved and exception lines are retained so the source export remains complete.",
    )
    shifts.append([])
    shifts.append([
        "Source Record", "Employee ID", "Employee Name", "Position Title",
        "Work Date", "Actual Start", "Actual Finish", "Unpaid Break (min)",
        "Direct Care", "Agency", "Approval Status", "Labour Cost", "Comment",
    ])
    for row in worked_rows:
        shifts.append(row)
    _table(shifts, 5, [20, 15, 23, 24, 14, 14, 14, 18, 14, 12, 17, 16, 45])
    for row in range(6, shifts.max_row + 1):
        shifts.cell(row, 5).number_format = "dd/mm/yyyy"
        shifts.cell(row, 6).number_format = "h:mm AM/PM"
        shifts.cell(row, 7).number_format = "h:mm AM/PM"
        shifts.cell(row, 12).number_format = "$#,##0.00"
    shifts.conditional_formatting.add(
        f"K6:K{shifts.max_row}",
        FormulaRule(formula=['K6<>"Approved"'], fill=PatternFill("solid", fgColor=PALE_RED)),
    )


def _minutes(start: time, finish: time, break_minutes: int) -> int:
    start_dt = datetime.combine(PERIOD_START, start)
    finish_dt = datetime.combine(PERIOD_START, finish)
    if finish_dt <= start_dt:
        finish_dt += timedelta(days=1)
    return int((finish_dt - start_dt).total_seconds() // 60) - break_minutes


def _verify(residents, workforce, worked_rows, resident_days):
    report_rows = [
        row for row in worked_rows
        if PERIOD_START <= row[4] <= PERIOD_END and row[10] == "Approved" and row[8] == "Yes"
    ]
    total_minutes = sum(_minutes(row[5], row[6], row[7]) for row in report_rows)
    rn_minutes = sum(
        _minutes(row[5], row[6], row[7]) for row in report_rows
        if row[3] == "Registered Nurse"
    )
    en_minutes = sum(
        _minutes(row[5], row[6], row[7]) for row in report_rows
        if row[3] == "Enrolled Nurse"
    )
    report_bed_days = len(list(daterange(PERIOD_START, PERIOD_END))) * len(residents)
    care_result = round(total_minutes / report_bed_days, 1)
    rn_result = round(rn_minutes / report_bed_days, 1)
    rn_en_result = round((rn_minutes + en_minutes) / report_bed_days, 1)

    target_total = sum(AN_ACC_AMOUNTS[r["an_acc_class"]][0] for r in residents)
    target_rn = sum(AN_ACC_AMOUNTS[r["an_acc_class"]][1] for r in residents)
    assert target_total / len(residents) == CARE_TARGET
    assert target_rn / len(residents) == RN_TARGET
    assert care_result < CARE_TARGET
    assert rn_result >= RN_TARGET * 0.9
    assert rn_en_result >= RN_TARGET
    assert resident_days == (
        len(list(daterange(REFERENCE_START, REFERENCE_END)))
        + len(list(daterange(PERIOD_START, PERIOD_END)))
    ) * len(residents)
    assert any(row[4] == RN_GAP_DAY and row[10] == "Not Worked" for row in worked_rows)
    assert len(workforce) == 42
    return {
        "residents": len(residents),
        "resident_days": resident_days,
        "staff": len(workforce),
        "approved_shifts": sum(row[10] == "Approved" for row in worked_rows),
        "non_worked_rows": sum(row[10] != "Approved" for row in worked_rows),
        "care_result": care_result,
        "rn_result": rn_result,
        "rn_en_result": rn_en_result,
        "bed_days": report_bed_days,
    }


def build():
    residents = _people()
    workforce = _workforce()
    worked_rows = _worked_rows(workforce)

    wb = Workbook()
    summary_sheet = wb.active
    summary_sheet.title = "Export Summary"
    resident_days = _resident_sheets(wb, residents)
    _workforce_sheets(wb, workforce, worked_rows)
    summary = _verify(residents, workforce, worked_rows, resident_days)
    _summary_sheet(summary_sheet, summary)
    wb.save(OUTPUT)

    check = load_workbook(OUTPUT, read_only=True, data_only=True)
    assert check.sheetnames == [
        "Export Summary", "Resident Register", "Daily Occupancy",
        "Employee Directory", "Approved Time Entries",
    ]
    check.close()
    return summary


def main():
    summary = build()
    print(OUTPUT)
    print(f"size_bytes={OUTPUT.stat().st_size}")
    for key, value in summary.items():
        print(f"{key}={value}")
    print(f"rn_gap={RN_GAP_DAY.isoformat()} 07:00-08:00")


if __name__ == "__main__":
    main()
