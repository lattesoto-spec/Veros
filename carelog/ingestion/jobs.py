"""Background import jobs.

The /import POST returns immediately; the slow work (AI format learning,
extraction, DB writes) happens outside the request so no gateway timeout can
kill an import. Job state lives in the database (models.ImportJob), so status
polling works no matter which instance serves it and a recycled process leaves
an inspectable record rather than a vanished job.

Two ways to run the work, chosen by environment:

  thread     a daemon thread in the same process — local dev and VM hosts
             (Fly), where the process outlives the response.
  invoke     an HTTP call to /import/run/<job_id> on this same deployment —
             serverless hosts, where a thread would be frozen the moment the
             response is sent. The call is fired and abandoned; the receiving
             function does the work within its own duration budget.

Set IMPORT_WORKER=thread|invoke to force one; unset picks 'invoke' on Vercel.
"""

import json
import os
import threading
import uuid
from datetime import datetime

from flask import current_app

from carelog.domain.compliance import CALC_VERSION
from carelog.models import Facility, ImportJob, ImportReceipt, db

from .analyzer import AnalyzerError
from .mapping import MappingError
from .pipeline import ingest_file
from .reader import FileReadError

# Guards the shift-replacing import against a concurrent second import in the
# same process. Cross-process safety comes from the job status check in _run.
_RUN_LOCK = threading.Lock()


def worker_mode() -> str:
    mode = (os.environ.get("IMPORT_WORKER") or "").strip().lower()
    if mode:
        return mode
    return "invoke" if os.environ.get("VERCEL") else "thread"


def get_job(job_id: str) -> ImportJob | None:
    return db.session.get(ImportJob, job_id)


def _touch(job: ImportJob, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    job.updated_at = datetime.utcnow()
    db.session.commit()


def start_job(app, facility_id: int, payloads: list[tuple[str, bytes]], storage,
              organization_id: int | None = None, user_id: int | None = None,
              evidence_type: str = "unverified") -> str:
    """Persist the uploads as audit evidence, record the job, dispatch it."""
    job_id = uuid.uuid4().hex[:12]
    prefix = f"imports/job-{job_id}"
    for fname, data in payloads:
        safe = os.path.basename(fname) or "upload"
        storage.put(f"{prefix}/{safe}", data)

    job = ImportJob(
        id=job_id,
        organization_id=organization_id,
        facility_id=facility_id,
        started_by_user_id=user_id,
        status="queued",
        evidence_type=evidence_type if evidence_type in ("worked", "rostered", "unverified") else "unverified",
        storage_prefix=prefix,
        files_json=json.dumps(
            [{"filename": fname, "stage": "waiting"} for fname, _ in payloads]
        ),
    )
    db.session.add(job)
    db.session.commit()

    if worker_mode() == "invoke":
        _dispatch_http(job_id)
    else:
        threading.Thread(target=_run_in_app, args=(app, job_id), daemon=True).start()
    return job_id


def _dispatch_http(job_id: str):
    """Fire-and-forget request to run the job in a fresh function invocation.

    A short timeout is expected to expire — we only need the request to reach
    the platform, not to wait for the import to finish.
    """
    import httpx

    base = os.environ.get("IMPORT_WORKER_URL") or _self_url()
    secret = os.environ.get("WORKER_SECRET", "")
    try:
        httpx.post(
            f"{base}/import/run/{job_id}",
            headers={"x-worker-secret": secret},
            timeout=httpx.Timeout(5.0, connect=5.0),
        )
    except httpx.HTTPError:
        # Expected: the worker keeps running server-side after we stop waiting.
        pass


def _self_url() -> str:
    host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
    if host:
        return f"https://{host}"
    return os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080")


def _run_in_app(app, job_id: str):
    with app.app_context():
        run_job(job_id)


def run_job(job_id: str):
    """Execute a queued job. Safe to call twice: the status check makes the
    second caller a no-op, so a retried dispatch cannot double-import."""
    job = db.session.get(ImportJob, job_id)
    if job is None or job.status != "queued":
        return

    if not _RUN_LOCK.acquire(timeout=600):
        _touch(job, status="failed",
               error="Another import is still running — try again in a few minutes.")
        return
    try:
        _touch(job, status="running")
        _process(job)
        _touch(job, status="done")
    except AnalyzerError as e:
        # Provider users need an actionable result, not platform vendor names,
        # credentials or model diagnostics. The detailed cause remains in the
        # server log for the platform owner.
        current_app.logger.warning("Import mapping service failed for %s: %s", job_id, e)
        db.session.rollback()
        _fail(
            job_id,
            "CareMin could not safely interpret this file structure. Nothing was "
            "imported. Contact platform support and include this import reference.",
        )
    except (FileReadError, MappingError) as e:
        db.session.rollback()
        _fail(job_id, str(e))
    except Exception:  # safety net: surface anything on the status page
        current_app.logger.exception("Unexpected import failure for %s", job_id)
        db.session.rollback()
        _fail(
            job_id,
            "CareMin could not complete this import. Nothing was imported. "
            "Contact platform support and include this import reference.",
        )
        raise
    finally:
        _RUN_LOCK.release()


def _process(job: ImportJob):
    from carelog.storage import build_storage
    from flask import current_app

    storage = build_storage(current_app.config["UPLOADS_DIR"])
    facility = db.session.get(Facility, job.facility_id)
    files = json.loads(job.files_json)

    # Receipt is created first so every shift row can carry its id (lineage).
    from carelog.models import User

    actor = db.session.get(User, job.started_by_user_id) if job.started_by_user_id else None
    receipt = ImportReceipt(
        organization_id=job.organization_id,
        facility_id=facility.id,
        imported_by_user_id=job.started_by_user_id,
        imported_by=(actor.email if actor else "web upload"),
        calc_version=CALC_VERSION,
        evidence_type=job.evidence_type,
    )
    db.session.add(receipt)
    db.session.flush()

    outcomes = []
    for state in files:
        def progress(stage, state=state):
            state["stage"] = stage
            _touch(job, files_json=json.dumps(files))

        progress("reading file")
        data = storage.get(f"{job.storage_prefix}/{os.path.basename(state['filename'])}")
        outcome = ingest_file(
            facility, state["filename"], data, receipt=receipt, progress=progress,
            evidence_type=job.evidence_type,
        )
        outcomes.append(outcome)
        state["stage"] = "done"
        state["detail"] = (
            f"{outcome.shifts_imported} shifts, {outcome.residents_imported} residents, "
            f"{outcome.resident_days_imported} resident days, "
            f"{outcome.care_episodes_imported} care episodes"
            + (f", {len(outcome.row_errors)} rows skipped" if outcome.row_errors else "")
        )
        _touch(job, files_json=json.dumps(files))

    receipt.residents_imported = sum(o.residents_imported for o in outcomes)
    receipt.residents_skipped = 0
    receipt.shifts_imported = sum(o.shifts_imported for o in outcomes)
    receipt.resident_days_imported = sum(o.resident_days_imported for o in outcomes)
    receipt.care_episodes_imported = sum(o.care_episodes_imported for o in outcomes)
    receipt.shifts_skipped = sum(len(o.row_errors) for o in outcomes)
    receipt.first_shift_date = min((o.first_shift_date for o in outcomes if o.first_shift_date), default=None)
    receipt.last_shift_date = max((o.last_shift_date for o in outcomes if o.last_shift_date), default=None)
    receipt.source_path = job.storage_prefix
    receipt.mapping_ids = ",".join(str(o.mapping_id) for o in outcomes if o.mapping_id)
    receipt.summary_json = json.dumps(_summarize(outcomes))
    db.session.commit()
    job.receipt_id = receipt.id


def _fail(job_id: str, message: str):
    job = db.session.get(ImportJob, job_id)
    if job is None:
        return
    files = json.loads(job.files_json)
    for f in files:
        if f["stage"] not in ("done", "waiting"):
            f["stage"] = "failed"
    _touch(job, status="failed", error=message, files_json=json.dumps(files))


def _summarize(outcomes) -> list[dict]:
    """JSON-serializable snapshot of the outcomes, persisted on the receipt so
    the summary page works after the job row is gone."""
    return [
        {
            "filename": o.filename,
            "fingerprint": o.fingerprint,
            "mapping_reused": o.mapping_reused,
            "ai_usage": o.ai_usage,
            "spec": o.spec,
            "warnings": o.warnings,
            "row_errors": o.row_errors,
            "results": [
                {
                    "kind": r.kind,
                    "sheet": r.sheet,
                    "imported": len(r.records),
                    "rows_seen": r.rows_seen,
                    "rows_filtered": r.rows_filtered,
                }
                for r in o.results
            ],
        }
        for o in outcomes
    ]
