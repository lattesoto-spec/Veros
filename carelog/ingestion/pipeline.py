"""Universal import pipeline.

upload -> read structure -> fingerprint -> known format?
   yes -> run stored mapping spec (no AI cost)
   no  -> Claude generates a spec -> validate -> store -> run
-> normalized records -> database -> existing Care Minutes engine.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from math import ceil, floor

from sqlalchemy.orm import Session

from carelog.domain.clock import facility_today
from carelog.domain.eligibility import PENDING, bucket_for, classify
from carelog.models import (
    SUPERSEDED,
    VOIDED,
    AuditLog,
    CareEpisode,
    FormatMapping,
    ImportReceipt,
    Organization,
    PayCategoryRule,
    Resident,
    ResidentDay,
    Shift,
    Staff,
    db,
)

from .analyzer import ai_ready, generate_mapping_spec
from .evidence import classify_shift_evidence
from .fingerprint import fingerprint, normalize_header
from .mapping import MappingError, TargetResult, apply_header_overrides, run_spec
from .paycodes import candidate_columns, pay_column, split_records
from .presets import matching_spec
from .quality import _minutes as record_minutes, check_shifts, daylight_saving_warnings
from .reader import read_upload, rebuild_with_header
from .statuses import STATUS_COLUMNS, disallowed, known_columns, prepare_status_filters, status_columns
from .durations import needs_unit_review


MAPPING_SPEC_VERSION = 2


class MappingRequired(Exception):
    """No trusted interpretation exists; a person must choose the columns."""


def _ai_mapping_allowed(organization_id) -> bool:
    organization = (
        db.session.get(Organization, organization_id) if organization_id else None
    )
    return bool(organization and organization.ai_mapping_enabled and ai_ready())


def _record_disclosure(organization_id, filename, sheets, model):
    """Audit the external call outside the import, which may yet roll back."""
    with Session(db.engine) as session:
        session.add(
            AuditLog(
                organization_id=organization_id,
                action="external_disclosure",
                entity="format_mapping",
                entity_id=filename,
                detail=f"Masked structure of {len(sheets)} sheet(s) sent to {model}; no data values disclosed",
            )
        )
        session.commit()


def _whole_minutes(value) -> int:
    """Floor to a minute without letting binary floats lose one (4.1 h is 246)."""
    return floor(round(float(value), 6))


def _break_whole_minutes(value) -> int:
    """Round breaks up: a fraction of a break is never counted as worked time."""
    return ceil(round(float(value or 0), 6))


@dataclass
class ImportOutcome:
    filename: str
    fingerprint: str
    mapping_reused: bool
    spec: dict
    results: list[TargetResult]
    shifts_imported: int = 0
    staff_imported: int = 0
    staff_new: int = 0
    staff_updated: int = 0
    staff_changes: list[dict] = field(default_factory=list)
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
    sha256: str | None = None
    census_changed: int = 0
    census_changes: list[dict] = field(default_factory=list)
    void_matches: list[dict] = field(default_factory=list)
    holds: list[dict] = field(default_factory=list)
    replaced_sample: list[dict] = field(default_factory=list)


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
        if not _ai_mapping_allowed(organization_id):
            raise MappingRequired(filename)
        if progress:
            progress("learning format (AI)")
        spec, usage = generate_mapping_spec(
            sheets,
            filename,
            on_disclosure=lambda model: _record_disclosure(
                organization_id, filename, sheets, model
            ),
        )
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
    _rebind_sheets(spec, sheets, filename)
    mapped_sheets = apply_header_overrides(spec, sheets)
    _read_pay_columns(spec, mapped_sheets)
    _read_identity_columns(spec, mapped_sheets)
    prepare_status_filters(spec, mapped_sheets)
    rules = {rule.category: rule.treatment for rule in PayCategoryRule.query.filter_by(organization_id=facility.organization_id)}
    results = run_spec(spec, mapped_sheets, rules)
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
    outcome.holds = _review_holds(spec, mapped_sheets, filename)
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
            organization = db.session.get(Organization, facility.organization_id) if facility.organization_id else None
            from .quality import apply_elapsed_minutes
            zone_name = target.get("clock_timezone") or (organization.timezone if organization else "Australia/Sydney")
            target["clock_timezone"] = zone_name
            apply_elapsed_minutes(r.records, zone_name)
            r.records, ruled_out, r.pay_categories = split_records(
                r.records, rules, record_minutes
            )
            r.pay_excluded_records = [rec for rec in ruled_out if rec.get("staff_id") and isinstance(rec.get("date"), date)]
            from .reconciliation import duration_holds
            outcome.holds.extend(duration_holds(r.records, target, sheet, filename))
            r.rows_filtered += sum(
                item["rows"] for item in r.pay_categories if item["treatment"] != "worked"
            )
            auto_type, basis = classify_shift_evidence(filename, sheet, target)
            if evidence_type in ("worked", "rostered", "unverified"):
                auto_type, basis = evidence_type, "server-side evidence override"
            elif auto_type == "unverified" and target.get("verified_source"):
                # A provider verified this layout as actual worked time. A
                # planned or forecast signal in the file still takes precedence.
                auto_type = "worked"
                basis = f"Provider verification: {target['verified_source']}"
            r.evidence_type = auto_type
            r.evidence_basis = (f"{basis}; clock intervals use elapsed time in {zone_name}"
                                if any(rec.get("_timezone_name") for rec in r.records) else basis)
            from .quality import validate_shifts

            if {"start_time", "end_time"} <= set(target.get("fields", {})) and "break_minutes" not in target.get("fields", {}):
                outcome.warnings.append(
                    f"{r.sheet}: no unpaid break column was read, so full start-to-finish "
                    "time will count. Check these times already exclude breaks."
                )
            local_today = facility_today(facility)
            validate_shifts(r.records, evidence_type=auto_type, today=local_today)
            outcome.warnings.extend(check_shifts(r.records, today=local_today))
            outcome.warnings.extend(daylight_saving_warnings(
                r.records, zone_name
            ))
            _import_shifts(
                facility, r.records, outcome, evidence_type=auto_type,
                ruled_out=[*r.pay_excluded_records, *r.excluded_records],
            )
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


def _staff_directory(facility) -> dict[str, list]:
    """Each person's records, one per role they have held at this facility."""
    directory = {}
    for member in Staff.query.filter_by(facility_id=facility.id).order_by(Staff.id):
        directory.setdefault(member.staff_id, []).append(member)
    return directory


def _validate_staff_identity(directory, rec, filename):
    """A source identifier alone cannot transfer one person's credentials."""
    sid = str(rec["staff_id"]).strip()
    name = " ".join(str(rec.get("staff_name") or "").split()).casefold()
    registration = str(rec.get("registration_number") or "").strip().casefold()
    for existing in directory.get(sid, []):
        old_name = " ".join((existing.name or "").split()).casefold()
        old_registration = (existing.registration_number or "").strip().casefold()
        aliases = {" ".join(alias["name"].split()).casefold() for alias in existing.identity_aliases}
        if name and old_name and old_name != sid.casefold() and name != old_name and name not in aliases:
            raise MappingError(
                f"{filename}: staff ID {sid} is recorded as {existing.name!r}, "
                f"but this file names {rec['staff_name']!r}. Nothing was imported. "
                "For a name-format difference, record a verified alias in Eligibility review and retry. "
                "For different people, use distinct identifiers across payroll and agency systems."
            )
        if registration and old_registration and registration != old_registration:
            raise MappingError(
                f"{filename}: staff ID {sid} has a different Ahpra registration number. "
                "Nothing was imported. Verify the person and use a distinct identifier for a different worker."
            )


def _record_for_role(versions, raw_role):
    """The record this title belongs to, or None when the role has changed."""
    if not versions or not raw_role:
        return versions[-1] if versions else None
    exact = next((m for m in reversed(versions) if m.source_role == raw_role), None)
    if exact:
        return exact
    # A different wording is the same role only if both are unambiguous titles
    # in one bucket. Two pending titles prove nothing and must not share a decision.
    bucket, status, _ = classify(raw_role)
    return next(
        (
            m
            for m in reversed(versions)
            if not m.source_role
            or (status == "approved" and classify(m.source_role)[:2] == (bucket, status))
        ),
        None,
    )


def _add_staff_record(facility, directory, sid, name, role, raw_role, status, reason):
    """A role change opens a new record, so past minutes keep their old role.

    Only the new record awaits review, and the rest of the batch can proceed.
    """
    previous = (directory.get(sid) or [None])[-1]
    if (
        previous
        and bucket_for(role) in ("RN", "EN")
        and bucket_for(previous.role) != bucket_for(role)
    ):
        # A new nursing role needs its own registration checked, not inherited.
        status, reason = PENDING, "Confirm registration for the new nursing role."
    member = Staff(
        facility_id=facility.id,
        staff_id=sid,
        name=previous.name if previous and previous.name != sid else name,
        role=role,
        source_role=raw_role,
        eligibility_status=status,
        eligibility_reason=reason,
    )
    if previous:
        member.eligibility_reason = (
            f"Role changed from {previous.source_role or previous.role} to "
            f"{raw_role}. {reason or ''}".strip()
        )
        for inherited in (
            "registration_number", "registration_expiry", "employment_type",
            "classification", "credential_gaps_json",
            "identity_aliases_json",
        ):
            setattr(member, inherited, getattr(previous, inherited))
    db.session.add(member)
    db.session.flush()
    directory.setdefault(sid, []).append(member)
    return member


def record_credential_gap(member, new_expiry: date, today: date):
    """A later expiry proves the present, not the days since the last one.

    If the previous registration had already lapsed, the time up to this
    evidence stays unverified until a reviewer confirms continuous registration.
    """
    previous = member.registration_expiry
    if not previous or new_expiry <= previous:
        return
    start, end = previous + timedelta(days=1), today - timedelta(days=1)
    if start > end:
        return
    gaps = json.loads(member.credential_gaps_json or "[]")
    gap = [start.isoformat(), end.isoformat()]
    if gap not in gaps:
        member.credential_gaps_json = json.dumps(gaps + [gap])


def _import_staff(facility, records: list[dict], outcome: ImportOutcome):
    shown_fields = ("name", "source_role", "employment_type", "classification",
                    "registration_number", "registration_expiry", "eligibility_status")
    def snapshot(member):
        return {key: str(getattr(member, key)) if getattr(member, key) is not None else None
                for key in shown_fields}
    directory = _staff_directory(facility)
    for rec in records:
        _validate_staff_identity(directory, rec, outcome.filename)
        sid = str(rec["staff_id"]).strip()
        name = str(rec.get("staff_name") or "").strip() or sid
        raw_role = str(rec.get("source_role") or rec.get("role") or "").strip()
        role = str(rec.get("role") or "OTHER").strip().upper() or "OTHER"
        bucket, status, reason = classify(raw_role or role)
        if status == "approved" and bucket in ("RN", "EN", "PCA"):
            role = "PCW" if bucket == "PCA" else bucket

        member = _record_for_role(directory.get(sid), raw_role)
        before = snapshot(member) if member else None
        if member is None:
            member = _add_staff_record(
                facility, directory, sid, name, role, raw_role, status, reason
            )
        else:
            if rec.get("staff_name") and member.name in ("", sid):
                member.name = name
            if member.approved_at is None and member.reviewed_at is None and raw_role:
                member.role = role
                member.source_role = raw_role
                member.eligibility_status = status
                member.eligibility_reason = reason

        registration = str(rec.get("registration_number") or "").strip()
        if registration:
            member.registration_number = registration
        expiry = rec.get("registration_expiry")
        if isinstance(expiry, date):
            record_credential_gap(member, expiry, facility_today(facility))
            member.registration_expiry = expiry
        employment = _normalise_employment_type(rec.get("employment_type"))
        if employment:
            member.employment_type = employment
        classification = str(rec.get("classification") or "").strip()
        if classification:
            member.classification = classification
        outcome.staff_imported += 1
        after = snapshot(member)
        if before is None:
            outcome.staff_new += 1
        elif before != after:
            outcome.staff_updated += 1
        if before != after and len(outcome.staff_changes) < 20:
            outcome.staff_changes.append({"staff_id": sid, "name": member.name,
                "action": "New profile" if before is None else "Update",
                "changes": [{"field": key, "before": before.get(key) if before else None,
                             "after": after[key]} for key in shown_fields
                            if (before or {}).get(key) != after[key]]})


def _target_columns(target) -> set:
    columns = set()
    for rule in target.get("fields", {}).values():
        columns.add(rule.get("column"))
        columns.update(rule.get("columns", []))
    return columns - {None}


def _rebind_sheets(spec, sheets, filename):
    """A remembered layout must survive next month's file or sheet name.

    Fingerprints ignore names on purpose, and a CSV's sheet is named after its
    file. Find the sheet with these columns; if that is unclear, ask a person.
    """
    names = {sheet.name for sheet in sheets}
    for target in spec.get("targets", []):
        if target.get("sheet") in names:
            continue
        header_row = target.get("header_row")
        matches = [
            sheet for sheet in sheets
            if _target_columns(target) <= set(
                rebuild_with_header(sheet, header_row).headers
                if isinstance(header_row, int) else sheet.headers
            )
        ]
        if len(matches) != 1:
            raise MappingRequired(filename)
        target["sheet"] = matches[0].name


SITE_COLUMNS = {
    "site", "site_name", "facility", "facility_name", "home", "home_name", "campus",
    "location", "cost_centre", "cost_center", "service_location", "ward",
    "business_unit", "department",
}


def _review_holds(spec, sheets, filename) -> list[dict]:
    """Questions only a person can answer before these rows may count."""
    holds = []
    for target in spec.get("targets", []):
        if target.get("kind") not in ("shifts", "resident_days", "care_episodes"):
            continue
        sheet = next(s for s in sheets if s.name == target.get("sheet"))
        mapped = _target_columns(target)
        fields = target.get("fields", {})
        for field_name in ("minutes", "break_minutes"):
            rule = fields.get(field_name)
            if rule and needs_unit_review(rule):
                holds.append({
                    "kind": "time_units", "file": filename, "sheet": sheet.name,
                    "field": field_name, "column": rule.get("column"),
                    "target_kind": target["kind"],
                    "options": ["minutes", "hours", "h:mm"],
                })
        if target["kind"] == "shifts":
            break_rule = fields.get("break_minutes", {})
            if {"start_time", "end_time"} <= set(fields) and (
                not break_rule or (break_rule.get("source") == "constant" and not target.get("break_basis"))
            ):
                holds.append({
                    "kind": "breaks", "file": filename, "sheet": sheet.name,
                    "options": candidate_columns(sheet.headers, sheet.rows, mapped),
                })
            recognised = known_columns(target)
            for header in status_columns(sheet, target.get("non_status_columns", {}), recognised):
                values = sorted({str(r.get(header) or "").strip() for r in sheet.rows})
                options = [v for v in values if v and not disallowed(v)]
                filters = [target.get("row_filter", {})] + target.get("status_filters", [])
                safe_filters = [{v.casefold() for v in f["include_values"]} for f in filters
                                if f.get("column") == header and f.get("include_values")
                                and all(str(v).strip() and not disallowed(v) for v in f["include_values"])]
                included = set.intersection(*safe_filters) if safe_filters else set()
                pending = target.get("status_review_required", {}).get(header, {})
                prior = pending.get("included", [])
                options = sorted(set(options) | set(prior))
                decision = target.get("status_decisions", {}).get(header, {})
                reviewed = {v.casefold() for v in decision.get("seen", [])} if decision.get("basis") else set()
                if not pending and safe_filters and {v.casefold() for v in options} <= included | reviewed:
                    continue
                attestation = target.get("status_attestations", {}).get(header)
                if (isinstance(attestation, dict) and attestation.get("basis")
                    and set(values) <= set(attestation.get("values", []))
                    and all(v and not disallowed(v) for v in values)):
                    continue
                holds.append({
                    "kind": "status", "file": filename, "sheet": sheet.name,
                    "column": header, "options": options,
                    "selected": [v for v in options if v.casefold() in included | {p.casefold() for p in prior}],
                    "counts": {v: sum(str(r.get(header) or "").strip() == v for r in sheet.rows) for v in options},
                    "can_ignore": normalize_header(header) not in STATUS_COLUMNS and header not in recognised,
                    "excluded": [v or "Blank" for v in values if disallowed(v) or not v],
                    "allow_all": bool(values) and all(v and not disallowed(v) for v in values),
                })
                break  # Ask about another status column after this one is resolved.
        if (
            target["kind"] == "shifts"
            and "minutes" in fields
            and "pay_category" not in fields
            and not target.get("all_lines_worked")
        ):
            holds.append({
                "kind": "pay_column", "file": filename, "sheet": sheet.name,
                "options": candidate_columns(sheet.headers, sheet.rows, mapped),
            })
        if not target.get("site_filter") and not target.get("single_site"):
            for header in sheet.headers:
                values = sorted({str(r.get(header) or "").strip() for r in sheet.rows} - {""})
                if normalize_header(header) in SITE_COLUMNS and len(values) > 1:
                    holds.append({
                        "kind": "site", "file": filename, "sheet": sheet.name,
                        "column": header, "options": values[:40],
                    })
                    break
    return holds


def _read_pay_columns(spec, sheets):
    """A pay column on a shift sheet is always read, mapped or not.

    Otherwise leaving it unmapped would silently count every line as worked.
    Keeping only chosen values with an include filter is an explicit decision.
    """
    for target in spec.get("targets", []):
        if target.get("kind") != "shifts" or "pay_category" in target.get("fields", {}):
            continue
        sheet = next((s for s in sheets if s.name == target.get("sheet")), sheets[0])
        column = pay_column(sheet.headers, sheet.rows, _target_columns(target))
        if target.get("all_lines_worked") and not column:
            continue
        row_filter = target.get("row_filter") or {}
        if column and not (row_filter.get("column") == column and row_filter.get("include_values")):
            target.setdefault("fields", {})["pay_category"] = {"column": column}


def _read_identity_columns(spec, sheets):
    """Omitting an available name or Ahpra column cannot hide an ID collision."""
    for target in spec.get("targets", []):
        if target.get("kind") not in ("shifts", "staff"):
            continue
        sheet = next(s for s in sheets if s.name == target["sheet"])
        for field_name, headers in (
            ("staff_name", {"staff_name", "employee_name", "worker_name", "full_name"}),
            ("registration_number", {"ahpra_registration", "ahpra_number", "ahpra_registration_number", "registration_number"}),
        ):
            if field_name not in target["fields"]:
                found = [h for h in sheet.headers if normalize_header(h) in headers]
                if len(found) == 1:
                    target["fields"][field_name] = {"column": found[0]}


def retire_shifts(query, receipt_id) -> int:
    """Keep replaced rows on record, restorable if the replacing import goes."""
    if not receipt_id:
        return query.delete(synchronize_session=False)
    return query.update(
        {
            Shift.prior_evidence_type: Shift.evidence_type,
            Shift.evidence_type: SUPERSEDED,
            Shift.superseded_by_receipt_id: receipt_id,
        },
        synchronize_session=False,
    )


def _shift_key(sid, day, start, end, duration, coverage_known):
    return (sid, day, start, end) if coverage_known else (sid, day, duration)


def layout_scope(facility_id, source_fingerprint):
    """Replacement follows the source layout, including renamed monthly files.

    Legacy rows without retained layout evidence are not silently replaced.
    Overlaps with another source remain a visible validation error.
    """
    from sqlalchemy import and_, false, or_

    if not source_fingerprint:
        return false()
    matches = []
    for receipt in ImportReceipt.query.filter_by(facility_id=facility_id):
        for source in json.loads(receipt.summary_json or "[]"):
            if source.get("fingerprint") == source_fingerprint and source.get("filename"):
                matches.append(and_(Shift.import_receipt_id == receipt.id,
                                    Shift.source_file == source["filename"]))
    return or_(*matches) if matches else false()


def _voided_keys(facility, records, evidence_type) -> dict:
    if not records:
        return {}
    days = [rec["date"] for rec in records]
    rows = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility.id,
            Shift.evidence_type == VOIDED,
            Shift.prior_evidence_type == evidence_type,
            Shift.date >= min(days),
            Shift.date <= max(days),
        )
    )
    return {
        _shift_key(
            staff.staff_id, shift.date, shift.start_time, shift.end_time,
            shift.duration_minutes, shift.coverage_known,
        ): shift.void_reason or "Voided"
        for shift, staff in rows
    }


def _import_shifts(
    facility,
    records: list[dict],
    outcome: ImportOutcome,
    evidence_type: str = "unverified",
    ruled_out: tuple = (),
):
    if evidence_type not in ("worked", "rostered", "unverified"):
        evidence_type = "unverified"

    # Replace only the supplied staff/date groups in this evidence stream.
    # Separate months and files for other staff remain intact.
    # A line now shown as leave still speaks for that person and date, so a
    # corrected file replaces the worked row it used to be.
    dates_by_staff = {}
    for rec in [*records, *ruled_out]:
        dates_by_staff.setdefault(str(rec["staff_id"]).strip(), set()).add(rec["date"])
    directory = _staff_directory(facility)
    # Check before retiring any shifts or updating any staff fields.
    for rec in [*records, *ruled_out]:
        _validate_staff_identity(directory, rec, outcome.filename)
    person_of = {m.id: sid for sid, versions in directory.items() for m in versions}
    from sqlalchemy import and_, false, or_

    groups = [and_(Shift.staff_id.in_([m.id for m in directory[sid]]), Shift.date.in_(days))
              for sid, days in dates_by_staff.items() if sid in directory]
    query = Shift.query.filter(
        Shift.facility_id == facility.id, Shift.evidence_type == evidence_type,
        layout_scope(facility.id, outcome.fingerprint), or_(*groups) if groups else false(),
    )
    receipt_id = getattr(outcome.receipt, "id", None)
    if receipt_id:
        query = query.filter(or_(Shift.import_receipt_id != receipt_id, Shift.import_receipt_id.is_(None)))
    new_names = {str(rec["staff_id"]).strip(): rec.get("staff_name") for rec in [*records, *ruled_out]}
    members_by_id = {m.id: m for versions in directory.values() for m in versions}
    for old in query.order_by(Shift.date, Shift.id).limit(max(0, 20 - len(outcome.replaced_sample))):
        member = members_by_id[old.staff_id]
        outcome.replaced_sample.append({
            "previous_staff": member.name, "incoming_staff": new_names.get(member.staff_id) or member.name,
            "date": old.date.isoformat(), "source_file": old.source_file,
        })
    outcome.shifts_replaced += retire_shifts(query, receipt_id)
    db.session.flush()

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
            existing_by_staff.setdefault(person_of[existing.staff_id], []).append(existing)
    voided = _voided_keys(facility, records, evidence_type)
    for rec in records:
        _validate_staff_identity(directory, rec, outcome.filename)
        sid = str(rec["staff_id"]).strip()
        role = (rec.get("role") or "OTHER").strip().upper() or "OTHER"
        name = (rec.get("staff_name") or "").strip() or sid

        raw_role = (rec.get("source_role") or rec.get("role") or "").strip()
        bucket, status, reason = classify(raw_role or role)
        if status == "approved" and bucket in ("RN", "EN", "PCA"):
            role = "PCW" if bucket == "PCA" else bucket

        staff = _record_for_role(directory.get(sid), raw_role)
        if staff is None:
            staff = _add_staff_record(
                facility, directory, sid, name, role, raw_role, status, reason
            )
        else:
            if rec.get("staff_name") and staff.name in ("", sid):
                staff.name = name
            # Any human decision is permanent until a human changes it. An
            # excluded worker must not become approved merely because a file
            # was imported again.
            if staff.approved_at is None and staff.reviewed_at is None and raw_role:
                staff.role = role
                staff.source_role = raw_role or staff.source_role
                staff.eligibility_status = status
                staff.eligibility_reason = reason

        break_minutes = _break_whole_minutes(rec.get("break_minutes"))
        start, end = rec.get("start_time"), rec.get("end_time")
        coverage_known = isinstance(start, time) and isinstance(end, time)
        duration = None
        if coverage_known and rec.get("_elapsed_minutes") is not None:
            duration = _whole_minutes(rec["_elapsed_minutes"]) - break_minutes
        if not coverage_known:
            duration = _whole_minutes(rec["minutes"]) - break_minutes
            start = end = time(0, 0)

        d = rec["date"]
        void_reason = voided.get(_shift_key(sid, d, start, end, duration, coverage_known))
        if void_reason is not None:
            # Cumulative exports repeat old rows. A void stands until a person
            # restores it, and review says what was left out.
            outcome.void_matches.append(
                {"staff": name, "date": d.isoformat(), "reason": void_reason}
            )
            continue
        previous = existing_by_staff.setdefault(sid, [])
        pay_category = (str(rec.get("pay_category") or "").strip() or None)
        for old in previous:
            separate_payroll_line = (
                not coverage_known
                and not old.coverage_known
                and pay_category
                and old.pay_category
                and pay_category != old.pay_category
                and old.import_receipt_id == getattr(outcome.receipt, "id", None)
            )
            if separate_payroll_line:
                continue
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
            timezone_name=rec.get("_timezone_name"),
            is_agency=bool(rec.get("is_agency", False)),
            pay_category=pay_category,
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
        )
        lineage = dict(
            import_receipt_id=getattr(outcome.receipt, "id", None),
            source_row=rec.get("_source_row"),
            source_file=rec.get("_source_file"),
            source_sheet=rec.get("_source_sheet"),
            source_page=rec.get("_source_page"),
        )
        if row:
            _update_census_row(row, values, lineage, outcome, mapped=set(rec))
        else:
            db.session.add(
                ResidentDay(
                    facility_id=facility.id, resident_id=rid, date=day, **values, **lineage
                )
            )
        outcome.resident_days_imported += 1
    unknown = sum(1 for rec in records if not str(rec.get("service_type") or "").strip())
    if unknown:
        outcome.warnings.append(
            f"{unknown} census rows have no service type, so they will not count as "
            "occupied bed days. Map the column, or state the service type for the sheet."
        )


CLEARABLE_CENSUS_FIELDS = ("leave_type", "leave_day_number", "exclusion_reason")


def _update_census_row(row, values, lineage, outcome, mapped=()):
    """Blank never erases identity or funding evidence, and each change is kept.

    A blank in a mapped leave or exclusion column is different: it says the
    resident is no longer on leave, and review lists the change.
    """
    changes = {
        name: (getattr(row, name), value)
        for name, value in values.items()
        if value != getattr(row, name)
        and (value is not None or (name in CLEARABLE_CENSUS_FIELDS and name in mapped))
    }
    if not changes:
        return  # An identical row stays with the import that first supplied it.
    history = json.loads(row.revisions_json or "[]")
    history.append({name: getattr(row, name) for name in (*values, *lineage)})
    row.revisions_json = json.dumps(history, default=str)
    outcome.census_changed += 1
    if len(outcome.census_changes) < 10:
        outcome.census_changes.append({
            "resident_id": row.resident_id,
            "date": row.date.isoformat(),
            "changes": [
                {"field": name, "from": old, "to": new}
                for name, (old, new) in changes.items()
                if name != "resident_name"
            ],
        })
    for name, (_, value) in changes.items():
        setattr(row, name, value)
    for name, value in lineage.items():
        setattr(row, name, value)


def restore_census_rows(receipt_id) -> tuple[int, int]:
    """Undo one import's census: earlier versions return, new rows go."""
    restored = removed = 0
    # Versions from this import under a later one must not return later on.
    for row in ResidentDay.query.filter(
        ResidentDay.revisions_json.contains(f'"import_receipt_id": {receipt_id}')
    ):
        history = [
            version for version in json.loads(row.revisions_json)
            if version.get("import_receipt_id") != receipt_id
        ]
        row.revisions_json = json.dumps(history) if history else None
    for row in ResidentDay.query.filter_by(import_receipt_id=receipt_id).all():
        history = json.loads(row.revisions_json or "[]")
        if not history:
            db.session.delete(row)
            removed += 1
            continue
        for name, value in history.pop().items():
            setattr(row, name, value)
        row.revisions_json = json.dumps(history) if history else None
        restored += 1
    return restored, removed


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
                minutes=max(_whole_minutes(minutes or 0), 0),
                import_receipt_id=getattr(outcome.receipt, "id", None),
                source_row=rec.get("_source_row"),
                source_file=rec.get("_source_file"),
                source_sheet=rec.get("_source_sheet"),
                source_page=rec.get("_source_page"),
            )
        )
        outcome.care_episodes_imported += 1
