from datetime import datetime, timedelta

from flask import (
    Response,
    flash,
    request,
    url_for,
)

from carelog.api import bp
from carelog.api.monitoring import _latest_data_date
from carelog.api.responses import navigate, resource
from carelog.auth import (
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
from carelog.services.workspace import current_facility


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
    )


def _export_range(facility):
    def _parse(name, fallback):
        try:
            return datetime.strptime(request.args.get(name, ""), "%Y-%m-%d").date()
        except ValueError:
            return fallback

    latest = _latest_data_date(facility)
    return _parse("start", latest - timedelta(days=89)), _parse("end", latest)


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
        if quarter not in (1, 2, 3, 4):
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
