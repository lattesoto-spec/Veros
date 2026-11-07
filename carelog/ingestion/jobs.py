"""Background import jobs.

The /import POST returns immediately; the slow work (AI format learning,
extraction, DB writes) happens outside the request so no gateway timeout can
kill an import. Job state lives in the database (models.ImportJob), so status
polling works no matter which instance serves it and a recycled process leaves
an inspectable record rather than a vanished job.

Two ways to run the work, chosen by environment:

  thread     a daemon thread in the same process — local dev and VM hosts
             where the process outlives the response.
  invoke     an HTTP call to /api/import/run/<job_id> on this same deployment —
             serverless hosts, where a thread would be frozen the moment the
             response is sent. The call is fired and abandoned; the receiving
             function does the work within its own duration budget.

Set IMPORT_WORKER=thread|invoke to force one; unset picks 'invoke' on Vercel.
"""

import hashlib
import json
import os
import threading
import uuid
from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy import update, select

from flask import current_app

from carelog.domain.care_minutes import quarter_bounds
from carelog.domain.clock import facility_today
from carelog.domain.compliance import CALC_VERSION, quarterly_tests
from carelog.models import (
    RETIRED_EVIDENCE,
    AuditLog,
    Facility,
    ImportJob,
    ImportReceipt,
    Shift,
    Staff,
    db,
)

from .analyzer import AnalyzerError
from .mapping import MappingError
from .pipeline import ingest_file, retire_shifts
from .reader import FileReadError
from .review import state_fingerprint

# Guards the shift-replacing import against a concurrent second import in the
# same process. Cross-process safety comes from a job claim and a facility row lock.
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


def start_job(
    app,
    facility_id: int,
    payloads: list[tuple[str, bytes]],
    storage,
    organization_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """Persist the uploads as audit evidence, record the job, dispatch it."""
    names = [os.path.basename(name) for name, _ in payloads]
    if (
        not names
        or any(
            not name or any(ord(c) < 32 for c in name) or "\\" in name for name in names
        )
        or len(set(names)) != len(names)
    ):
        raise FileReadError(
            "Each uploaded file needs a distinct filename; no files were imported."
        )
    payloads = [(name, data) for name, (_, data) in zip(names, payloads)]
    job_id = uuid.uuid4().hex[:12]
    prefix = f"imports/job-{job_id}"
    job = ImportJob(
        id=job_id,
        organization_id=organization_id,
        facility_id=facility_id,
        started_by_user_id=user_id,
        status="retaining",
        evidence_type="auto",
        storage_prefix=prefix,
        files_json=json.dumps(
            [
                {
                    "filename": fname,
                    "stage": "waiting",
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for fname, data in payloads
            ]
        ),
    )
    db.session.add(job)
    db.session.commit()

    try:
        for fname, data in payloads:
            storage.put(f"{prefix}/{os.path.basename(fname)}", data)
    except Exception:
        _fail(
            job_id,
            "Some source files could not be retained. No care data was imported. Retry the upload.",
        )
        raise
    _touch(job, status="queued")
    if worker_mode() == "invoke" and not os.environ.get("WORKER_SECRET"):
        _fail(
            job_id,
            "The import worker is not configured. Your originals are retained. Contact platform support, then retry from history.",
        )
        return job_id
    dispatch_job(app, job_id)
    return job_id


def dispatch_job(app, job_id):
    if worker_mode() == "invoke":
        _dispatch_http(job_id)
    else:
        threading.Thread(target=_run_in_app, args=(app, job_id), daemon=True).start()


def _dispatch_http(job_id: str):
    """Fire-and-forget request to run the job in a fresh function invocation.

    A short timeout is expected to expire — we only need the request to reach
    the platform, not to wait for the import to finish.
    """
    import httpx

    base = os.environ.get("IMPORT_WORKER_URL") or _self_url()
    secret = os.environ.get("WORKER_SECRET", "")
    headers = {"x-worker-secret": secret}
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    if bypass:
        headers["x-vercel-protection-bypass"] = bypass
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/api/import/run/{job_id}",
            headers=headers,
            timeout=httpx.Timeout(5.0, connect=5.0),
        )
        if response.status_code >= 400:
            _fail(
                job_id,
                "The import worker could not be started. Contact platform support.",
            )
    except httpx.ReadTimeout:
        # A read timeout can mean the worker is still processing the request.
        pass
    except httpx.HTTPError:
        _fail(
            job_id,
            "The import worker could not be reached. Your files are retained. Retry from import history.",
        )


def _self_url() -> str:
    host = os.environ.get("VERCEL_URL") or os.environ.get(
        "VERCEL_PROJECT_PRODUCTION_URL"
    )
    if host:
        return f"https://{host}"
    return os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080")


def _run_in_app(app, job_id: str):
    with app.app_context():
        run_job(job_id)


def run_job(job_id: str):
    """Execute a queued job. Safe to call twice: the status check makes the
    second caller a no-op, so a retried dispatch cannot double-import."""
    if not _RUN_LOCK.acquire(timeout=60):
        db.session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id, ImportJob.status == "queued")
            .values(
                status="failed",
                error="Another import is still running. Retry this upload shortly.",
                updated_at=datetime.utcnow(),
            )
        )
        db.session.commit()
        return
    try:
        # Compare-and-set prevents a duplicate serverless invocation claiming
        # the same job. Progress uses another session, never the care transaction.
        claimed = db.session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id, ImportJob.status == "queued")
            .values(status="running", updated_at=datetime.utcnow())
        )
        db.session.commit()
        if claimed.rowcount != 1:
            return
        job = db.session.get(ImportJob, job_id)
        # Serialize replacement imports for one facility across processes.
        db.session.execute(
            select(Facility).where(Facility.id == job.facility_id).with_for_update()
        )
        baseline = state_fingerprint(job.facility_id)
        review = json.loads(job.review_json) if job.review_json else None
        if job.approved_at and (not review or review["state_fingerprint"] != baseline):
            raise FileReadError(
                "Facility evidence changed since this review. Nothing was imported. Retry to review the current effect."
            )
        prepared = _process(job, review=review if job.approved_at else None)
        if not job.approved_at:
            files = json.loads(job.files_json)
            for state in files:
                state["stage"] = "ready for review"
            # Measure inside the uncommitted transaction, then again after the
            # rollback, so review shows the real effect on all three tests.
            periods = _affected_periods(job.facility_id, prepared.pop("dates"))
            after = _test_positions(job.facility_id, periods)
            leftovers = _period_rows(job.facility_id, job.receipt_id, prepared["scopes"])
            if leftovers.count():
                retire_shifts(leftovers, job.receipt_id)
                after_period = _test_positions(job.facility_id, periods)
            else:
                after_period = None
            db.session.rollback()
            job = db.session.get(ImportJob, job_id)
            prepared["effect"] = _merge_effect(
                _test_positions(job.facility_id, periods), after, after_period
            )
            prepared["state_fingerprint"] = baseline
            _touch(
                job,
                status="review",
                review_json=json.dumps(prepared),
                files_json=json.dumps(files),
                receipt_id=None,
            )
        else:
            # A recovery action may have superseded a stalled worker. Lock and
            # re-check its state before committing any care data.
            status = db.session.execute(
                select(ImportJob.status).where(ImportJob.id == job_id).with_for_update()
            ).scalar_one()
            if status != "running":
                db.session.rollback()
                return
            if job.replace_period:
                retired = retire_shifts(
                    _period_rows(job.facility_id, job.receipt_id, prepared["scopes"]),
                    job.receipt_id,
                )
                db.session.add(
                    AuditLog(
                        organization_id=job.organization_id,
                        user_id=job.reviewed_by_user_id,
                        action="import_period_replaced",
                        entity="import_job",
                        entity_id=job.id,
                        detail=f"{retired} shift rows in the file's date range were absent from the corrected export and retained as superseded",
                    )
                )
            _touch(job, status="done")
    except AnalyzerError as e:
        # Provider users need an actionable result, not platform vendor names,
        # credentials or model diagnostics. The detailed cause remains in the
        # server log for the platform owner.
        current_app.logger.warning(
            "Import mapping service failed for %s: %s", job_id, e
        )
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


def _process(job: ImportJob, review=None):
    from carelog.storage import build_storage
    from flask import current_app

    storage = build_storage(current_app.config["UPLOADS_DIR"])
    facility = db.session.get(Facility, job.facility_id)
    files = json.loads(job.files_json)

    # Receipt is created first so every shift row can carry its id (lineage).
    from carelog.models import User

    actor = (
        db.session.get(User, job.started_by_user_id) if job.started_by_user_id else None
    )
    receipt = ImportReceipt(
        organization_id=job.organization_id,
        facility_id=facility.id,
        imported_by_user_id=job.started_by_user_id,
        imported_by=(actor.email if actor else "web upload"),
        calc_version=CALC_VERSION,
        evidence_type="auto",
    )
    db.session.add(receipt)
    db.session.flush()

    outcomes = []
    reviewed = (
        {o["filename"]: o["spec"] for o in review["outcomes"]}
        if review
        else json.loads(job.manual_specs_json or "{}")
    )
    for state in files:

        def progress(stage, state=state):
            state["stage"] = stage
            _progress(job.id, files)

        progress("reading file")
        data = storage.get(
            f"{job.storage_prefix}/{os.path.basename(state['filename'])}"
        )
        digest = hashlib.sha256(data).hexdigest()
        if state.get("sha256") and state["sha256"] != digest:
            raise FileReadError(
                f"{state['filename']} no longer matches the file that was uploaded. "
                "Nothing was imported. Contact platform support with this import reference."
            )
        outcome = ingest_file(
            facility,
            state["filename"],
            data,
            receipt=receipt,
            progress=progress,
            reviewed_spec=reviewed.get(state["filename"]),
        )
        outcome.sha256 = digest
        outcomes.append(outcome)
        state["stage"] = "done"
        state["detail"] = (
            f"{outcome.shifts_imported} shifts, {outcome.staff_imported} staff profiles, "
            f"{outcome.residents_imported} residents, "
            f"{outcome.resident_days_imported} resident days, "
            f"{outcome.care_episodes_imported} care episodes"
            + (
                f", {len(outcome.row_errors)} rows skipped"
                if outcome.row_errors
                else ""
            )
        )
        _progress(job.id, files)

    receipt.residents_imported = sum(o.residents_imported for o in outcomes)
    receipt.residents_skipped = 0
    receipt.shifts_imported = sum(o.shifts_imported for o in outcomes)
    receipt.resident_days_imported = sum(o.resident_days_imported for o in outcomes)
    receipt.care_episodes_imported = sum(o.care_episodes_imported for o in outcomes)
    receipt.shifts_skipped = sum(len(o.row_errors) for o in outcomes)
    shift_types = {
        r.evidence_type
        for o in outcomes
        for r in o.results
        if r.kind == "shifts" and r.evidence_type
    }
    receipt.evidence_type = (
        next(iter(shift_types))
        if len(shift_types) == 1
        else ("mixed" if shift_types else "not applicable")
    )
    job.evidence_type = receipt.evidence_type
    receipt.original_evidence_type = receipt.evidence_type
    receipt.first_shift_date = min(
        (o.first_shift_date for o in outcomes if o.first_shift_date), default=None
    )
    receipt.last_shift_date = max(
        (o.last_shift_date for o in outcomes if o.last_shift_date), default=None
    )
    receipt.source_path = job.storage_prefix
    receipt.mapping_ids = ",".join(str(o.mapping_id) for o in outcomes if o.mapping_id)
    receipt.summary_json = json.dumps(_summarize(outcomes))
    job.receipt_id = receipt.id
    job.files_json = json.dumps(files)
    # run_job either rolls this entire transaction back for review, or commits
    # receipt, care rows, mapping changes and done status together.
    scopes = _shift_scopes(outcomes)
    leftovers = _period_rows(job.facility_id, receipt.id, scopes)
    return {
        "outcomes": _summarize(outcomes),
        "samples": _samples(outcomes),
        "scopes": scopes,
        "dates": sorted(_evidence_dates(outcomes)),
        "impact": {
            "shifts_imported": receipt.shifts_imported,
            "shifts_replaced": sum(o.shifts_replaced for o in outcomes),
            "residents_imported": receipt.residents_imported,
            "resident_days_imported": receipt.resident_days_imported,
            "care_episodes_imported": receipt.care_episodes_imported,
            "care_episodes_replaced": sum(o.care_episodes_replaced for o in outcomes),
            "period_rows": leftovers.count(),
            "period_sample": _period_sample(leftovers),
            "first_date": receipt.first_shift_date.isoformat()
            if receipt.first_shift_date
            else None,
            "last_date": receipt.last_shift_date.isoformat()
            if receipt.last_shift_date
            else None,
        },
    }


def _shift_scopes(outcomes) -> list[dict]:
    """The evidence stream and date range each shift sheet claims to describe."""
    scopes = []
    for outcome in outcomes:
        for result in outcome.results:
            days = [rec["date"] for rec in result.records] if result.kind == "shifts" else []
            if days:
                scopes.append(
                    {
                        "evidence_type": result.evidence_type,
                        "first": min(days).isoformat(),
                        "last": max(days).isoformat(),
                    }
                )
    return scopes


def _period_rows(facility_id, receipt_id, scopes):
    """Earlier rows in a file's date range that the file does not contain."""
    from sqlalchemy import and_, false, or_

    ranges = [
        and_(
            Shift.evidence_type == scope["evidence_type"],
            Shift.date >= date.fromisoformat(scope["first"]),
            Shift.date <= date.fromisoformat(scope["last"]),
        )
        for scope in scopes
    ]
    return Shift.query.filter(
        Shift.facility_id == facility_id,
        Shift.evidence_type.notin_(RETIRED_EVIDENCE),
        or_(Shift.import_receipt_id != receipt_id, Shift.import_receipt_id.is_(None)),
        or_(*ranges) if ranges else false(),
    )


def _period_sample(query, limit=10) -> list[dict]:
    from carelog.domain.compliance import worked_minutes

    rows = query.order_by(Shift.date, Shift.id).limit(limit).all()
    names = {
        staff.id: staff.name
        for staff in Staff.query.filter(Staff.id.in_({r.staff_id for r in rows}))
    }
    return [
        {
            "staff": names.get(row.staff_id, "Unknown"),
            "date": row.date.isoformat(),
            "minutes": worked_minutes(row),
            "source_file": row.source_file,
        }
        for row in rows
    ]


def _evidence_dates(outcomes) -> set[str]:
    return {
        rec["date"].isoformat()
        for outcome in outcomes
        for result in outcome.results
        for rec in result.records
        if isinstance(rec.get("date"), date)
    }


def _affected_periods(facility_id, dates) -> list[tuple[date, date]]:
    """Each touched quarter to date, so before and after compare the same span."""
    today = facility_today(db.session.get(Facility, facility_id))
    periods = {}
    for value in dates:
        start, end = quarter_bounds(date.fromisoformat(value))
        periods[start] = max(start, min(end, today))
    return sorted(periods.items())


def _test_positions(facility_id, periods) -> list[dict]:
    facility = db.session.get(Facility, facility_id)
    positions = []
    for start, end in periods:
        result = quarterly_tests(facility, start, end)
        positions.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "period_label": result["period_label"],
                "provisional": result["provisional"],
                "tests": [
                    {
                        key: test[key]
                        for key in ("key", "label", "achieved", "target", "on_track", "passed")
                    }
                    for test in result["tests"]
                ],
            }
        )
    return positions


def _merge_effect(before, after, after_period) -> list[dict]:
    return [
        {
            "start": old["start"],
            "end": old["end"],
            "period_label": old["period_label"],
            "before": old,
            "after": new,
            "after_period_replacement": replaced,
        }
        for old, new, replaced in zip(before, after, after_period or [None] * len(before))
    ]


def _samples(outcomes, rows=3) -> dict:
    """Interpreted example values, shown only to users who may see records."""
    samples = {}
    for outcome in outcomes:
        for result in outcome.results:
            shown = {}
            for rec in result.records[:rows]:
                for name, value in rec.items():
                    if not name.startswith("_") and value not in (None, ""):
                        shown.setdefault(name, []).append(str(value))
            samples.setdefault(outcome.filename, {})[
                f"{result.sheet}/{result.kind}"
            ] = shown
    return samples


def _progress(job_id, files):
    # A separate connection keeps status visible without committing care rows.
    with Session(db.engine) as progress_session:
        progress_session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(files_json=json.dumps(files), updated_at=datetime.utcnow())
        )
        progress_session.commit()


def _fail(job_id: str, message: str):
    job = db.session.get(ImportJob, job_id)
    if job is None:
        return
    files = json.loads(job.files_json)
    for f in files:
        if f["stage"] != "waiting":
            f["stage"] = "rolled back"
    _touch(job, status="failed", error=message, files_json=json.dumps(files))


def _summarize(outcomes) -> list[dict]:
    """JSON-serializable snapshot of the outcomes, persisted on the receipt so
    the summary page works after the job row is gone."""
    return [
        {
            "filename": o.filename,
            "fingerprint": o.fingerprint,
            "sha256": o.sha256,
            "mapping_reused": o.mapping_reused,
            "ai_usage": o.ai_usage,
            "spec": o.spec,
            "parsers": o.parsers,
            "warnings": o.warnings,
            "row_errors": o.row_errors,
            "results": [
                {
                    "kind": r.kind,
                    "sheet": r.sheet,
                    "imported": len(r.records),
                    "rows_seen": r.rows_seen,
                    "rows_filtered": r.rows_filtered,
                    "evidence_type": r.evidence_type,
                    "evidence_basis": r.evidence_basis,
                }
                for r in o.results
            ],
        }
        for o in outcomes
    ]
