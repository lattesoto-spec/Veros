

import hashlib

from flask import (
    Response,
    abort,
    flash,
    request,
    url_for,
)

from carelog import auth
from carelog.api import bp
from carelog.api.monitoring import _latest_data_date
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_user,
    require,
)
from carelog.domain import exports as export_builders
from carelog.domain.care_minutes import (
    quarter_bounds,
)
from carelog.domain.compliance import (
    CALC_VERSION,
    evidence_summary,
)
from carelog.domain.reports import available_quarters, build_quarterly_pdf
from carelog.domain.targets import facility_for_period
from carelog.models import AuditLog, QuarterClosure, db
from carelog.services.closure import close_quarter, closure_statuses
from carelog.services.workspace import _storage, current_facility
from carelog.storage import StorageError
from carelog.services.periods import reporting_range


@bp.route("/export/daily.csv")
@require("export_data")
def export_daily_csv():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    start, end = _export_range(facility)
    text = export_builders.daily_csv(facility, start, end)
    return Response(
        text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=care-minutes-daily_{start}_{end}.csv"
        },
    )


@bp.route("/export/summary.xlsx")
@require("export_data")
def export_summary_xlsx():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    start, end = _export_range(facility)
    blob = export_builders.summary_xlsx(facility, start, end)
    return Response(
        blob,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=care-minutes-summary_{start}_{end}.xlsx"
        },
    )


@bp.route("/report/board.pdf")
@require("export_data")
def board_report():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    blob = export_builders.board_pdf(facility, _latest_data_date(facility))
    return Response(
        blob,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=board-care-minutes-report.pdf"
        },
    )


@bp.route("/reports")
@require("export_data")
def reports_view():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    latest = _latest_data_date(facility)
    q_start, _ = quarter_bounds(latest)
    return resource(
        "reports",
        facility=facility_for_period(facility, latest),
        latest=latest,
        quarters=available_quarters(facility.id),
        calc_version=CALC_VERSION,
        evidence=evidence_summary(facility.id, q_start, latest),
        closures=closure_statuses(facility),
    )


@bp.post("/reports/close")
@require("review_eligibility")
def report_close():
    """Keep the figures and statement for a finished quarter as issued."""
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    basis = (form_data().get("basis") or "").strip()
    if not basis:
        return {"error": "Record why this quarter is being closed, such as the submission it supports."}, 400
    try:
        year, quarter = _parse_quarter(form_data().get("q"))
        closure = close_quarter(facility, year, quarter, current_user(), basis)
    except ValueError as error:
        return {"error": str(error) or "Pick a valid quarter."}, 400
    except StorageError:
        return {"error": "The statement could not be retained, so the quarter was not closed. Retry."}, 502
    db.session.flush()
    auth.record(
        "quarter_closed", "quarter_closure", closure.id,
        f"{year}-Q{quarter}; provisional={closure.provisional}; "
        f"statement SHA-256 {closure.pdf_sha256}; basis: {basis}",
    )
    db.session.commit()
    flash("Quarter closed. Its figures and statement are kept as issued.")
    return navigate(url_for("api.reports_view"))


@bp.route("/reports/closure/<int:closure_id>.pdf")
@require("export_data")
def report_closure_pdf(closure_id):
    facility = current_facility()
    closure = (
        QuarterClosure.query.filter_by(id=closure_id, facility_id=facility.id).first()
        if facility
        else None
    )
    if closure is None:
        abort(404)
    try:
        pdf = _storage().get(closure.pdf_key)
    except StorageError:
        return {"error": "The issued statement could not be retrieved. Retry."}, 502
    if hashlib.sha256(pdf).hexdigest() != closure.pdf_sha256:
        db.session.add(AuditLog(
            organization_id=facility.organization_id, action="evidence_integrity_failed",
            entity="quarter_closure", entity_id=str(closure.id),
            detail="Issued statement no longer matches its recorded SHA-256",
        ))
        db.session.commit()
        return {"error": "The issued statement failed its integrity check. Contact platform support."}, 502
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=closed-statement_{closure.year}-Q{closure.quarter}.pdf"
        },
    )


def _parse_quarter(value):
    year_text, quarter_text = (value or "").strip().split("-Q")
    year, quarter = int(year_text), int(quarter_text)
    if quarter not in (1, 2, 3, 4) or not 2000 <= year <= 2100:
        raise ValueError("Pick a valid quarter.")
    return year, quarter


def _export_range(facility):
    try:
        return reporting_range(request.args, _latest_data_date(facility))
    except ValueError as error:
        abort(400, description=str(error))


@bp.route("/report/quarter.pdf")
@require("export_data")
def quarterly_report():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    q = (request.args.get("q") or "").strip()
    try:
        year_str, q_str = q.split("-Q")
        year = int(year_str)
        quarter = int(q_str)
        if quarter not in (1, 2, 3, 4) or not 2000 <= year <= 2100:
            raise ValueError
    except (ValueError, AttributeError):
        flash("Pick a valid quarter to generate a report.")
        return navigate(url_for("api.reports_view"))

    pdf_bytes = build_quarterly_pdf(facility, year, quarter)
    safe_name = (
        "".join(c if c.isalnum() else "-" for c in facility.name).strip("-").lower()
    )
    filename = f"care-minutes-statement_{safe_name}_{year}-Q{quarter}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
