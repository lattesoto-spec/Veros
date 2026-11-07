"""Provider traceability and audited evidence-class decisions."""

import json

from flask import abort, request, url_for

from carelog import auth
from carelog.api import bp
from carelog.api.monitoring import _latest_data_date
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import owned_or_404, require
from carelog.domain.compliance import worked_minutes
from carelog.domain.eligibility import counts_toward_care, registration_problem
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.quality import validate_shifts
from carelog.models import ImportReceipt, Shift, Staff, db
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
    if evidence not in {"all", "worked", "rostered", "unverified"}:
        return {"error": "Choose worked, rostered, unverified or all evidence."}, 400
    query = db.session.query(Shift, Staff).join(Staff, Shift.staff_id == Staff.id).filter(
        Shift.facility_id == facility.id, Shift.date >= start, Shift.date <= end)
    if evidence != "all":
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
            f"{shift.evidence_type}: excluded from actual results" if shift.evidence_type != "worked"
            else "Not direct care" if not shift.is_direct_care
            else registration_problem(member, shift.date) or
            ("Outside approved effective dates" if member.eligibility_status == "approved" else member.eligibility_reason)
            or "Eligibility review required")
        records.append({"shift": shift, "staff_name": member.name, "source_role": member.source_role,
                        "role": member.role, "minutes": worked_minutes(shift), "counted": counted, "reason": reason})
    return resource("evidence_records", records=records, start=start, end=end, page=page,
                    total=total, has_next=page * 100 < total, evidence=evidence, staff=staff_id)


@bp.post("/audit/receipt/<int:receipt_id>/evidence")
@require("manage_facility")
def classify_receipt(receipt_id):
    receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
    target = form_data().get("evidence_type")
    reason = (form_data().get("reason") or "").strip()
    if target not in {"worked", "rostered", "unverified"} or not reason:
        return {"error": "Choose the evidence class and record the source verification basis."}, 400
    # Serialize evidence decisions with imports before reading active rows.
    from sqlalchemy import select, or_
    from carelog.models import Facility
    db.session.execute(select(Facility).where(Facility.id == receipt.facility_id).with_for_update())
    rows = Shift.query.filter_by(import_receipt_id=receipt.id).all()
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
        validate_shifts([{"staff_id": row.staff_id, "date": row.date, "start_time": row.start_time if row.coverage_known else None,
                          "end_time": row.end_time if row.coverage_known else None,
                          "minutes": row.duration_minutes, "break_minutes": row.break_minutes if row.coverage_known else 0,
                          "_source_file": row.source_file, "_source_row": row.source_row} for row in rows], evidence_type=target)
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
