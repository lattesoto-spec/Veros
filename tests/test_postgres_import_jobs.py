"""Integration checks against a disposable local PostgreSQL database only."""

import json
import os
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from carelog.app import create_app
from carelog.database import init_db
from carelog.ingestion import jobs
from carelog.ingestion.reader import FileReadError
from carelog.models import Facility, ImportJob, ImportReceipt, Organization, Shift, db
from carelog.storage import LocalStorage


@pytest.fixture()
def pg_app(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to a disposable local PostgreSQL test database"
        )
    parsed = make_url(url)
    assert parsed.host in ("127.0.0.1", "localhost") and parsed.database.endswith(
        "_test"
    )
    monkeypatch.setenv("AUTO_INIT_DB", "0")
    monkeypatch.setenv("IMPORT_WORKER", "invoke")
    monkeypatch.setenv("WORKER_SECRET", "test-only-worker-key")
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": url,
            "UPLOADS_DIR": str(tmp_path),
            "SECRET_KEY": "isolated-test",
        }
    )
    storage = LocalStorage(str(tmp_path))
    monkeypatch.setattr("carelog.storage.build_storage", lambda _: storage)
    monkeypatch.setattr(jobs, "_dispatch_http", lambda _: None)
    with app.app_context():
        db.drop_all()
        init_db()
        org = Organization(name="Test Provider")
        db.session.add(org)
        db.session.flush()
        facility = Facility(organization_id=org.id, name="Test home")
        db.session.add(facility)
        db.session.commit()
        yield app, facility, storage
        db.session.remove()
        db.drop_all()


def test_postgres_batch_rollback_and_success_provenance(pg_app):
    app, facility, storage = pg_app
    source = (
        Path(__file__).parents[1]
        / "sample_data/demo_video/caremin_demo_actual_worked_export.xlsx"
    )
    valid = (source.name, source.read_bytes())
    failed_id = jobs.start_job(
        app,
        facility.id,
        [valid, ("broken.pdf", b"not a PDF")],
        storage,
        organization_id=facility.organization_id,
    )
    jobs.run_job(failed_id)
    db.session.expire_all()
    assert db.session.get(ImportJob, failed_id).status == "failed"
    assert Shift.query.count() == 0
    assert ImportReceipt.query.count() == 0
    assert storage.get(f"imports/job-{failed_id}/{source.name}") == valid[1]
    good_id = jobs.start_job(
        app, facility.id, [valid], storage, organization_id=facility.organization_id
    )
    jobs.run_job(good_id)
    db.session.expire_all()
    job = db.session.get(ImportJob, good_id)
    assert job.status == "review"
    assert Shift.query.count() == 0
    assert ImportReceipt.query.count() == 0
    assert job.as_dict()["review"]["impact"]["shifts_imported"] > 0
    job.status = "queued"
    job.approved_at = datetime.utcnow()
    db.session.commit()
    jobs.run_job(good_id)
    db.session.expire_all()
    job = db.session.get(ImportJob, good_id)
    assert job.status == "done"
    receipt = db.session.get(ImportReceipt, job.receipt_id)
    assert receipt.shifts_imported == Shift.query.count() > 0
    assert json.loads(receipt.summary_json)[0]["parsers"] == ["xberg/1.3.9"]
    shift = Shift.query.first()
    assert shift.source_file == source.name
    assert shift.source_sheet and shift.source_row > 1
    jobs.run_job(good_id)
    assert ImportReceipt.query.count() == 1


def test_duplicate_names_rejected_before_storage(pg_app):
    app, facility, storage = pg_app
    with pytest.raises(FileReadError, match="distinct filename"):
        jobs.start_job(
            app, facility.id, [("a.csv", b"one"), ("folder/a.csv", b"two")], storage
        )
    assert ImportJob.query.count() == 0


def test_imports_for_other_organisations_do_not_wait_on_a_process_lock(pg_app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    app, facility, storage = pg_app
    other_org = Organization(name="Another synthetic provider")
    db.session.add(other_org)
    db.session.flush()
    other = Facility(organization_id=other_org.id, name="Another home")
    db.session.add(other)
    db.session.commit()
    source = Path(__file__).parents[1] / "sample_data/demo_video/caremin_demo_actual_worked_export.xlsx"
    inputs = [(source.name, source.read_bytes())]
    first = jobs.start_job(app, facility.id, inputs, storage, organization_id=facility.organization_id)
    second = jobs.start_job(app, other.id, inputs, storage, organization_id=other.organization_id)
    started, release, second_done = Event(), Event(), Event()
    process = jobs._process

    def controlled(job, review=None):
        if job.id == first:
            started.set()
            assert release.wait(20), "Test did not release the first facility import"
        return process(job, review=review)

    def run(job_id):
        try:
            jobs._run_in_app(app, job_id)
        finally:
            if job_id == second:
                second_done.set()

    monkeypatch.setattr(jobs, "_process", controlled)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_run = executor.submit(run, first)
        try:
            assert started.wait(5)
            second_run = executor.submit(run, second)
            assert second_done.wait(15), "Another organisation was blocked by the first import"
            second_run.result()
        finally:
            release.set()
        first_run.result()
    db.session.expire_all()
    assert db.session.get(ImportJob, first).status == "review"
    assert db.session.get(ImportJob, second).status == "review"
    assert Shift.query.count() == ImportReceipt.query.count() == 0


def test_missing_worker_fails_readiness_and_retains_the_source(pg_app, monkeypatch):
    app, facility, storage = pg_app
    monkeypatch.delenv("WORKER_SECRET")
    job_id = jobs.start_job(
        app,
        facility.id,
        [("source.csv", b"retained original")],
        storage,
        organization_id=facility.organization_id,
    )
    job = db.session.get(ImportJob, job_id)
    assert job.status == "failed"
    assert storage.get(f"{job.storage_prefix}/source.csv") == b"retained original"
    response = app.test_client().get("/healthz")
    assert response.status_code == 503
    assert response.json["worker_configured"] is False


def test_additive_provenance_migration_keeps_rows(pg_app):
    from sqlalchemy import text, inspect

    app, facility, _ = pg_app
    # Simulate a pre-split schema while retaining the facility row.
    for table in ("shifts", "resident_days", "care_episodes"):
        for column in ("source_file", "source_sheet", "source_page"):
            db.session.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    db.session.commit()
    added = init_db()
    assert len(added) == 9
    assert db.session.get(Facility, facility.id).name == "Test home"
    assert "source_sheet" in {
        c["name"] for c in inspect(db.engine).get_columns("shifts")
    }


def test_additive_duration_migration_keeps_existing_facility(pg_app):
    from sqlalchemy import text, inspect

    _, facility, _ = pg_app
    for column in ("duration_minutes", "coverage_known"):
        db.session.execute(text(f"ALTER TABLE shifts DROP COLUMN {column}"))
    db.session.commit()
    assert set(init_db()) == {"shifts.duration_minutes", "shifts.coverage_known"}
    assert db.session.get(Facility, facility.id).name == "Test home"
    assert {"duration_minutes", "coverage_known"} <= {
        c["name"] for c in inspect(db.engine).get_columns("shifts")
    }


def test_additive_target_history_migration_keeps_legacy_values_unapproved(pg_app):
    from sqlalchemy import text, inspect
    from carelog.models import FacilityTarget

    _, facility, _ = pg_app
    db.session.execute(text("DROP TABLE facility_targets"))
    db.session.commit()
    init_db()
    assert "facility_targets" in inspect(db.engine).get_table_names()
    assert db.session.get(Facility, facility.id).ancc_target == 215
    assert FacilityTarget.query.count() == 0


def test_additive_review_migration_preserves_jobs(pg_app):
    from sqlalchemy import text

    _, facility, _ = pg_app
    db.session.add(
        ImportJob(
            id="retained-job",
            facility_id=facility.id,
            storage_prefix="imports/job-retained",
        )
    )
    db.session.commit()
    for column in ("review_json", "approved_at", "reviewed_by_user_id"):
        db.session.execute(text(f"ALTER TABLE import_jobs DROP COLUMN {column}"))
    db.session.execute(
        text("ALTER TABLE import_receipts DROP COLUMN original_evidence_type")
    )
    db.session.commit()
    assert set(init_db()) == {
        "import_jobs.review_json",
        "import_jobs.approved_at",
        "import_jobs.reviewed_by_user_id",
        "import_receipts.original_evidence_type",
    }
    assert (
        db.session.get(ImportJob, "retained-job").storage_prefix
        == "imports/job-retained"
    )


def test_review_cannot_commit_after_facility_evidence_changes(pg_app):
    from carelog.models import Resident

    app, facility, storage = pg_app
    source = (
        Path(__file__).parents[1]
        / "sample_data/demo_video/caremin_demo_actual_worked_export.xlsx"
    )
    job_id = jobs.start_job(
        app,
        facility.id,
        [(source.name, source.read_bytes())],
        storage,
        organization_id=facility.organization_id,
    )
    jobs.run_job(job_id)
    job = db.session.get(ImportJob, job_id)
    assert job.status == "review"
    db.session.add(
        Resident(facility_id=facility.id, resident_id="NEW", name="New resident")
    )
    job.status, job.approved_at = "queued", datetime.utcnow()
    db.session.commit()
    jobs.run_job(job_id)
    db.session.expire_all()
    job = db.session.get(ImportJob, job_id)
    assert job.status == "failed"
    assert "changed since" in job.error
    assert Shift.query.count() == ImportReceipt.query.count() == 0
    assert storage.get(f"imports/job-{job_id}/{source.name}") == source.read_bytes()


def test_period_queries_stay_bounded_as_more_dates_are_added(pg_app):
    from time import perf_counter
    from sqlalchemy import event, inspect, text
    from carelog import auth
    from carelog.models import FacilityTarget, ResidentDay, Staff

    app, facility, _ = pg_app
    user = auth.create_user(
        organization_id=facility.organization_id,
        email="performance@example.test",
        name="Manager",
        role="administrator",
        password="TestPassword123!",
        must_change_password=False,
    )
    member = Staff(
        facility_id=facility.id,
        staff_id="RN1",
        name="Nurse",
        role="RN",
        source_role="RN",
        eligibility_status="approved",
        registration_number="TEST-RN",
        registration_expiry=date(2027, 1, 1),
    )
    db.session.add_all(
        [
            member,
            FacilityTarget(
                facility_id=facility.id,
                quarter_start=date(2026, 7, 1),
                ancc_target=215,
                rn_target=44,
                source="Synthetic notice",
            ),
        ]
    )
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"], session["auth_version"] = user.id, 1
        session["last_activity_at"] = _time.time()
    measurements = []
    for start, stop in [(0, 31), (31, 61)]:
        for offset in range(start, stop):
            day = date(2026, 7, 1) + timedelta(days=offset)
            db.session.add(
                ResidentDay(
                    facility_id=facility.id,
                    resident_id="R1",
                    date=day,
                    occupied=True,
                    service_type="permanent",
                )
            )
            db.session.add(
                Shift(
                    facility_id=facility.id,
                    staff_id=member.id,
                    date=day,
                    start_time=time(8),
                    end_time=time(16),
                    evidence_type="worked",
                )
            )
        db.session.commit()
        queries = []

        def count(*args):
            queries.append(1)

        event.listen(db.engine, "before_cursor_execute", count)
        started = perf_counter()
        try:
            assert client.get("/api/dashboard").status_code == 200
        finally:
            elapsed = perf_counter() - started
            event.remove(db.engine, "before_cursor_execute", count)
        measurements.append((len(queries), elapsed))
    print(
        f"PostgreSQL dashboard: 31 days {measurements[0]}; 61 days {measurements[1]} (queries, seconds)"
    )
    assert measurements[0][0] <= 120
    assert measurements[1][0] <= measurements[0][0] + 3
    for name in ("ix_shift_facility_stream_date", "ix_resident_day_facility_date"):
        db.session.execute(text(f"DROP INDEX {name}"))
    db.session.commit()
    init_db()
    assert "ix_shift_facility_stream_date" in {
        i["name"] for i in inspect(db.engine).get_indexes("shifts")
    }
    assert "ix_resident_day_facility_date" in {
        i["name"] for i in inspect(db.engine).get_indexes("resident_days")
    }


def test_additive_staff_review_metadata_preserves_credentials(pg_app):
    from sqlalchemy import text
    from carelog.models import Staff

    app, facility, storage = pg_app
    member = Staff(facility_id=facility.id, staff_id="RN1", name="Synthetic Nurse", role="RN",
                   eligibility_status="pending", registration_number="NMW1", registration_expiry=date(2027, 5, 31))
    db.session.add(member)
    db.session.commit()
    member_id = member.id
    for column in ("reviewed_at", "reviewed_by_user_id", "identity_aliases_json"):
        db.session.execute(text(f'ALTER TABLE staff DROP COLUMN "{column}"'))
    db.session.commit()
    assert set(init_db()) == {"staff.reviewed_at", "staff.reviewed_by_user_id", "staff.identity_aliases_json"}
    db.session.expire_all()
    retained = db.session.get(Staff, member_id)
    assert retained.registration_expiry == date(2027, 5, 31)
    assert retained.registration_number == "NMW1" and retained.eligibility_status == "pending"
    assert retained.reviewed_at is None
