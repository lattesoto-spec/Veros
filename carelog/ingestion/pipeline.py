"""Universal import pipeline.

upload -> read structure -> fingerprint -> known format?
   yes -> run stored mapping spec (no AI cost)
   no  -> Claude generates a spec -> validate -> store -> run
-> normalized records -> database -> existing Care Minutes engine.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from carelog.domain.eligibility import classify
from carelog.models import (
    CareEpisode,
    FormatMapping,
    Resident,
    ResidentDay,
    Shift,
    Staff,
    db,
)

from .analyzer import generate_mapping_spec
from .evidence import classify_shift_evidence
from .fingerprint import fingerprint
from .mapping import MappingError, TargetResult, apply_header_overrides, run_spec
from .presets import matching_spec
from .quality import check_shifts
from .reader import read_upload


MAPPING_SPEC_VERSION = 2


@dataclass
class ImportOutcome:
    filename: str
    fingerprint: str
    mapping_reused: bool
    spec: dict
    results: list[TargetResult]
    shifts_imported: int = 0
    staff_imported: int = 0
    residents_imported: int = 0
    resident_days_imported: int = 0
    care_episodes_imported: int = 0
    shifts_replaced: int = 0
    care_episodes_replaced: int = 0
    row_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    first_shift_date: date | None = None
    last_shift_date: date | None = None
    ai_usage: dict | None = None
    mapping_id: int | None = None
    receipt: object = None
    parsers: list[str] = field(default_factory=list)


def get_or_create_mapping(
    sheets, filename: str, progress=None, organization_id=None, reviewed_spec=None
) -> tuple[FormatMapping, bool, dict | None]:
    """Returns (mapping, reused, ai_usage).

    Learned specs are per-organization: a spec names the columns of a
    customer's roster system, so reusing one across tenants would leak how
    another customer's systems are laid out.
    """
    fp = fingerprint(sheets)
    stored = FormatMapping.query.filter_by(
        fingerprint=fp, organization_id=organization_id
    ).first()
    built_in = matching_spec(sheets)
    if stored and reviewed_spec is None:
        try:
            stored_spec = json.loads(stored.spec_json)
        except (TypeError, json.JSONDecodeError):
            stored_spec = {}
        if stored_spec.get("schema_version") == MAPPING_SPEC_VERSION:
            # Built-in presets can gain stricter validation without making
            # every customer's learned format call the analyzer again.
            is_stale_preset = (
                built_in is not None
                and stored_spec.get("reasoning") == "exact built-in evidence layout"
                and stored_spec.get("targets") != built_in["targets"]
            )
            if not is_stale_preset:
                return stored, True, None
        if progress:
            progress("refreshing stored format")

    spec = reviewed_spec if reviewed_spec is not None else built_in
    if spec:
        usage = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "method": "built-in exact mapping",
        }
        if progress:
            progress("matched built-in format")
    else:
        if progress:
            progress("learning format (AI)")
        spec, usage = generate_mapping_spec(sheets, filename)
    spec["schema_version"] = MAPPING_SPEC_VERSION
    if stored:
        mapping = stored
        mapping.spec_json = json.dumps(spec)
        mapping.source_filename = filename
        mapping.kinds = ",".join(t["kind"] for t in spec["targets"])
    else:
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


def ingest_file(
    facility,
    filename: str,
    data: bytes,
    receipt=None,
    progress=None,
    evidence_type: str | None = None,
    reviewed_spec=None,
) -> ImportOutcome:
    """`receipt` (optional ImportReceipt) stamps audit lineage onto every shift.
    `progress` (optional callable taking a stage string) receives live status
    updates for the background-job status page."""
    sheets = read_upload(filename, data)
    mapping, reused, usage = get_or_create_mapping(
        sheets,
        filename,
        progress=progress,
        organization_id=getattr(facility, "organization_id", None),
        reviewed_spec=reviewed_spec,
    )
    spec = json.loads(mapping.spec_json)
    if progress:
        progress("extracting rows")
    mapped_sheets = apply_header_overrides(spec, sheets)
    results = run_spec(spec, mapped_sheets)
    for result in results:
        if not result.records:
            raise MappingError(
                f"{filename} / {result.sheet}: no usable {result.kind} rows; nothing was imported."
            )
        if result.row_errors:
            raise MappingError(
                f"{filename} / {result.sheet}: " + "; ".join(result.row_errors[:10])
            )

    outcome = ImportOutcome(
        filename=filename,
        fingerprint=mapping.fingerprint,
        mapping_reused=reused,
        spec=spec,
        results=results,
        ai_usage=usage,
        parsers=sorted({s.parser for s in sheets}),
    )
    outcome.mapping_id = mapping.id
    outcome.receipt = receipt
    for r in results:
        outcome.row_errors.extend(r.row_errors)
    # Credentials must be applied before shifts so newly-created nurses can be
    # counted immediately when their employee-directory evidence is current.
    for r in results:
        if r.kind == "staff":
            _import_staff(facility, r.records, outcome)
    for r in results:
        if r.kind == "shifts":
            sheet = next(s for s in mapped_sheets if s.name == r.sheet)
            target = next(t for t in spec["targets"] if t["kind"] == "shifts" and t["sheet"] == r.sheet)
            auto_type, basis = classify_shift_evidence(filename, sheet, target)
            if evidence_type in ("worked", "rostered", "unverified"):
                auto_type, basis = evidence_type, "server-side evidence override"
            r.evidence_type = auto_type
            r.evidence_basis = basis
            from .quality import validate_shifts

            validate_shifts(r.records, evidence_type=auto_type)
            outcome.warnings.extend(check_shifts(r.records))
            _import_shifts(facility, r.records, outcome, evidence_type=auto_type)
        elif r.kind == "staff":
            continue
        elif r.kind == "residents":
            _import_residents(facility, r.records, outcome)
        elif r.kind == "resident_days":
            _import_resident_days(facility, r.records, outcome)
        elif r.kind == "care_episodes":
            _import_care_episodes(facility, r.records, outcome)
    return outcome


def _normalise_employment_type(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if "agency" in raw:
        return "agency"
    if "contract" in raw:
        return "contractor"
    if any(
        term in raw
        for term in ("permanent", "employee", "casual", "part-time", "full-time")
    ):
        return "employee"
    return raw


def _guard_role_history(member, raw_role):
    if not raw_role or not member.source_role:
        return
    previous = classify(member.source_role)[:2]
    incoming = classify(raw_role)[:2]
    if (
        previous != incoming
        and Shift.query.filter_by(
            staff_id=member.id, facility_id=member.facility_id
        ).first()
    ):
        raise MappingError(
            f"Staff {member.staff_id}: the source role changed from {member.source_role!r} to {raw_role!r}. "
            "Past worked records must not be reclassified silently. Review eligibility with explicit effective dates, "
            "or export separate employment-role identifiers for a genuine role change. Nothing was imported."
        )


def _import_staff(facility, records: list[dict], outcome: ImportOutcome):
    cache = {
        s.staff_id: s for s in Staff.query.filter_by(facility_id=facility.id).all()
    }
    for rec in records:
        sid = str(rec["staff_id"]).strip()
        name = str(rec.get("staff_name") or "").strip() or sid
        raw_role = str(rec.get("source_role") or rec.get("role") or "").strip()
        role = str(rec.get("role") or "OTHER").strip().upper() or "OTHER"
        bucket, status, reason = classify(raw_role or role)
        if status == "approved" and bucket in ("RN", "EN", "PCA"):
            role = "PCW" if bucket == "PCA" else bucket

        member = cache.get(sid)
        if member is None:
            member = Staff(
                facility_id=facility.id,
                staff_id=sid,
                name=name,
                role=role,
                source_role=raw_role,
                eligibility_status=status,
                eligibility_reason=reason,
            )
            db.session.add(member)
            db.session.flush()
            cache[sid] = member
        else:
            _guard_role_history(member, raw_role)
            if rec.get("staff_name"):
                member.name = name
            if member.approved_at is None and raw_role:
                member.role = role
                member.source_role = raw_role
                member.eligibility_status = status
                member.eligibility_reason = reason

        registration = str(rec.get("registration_number") or "").strip()
        if registration:
            member.registration_number = registration
        expiry = rec.get("registration_expiry")
        if isinstance(expiry, date):
            member.registration_expiry = expiry
        employment = _normalise_employment_type(rec.get("employment_type"))
        if employment:
            member.employment_type = employment
        classification = str(rec.get("classification") or "").strip()
        if classification:
            member.classification = classification
        outcome.staff_imported += 1


def _import_shifts(
    facility,
    records: list[dict],
    outcome: ImportOutcome,
    evidence_type: str = "unverified",
):
    if evidence_type not in ("worked", "rostered", "unverified"):
        evidence_type = "unverified"

    # Replace only the supplied staff/date groups in this evidence stream.
    # Separate months and files for other staff remain intact.
    dates_by_staff = {}
    for rec in records:
        dates_by_staff.setdefault(str(rec["staff_id"]).strip(), set()).add(rec["date"])
    for sid, days in dates_by_staff.items():
        member = Staff.query.filter_by(facility_id=facility.id, staff_id=sid).first()
        if member:
            query = Shift.query.filter(
                Shift.facility_id == facility.id,
                Shift.staff_id == member.id,
                Shift.evidence_type == evidence_type,
                Shift.date.in_(days),
            )
            receipt_id = getattr(outcome.receipt, "id", None)
            if receipt_id:
                from sqlalchemy import or_

                query = query.filter(
                    or_(
                        Shift.import_receipt_id != receipt_id,
                        Shift.import_receipt_id.is_(None),
                    )
                )
            outcome.shifts_replaced += query.delete(synchronize_session=False)
    db.session.flush()

    staff_cache = {
        s.staff_id: s for s in Staff.query.filter_by(facility_id=facility.id).all()
    }
    existing_by_staff = {}
    if records:
        earliest, latest = (
            min(r["date"] for r in records),
            max(r["date"] for r in records),
        )
        for existing in Shift.query.filter(
            Shift.facility_id == facility.id,
            Shift.evidence_type == evidence_type,
            Shift.date >= earliest - timedelta(days=1),
            Shift.date <= latest + timedelta(days=1),
        ).all():
            existing_by_staff.setdefault(existing.staff_id, []).append(existing)
    for rec in records:
        sid = str(rec["staff_id"]).strip()
        role = (rec.get("role") or "OTHER").strip().upper() or "OTHER"
        name = (rec.get("staff_name") or "").strip() or sid

        raw_role = (rec.get("source_role") or rec.get("role") or "").strip()
        bucket, status, reason = classify(raw_role or role)
        if status == "approved" and bucket in ("RN", "EN", "PCA"):
            role = "PCW" if bucket == "PCA" else bucket

        staff = staff_cache.get(sid)
        if staff:
            _guard_role_history(staff, raw_role)
            if rec.get("staff_name"):
                staff.name = name
            # Any human decision is permanent until a human changes it. An
            # excluded worker must not become approved merely because a file
            # was imported again.
            if staff.approved_at is None and raw_role:
                staff.role = role
                staff.source_role = raw_role or staff.source_role
                staff.eligibility_status = status
                staff.eligibility_reason = reason
        else:
            staff = Staff(
                facility_id=facility.id,
                staff_id=sid,
                name=name,
                role=role,
                source_role=raw_role,
                eligibility_status=status,
                eligibility_reason=reason,
            )
            db.session.add(staff)
            db.session.flush()
            staff_cache[sid] = staff

        break_minutes = int(rec.get("break_minutes") or 0)
        start, end = rec.get("start_time"), rec.get("end_time")
        coverage_known = isinstance(start, time) and isinstance(end, time)
        duration = None
        if not coverage_known:
            duration = int(rec["minutes"]) - break_minutes
            start = end = time(0, 0)

        d = rec["date"]
        previous = existing_by_staff.setdefault(staff.id, [])
        for old in previous:
            if (not coverage_known or not old.coverage_known) and old.date == d:
                raise MappingError(
                    f"{outcome.filename}, row {rec.get('_source_row', '?')}: duration-only rows overlap a staff/date group; correct the source before importing."
                )
            if coverage_known and old.coverage_known and old.start_time != old.end_time:
                left, right = datetime.combine(d, start), datetime.combine(d, end)
                if right < left:
                    right += timedelta(days=1)
                old_left, old_right = (
                    datetime.combine(old.date, old.start_time),
                    datetime.combine(old.date, old.end_time),
                )
                if old_right < old_left:
                    old_right += timedelta(days=1)
                if left < old_right and old_left < right:
                    raise MappingError(
                        f"{outcome.filename}, row {rec.get('_source_row', '?')}: duplicate or overlapping staff time across source files; nothing was imported."
                    )
        shift = Shift(
            staff_id=staff.id,
            facility_id=facility.id,
            date=d,
            start_time=start,
            end_time=end,
            is_direct_care=bool(rec.get("is_direct_care", True)),
            break_minutes=break_minutes,
            duration_minutes=duration,
            coverage_known=coverage_known,
            is_agency=bool(rec.get("is_agency", False)),
            evidence_type=evidence_type,
            labour_cost=(
                float(rec["labour_cost"])
                if isinstance(rec.get("labour_cost"), (int, float))
                else None
            ),
            import_receipt_id=getattr(outcome.receipt, "id", None),
            source_row=rec.get("_source_row"),
            source_file=rec.get("_source_file"),
            source_sheet=rec.get("_source_sheet"),
            source_page=rec.get("_source_page"),
        )
        db.session.add(shift)
        previous.append(shift)
        outcome.shifts_imported += 1
        if outcome.first_shift_date is None or d < outcome.first_shift_date:
            outcome.first_shift_date = d
        if outcome.last_shift_date is None or d > outcome.last_shift_date:
            outcome.last_shift_date = d


def _import_residents(facility, records: list[dict], outcome: ImportOutcome):
    for rec in records:
        rid = str(rec["resident_id"]).strip()
        existing = Resident.query.filter_by(
            facility_id=facility.id, resident_id=rid
        ).first()
        values = dict(
            name=str(rec.get("name") or "").strip(),
            ancc_class=str(rec.get("ancc_class") or "").strip(),
            admitted_date=rec.get("admitted_date"),
            discharged_date=rec.get("discharged_date"),
        )
        if existing:
            for k, v in values.items():
                if v not in (None, ""):
                    setattr(existing, k, v)
        else:
            db.session.add(Resident(facility_id=facility.id, resident_id=rid, **values))
        outcome.residents_imported += 1


def _import_resident_days(facility, records: list[dict], outcome: ImportOutcome):
    # Validate the entire batch before any writes. Never resolve conflicting
    # census identities by order of appearance or Python truthiness.
    seen = set()
    for rec in records:
        key = (str(rec["resident_id"]).strip(), rec["date"])
        if key in seen:
            raise MappingError(f"Duplicate resident-day {key[0]} on {key[1]}; correct the source.")
        seen.add(key)
        if type(rec.get("occupied")) is not bool:
            raise MappingError(f"Resident-day {key[0]} on {key[1]} requires an explicit boolean occupied value.")
        if rec["occupied"] and str(rec.get("exclusion_reason") or "").strip():
            raise MappingError(f"Resident-day {key[0]} on {key[1]} is occupied but has an exclusion reason; reconcile the source.")
    for rec in records:
        rid = str(rec["resident_id"]).strip()
        day = rec["date"]
        row = ResidentDay.query.filter_by(
            facility_id=facility.id, resident_id=rid, date=day
        ).first()
        values = dict(
            resident_name=str(rec.get("resident_name") or "").strip() or None,
            occupied=rec["occupied"],
            service_type=str(rec.get("service_type") or "").strip() or None,
            leave_type=str(rec.get("leave_type") or "").strip() or None,
            leave_day_number=(
                int(rec["leave_day_number"])
                if isinstance(rec.get("leave_day_number"), (int, float))
                else None
            ),
            ancc_class=str(rec.get("ancc_class") or "").strip() or None,
            exclusion_reason=str(rec.get("exclusion_reason") or "").strip() or None,
            import_receipt_id=getattr(outcome.receipt, "id", None),
            source_row=rec.get("_source_row"),
            source_file=rec.get("_source_file"),
            source_sheet=rec.get("_source_sheet"),
            source_page=rec.get("_source_page"),
        )
        if row:
            for key, value in values.items():
                setattr(row, key, value)
        else:
            db.session.add(
                ResidentDay(
                    facility_id=facility.id, resident_id=rid, date=day, **values
                )
            )
        outcome.resident_days_imported += 1


def _import_care_episodes(facility, records: list[dict], outcome: ImportOutcome):
    # Replace supplied resident/date groups, preserving unrelated history.
    receipt_id = getattr(outcome.receipt, "id", None)
    for rid, day in {(str(r["resident_id"]).strip(), r["date"]) for r in records}:
        query = CareEpisode.query.filter_by(
            facility_id=facility.id, resident_id=rid, date=day
        )
        if receipt_id:
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    CareEpisode.import_receipt_id != receipt_id,
                    CareEpisode.import_receipt_id.is_(None),
                )
            )
        outcome.care_episodes_replaced += query.delete(synchronize_session=False)

    for rec in records:
        minutes = rec.get("minutes")
        if not isinstance(minutes, (int, float)):
            start, end = rec.get("start_time"), rec.get("end_time")
            if isinstance(start, time) and isinstance(end, time):
                a = datetime.combine(rec["date"], start)
                b = datetime.combine(rec["date"], end)
                if b < a:
                    b += timedelta(days=1)
                minutes = (b - a).total_seconds() / 60
        db.session.add(
            CareEpisode(
                facility_id=facility.id,
                resident_id=str(rec["resident_id"]).strip(),
                resident_name=str(rec.get("resident_name") or "").strip() or None,
                date=rec["date"],
                care_type=str(rec.get("care_type") or "").strip() or None,
                care_category=str(rec.get("care_category") or "").strip() or None,
                staff_id=str(rec.get("staff_id") or "").strip() or None,
                staff_name=str(rec.get("staff_name") or "").strip() or None,
                source_role=str(rec.get("source_role") or rec.get("role") or "").strip()
                or None,
                start_time=rec.get("start_time"),
                end_time=rec.get("end_time"),
                minutes=max(int(minutes or 0), 0),
                import_receipt_id=getattr(outcome.receipt, "id", None),
                source_row=rec.get("_source_row"),
                source_file=rec.get("_source_file"),
                source_sheet=rec.get("_source_sheet"),
                source_page=rec.get("_source_page"),
            )
        )
        outcome.care_episodes_imported += 1
