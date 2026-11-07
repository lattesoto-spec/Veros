"""Provider traceability and audited evidence-class decisions."""

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from flask import abort, request, url_for
from sqlalchemy import or_, select

from carelog import auth
from carelog.api import bp
from carelog.api.monitoring import _latest_data_date
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import owned_or_404, require
from carelog.domain.clock import facility_today
from carelog.domain.compliance import worked_minutes
from carelog.domain.eligibility import counts_toward_care, registration_problem
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.quality import validate_shifts
from carelog.models import (
    RETIRED_EVIDENCE,
    SUPERSEDED,
    VOIDED,
    Facility,
    ImportReceipt,
    Shift,
    Staff,
    db,
)
from carelog.services.periods import reporting_range
from carelog.services.workspace import current_facility


@bp.get("/audit/records")
@require("view_audit")
@require("view_records")
def evidence_records():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    try:
        start, end = reporting_range(request.args, _latest_data_date(facility))
        page = int(request.args.get("page", "1"))
        if page < 1:
            raise ValueError("Page must be a positive whole number.")
        staff_id = int(request.args["staff"]) if request.args.get("staff") else None
    except ValueError as error:
        return {"error": str(error)}, 400
    evidence = request.args.get("evidence", "all")
    if evidence not in {"all", "worked", "rostered", "unverified", *RETIRED_EVIDENCE}:
        return {"error": "Choose worked, rostered, unverified, voided, superseded or all evidence."}, 400
    query = db.session.query(Shift, Staff).join(Staff, Shift.staff_id == Staff.id).filter(
        Shift.facility_id == facility.id, Shift.date >= start, Shift.date <= end)
    if evidence == "all":
        query = query.filter(Shift.evidence_type.notin_(RETIRED_EVIDENCE))
    else:
        query = query.filter(Shift.evidence_type == evidence)
    if staff_id is not None:
        if Staff.query.filter_by(id=staff_id, facility_id=facility.id).first() is None:
            abort(404)
        query = query.filter(Shift.staff_id == staff_id)
    total = query.count()
    records = []
    for shift, member in query.order_by(Shift.date.desc(), Shift.id).offset((page - 1) * 100).limit(100):
        eligible = shift.is_direct_care and counts_toward_care(member, shift.date)
        counted = eligible and shift.evidence_type == "worked"
        reason = "Eligible actual worked" if counted else (
            f"Voided: {shift.void_reason}" if shift.evidence_type == VOIDED
            else f"Replaced by import #{shift.superseded_by_receipt_id}" if shift.evidence_type == SUPERSEDED
            else f"{shift.evidence_type}: excluded from actual results" if shift.evidence_type != "worked"
            else "Not direct care" if not shift.is_direct_care
            else registration_problem(member, shift.date) or
            ("Outside approved effective dates" if member.eligibility_status == "approved" else member.eligibility_reason)
            or "Eligibility review required")
        records.append({"shift": shift, "staff_name": member.name, "source_role": member.source_role,
                        "role": member.role, "minutes": worked_minutes(shift), "counted": counted, "reason": reason})
    return resource("evidence_records", records=records, start=start, end=end, page=page,
                    total=total, has_next=page * 100 < total, evidence=evidence, staff=staff_id)


def _facility_shift_or_404(shift_id):
    """Explicit tenant predicate: shifts carry a facility, not an organisation."""
    facility = current_facility()
    if not facility:
        abort(404)
    db.session.execute(select(Facility).where(Facility.id == facility.id).with_for_update())
    row = Shift.query.filter_by(id=shift_id, facility_id=facility.id).first()
    if row is None:
        abort(404)
    return facility, row


def _shift_label(row):
    source = f"{row.source_file or 'source not recorded'} / {row.source_sheet or '-'} / row {row.source_row or '-'}"
    return f"staff {row.staff_id}, {row.date}, {worked_minutes(row)} minutes, {source}"


@bp.post("/audit/shift/<int:shift_id>/void")
@require("review_eligibility")
def void_shift(shift_id):
    """Remove one wrong row from the evidence without deleting the record."""
    _, row = _facility_shift_or_404(shift_id)
    reason = (form_data().get("reason") or "").strip()
    if not reason:
        return {"error": "Record why this row should not count, such as a no-show or a duplicate."}, 400
    if row.evidence_type in RETIRED_EVIDENCE:
        return {"error": "This row is already outside the evidence streams. Reload the page."}, 409
    row.prior_evidence_type, row.evidence_type = row.evidence_type, VOIDED
    row.void_reason, row.voided_at = reason, datetime.utcnow()
    row.voided_by_user_id = auth.current_user().id
    auth.record("shift_voided", "shift", row.id, f"{_shift_label(row)}; basis: {reason}")
    db.session.commit()
    return _back_to_records()


@bp.post("/audit/shift/<int:shift_id>/restore")
@require("review_eligibility")
def restore_shift(shift_id):
    facility, row = _facility_shift_or_404(shift_id)
    reason = (form_data().get("reason") or "").strip()
    if not reason:
        return {"error": "Record why this row should count again."}, 400
    if row.evidence_type != VOIDED:
        return {"error": "Only a voided row can be restored."}, 409
    stream = row.prior_evidence_type or "unverified"
    active = Shift.query.filter(
        Shift.facility_id == row.facility_id, Shift.staff_id == row.staff_id,
        Shift.evidence_type == stream,
        Shift.date >= row.date - timedelta(days=1), Shift.date <= row.date + timedelta(days=1),
    ).all()
    try:
        validate_shifts([_validation_row(item) for item in active + [row]],
                        evidence_type=stream, today=facility_today(facility))
    except MappingError as error:
        return {"error": str(error).replace("Correct the source and upload again.", "Void the other row first.")}, 409
    row.evidence_type, row.prior_evidence_type = stream, None
    row.void_reason = row.voided_at = row.voided_by_user_id = None
    auth.record("shift_restored", "shift", row.id, f"{_shift_label(row)}; basis: {reason}")
    db.session.commit()
    return _back_to_records()


def _back_to_records():
    allowed = {"start", "end", "evidence", "staff", "page"}
    args = {key: value for key, value in parse_qsl(form_data().get("return_query") or "") if key in allowed}
    return navigate(url_for("api.evidence_records", **args))


def _validation_row(row):
    return {"staff_id": row.staff_id, "date": row.date, "start_time": row.start_time if row.coverage_known else None,
            "end_time": row.end_time if row.coverage_known else None,
            "minutes": row.duration_minutes, "break_minutes": row.break_minutes if row.coverage_known else 0,
            "pay_category": row.pay_category, "_source_file": row.source_file, "_source_row": row.source_row}


@bp.post("/audit/receipt/<int:receipt_id>/evidence")
@require("manage_facility")
def classify_receipt(receipt_id):
    receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
    target = form_data().get("evidence_type")
    reason = (form_data().get("reason") or "").strip()
    if target not in {"worked", "rostered", "unverified"} or not reason:
        return {"error": "Choose the evidence class and record the source verification basis."}, 400
    # Serialize evidence decisions with imports before reading active rows.
    db.session.execute(select(Facility).where(Facility.id == receipt.facility_id).with_for_update())
    rows = Shift.query.filter(Shift.import_receipt_id == receipt.id,
                              Shift.evidence_type.notin_(RETIRED_EVIDENCE)).all()
    types = {row.evidence_type for row in rows}
    if not rows or len(types) != 1:
        return {"error": "This receipt has no single active shift stream. Re-import the relevant file separately for review."}, 409
    old = next(iter(types))
    original = receipt.original_evidence_type or (receipt.evidence_type if receipt.evidence_type in {"rostered", "mixed"} else old)
    if (old == "rostered" or original in {"rostered", "mixed"}) and target == "worked":
        return {"error": "Planned roster evidence cannot be confirmed as actual worked. Upload actual time-and-attendance evidence."}, 400
    others = Shift.query.filter(Shift.facility_id == receipt.facility_id, Shift.evidence_type == target,
                                or_(Shift.import_receipt_id != receipt.id, Shift.import_receipt_id.is_(None))).all()
    keys = {(row.staff_id, row.date) for row in rows}
    if target != old and any((row.staff_id, row.date) in keys for row in others):
        return {"error": "That stream already has evidence for these staff/dates. Re-import for a replacement review instead."}, 409
    try:
        validate_shifts([_validation_row(row) for row in rows], evidence_type=target)
    except MappingError as error:
        return {"error": str(error)}, 400
    for row in rows:
        row.evidence_type = target
    receipt.original_evidence_type = original
    receipt.evidence_type = target
    summary = json.loads(receipt.summary_json or "[]")
    for outcome in summary:
        for result in outcome.get("results", []):
            if result["kind"] == "shifts":
                result["evidence_type"] = target
                result["evidence_basis"] = f"Provider verification: {reason}"
    receipt.summary_json = json.dumps(summary)
    auth.record("evidence_classified", "import_receipt", receipt.id, f"{old} -> {target}; {len(rows)} active rows; basis: {reason}")
    db.session.commit()
    return navigate(url_for("api.import_summary", receipt_id=receipt.id))
