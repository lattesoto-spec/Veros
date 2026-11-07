from carelog.domain.clock import facility_today
from datetime import date, datetime, timedelta

from flask import (
    abort,
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
from carelog.domain.targets import facility_for_period
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
        .filter(Shift.date <= facility_today(facility))
        .order_by(Shift.date.desc())
        .first()
    )
    return latest.date if latest else facility_today(facility)


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
        facility=facility_for_period(facility, today),
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
        abort(400, description="Choose a valid reporting month using YYYY-MM.")
    if not 2000 <= selected_month.year <= 2100:
        abort(400, description="Choose a reporting month between 2000 and 2100.")
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
        result = rn_coverage(facility.id, day)
        available = day in source_dates and result["coverage_known"]
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
        facility=facility_for_period(facility, today),
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
    result, errors = None, []
    if request.method == "POST" and base:
        try:
            params["rn_shifts_removed"] = int(form_data().get("rn_shifts_removed") or 0)
            params["occupancy_delta"] = int(form_data().get("occupancy_delta") or 0)
            if (
                not 0 <= params["rn_shifts_removed"] <= 20
                or not -50 <= params["occupancy_delta"] <= 50
            ):
                raise ValueError(
                    "Use 0–20 RN shifts and an occupancy change between -50 and 50."
                )
            agency = form_data().get("agency_removed")
            if agency not in (None, "", "on"):
                raise ValueError("Choose an explicit agency-removal assumption.")
            params["agency_removed"] = agency == "on"
            result = forecast_quarter(facility, today, adjustments=params)
        except (TypeError, ValueError) as error:
            errors.append(str(error) or "Scenario inputs must be whole numbers.")
    return resource(
        "scenarios",
        facility=facility_for_period(facility, today),
        today=today,
        base=base,
        params=params,
        result=result,
        errors=errors,
        thresholds={
            row["key"]: row["target"]
            for row in quarterly_tests(facility, quarter_bounds(today)[0], today)[
                "tests"
            ]
        },
    ), 400 if errors else 200


@bp.route("/dashboard")
@require("view_dashboard")
def dashboard():
    facility = current_facility()
    if not facility:
        return navigate(url_for("api.universal_import"))

    latest_worked_shift = (
        Shift.query.filter_by(facility_id=facility.id, evidence_type="worked")
        .filter(Shift.date <= facility_today(facility))
        .order_by(Shift.date.desc())
        .first()
    )
    today = latest_worked_shift.date if latest_worked_shift else facility_today(facility)

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
    from carelog.domain.targets import target_for_quarter

    approved_target = target_for_quarter(facility, today)
    q_gap = round(q_avg["care_per_resident"] - approved_target.ancc_target, 1) if approved_target else None
    q_gap_pct = (
        round((q_gap / approved_target.ancc_target) * 100, 1) if approved_target else None
    )

    from carelog.domain.targets import target_for_quarter

    approved_target = target_for_quarter(facility, today)
    chart_data = {
        "labels": [r["date"].strftime("%d %b") for r in last_14],
        "values": [
            r["care_per_resident"]
            if r["active_residents"] and r["total_minutes"]
            else None
            for r in last_14
        ],
        "target": approved_target.ancc_target if approved_target else None,
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
    ) and coverage["coverage_known"]
    alerts = detect_gaps(facility, today)
    last_receipt = _latest_receipt(facility)
    data_age_days = (
        (facility_today(facility) - last_receipt.imported_at.date()).days if last_receipt else None
    )
    receipts = ImportReceipt.query.filter_by(facility_id=facility.id).all()
    # The dashboard must not make one storage call per receipt. The audit page
    # lists the stored objects; here a recorded retention location is enough.
    with_files = sum(1 for r in receipts if r.source_path)
    audit_readiness = round(with_files / len(receipts) * 100) if receipts else None
    evidence = evidence_summary(facility.id, q_start, today)
    tests = quarterly_tests(facility, q_start, today)
    nurses = Staff.query.filter(
        Staff.facility_id == facility.id, Staff.role.in_(["RN", "EN"])
    ).all()
    credential_issues = sum(
        bool(el.registration_problem(member, today)) for member in nurses
    )
    setup_checks = [
        {
            "label": "Quarterly targets",
            "ready": approved_target is not None,
            "detail": "Record the quarter and provider target notice.",
            "href": "/settings",
            "permission": "manage_facility",
        },
        {
            "label": "Actual-worked hours",
            "ready": latest_worked_shift is not None,
            "detail": "Import approved time-and-attendance records.",
            "href": "/import",
            "permission": "import_data",
        },
        {
            "label": "Resident-day census",
            "ready": evidence["resident_day_ledger_complete"],
            "detail": "Verify every date and resident against the source census.",
            "href": "/audit",
            "permission": "view_audit",
        },
        {
            "label": "Nursing credentials",
            "ready": bool(nurses) and credential_issues == 0,
            "detail": f"{len(nurses)} nursing profiles; {credential_issues} registration issue(s). Import staff identifiers, Ahpra numbers and expiry dates.",
            "href": "/eligibility",
            "permission": "view_audit",
        },
    ]

    return resource(
        "dashboard",
        compliance_pct=pct,
        coverage=coverage,
        coverage_available=coverage_available,
        alerts=alerts,
        data_age_days=data_age_days,
        audit_readiness=audit_readiness,
        facility=facility_for_period(facility, today),
        today=today,
        has_worked_data=latest_worked_shift is not None,
        worked_data_age_days=(facility_today(facility) - today).days
        if latest_worked_shift
        else None,
        setup_checks=setup_checks,
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
        evidence=evidence,
        tests=tests,
        pending_eligibility=Staff.query.filter_by(
            facility_id=facility.id, eligibility_status=el.PENDING
        ).count(),
        calc_version=CALC_VERSION,
    )
