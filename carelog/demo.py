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
    ResidentDay,
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
ANCC_CLASSES = [f"Class {i}" for i in range(1, 14)] + [f"Respite Class {i}" for i in range(1, 4)]

# (role, count, shift windows) — the mix that produces a credible roster.
ROSTER = [
    ("RN",  5, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0)), (time(23, 0), time(7, 0))]),
    ("EN",  4, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0))]),
    ("PCW", 11, [(time(7, 0), time(15, 0)), (time(15, 0), time(23, 0)), (time(23, 0), time(7, 0))]),
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
    resident_ids = []
    resident_classes = {}
    for i in range(residents):
        while True:
            person = (rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
            if person not in used:
                used.add(person)
                break
        resident_id = f"R{i + 1:04d}"
        resident_ids.append(resident_id)
        ancc_class = rng.choice(ANCC_CLASSES)
        resident_classes[resident_id] = ancc_class
        db.session.add(Resident(
            facility_id=facility.id,
            resident_id=resident_id,
            name=f"{person[0]} {person[1]}",
            ancc_class=ancc_class,
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
                source_role=role,
                eligibility_status="approved" if role in ("RN", "EN", "PCW") else "excluded",
                eligibility_reason="verified demo classification",
                approved_at=datetime.utcnow(),
                registration_number=(f"DEM{role}{i + 1:04d}" if role in ("RN", "EN") else None),
                registration_expiry=(today + timedelta(days=365) if role in ("RN", "EN") else None),
            )
            db.session.add(member)
            staff_rows.append((member, role, windows, i))
    db.session.flush()

    receipt = ImportReceipt(
        organization_id=org.id,
        facility_id=facility.id,
        imported_by="demo seed",
        calc_version=CALC_VERSION,
        evidence_type="worked",
        first_shift_date=start,
        last_shift_date=today,
    )
    db.session.add(receipt)
    db.session.flush()

    shifts = 0
    for offset in range(days):
        day = start + timedelta(days=offset)
        for rid in resident_ids:
            db.session.add(ResidentDay(
                facility_id=facility.id, resident_id=rid, date=day,
                occupied=True, service_type="an_acc", ancc_class=resident_classes[rid],
                import_receipt_id=receipt.id,
            ))
        weekend = day.weekday() >= 5
        for member, role, windows, staff_index in staff_rows:
            # Each person works at most one shift per day. The first three RNs
            # are the coverage anchors; the rest of the roster is deliberately
            # a little thin so the demo exposes useful amber actions.
            if role == "ADMIN" and weekend:
                continue
            anchor_rn = role == "RN" and staff_index < 3
            chance = {"RN": 0.75, "EN": 0.85, "PCW": 0.92, "ADMIN": 0.90}[role]
            if weekend and role != "RN":
                chance -= 0.05
            if not anchor_rn and rng.random() > chance:
                continue
            window = windows[staff_index % len(windows)] if anchor_rn else windows[(offset + staff_index) % len(windows)]
            db.session.add(Shift(
                staff_id=member.id,
                facility_id=facility.id,
                date=day,
                start_time=window[0],
                end_time=window[1],
                is_direct_care=(role != "ADMIN"),
                break_minutes=30 if role != "ADMIN" else 0,
                is_agency=(rng.random() < 0.08),
                evidence_type="worked",
                labour_cost=round((75 if role == "RN" else 48 if role == "EN" else 36) * 7.5, 2)
                if role != "ADMIN" else None,
                import_receipt_id=receipt.id,
                source_row=shifts + 2,
            ))
            shifts += 1

    # Seed the statutory reference period too, so the configured targets can be
    # independently reconciled in the demo.
    from carelog.domain.targets import reconcile_configured_targets, reference_period

    quarter_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    ref_start, ref_end, _ = reference_period(quarter_start)
    ref_days = (ref_end - ref_start).days + 1
    extra_ref_days = 0
    for offset in range(ref_days):
        day = ref_start + timedelta(days=offset)
        if start <= day <= today:
            continue
        extra_ref_days += 1
        for rid in resident_ids:
            db.session.add(ResidentDay(
                facility_id=facility.id, resident_id=rid, date=day,
                occupied=True, service_type="an_acc", ancc_class=resident_classes[rid],
                import_receipt_id=receipt.id,
            ))
    db.session.flush()
    target_check = reconcile_configured_targets(facility, quarter_start)
    facility.ancc_target = target_check["derived_care_target"] or facility.ancc_target
    facility.rn_target = target_check["derived_rn_target"] or facility.rn_target

    receipt.shifts_imported = shifts
    receipt.residents_imported = residents
    receipt.resident_days_imported = residents * (days + extra_ref_days)
    receipt.imported_at = datetime.utcnow()
    db.session.commit()

    return {
        "organization": org.name,
        "facility": facility.name,
        "residents": residents,
        "staff": len(staff_rows),
        "shifts": shifts,
    }
