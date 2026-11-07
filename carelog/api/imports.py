import hmac
import json
import os
from datetime import datetime, timedelta
from sqlalchemy import update

from flask import (
    Response,
    abort,
    current_app,
    flash,
    request,
    send_from_directory,
    session,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_organization_id,
    current_user,
    has_permission,
    owned_or_404,
    require,
)
from carelog.domain.compliance import (
    CALC_VERSION,
)
from carelog.ingestion import jobs as import_jobs
from carelog.ingestion.reader import FileReadError
from carelog.models import (
    CareEpisode,
    Facility,
    ImportJob,
    ImportReceipt,
    ResidentDay,
    Shift,
    AuditLog,
    db,
)
from carelog.services.targets import target_details, save_target
from carelog.services.workspace import (
    ROOT,
    _confirmed,
    _purge_evidence,
    _receipt_files,
    _storage,
    _storage_for,
    current_facility,
    org_facilities,
)
from carelog.storage import StorageError


@bp.route("/import", methods=["GET", "POST"])
@require("import_data")
def universal_import():
    facility = current_facility()
    if request.method == "GET":
        return resource(
            "universal_import",
            facility=facility,
            facilities=org_facilities(),
            errors=[],
            jobs=[
                job.as_dict()
                for job in ImportJob.query.filter_by(facility_id=facility.id)
                .order_by(ImportJob.created_at.desc())
                .limit(30)
            ]
            if facility
            else [],
        )

    errors = []
    name = (form_data().get("facility_name") or "").strip()
    files = [f for f in request.files.getlist("data_files") if f and f.filename]
    # With several homes in one organization, the upload must say which one
    # it belongs to rather than silently landing in whichever is active.
    target_id = form_data().get("facility_id", type=int)
    if form_data().get("facility_id") and target_id is None:
        errors.append("Select a valid destination facility; nothing was imported.")
    if len(org_facilities()) > 1 and not target_id:
        errors.append(
            "Select the destination facility explicitly when the organisation has multiple facilities."
        )
    if target_id:
        facility = Facility.query.filter_by(
            id=target_id, organization_id=current_organization_id()
        ).first()
        if facility is None:
            abort(404)
        session["facility_id"] = facility.id

    if not name and not facility:
        errors.append("Facility name is required for the first import.")
    targets = None
    if not facility:
        if not has_permission("manage_facility"):
            errors.append(
                "Ask your administrator to create a facility before importing."
            )
        try:
            targets = target_details(form_data())
        except ValueError as error:
            errors.append(str(error))
    if not files:
        errors.append("Upload at least one file.")
    if errors:
        return resource(
            "universal_import",
            facility=facility,
            facilities=org_facilities(),
            errors=errors,
        ), 400

    if not facility:
        facility = Facility(
            name=name,
            organization_id=current_organization_id(),
            ancc_target=targets[0],
            rn_target=targets[1],
        )
        db.session.add(facility)
        db.session.flush()
        save_target(facility, form_data(), current_user())
        session["facility_id"] = facility.id
    # Commit now: the background worker needs the facility id, and the
    # request must not hold an open transaction while the job runs.
    db.session.commit()

    # The slow work (AI format learning, extraction) happens in a
    # background job so this request returns immediately — no gateway
    # timeout can kill an import, however large the file.
    payloads = [(f.filename, f.read()) for f in files]
    user = current_user()
    try:
        job_id = import_jobs.start_job(
            current_app._get_current_object(),
            facility.id,
            payloads,
            _storage(),
            organization_id=current_organization_id(),
            user_id=user.id if user else None,
        )
    except (FileReadError, StorageError) as error:
        return resource(
            "universal_import",
            facility=facility,
            facilities=org_facilities(),
            errors=[str(error)],
        ), 400
    auth.record(
        "import_started",
        "import_job",
        job_id,
        ", ".join(fname for fname, _ in payloads),
    )
    db.session.commit()
    return navigate(url_for("api.import_status", job_id=job_id))


@bp.route("/import/guide")
@require("import_data")
def import_guide():
    """Optional import reference kept outside the primary upload task."""
    return resource("import_guide")


@bp.route("/import/run/<job_id>", methods=["POST"])
@auth.csrf.exempt
def import_run(job_id):
    """Worker entrypoint. On serverless hosts the upload request cannot do
    the work itself, so it calls this to run the job in its own
    invocation."""
    expected = os.environ.get("WORKER_SECRET", "")
    if not expected or not hmac.compare_digest(
        request.headers.get("x-worker-secret", ""), expected
    ):
        return {"error": "forbidden"}, 403
    import_jobs.run_job(job_id)
    job = import_jobs.get_job(job_id)
    return (job.as_dict() if job else {"error": "unknown job"}), (200 if job else 404)


@bp.route("/import/status/<job_id>")
@require("view_audit")
def import_status(job_id):
    job = import_jobs.get_job(job_id)
    if not job:
        flash("That import job no longer exists. Check the audit trail for the result.")
        return navigate(url_for("api.universal_import"))
    return resource("import_status", job=owned_or_404(job).as_dict())


@bp.route("/import/<job_id>/confirm", methods=["POST"])
@require("import_data")
def import_confirm(job_id):
    job = owned_or_404(import_jobs.get_job(job_id))
    changed = db.session.execute(
        update(ImportJob)
        .where(ImportJob.id == job.id, ImportJob.status == "review")
        .values(
            status="queued",
            approved_at=datetime.utcnow(),
            reviewed_by_user_id=current_user().id,
            updated_at=datetime.utcnow(),
        )
    )
    if changed.rowcount != 1:
        db.session.rollback()
        return {
            "error": "This import is no longer awaiting review. Reload its status."
        }, 409
    auth.record(
        "import_approved",
        "import_job",
        job.id,
        "Reviewed column mapping, evidence class, dates and replacement scope",
    )
    db.session.commit()
    import_jobs.dispatch_job(current_app._get_current_object(), job.id)
    return navigate(url_for("api.import_status", job_id=job.id))


@bp.route("/import/<job_id>/cancel", methods=["POST"])
@require("import_data")
def import_cancel(job_id):
    job = owned_or_404(import_jobs.get_job(job_id))
    changed = db.session.execute(
        update(ImportJob)
        .where(ImportJob.id == job.id, ImportJob.status == "review")
        .values(status="cancelled", updated_at=datetime.utcnow())
    )
    if changed.rowcount != 1:
        db.session.rollback()
        return {"error": "Only an import awaiting review can be cancelled."}, 409
    auth.record(
        "import_cancelled",
        "import_job",
        job.id,
        "No care data committed; sources retained",
    )
    db.session.commit()
    return navigate(url_for("api.import_status", job_id=job.id))


@bp.route("/import/<job_id>/retry", methods=["POST"])
@require("import_data")
def import_retry(job_id):
    job = owned_or_404(import_jobs.get_job(job_id))
    cutoff = datetime.utcnow() - timedelta(minutes=15)
    if job.status not in {"failed", "cancelled"}:
        changed = db.session.execute(
            update(ImportJob)
            .where(
                ImportJob.id == job.id,
                ImportJob.status.in_(["queued", "running"]),
                ImportJob.updated_at < cutoff,
            )
            .values(
                status="failed",
                error="Superseded by a recovery attempt; original sources retained.",
            )
        )
        if changed.rowcount != 1:
            db.session.rollback()
            return {
                "error": "This import is still active or already completed. Reload its status."
            }, 409
        db.session.commit()
    try:
        files = json.loads(job.files_json)
        payloads = [
            (
                os.path.basename(f["filename"]),
                _storage().get(
                    f"{job.storage_prefix}/{os.path.basename(f['filename'])}"
                ),
            )
            for f in files
        ]
        new_id = import_jobs.start_job(
            current_app._get_current_object(),
            job.facility_id,
            payloads,
            _storage(),
            organization_id=current_organization_id(),
            user_id=current_user().id,
        )
    except (FileReadError, StorageError) as error:
        return {"error": str(error)}, 400
    auth.record(
        "import_retried",
        "import_job",
        new_id,
        f"Retained sources from {job.id}; review required again",
    )
    db.session.commit()
    return navigate(url_for("api.import_status", job_id=new_id))


@bp.route("/audit/job/<job_id>/file/<path:filename>")
@require("download_evidence")
def audit_job_file(job_id, filename):
    job = owned_or_404(import_jobs.get_job(job_id))
    names = {os.path.basename(f["filename"]) for f in json.loads(job.files_json)}
    if filename not in names:
        abort(404)
    try:
        contents = _storage().get(f"{job.storage_prefix}/{filename}")
    except StorageError:
        return {
            "error": "The retained file could not be retrieved. Retry or contact support with this import reference."
        }, 502
    from io import BytesIO
    from flask import send_file

    return send_file(
        BytesIO(contents),
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream",
    )


@bp.route("/import/status/<job_id>.json")
@require("view_audit")
def import_status_json(job_id):
    job = import_jobs.get_job(job_id)
    if not job:
        return {"status": "gone"}, 404
    return owned_or_404(job).as_dict()


@bp.route("/import/summary/<int:receipt_id>")
@require("view_audit")
def import_summary(receipt_id):
    receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
    outcomes = json.loads(receipt.summary_json) if receipt.summary_json else []
    return resource(
        "import_summary",
        facility=current_facility(),
        outcomes=outcomes,
        receipt=receipt,
    )


@bp.route("/audit")
@require("view_audit")
def audit():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    receipts = (
        ImportReceipt.query.filter_by(facility_id=facility.id)
        .order_by(ImportReceipt.imported_at.desc())
        .all()
    )
    files_by_receipt = {}
    for r in receipts:
        names = [o.name for o in _receipt_files(r)]
        if names:
            files_by_receipt[r.id] = names
    with_files = sum(1 for r in receipts if r.id in files_by_receipt)
    return resource(
        "audit",
        facility=facility,
        receipts=receipts,
        files_by_receipt=files_by_receipt,
        readiness=round(with_files / len(receipts) * 100) if receipts else None,
        calc_version=CALC_VERSION,
        jobs=[
            job.as_dict()
            for job in ImportJob.query.filter_by(facility_id=facility.id)
            .order_by(ImportJob.created_at.desc())
            .limit(100)
        ],
        decisions=AuditLog.query.filter(
            AuditLog.organization_id == current_organization_id(),
            AuditLog.action.in_(
                [
                    "target_approved",
                    "target_corrected",
                    "eligibility_decided",
                    "import_approved",
                    "import_cancelled",
                    "import_retried",
                    "evidence_classified",
                ]
            ),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all(),
    )


@bp.route("/audit/file/<int:receipt_id>/<path:filename>")
@require("download_evidence")
def audit_file(receipt_id, filename):
    receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
    safe = os.path.basename(filename)
    match = next((o for o in _receipt_files(receipt) if o.name == safe), None)
    if not match:
        return ("Not found", 404)
    try:
        blob = _storage_for(receipt).get(match.key)
    except StorageError as e:
        current_app.logger.warning("Audit file fetch failed: %s", e)
        return ("That evidence file could not be retrieved from storage.", 502)
    return Response(
        blob,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={safe}"},
    )


@bp.route("/audit/receipt/<int:receipt_id>/delete", methods=["POST"])
@require("manage_facility")
def delete_receipt(receipt_id):
    """Delete one import: its rows, its receipt and its retained files."""
    receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
    Facility.query.filter_by(
        id=receipt.facility_id, organization_id=current_organization_id()
    ).with_for_update().first()
    if not _confirmed(request):
        flash('Type "delete" to confirm. Nothing was deleted.')
        return navigate(url_for("api.audit"))

    try:
        files = _purge_evidence([receipt])
    except StorageError:
        return {
            "error": "Evidence storage could not confirm deletion. The receipt was kept; investigate storage and retry."
        }, 502
    shifts = Shift.query.filter_by(import_receipt_id=receipt.id).delete(
        synchronize_session=False
    )
    resident_days = ResidentDay.query.filter_by(import_receipt_id=receipt.id).delete(
        synchronize_session=False
    )
    episodes = CareEpisode.query.filter_by(import_receipt_id=receipt.id).delete(
        synchronize_session=False
    )
    ImportJob.query.filter_by(receipt_id=receipt.id).update(
        {"receipt_id": None}, synchronize_session=False
    )
    db.session.delete(receipt)
    auth.record(
        "import_deleted",
        "import_receipt",
        receipt_id,
        f"{shifts} shifts, {resident_days} resident days, {episodes} care episodes, "
        f"{files} evidence file(s)",
    )
    db.session.commit()
    flash(
        f"Import #{receipt_id} deleted: {shifts} shift row(s), {resident_days} "
        f"resident-day row(s), {episodes} care episode(s) and {files} evidence file(s) removed."
    )
    return navigate(url_for("api.audit"))


@bp.route("/samples/<path:filename>")
def sample_file(filename):
    simple = {"residents.csv", "shifts.csv"}
    realistic = {
        "resident_census_july_2026.xlsx",
        "worked_staffing_july_2026.xlsx",
        "care_delivery_july_2026.xlsx",
    }
    if filename in simple:
        directory = os.path.join(ROOT, "sample_data")
    elif filename in realistic:
        directory = os.path.join(ROOT, "sample_data", "realistic_imports")
    else:
        return ("Not found", 404)
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
    )
