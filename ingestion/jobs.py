"""Background import jobs.

The /import POST returns immediately; a daemon thread does the slow work
(AI format learning, extraction, DB writes) while the browser polls a status
endpoint. This keeps every HTTP request fast regardless of file size or
Anthropic API latency — no gunicorn/proxy timeout can kill an import.

Job state lives in process memory: gunicorn runs a single worker process
(threads only — do not add --workers to the Dockerfile), so every web thread
sees the same registry. If the process restarts mid-job the tracking is lost,
but the import transaction commits as a whole, so the database stays
consistent either way.
"""

import json
import os
import threading
import uuid
from datetime import datetime

from compliance import CALC_VERSION
from models import Facility, ImportReceipt, db

from .analyzer import AnalyzerError
from .mapping import MappingError
from .pipeline import ingest_file
from .reader import FileReadError

_JOBS: dict[str, dict] = {}
# Imports replace the facility's shift data, so two concurrent jobs would
# race on the delete-then-insert. Serialize them.
_RUN_LOCK = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


def start_job(app, facility_id: int, payloads: list[tuple[str, bytes]], uploads_root: str) -> str:
    """Save the uploads to disk (audit evidence), register the job, and kick
    off the worker thread. Returns the job id for the status page."""
    job_id = uuid.uuid4().hex[:12]
    upload_dir = os.path.join(uploads_root, f"job-{job_id}")
    os.makedirs(upload_dir, exist_ok=True)
    for fname, data in payloads:
        safe = os.path.basename(fname) or "upload"
        with open(os.path.join(upload_dir, safe), "wb") as fh:
            fh.write(data)

    job = {
        "id": job_id,
        "status": "queued",
        "error": None,
        "receipt_id": None,
        "files": [{"filename": fname, "stage": "waiting"} for fname, _ in payloads],
        "started_at": datetime.utcnow().isoformat(),
    }
    _JOBS[job_id] = job
    threading.Thread(
        target=_run, args=(app, job, facility_id, payloads, upload_dir), daemon=True
    ).start()
    return job_id


def _run(app, job, facility_id, payloads, upload_dir):
    with app.app_context():
        if not _RUN_LOCK.acquire(timeout=600):
            job["error"] = "Another import is still running — try again in a few minutes."
            job["status"] = "failed"
            return
        try:
            job["status"] = "running"
            _process(job, facility_id, payloads, upload_dir)
            job["status"] = "done"
        except (FileReadError, MappingError, AnalyzerError) as e:
            db.session.rollback()
            _fail(job, str(e))
        except Exception as e:  # safety net: surface anything on the status page
            app.logger.exception("Import job %s failed", job["id"])
            db.session.rollback()
            _fail(job, f"Unexpected error during import: {type(e).__name__}: {e}")
        finally:
            _RUN_LOCK.release()


def _process(job, facility_id, payloads, upload_dir):
    facility = db.session.get(Facility, facility_id)

    # Receipt is created first so every shift row can carry its id (lineage).
    receipt = ImportReceipt(
        facility_id=facility.id,
        imported_by="web upload",
        calc_version=CALC_VERSION,
    )
    db.session.add(receipt)
    db.session.flush()

    outcomes = []
    for file_state, (fname, data) in zip(job["files"], payloads):
        def progress(stage, file_state=file_state):
            file_state["stage"] = stage

        progress("reading file")
        outcome = ingest_file(facility, fname, data, receipt=receipt, progress=progress)
        outcomes.append(outcome)
        file_state["stage"] = "done"
        file_state["detail"] = (
            f"{outcome.shifts_imported} shifts, {outcome.residents_imported} residents"
            + (f", {len(outcome.row_errors)} rows skipped" if outcome.row_errors else "")
        )

    receipt.residents_imported = sum(o.residents_imported for o in outcomes)
    receipt.residents_skipped = 0
    receipt.shifts_imported = sum(o.shifts_imported for o in outcomes)
    receipt.shifts_skipped = sum(len(o.row_errors) for o in outcomes)
    receipt.first_shift_date = min((o.first_shift_date for o in outcomes if o.first_shift_date), default=None)
    receipt.last_shift_date = max((o.last_shift_date for o in outcomes if o.last_shift_date), default=None)
    receipt.source_path = upload_dir
    receipt.mapping_ids = ",".join(str(o.mapping_id) for o in outcomes if o.mapping_id)
    receipt.summary_json = json.dumps(_summarize(outcomes))
    db.session.commit()
    job["receipt_id"] = receipt.id


def _fail(job, message):
    job["error"] = message
    job["status"] = "failed"
    for f in job["files"]:
        if f["stage"] not in ("done", "waiting"):
            f["stage"] = "failed"


def _summarize(outcomes) -> list[dict]:
    """JSON-serializable snapshot of the outcomes, persisted on the receipt so
    the summary page works after the job (or the process) is gone."""
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
