"""Universal import pipeline.

upload -> read structure -> fingerprint -> known format?
   yes -> run stored mapping spec (no AI cost)
   no  -> Claude generates a spec -> validate -> store -> run
-> normalized records -> database -> existing Care Minutes engine.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from carelog.models import FormatMapping, Resident, Shift, Staff, db

from .analyzer import generate_mapping_spec
from .fingerprint import fingerprint
from .mapping import TargetResult, run_spec
from .quality import check_shifts
from .reader import read_upload


@dataclass
class ImportOutcome:
    filename: str
    fingerprint: str
    mapping_reused: bool
    spec: dict
    results: list[TargetResult]
    shifts_imported: int = 0
    residents_imported: int = 0
    row_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    first_shift_date: date | None = None
    last_shift_date: date | None = None
    ai_usage: dict | None = None
    mapping_id: int | None = None
    receipt: object = None


def get_or_create_mapping(sheets, filename: str, progress=None,
                          organization_id=None) -> tuple[FormatMapping, bool, dict | None]:
    """Returns (mapping, reused, ai_usage).

    Learned specs are per-organization: a spec names the columns of a
    customer's roster system, so reusing one across tenants would leak how
    another customer's systems are laid out.
    """
    fp = fingerprint(sheets)
    stored = FormatMapping.query.filter_by(
        fingerprint=fp, organization_id=organization_id
    ).first()
    if stored:
        return stored, True, None

    if progress:
        progress("learning format (AI)")
    spec, usage = generate_mapping_spec(sheets, filename)
    mapping = FormatMapping(
        organization_id=organization_id,
        fingerprint=fp,
        spec_json=json.dumps(spec),
        source_filename=filename,
        kinds=",".join(t["kind"] for t in spec["targets"]),
    )
    db.session.add(mapping)
    db.session.flush()
    return mapping, False, usage


def ingest_file(facility, filename: str, data: bytes, receipt=None, progress=None) -> ImportOutcome:
    """`receipt` (optional ImportReceipt) stamps audit lineage onto every shift.
    `progress` (optional callable taking a stage string) receives live status
    updates for the background-job status page."""
    sheets = read_upload(filename, data)
    mapping, reused, usage = get_or_create_mapping(
        sheets, filename, progress=progress,
        organization_id=getattr(facility, "organization_id", None),
    )
    spec = json.loads(mapping.spec_json)
    if progress:
        progress("extracting rows")
    results = run_spec(spec, sheets)

    outcome = ImportOutcome(
        filename=filename, fingerprint=mapping.fingerprint, mapping_reused=reused,
        spec=spec, results=results, ai_usage=usage,
    )
    outcome.mapping_id = mapping.id
    outcome.receipt = receipt
    for r in results:
        outcome.row_errors.extend(r.row_errors)
        if r.kind == "shifts":
            outcome.warnings.extend(check_shifts(r.records))
            _import_shifts(facility, r.records, outcome)
        else:
            _import_residents(facility, r.records, outcome)
    return outcome


def _import_shifts(facility, records: list[dict], outcome: ImportOutcome):
    # Same contract as the classic upload: a shifts import replaces the
    # facility's shifts. Only clear once even if several files carry shifts.
    if outcome.shifts_imported == 0:
        Shift.query.filter_by(facility_id=facility.id).delete()

    staff_cache = {s.staff_id: s for s in Staff.query.filter_by(facility_id=facility.id).all()}
    for rec in records:
        sid = str(rec["staff_id"]).strip()
        role = (rec.get("role") or "OTHER").strip().upper() or "OTHER"
        name = (rec.get("staff_name") or "").strip() or sid

        staff = staff_cache.get(sid)
        if staff:
            if rec.get("staff_name"):
                staff.name = name
            staff.role = role
        else:
            staff = Staff(facility_id=facility.id, staff_id=sid, name=name, role=role)
            db.session.add(staff)
            db.session.flush()
            staff_cache[sid] = staff

        break_minutes = int(rec.get("break_minutes") or 0)
        start, end = rec.get("start_time"), rec.get("end_time")
        if not (isinstance(start, time) and isinstance(end, time)):
            # Duration-only exports: synthesize a midnight-anchored window so
            # the minutes-between computation in the reporting engine holds.
            minutes = min(int(rec["minutes"]), 24 * 60 - 1)
            start = time(0, 0)
            end_dt = datetime.combine(date.today(), start) + timedelta(minutes=minutes)
            end = end_dt.time()

        d = rec["date"]
        db.session.add(Shift(
            staff_id=staff.id,
            facility_id=facility.id,
            date=d,
            start_time=start,
            end_time=end,
            is_direct_care=bool(rec.get("is_direct_care", True)),
            break_minutes=break_minutes,
            is_agency=bool(rec.get("is_agency", False)),
            import_receipt_id=getattr(outcome.receipt, "id", None),
            source_row=rec.get("_source_row"),
        ))
        outcome.shifts_imported += 1
        if outcome.first_shift_date is None or d < outcome.first_shift_date:
            outcome.first_shift_date = d
        if outcome.last_shift_date is None or d > outcome.last_shift_date:
            outcome.last_shift_date = d


def _import_residents(facility, records: list[dict], outcome: ImportOutcome):
    for rec in records:
        rid = str(rec["resident_id"]).strip()
        existing = Resident.query.filter_by(facility_id=facility.id, resident_id=rid).first()
        values = dict(
            name=str(rec.get("name") or "").strip(),
            ancc_class=str(rec.get("ancc_class") or "").strip(),
            admitted_date=rec.get("admitted_date"),
            discharged_date=rec.get("discharged_date"),
        )
        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
        else:
            db.session.add(Resident(facility_id=facility.id, resident_id=rid, **values))
        outcome.residents_imported += 1
