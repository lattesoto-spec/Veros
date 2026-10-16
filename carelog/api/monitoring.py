from datetime import date, datetime, timedelta

from flask import (
    flash,
    request,
    session,
    url_for,
)

from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_user,
    require,
)
from carelog.domain import eligibility as el
from carelog.domain.care_minutes import (
    active_residents_on,
    average,
    daily_stats,
    quarter_bounds,
    range_stats,
)
from carelog.domain.compliance import (
    CALC_VERSION,
    compliance_pct,
    detect_gaps,
    evidence_summary,
    forecast_quarter,
    monthly_breakdown,
    quarterly_tests,
    range_breakdown,
    rn_coverage,
)
from carelog.models import (
    ImportReceipt,
    Shift,
    Staff,
    db,
)
from carelog.services.workspace import (
    _clock_label,
    _latest_receipt,
    _receipt_files,
    current_facility,
)


@bp.route("/")
def index():
    user = current_user()
    if user and user.is_superuser and not session.get("acting_org_id"):
        return navigate(url_for("api.platform_console"))
    facility = current_facility()
    if facility and Shift.query.filter_by(facility_id=facility.id).first():
        return navigate(url_for("api.dashboard"))
    return navigate(url_for("api.universal_import"))


def _latest_data_date(facility):
    latest = (
        Shift.query.filter_by(facility_id=facility.id, evidence_type="worked")
        .order_by(Shift.date.desc())
        .first()
    )
    if latest is None:
        latest = (
            Shift.query.filter_by(facility_id=facility.id)
            .order_by(Shift.date.desc())
            .first()
        )
    return latest.date if latest else date.today()


@bp.route("/compliance")
@require("view_dashboard")
def compliance_view():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    today = _latest_data_date(facility)
    fc = forecast_quarter(facility, today)
    q_start, _ = quarter_bounds(today)
    return resource(
        "compliance",
        facility=facility,
        today=today,
        tests=quarterly_tests(facility, q_start, today),
        forecast=fc,
        alerts=detect_gaps(facility, today),
        coverage=rn_coverage(facility.id, today),
        months=monthly_breakdown(facility.id, fc["q_start"], today) if fc else [],
        week=range_breakdown(facility.id, today - timedelta(days=6), today),
        calc_version=CALC_VERSION,
        evidence=evidence_summary(facility.id, q_start, today),
    )


@bp.route("/rn-coverage")
@require("view_dashboard")
def rn_coverage_view():
    """Operational RN continuity view using the existing coverage engine."""
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    today = _latest_data_date(facility)
    try:
        selected_month = (
            datetime.strptime(
                request.args.get("month", today.strftime("%Y-%m")), "%Y-%m"
            )
            .date()
            .replace(day=1)
        )
    except ValueError:
        selected_month = today.replace(day=1)
    next_month = (
        date(selected_month.year + 1, 1, 1)
        if selected_month.month == 12
        else date(selected_month.year, selected_month.month + 1, 1)
    )
    start = selected_month
    end = next_month - timedelta(days=1)
    source_dates = {
        row[0]
        for row in db.session.query(Shift.date)
        .filter(
            Shift.facility_id == facility.id,
            Shift.evidence_type == "worked",
            Shift.date >= start,
            Shift.date <= end,
        )
        .distinct()
        .all()
    }
    days = []
    reportable_gaps = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        available = day in source_dates
        result = rn_coverage(facility.id, day)
        result["date"] = day
        result["available"] = available
        days.append(result)
        if available:
            source_intervals = [
                f"{_clock_label(segment['start_minute'])} to "
                f"{_clock_label(segment['end_minute'])}"
                for segment in result["timeline"]
            ]
            for gap in result["gap_intervals"]:
                if gap["reportable"]:
                    reportable_gaps.append(
                        {
                            "date": day,
                            "start": _clock_label(gap["start_minute"]),
                            "end": _clock_label(gap["end_minute"]),
                            "minutes": gap["minutes"],
                            "source_intervals": source_intervals,
                        }
                    )
    available = [row for row in days if row["available"]]
    period_pct = (
        round(
            sum(row["covered_minutes"] for row in available)
            / (len(available) * 1440)
            * 100,
            1,
        )
        if available
        else None
    )
    return resource(
        "rn_coverage",
        facility=facility,
        start=start,
        end=end,
        month_value=start.strftime("%Y-%m"),
        days=days,
        available_days=len(available),
        period_pct=period_pct,
        reportable_gaps=reportable_gaps,
        calc_version=CALC_VERSION,
    )


@bp.route("/scenarios", methods=["GET", "POST"])
@require("run_scenarios")
def scenarios():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))
    today = _latest_data_date(facility)
    base = forecast_quarter(facility, today)

    params = {"rn_shifts_removed": 0, "agency_removed": False, "occupancy_delta": 0}
    result = None
    if request.method == "POST" and base:
        try:
            params["rn_shifts_removed"] = max(
                int(form_data().get("rn_shifts_removed") or 0), 0
            )
            params["occupancy_delta"] = int(form_data().get("occupancy_delta") or 0)
        except ValueError:
            flash("Scenario inputs must be whole numbers.")
        params["agency_removed"] = form_data().get("agency_removed") == "on"
        result = forecast_quarter(facility, today, adjustments=params)

    return resource(
        "scenarios",
        facility=facility,
        today=today,
        base=base,
        params=params,
        result=result,
    )


@bp.route("/dashboard")
@require("view_dashboard")
def dashboard():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))

    latest_worked_shift = (
        Shift.query.filter_by(facility_id=facility.id, evidence_type="worked")
        .order_by(Shift.date.desc())
        .first()
    )
    latest_shift = latest_worked_shift
    if latest_shift is None:
        latest_shift = (
            Shift.query.filter_by(facility_id=facility.id)
            .order_by(Shift.date.desc())
            .first()
        )
    today = latest_shift.date if latest_shift else date.today()

    today_stats = daily_stats(
        facility.id, today, facility.ancc_target, facility.rn_target
    )

    last_14_start = today - timedelta(days=13)
    last_14 = range_stats(
        facility.id, last_14_start, today, facility.ancc_target, facility.rn_target
    )

    q_start, q_end = quarter_bounds(today)
    q_rows = range_stats(
        facility.id, q_start, q_end, facility.ancc_target, facility.rn_target
    )
    q_avg = average(q_rows)
    q_gap = round(q_avg["care_per_resident"] - facility.ancc_target, 1)
    q_gap_pct = (
        round((q_gap / facility.ancc_target) * 100, 1) if facility.ancc_target else 0
    )

    chart_data = {
        "labels": [r["date"].strftime("%d %b") for r in last_14],
        "values": [
            r["care_per_resident"]
            if r["active_residents"] and r["total_minutes"]
            else None
            for r in last_14
        ],
        "target": facility.ancc_target,
    }

    resident_count = active_residents_on(facility.id, today)

    # Phase 6 additions
    pct = compliance_pct(facility, today)
    coverage = rn_coverage(facility.id, today)
    coverage_available = (
        Shift.query.filter_by(
            facility_id=facility.id, evidence_type="worked", date=today
        ).first()
        is not None
    )
    alerts = detect_gaps(facility, today)
    last_receipt = _latest_receipt(facility)
    data_age_days = (
        (date.today() - last_receipt.imported_at.date()).days if last_receipt else None
    )
    receipts = ImportReceipt.query.filter_by(facility_id=facility.id).all()
    with_files = sum(1 for r in receipts if _receipt_files(r))
    audit_readiness = round(with_files / len(receipts) * 100) if receipts else None

    return resource(
        "dashboard",
        compliance_pct=pct,
        coverage=coverage,
        coverage_available=coverage_available,
        alerts=alerts,
        data_age_days=data_age_days,
        audit_readiness=audit_readiness,
        facility=facility,
        today=today,
        today_stats=today_stats,
        last_14=list(reversed(last_14)),
        chart_data=chart_data,
        q_avg=q_avg,
        q_gap=q_gap,
        q_gap_pct=q_gap_pct,
        q_start=q_start,
        q_end=q_end,
        resident_count=resident_count,
        last_receipt=_latest_receipt(facility),
        evidence=evidence_summary(facility.id, q_start, today),
        tests=quarterly_tests(facility, q_start, today),
        pending_eligibility=Staff.query.filter_by(
            facility_id=facility.id, eligibility_status=el.PENDING
        ).count(),
        calc_version=CALC_VERSION,
    )
