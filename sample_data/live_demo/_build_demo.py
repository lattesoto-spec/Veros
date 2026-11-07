"""Reproducible synthetic two-file demos; no patient data or network access."""

from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import Workbook

from sample_data.demo_video._build_demo import _people, _workforce, daterange


def build(output):
    output = Path(output)
    start, end = date(2026, 10, 1), date(2026, 11, 2)
    people, workforce = _people(), _workforce()
    for slug, minutes in (
        ("01_banksia", {"RN": 1800, "EN": 900, "PCW": 6288}),
        ("02_wattle", {"RN": 1800, "EN": 900, "PCW": 6738}),
        ("03_waratah", {"RN": 1350, "EN": 450, "PCW": 7638}),
    ):
        folder = output / slug
        folder.mkdir(parents=True, exist_ok=True)
        census = Workbook()
        register = census.active
        register.title = "Resident Register"
        register.append(["Resident ID", "Resident Name", "AN-ACC Class", "Admission Date",
                         "Discharge Date", "Status", "Room", "Wing"])
        for person in people:
            register.append([person["id"], person["name"], person["an_acc_class"],
                             person["admitted"], None, "Active", person["room"], person["wing"]])
        ledger = census.create_sheet("Daily Occupancy")
        ledger.append(["Census Date", "Resident ID", "Resident Name", "Occupied Bed Day",
                       "Service Type", "Leave Type", "Leave Day", "AN-ACC Class", "Exclusion Reason"])
        for day in [*daterange(date(2026, 6, 1), date(2026, 8, 31)), *daterange(start, end)]:
            for person in people:
                ledger.append([day, person["id"], person["name"], "Yes", "Permanent",
                               "", "", person["an_acc_class"], ""])
        census.save(folder / "resident_census.xlsx")

        worked = Workbook()
        directory = worked.active
        directory.title = "Employee Directory"
        directory.append(["Employee ID", "Employee Name", "Position Title", "Employment Type",
                          "AHPRA Registration", "Registration Expiry", "Status", "Home Facility"])
        for worker in workforce:
            directory.append([worker["id"], worker["name"], worker["title"], worker["employment"],
                              worker["registration"], worker["registration_expiry"], "Active", slug])
        shifts = worked.create_sheet("Approved Time Entries")
        shifts.append(["Source Record", "Employee ID", "Employee Name", "Position Title", "Work Date",
                       "Actual Start", "Actual Finish", "Unpaid Break (min)", "Direct Care", "Agency",
                       "Approval Status", "Labour Cost", "Comment"])
        for day in daterange(start, end):
            for role, daily_minutes in minutes.items():
                remaining = daily_minutes - (120 if slug == "01_banksia" and role == "RN" and day == end else 0)
                for worker in (w for w in workforce if w["role"] == role):
                    if remaining <= 0:
                        break
                    duration = min(450, remaining)
                    finish = (datetime.combine(day, time(8)) + timedelta(minutes=duration + 30)).time()
                    shifts.append([f"S-{shifts.max_row}", worker["id"], worker["name"], worker["title"],
                                   day, time(8), finish, 30, "Yes", "No", "Approved", 0, "Synthetic worked time"])
                    remaining -= duration
                assert remaining == 0
        worked.save(folder / "actual_worked_time.xlsx")


if __name__ == "__main__":
    build(Path(__file__).resolve().parent)
