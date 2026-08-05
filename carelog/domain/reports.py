"""Quarterly Care Minutes Performance Statement PDF.

Maps the system's shift and resident data onto the structure of the Department of
Health & Aged Care's Care Minutes Performance Statement (2025-26) template:

  - Labour worked hours – direct care, by role (RN, EN, PCW/AIN)
  - Occupied bed days (sum of active residents per day in the quarter)
  - Monthly 24/7 RN coverage percentage (with <30 min gaps ignored)
  - Direct care minutes (worked) per occupied bed day, by role and total

Australian financial-year quarters:
  Q1: 1 Jul – 30 Sep
  Q2: 1 Oct – 31 Dec
  Q3: 1 Jan – 31 Mar
  Q4: 1 Apr – 30 Jun
"""
from calendar import monthrange
from datetime import date, timedelta
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func
from carelog.domain import eligibility
from carelog.domain.care_minutes import active_residents_on
from carelog.models import Facility, ImportReceipt, Shift, Staff, db

ROLES = [
    ("RN", "Registered Nurse"),
    ("EN", "Enrolled Nurse"),
    ("PCA", "Personal Care Worker / Assistant in Nursing"),
]


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def financial_quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    """Return (start, end) calendar dates for an AU FY quarter.

    `year` is the calendar year of the quarter's *start* month, so FY2025-26 Q3
    (Jan-Mar 2026) is year=2026, quarter=3.
    """
    if quarter == 1:
        return date(year, 7, 1), date(year, 9, 30)
    if quarter == 2:
        return date(year, 10, 1), date(year, 12, 31)
    if quarter == 3:
        return date(year, 1, 1), date(year, 3, 31)
    if quarter == 4:
        return date(year, 4, 1), date(year, 6, 30)
    raise ValueError(f"quarter must be 1..4, got {quarter}")


def _fy_label(year: int, quarter: int) -> str:
    if quarter in (1, 2):
        return f"FY{year}-{(year + 1) % 100:02d} Q{quarter}"
    return f"FY{year - 1}-{year % 100:02d} Q{quarter}"


def available_quarters(facility_id: int) -> list[dict]:
    """All FY quarters that contain at least one shift for the facility."""
    rows = db.session.query(Shift.date).filter(Shift.facility_id == facility_id).all()
    if not rows:
        return []
    dates = {r[0] for r in rows}
    out = {}
    for d in dates:
        year, quarter = _quarter_of(d)
        key = (year, quarter)
        if key in out:
            continue
        start, end = financial_quarter_bounds(year, quarter)
        out[key] = {
            "year": year,
            "quarter": quarter,
            "start": start,
            "end": end,
            "label": _fy_label(year, quarter),
        }
    return sorted(out.values(), key=lambda q: (q["start"]), reverse=True)


def _quarter_of(d: date) -> tuple[int, int]:
    m = d.month
    if 7 <= m <= 9:
        return d.year, 1
    if 10 <= m <= 12:
        return d.year, 2
    if 1 <= m <= 3:
        return d.year, 3
    return d.year, 4


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_quarter_stats(facility_id: int, year: int, quarter: int) -> dict:
    start, end = financial_quarter_bounds(year, quarter)
    from carelog.domain.compliance import (
        evidence_summary,
        per_bed_day,
        quarter_totals,
        quarterly_tests,
        rn_coverage,
        worked_minutes as shift_worked_minutes,
    )

    facility = db.session.get(Facility, facility_id)
    latest_worked = db.session.query(func.max(Shift.date)).filter(
        Shift.facility_id == facility_id,
        Shift.evidence_type == "worked",
        Shift.date >= start,
        Shift.date <= end,
    ).scalar()
    # A review draft for an open quarter must not divide worked minutes to
    # date by future occupied-bed days. Calculate the figures through the last
    # actual-worked date, while the readiness checks below continue to assess
    # the complete statement period and prevent premature submission.
    calculation_end = min(latest_worked, end) if latest_worked else start
    totals = quarter_totals(facility_id, start, calculation_end)
    tests = quarterly_tests(facility, start, calculation_end)
    evidence = evidence_summary(facility_id, start, end)

    labour = {
        role: {"employee_minutes": 0, "agency_minutes": 0,
               "employee_cost": 0.0, "agency_cost": 0.0,
               "missing_cost_rows": 0}
        for role, _ in ROLES
    }
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.is_direct_care == True,  # noqa: E712
            Shift.evidence_type == "worked",
            Shift.date >= start,
            Shift.date <= end,
        )
        .all()
    )
    for s, st in shifts:
        if not eligibility.counts_toward_care(st, s.date):
            continue
        role = eligibility.bucket_for(st.role)
        if role not in labour:
            continue
        stream = "agency" if s.is_agency else "employee"
        labour[role][f"{stream}_minutes"] += shift_worked_minutes(s)
        if s.labour_cost is None:
            labour[role]["missing_cost_rows"] += 1
        else:
            labour[role][f"{stream}_cost"] += float(s.labour_cost)

    worked_minutes = {
        "RN": totals["rn_minutes"], "EN": totals["en_minutes"], "PCA": totals["pca_minutes"]
    }
    obd = totals["bed_days"]
    # Monthly 24/7 RN coverage %.
    monthly_rn = []
    for y, m in _months_in_range(start, end):
        month_first = max(start, date(y, m, 1))
        month_last = min(end, date(y, m, monthrange(y, m)[1]))
        data_last = (
            min(month_last, latest_worked)
            if latest_worked and latest_worked >= month_first else None
        )
        covered = 0
        days_count = 0
        d = month_first
        while data_last and d <= data_last:
            covered += rn_coverage(facility_id, d)["covered_minutes"]
            days_count += 1
            d += timedelta(days=1)
        pct = covered / (days_count * 1440) * 100 if days_count else None
        basis = (
            "Complete month" if data_last == month_last
            else f"Through {data_last.strftime('%d %b')}" if data_last
            else "No actual-worked data"
        )
        monthly_rn.append({
            "year": y,
            "month": m,
            "label": date(y, m, 1).strftime("%B %Y"),
            "coverage_pct": round(pct, 2) if pct is not None else None,
            "basis": basis,
        })

    # Care minutes per occupied bed day = worked_hours / OBD * 60 = worked_minutes / OBD.
    per_obd = {}
    for role, _ in ROLES:
        per_obd[role] = per_bed_day(worked_minutes[role], obd)
    total_per_obd = round(sum(per_obd.values()), 1)

    worked_hours = {role: round(m / 60, 1) for role, m in worked_minutes.items()}
    blockers = []
    if latest_worked is None or latest_worked < end:
        blockers.append(
            f"Actual-worked data ends {latest_worked.isoformat() if latest_worked else 'before the period'}; "
            f"the statement period ends {end.isoformat()}."
        )
    if not obd:
        blockers.append("No occupied bed days are available for the quarter.")
    if not worked_minutes or sum(worked_minutes.values()) == 0:
        blockers.append("No eligible actual-worked direct-care minutes are available.")
    if totals["excluded_minutes"]:
        blockers.append(f"{totals['excluded_minutes']} worked minutes are withheld by eligibility checks.")
    if evidence["unverified_minutes"]:
        blockers.append(f"{evidence['unverified_minutes']} minutes remain unverified.")
    if not evidence["resident_day_ledger_complete"]:
        blockers.append(
            f"Resident-day ledger covers {evidence['resident_day_dates']} of "
            f"{evidence['resident_day_expected_dates']} day(s) in the period."
        )
    if evidence["resident_day_unverified_rows"]:
        blockers.append(
            f"Funding/service type is missing for {evidence['resident_day_unverified_rows']} "
            "resident-day row(s)."
        )
    missing_cost_rows = sum(x["missing_cost_rows"] for x in labour.values())
    if missing_cost_rows:
        blockers.append(f"Labour cost is missing for {missing_cost_rows} eligible worked shift row(s).")
    target_check = tests["target_reconciliation"]
    if not target_check["complete"]:
        blockers.append(
            f"The statutory target reference-period ledger covers {target_check['ledger_dates']} "
            f"of {target_check['expected_dates']} dates and has "
            f"{target_check['missing_classification_days']} unrecognised classification day(s)."
        )
    elif not target_check["matched"]:
        blockers.append(
            f"Configured targets {facility.ancc_target}/{facility.rn_target} do not match the "
            f"classification-derived values {target_check['derived_care_target']}/"
            f"{target_check['derived_rn_target']}."
        )
    receipts = ImportReceipt.query.filter(
        ImportReceipt.facility_id == facility_id,
        ImportReceipt.imported_at != None,  # noqa: E711
    ).count()
    if receipts == 0:
        blockers.append("No retained import receipt supports this period.")

    return {
        "facility_id": facility_id,
        "year": year,
        "quarter": quarter,
        "label": _fy_label(year, quarter),
        "start": start,
        "end": end,
        "calculation_end": calculation_end,
        "calculation_is_partial": calculation_end < end,
        "worked_minutes": worked_minutes,
        "worked_hours": worked_hours,
        "occupied_bed_days": obd,
        "monthly_rn_coverage": monthly_rn,
        "labour": labour,
        "tests": tests,
        "evidence": evidence,
        "readiness": {"ready": not blockers, "blockers": blockers},
        "care_minutes_per_obd": per_obd,
        "total_care_minutes_per_obd": total_per_obd,
    }


def _active_residents_on(facility_id: int, day: date) -> int:
    return active_residents_on(facility_id, day)


def _months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def build_quarterly_pdf(facility, year: int, quarter: int) -> bytes:
    stats = compute_quarter_stats(facility.id, year, quarter)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Care Minutes Performance Statement - {facility.name} - {stats['label']}",
        author="CareMin",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    muted = ParagraphStyle("muted", parent=body, textColor=colors.HexColor("#666"))

    story = []
    story.append(Paragraph("Care Minutes Performance Statement", h1))
    story.append(Paragraph(
        f"{facility.name} | {stats['label']} | "
        f"{stats['start'].strftime('%d %b %Y')} - {stats['end'].strftime('%d %b %Y')}",
        body,
    ))
    if stats["readiness"]["ready"]:
        story.append(Paragraph(
            "RECONCILED DRAFT - all automated evidence checks passed. Figures derive only from "
            "eligible actual-worked direct-care rows and the occupied-bed-day ledger. Provider "
            "review and any required independent assurance remain external approval steps.", muted))
    else:
        story.append(Paragraph(
            "NOT READY FOR SUBMISSION - automated evidence checks found unresolved items. The "
            "figures below remain a review draft until every blocker is resolved.",
            ParagraphStyle("warning", parent=body, textColor=colors.HexColor("#a61b1b"),
                           backColor=colors.HexColor("#fdeaea"), borderPadding=6),
        ))
        for i, blocker in enumerate(stats["readiness"]["blockers"], 1):
            story.append(Paragraph(f"{i}. {blocker}", body))
    story.append(Spacer(1, 6))

    story.append(Paragraph("1. Labour worked hours - eligible direct care", h2))
    story.append(_labour_hours_table(stats))
    story.append(Paragraph(
        "Employee and agency hours derive from eligible actual-worked rows after unpaid breaks. "
        "Rostered, unverified and delivered-care episode minutes are not added to these hours.",
        muted,
    ))

    story.append(Paragraph("2. Labour costs - direct care", h2))
    story.append(_labour_costs_table(stats))
    story.append(Paragraph(
        "Costs are included only where the source row supplied an attributable direct-care labour "
        "cost. Missing cost rows appear in the submission blockers above.",
        muted,
    ))

    story.append(Paragraph("3. 24/7 RN coverage - monthly", h2))
    story.append(_rn_coverage_table(stats))
    story.append(Paragraph(
        "Coverage = (minutes in month - RN-on-site gaps of 30 minutes or more) / minutes in "
        "month. Computed from RN shift intervals merged across gaps shorter than 30 minutes.",
        muted,
    ))

    story.append(Paragraph("4. Occupied bed days used in this draft", h2))
    story.append(_obd_table(stats))
    story.append(Paragraph(
        ("Occupied bed days come from a complete imported daily resident ledger."
         if stats["evidence"]["resident_day_ledger_complete"] else
         "The resident-day ledger is partial; uncovered dates use the admission/discharge "
         "fallback. Complete the ledger before treating this statement as submission-ready."),
        muted,
    ))

    through = (
        f" through {stats['calculation_end'].strftime('%d %b %Y')}"
        if stats["calculation_is_partial"] else ""
    )
    story.append(Paragraph(
        f"5. Direct care minutes (worked) per occupied bed day{through}", h2
    ))
    story.append(_care_minutes_per_obd_table(stats, facility))
    story.append(Paragraph(
        "Per-OBD minutes = eligible actual-worked minutes / occupied bed days. The three tests "
        "below are assessed independently and all must pass.",
        muted,
    ))

    test_scope = "Quarter-to-date" if stats["calculation_is_partial"] else "Quarterly"
    story.append(Paragraph(f"6. {test_scope} compliance tests", h2))
    story.append(_compliance_tests_table(stats))
    story.append(Paragraph("7. Evidence reconciliation", h2))
    story.append(_evidence_table(stats))
    story.append(Paragraph(
        "Delivered-care minutes are reconciliation evidence only. They are never added to staffing "
        "worked hours because concurrent and overlapping episodes can double count time.", muted))

    story.append(Paragraph("Variance summary", h2))
    story.append(_variance_paragraph(stats, facility, body))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(18 * mm, 9 * mm, f"CareMin calculation {stats['tests']['calc_version']}")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    pdf = buf.getvalue()
    buf.close()
    return pdf


_HEADER_BG = colors.HexColor("#1f1f1f")
_HEADER_FG = colors.whitesmoke
_ROW_ALT = colors.HexColor("#f5f5f5")


def _base_table_style(header_rows: int = 1) -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), _HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), _HEADER_FG),
        ("FONTNAME", (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [colors.white, _ROW_ALT]),
    ])


def _labour_hours_table(stats: dict) -> Table:
    data = [["Role", "Employee (hrs)", "Agency (hrs)", "Total (hrs)"]]
    for role, label in ROLES:
        row = stats["labour"][role]
        employee = row["employee_minutes"] / 60
        agency = row["agency_minutes"] / 60
        data.append([label, f"{employee:,.1f}", f"{agency:,.1f}", f"{employee + agency:,.1f}"])
    employee_total = sum(x["employee_minutes"] for x in stats["labour"].values()) / 60
    agency_total = sum(x["agency_minutes"] for x in stats["labour"].values()) / 60
    data.append(["Total", f"{employee_total:,.1f}", f"{agency_total:,.1f}",
                 f"{employee_total + agency_total:,.1f}"])
    t = Table(data, colWidths=[80 * mm, 30 * mm, 30 * mm, 30 * mm])
    style = _base_table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaeaea"))
    t.setStyle(style)
    return t


def _labour_costs_table(stats: dict) -> Table:
    data = [["Role", "Employee ($)", "Agency ($)", "Total ($)"]]
    for role, label in ROLES:
        row = stats["labour"][role]
        employee = row["employee_cost"]
        agency = row["agency_cost"]
        data.append([label, f"{employee:,.2f}", f"{agency:,.2f}", f"{employee + agency:,.2f}"])
    employee_total = sum(x["employee_cost"] for x in stats["labour"].values())
    agency_total = sum(x["agency_cost"] for x in stats["labour"].values())
    data.append(["Total", f"{employee_total:,.2f}", f"{agency_total:,.2f}",
                 f"{employee_total + agency_total:,.2f}"])
    t = Table(data, colWidths=[80 * mm, 30 * mm, 30 * mm, 30 * mm])
    t.setStyle(_base_table_style())
    return t


def _rn_coverage_table(stats: dict) -> Table:
    data = [["Month", "RN coverage (%)", "Basis"]]
    for row in stats["monthly_rn_coverage"]:
        pct = f"{row['coverage_pct']:.2f}%" if row["coverage_pct"] is not None else "No worked data"
        data.append([row["label"], pct, row["basis"]])
    t = Table(data, colWidths=[60 * mm, 45 * mm, 65 * mm])
    t.setStyle(_base_table_style())
    return t


def _obd_table(stats: dict) -> Table:
    data = [
        ["Period", "Occupied bed days"],
        [
            f"{stats['start'].strftime('%d %b %Y')} - "
            f"{stats['calculation_end'].strftime('%d %b %Y')}",
            f"{stats['occupied_bed_days']:,}",
        ],
    ]
    t = Table(data, colWidths=[110 * mm, 60 * mm])
    t.setStyle(_base_table_style())
    return t


def _care_minutes_per_obd_table(stats: dict, facility) -> Table:
    data = [["Role", "Care minutes per OBD", "Target"]]
    target_map = {"RN": facility.rn_target * 0.9, "EN": "-", "PCA": "-"}
    for role, label in ROLES:
        target = target_map.get(role, "—")
        target_str = f"{target:.1f}" if isinstance(target, (int, float)) else target
        data.append([label, f"{stats['care_minutes_per_obd'][role]:.1f}", target_str])
    data.append([
        "Total", f"{stats['total_care_minutes_per_obd']:.1f}",
        f"{facility.ancc_target:.1f}",
    ])
    t = Table(data, colWidths=[100 * mm, 40 * mm, 30 * mm])
    style = _base_table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaeaea"))
    t.setStyle(style)
    return t


def _compliance_tests_table(stats: dict) -> Table:
    data = [["Test", "Achieved", "Required", "Result", "Shortfall total"]]
    for row in stats["tests"]["tests"]:
        result = "PASS" if row["passed"] else "FAIL" if row["passed"] is not None else "NO TARGET"
        data.append([
            row["label"], f"{row['achieved']:.1f}", f"{row['target']:.1f}", result,
            f"{row['shortfall_minutes']:,.0f} min" if row["shortfall_minutes"] else "-",
        ])
    table = Table(data, colWidths=[60 * mm, 27 * mm, 27 * mm, 25 * mm, 31 * mm])
    table.setStyle(_base_table_style())
    return table


def _evidence_table(stats: dict) -> Table:
    e = stats["evidence"]
    data = [
        ["Actual worked", "Rostered", "Unverified", "Delivered care", "Resident-day ledger"],
        [f"{e['worked_minutes']:,} min", f"{e['rostered_minutes']:,} min",
         f"{e['unverified_minutes']:,} min", f"{e['delivered_minutes']:,} min",
         (f"{e['resident_day_dates']}/{e['resident_day_expected_dates']} days"
          if e["uses_resident_day_ledger"] else "Fallback")],
    ]
    table = Table(data, colWidths=[34 * mm] * 5)
    table.setStyle(_base_table_style())
    return table


def _variance_paragraph(stats: dict, facility, body) -> Paragraph:
    parts = []
    for row in stats["tests"]["tests"]:
        if row["passed"]:
            variance = round(row["achieved"] - row["target"], 1)
            parts.append(f"{row['label']}: <b>{variance}</b> mins/OBD above requirement")
        elif row["passed"] is False:
            parts.append(f"{row['label']}: <b>{row['shortfall']}</b> mins/OBD below requirement")
        else:
            parts.append(f"{row['label']}: no target configured")
    return Paragraph(
        ".<br/>".join(parts) + ".",
        body,
    )
