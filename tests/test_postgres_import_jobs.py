"""Integration checks against a disposable local PostgreSQL database only."""

import json
import os
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
