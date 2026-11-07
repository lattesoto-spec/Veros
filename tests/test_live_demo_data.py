"""Exercise the exact two-file upload inputs through the real mapping and reports."""
import time

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from carelog.domain.compliance import quarterly_tests, rn_coverage
from carelog.domain.reports import build_quarterly_pdf
from carelog.ingestion.mapping import validate_results
from carelog.ingestion.pipeline import ingest_file
from carelog.ingestion.presets import matching_spec
from carelog.ingestion.reader import read_upload
from carelog.models import db


ROOT = Path(__file__).resolve().parents[1] / "sample_data/live_demo"
PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 11, 2)


@pytest.fixture(scope="module", autouse=True)
def demo_files(tmp_path_factory):
    """Generate isolated sources so a clean checkout needs no local workbooks."""
    from sample_data.live_demo._build_demo import build

    global ROOT
    ROOT = tmp_path_factory.mktemp("live_demo")
    build(ROOT)


@pytest.mark.parametrize(
    ("slug", "expected_achieved", "expected_passes"),
    [
        ("01_banksia", [213.9, 42.8, 64.2], [False, True, True]),
        ("02_wattle", [224.7, 42.9, 64.3], [True, True, True]),
        ("03_waratah", [224.7, 32.1, 42.9], [True, False, False]),
    ],
)
def test_two_file_live_demo_import_and_report(app, facility, slug, expected_achieved, expected_passes):
    files = [ROOT / slug / "resident_census.xlsx", ROOT / slug / "actual_worked_time.xlsx"]
    assert all(path.is_file() for path in files)
    assert sum(path.stat().st_size for path in files) < 4_000_000
    with app.app_context():
        for path in files:
            contents = path.read_bytes()
            sheets = read_upload(path.name, contents)
            assert matching_spec(sheets) is not None
            outcome = ingest_file(facility, path.name, contents)
            assert not outcome.row_errors
            assert not outcome.warnings
            assert not validate_results(outcome.results)
            assert all(result.rows_filtered == 0 for result in outcome.results) or slug == "01_banksia"
        db.session.commit()

        result = quarterly_tests(facility, PERIOD_START, PERIOD_END)
        assert result["totals"]["bed_days"] == 42 * 33
        assert result["target_reconciliation"]["matched"] is True
        assert [test["achieved"] for test in result["tests"]] == expected_achieved
        assert [test["on_track"] for test in result["tests"]] == expected_passes
        pdf = build_quarterly_pdf(facility, 2026, 2)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 5000
        if slug == "01_banksia":
            assert any(gap["reportable"] for gap in rn_coverage(facility.id, PERIOD_END)["gap_intervals"])


def test_demo_api_from_organisation_to_pdf(monkeypatch, tmp_path):
    """Use the same API actions as the UI, including the two-file job receipt."""
    from carelog import auth
    from carelog.app import create_app
    from carelog.ingestion import jobs
    from carelog.models import Facility, ImportJob, ImportReceipt, Organization

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo_test.sqlite'}")
    monkeypatch.setenv("AUTO_INIT_DB", "1")
    monkeypatch.setenv("IMPORT_WORKER", "invoke")
    monkeypatch.setenv("WORKER_SECRET", "test-only-worker-secret")
    monkeypatch.setattr(jobs, "_dispatch_http", lambda job_id: None)
    # SQLite cannot write the job-progress session while its care transaction
    # holds a write lock. Production's PostgreSQL path retains that behavior.
    monkeypatch.setattr(jobs, "_progress", lambda job_id, files: None)
    app = create_app({"TESTING": True, "UPLOADS_DIR": str(tmp_path / "uploads")})
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        platform = Organization(name="Demo Platform")
        db.session.add(platform)
        db.session.flush()
        owner = auth.create_user(
            organization_id=platform.id, email="owner@example.test", name="Owner",
            role="administrator", password="Password123!", is_superuser=True,
            must_change_password=False,
        )
        db.session.commit()
        owner_id = owner.id

    client = app.test_client()
    with client.session_transaction() as state:
        state["user_id"] = owner_id
        state["auth_version"] = 1
        state["last_activity_at"] = time.time()
    created = client.post("/api/platform/organizations/create", data={
        "name": "Banksia Demo Organisation", "admin_name": "Demo Admin",
        "admin_email": "demo@example.test", "password": "Temporary123!",
    })
    assert created.status_code == 200
    with app.app_context():
        org = Organization.query.filter_by(name="Banksia Demo Organisation").one()
        org_id = org.id
    assert client.post("/api/platform/act", data={"organization_id": org_id}).status_code == 200
    made_facility = client.post("/api/facilities/create", data={
        "name": "Banksia Grove Residential Care", "ancc_target": "215", "rn_target": "44",
        "target_quarter": "2026-10-01", "target_source": "Synthetic provider target notice",
    })
    assert made_facility.status_code == 200
    with app.app_context():
        home = Facility.query.filter_by(organization_id=org_id, name="Banksia Grove Residential Care").one()
        home_id = home.id

    folder = ROOT / "01_banksia"
    response = client.post("/api/import", data={
        "facility_id": str(home_id),
        "data_files": [
            (BytesIO((folder / "resident_census.xlsx").read_bytes()), "resident_census.xlsx"),
            (BytesIO((folder / "actual_worked_time.xlsx").read_bytes()), "actual_worked_time.xlsx"),
        ],
    }, content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    job_id = response.json["navigate"].rsplit("/", 1)[-1]
    with app.app_context():
        jobs.run_job(job_id)
        job = db.session.get(ImportJob, job_id)
        assert job.status == "review", job.error
        assert ImportReceipt.query.count() == 0
    assert client.post(f"/api/import/{job_id}/confirm").status_code == 200
    with app.app_context():
        jobs.run_job(job_id)
        job = db.session.get(ImportJob, job_id)
        assert job.status == "done", job.error
        receipt = db.session.get(ImportReceipt, job.receipt_id)
        assert receipt.shifts_imported > 0
        assert receipt.resident_days_imported == 42 * (92 + 33)
    assert client.get(f"/api/import/status/{job_id}").json["data"]["job"]["status"] == "done"
    assert client.get("/api/reports").json["view"] == "reports"
    pdf = client.get("/api/report/quarter.pdf?q=2026-Q2")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b"%PDF")
