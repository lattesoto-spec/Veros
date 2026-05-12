"""Generate sample CSVs for demos. Outputs sample_data/residents.csv and shifts.csv.

The data is shaped so the dashboard reads ~210 mins/resident against a 217 target
(roughly 3% below) which produces amber/red bars and a credible "you are slipping" story.
"""
import csv
import os
import random
from datetime import date, datetime, time, timedelta

random.seed(42)

OUT_DIR = os.path.join(os.path.dirname(__file__), "sample_data")
os.makedirs(OUT_DIR, exist_ok=True)

TODAY = date(2026, 5, 12)
DAYS = 30
RESIDENTS = 85
ANCC_CLASSES = ["02-01", "03-02", "05-03", "07-02", "08-02", "10-03", "13-04"]

FIRST_NAMES = ["Joan", "Peter", "Mary", "John", "Margaret", "David", "Patricia", "Robert",
               "Jennifer", "Michael", "Linda", "William", "Elizabeth", "James", "Barbara",
               "Richard", "Susan", "Thomas", "Jessica", "Charles", "Sarah", "Christopher",
               "Karen", "Daniel", "Nancy", "Matthew", "Lisa", "Anthony", "Betty", "Mark",
               "Helen", "Donald", "Sandra", "Steven", "Donna", "Paul", "Carol", "Andrew",
               "Ruth", "Joshua"]
LAST_NAMES = ["Smith", "Jones", "Brown", "Wilson", "Taylor", "Johnson", "White", "Martin",
              "Anderson", "Thompson", "Nguyen", "Walker", "Harris", "Lewis", "Robinson",
              "Clark", "Lee", "King", "Wright", "Hill", "Scott", "Green", "Baker", "Adams",
              "Nelson", "Hall", "Mitchell", "Roberts", "Carter", "Phillips"]

# Roster targets per shift. The "behind" rooster: morning -1 PCA, afternoon -1 PCA on some days.
SHIFT_BLOCKS = [
    ("morning", time(7, 0), time(15, 0), {"RN": 2, "EN": 2, "PCA": 6}),
    ("afternoon", time(15, 0), time(23, 0), {"RN": 1, "EN": 2, "PCA": 5}),
    ("night", time(23, 0), time(7, 0), {"RN": 1, "EN": 1, "PCA": 3}),
]


def _name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def write_residents():
    path = os.path.join(OUT_DIR, "residents.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["resident_id", "name", "ancc_class", "admitted_date", "discharged_date"])
        for i in range(1, RESIDENTS + 1):
            rid = f"R{i:03d}"
            admit = TODAY - timedelta(days=random.randint(60, 1500))
            # ~5 discharged within last quarter
            discharged = ""
            if i > RESIDENTS - 5:
                discharged = (TODAY - timedelta(days=random.randint(5, 80))).isoformat()
            w.writerow([rid, _name(), random.choice(ANCC_CLASSES), admit.isoformat(), discharged])
    print(f"wrote {path}")


def _build_staff():
    staff = []
    sid = 1
    for role, count in (("RN", 6), ("EN", 6), ("PCA", 18)):
        for _ in range(count):
            staff.append({"staff_id": f"S{sid:03d}", "name": _name(), "role": role})
            sid += 1
    return staff


def write_shifts(staff):
    path = os.path.join(OUT_DIR, "shifts.csv")
    by_role = {r: [s for s in staff if s["role"] == r] for r in ("RN", "EN", "PCA")}

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["staff_id", "staff_name", "role", "date", "start_time", "end_time", "is_direct_care"])

        for offset in range(DAYS, 0, -1):
            d = TODAY - timedelta(days=offset - 1)
            # Make ~60% of days slightly understaffed so we hit the "3% behind" story.
            understaff = random.random() < 0.6

            for block_name, start, end, targets in SHIFT_BLOCKS:
                for role, base_count in targets.items():
                    count = base_count
                    if understaff and role == "PCA" and block_name in ("morning", "afternoon"):
                        count -= 1
                    count += random.choice([-1, 0, 0, 0, 1])
                    count = max(1, count)

                    picks = random.sample(by_role[role], min(count, len(by_role[role])))
                    for s in picks:
                        direct = "true" if random.random() < 0.9 else "false"
                        w.writerow([s["staff_id"], s["name"], s["role"],
                                    d.isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"), direct])
    print(f"wrote {path}")


if __name__ == "__main__":
    write_residents()
    staff = _build_staff()
    write_shifts(staff)
    print("done")
