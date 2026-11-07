"""Closed quarters: what was reported stays reproducible.

Live figures move when evidence is corrected, which is right for monitoring but
not for a statement already given to a board or the Department. Closing keeps
the figures, the issued PDF and the source hashes, and later shows any drift.
"""

import hashlib
import json
import uuid
from datetime import datetime

from flask import current_app

from carelog.domain.clock import facility_today
from carelog.domain.compliance import CALC_VERSION, quarterly_tests
from carelog.domain.reports import (
    _fy_label,
    build_quarterly_pdf,
    financial_quarter_bounds,
)
from carelog.models import ImportReceipt, QuarterClosure, db
from carelog.storage import build_storage


def _figures(facility, start, end) -> dict:
    result = quarterly_tests(facility, start, end)
    totals = result["totals"]
    return {
        "provisional": result["provisional"],
        "limitations": result["limitations"],
        "bed_days": float(totals["bed_days"]),
        "total_minutes": float(totals["total_minutes"]),
        "rn_minutes": float(totals["rn_minutes"]),
        "rn_en_minutes": float(totals["rn_en_minutes"]),
        "tests": [
            {key: test[key] for key in ("key", "label", "achieved", "target", "passed")}
            for test in result["tests"]
        ],
    }


def _sources(facility, start, end) -> list[dict]:
    sources = []
    receipts = ImportReceipt.query.filter(
        ImportReceipt.facility_id == facility.id,
        ImportReceipt.first_shift_date <= end,
        ImportReceipt.last_shift_date >= start,
    ).order_by(ImportReceipt.id)
    for receipt in receipts:
        for outcome in json.loads(receipt.summary_json or "[]"):
            sources.append(
                {
                    "receipt_id": receipt.id,
                    "filename": outcome.get("filename"),
                    "sha256": outcome.get("sha256"),
                }
            )
    return sources


def close_quarter(facility, year, quarter, actor, basis) -> QuarterClosure:
    start, end = financial_quarter_bounds(year, quarter)
    if end >= facility_today(facility):
        raise ValueError("A quarter can be closed once it has ended.")
    pdf = build_quarterly_pdf(facility, year, quarter)
    key = f"closures/{facility.id}/{year}-Q{quarter}-{uuid.uuid4().hex[:12]}.pdf"
    build_storage(current_app.config["UPLOADS_DIR"]).put(key, pdf, "application/pdf")
    figures = _figures(facility, start, end)
    closure = QuarterClosure(
        organization_id=facility.organization_id,
        facility_id=facility.id,
        year=year,
        quarter=quarter,
        closed_at=datetime.utcnow(),
        closed_by_user_id=actor.id,
        closed_by=actor.email,
        basis=basis,
        calc_version=CALC_VERSION,
        provisional=figures["provisional"],
        figures_json=json.dumps(figures, default=str),
        sources_json=json.dumps(_sources(facility, start, end)),
        pdf_key=key,
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
    )
    db.session.add(closure)
    return closure


def closure_statuses(facility) -> list[dict]:
    """The latest closure of each quarter, and whether live figures have moved."""
    latest = {}
    for closure in QuarterClosure.query.filter_by(facility_id=facility.id).order_by(
        QuarterClosure.closed_at
    ):
        latest[(closure.year, closure.quarter)] = closure
    statuses = []
    for (year, quarter), closure in sorted(latest.items(), reverse=True):
        start, end = financial_quarter_bounds(year, quarter)
        closed = json.loads(closure.figures_json)
        live = json.loads(json.dumps(_figures(facility, start, end), default=str))
        statuses.append(
            {
                "id": closure.id,
                "label": _fy_label(year, quarter),
                "closed_at": closure.closed_at,
                "closed_by": closure.closed_by,
                "basis": closure.basis,
                "calc_version": closure.calc_version,
                "provisional": closure.provisional,
                "tests": closed["tests"],
                "source_files": len(json.loads(closure.sources_json)),
                "pdf_sha256": closure.pdf_sha256,
                "moved": closed["tests"] != live["tests"]
                or closed["bed_days"] != live["bed_days"]
                or closed["total_minutes"] != live["total_minutes"],
                "live_tests": live["tests"],
            }
        )
    return statuses
