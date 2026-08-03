"""Builds a self-contained demo organisation.

Sales demos need data that looks real and tells a story — a facility sitting
slightly under its care-minute target, so the dashboard shows amber rather than
a flat green nothing-to-see. That data must never live in a real customer's
tenant, so this creates its own organisation with its own administrator.

Rows are written directly rather than pushed through the import pipeline: the
pipeline would spend an Anthropic call learning a format, and the demo should
be reproducible offline and free.
"""

import random
from datetime import date, datetime, time, timedelta

from carelog import auth
from carelog.domain.compliance import CALC_VERSION
from carelog.models import (
    Facility,
    ImportReceipt,
    Organization,
    Resident,
    Shift,
    Staff,
    db,
)

FIRST_NAMES = [
    "Joan", "Peter", "Mary", "John", "Margaret", "David", "Patricia", "Robert",
    "Jennifer", "Michael", "Linda", "William", "Elizabeth", "James", "Barbara",
    "Richard", "Susan", "Thomas", "Jessica", "Charles", "Sarah", "Grace",
    "Karen", "Daniel", "Nancy", "Matthew", "Lisa", "Anthony", "Betty", "Mark",
]
LAST_NAMES = [
    "Nguyen", "Smith", "Jones", "Brown", "Wilson", "Taylor", "Johnson", "White",
    "Martin", "Anderson", "Thompson", "Walker", "Harris", "Lewis", "Robinson",
    "Clark", "Lee", "King", "Wright", "Hill", "Scott", "Green", "Baker", "Adams",
]
ANCC_CLASSES = ["02-01", "03-02", "05-03", "07-02", "08-02", "10-03", "13-04"]

# (role, count, shift windows) — the mix that produces a credible roster.
ROSTER = [
    ("RN",  4, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0)), (time(23, 0), time(7, 0))]),
    ("EN",  3, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0))]),
    ("PCW", 12, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0)), (time(23, 0), time(7, 0))]),
    ("ADMIN", 2, [(time(9, 0), time(17, 0))]),
]


def build_demo_organization(*, name="Demo", admin_email="demo@caremin.app",
                            password="", days=60, residents=42) -> dict:
    rng = random.Random(20260803)  # fixed seed: the demo looks the same every time

    org = Organization(name=name)
    db.session.add(org)
    db.session.flush()

    auth.create_user(
        organization_id=org.id, email=admin_email, name="Demo Administrator",
        role="administrator", password=password, must_change_password=True,
    )

    facility = Facility(
        organization_id=org.id, name=f"{name} Aged Care", ancc_target=215.0, rn_target=44.0
    )
    db.session.add(facility)
    db.session.flush()

    today = date.today()
    start = today - timedelta(days=days - 1)

    used = set()
    for i in range(residents):
        while True:
            person = (rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
            if person not in used:
                used.add(person)
                break
        db.session.add(Resident(
            facility_id=facility.id,
            resident_id=f"R{i + 1:04d}",
            name=f"{person[0]} {person[1]}",
            ancc_class=rng.choice(ANCC_CLASSES),
            admitted_date=start - timedelta(days=rng.randint(30, 900)),
            discharged_date=None,
        ))

    staff_rows = []
    for role, count, windows in ROSTER:
        for i in range(count):
            person = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            member = Staff(
                facility_id=facility.id,
                staff_id=f"{role}{i + 1:03d}",
                name=person,
                role=role,
            )
            db.session.add(member)
            staff_rows.append((member, role, windows))
    db.session.flush()

    receipt = ImportReceipt(
        organization_id=org.id,
        facility_id=facility.id,
        imported_by="demo seed",
        calc_version=CALC_VERSION,
        first_shift_date=start,
        last_shift_date=today,
    )
    db.session.add(receipt)
    db.session.flush()

    shifts = 0
    for offset in range(days):
        day = start + timedelta(days=offset)
        weekend = day.weekday() >= 5
        for member, role, windows in staff_rows:
            for window in windows:
                # Thin the roster so the facility lands just under target —
                # a demo that is comfortably compliant shows nothing useful.
                if role == "ADMIN" and weekend:
                    continue
                chance = 0.62 if weekend else 0.72
                if role == "RN":
                    chance -= 0.06
                if rng.random() > chance:
                    continue
                db.session.add(Shift(
                    staff_id=member.id,
                    facility_id=facility.id,
                    date=day,
                    start_time=window[0],
                    end_time=window[1],
                    is_direct_care=(role != "ADMIN"),
                    break_minutes=30 if role != "ADMIN" else 0,
                    is_agency=(rng.random() < 0.08),
                    import_receipt_id=receipt.id,
                    source_row=shifts + 2,
                ))
                shifts += 1

    receipt.shifts_imported = shifts
    receipt.residents_imported = residents
    receipt.imported_at = datetime.utcnow()
    db.session.commit()

    return {
        "organization": org.name,
        "facility": facility.name,
        "residents": residents,
        "staff": len(staff_rows),
        "shifts": shifts,
    }
