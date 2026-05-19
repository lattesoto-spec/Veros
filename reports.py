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
from datetime import date, datetime, timedelta
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import or_

from care_minutes import _minutes_between
from models import Resident, Shift, Staff, db

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

    # Labour worked minutes by role (direct-care shifts only).
    worked_minutes = {role: 0 for role, _ in ROLES}
    shifts = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Shift.is_direct_care == True,  # noqa: E712
            Shift.date >= start,
            Shift.date <= end,
        )
        .all()
    )
    for s, st in shifts:
        role = st.role.upper()
        if role in worked_minutes:
            worked_minutes[role] += _minutes_between(s.start_time, s.end_time)

    # Occupied bed days = sum of active residents on each day in the quarter.
    obd = 0
    d = start
    while d <= end:
        obd += _active_residents_on(facility_id, d)
        d += timedelta(days=1)

    # Monthly 24/7 RN coverage %.
    monthly_rn = []
    for y, m in _months_in_range(start, end):
        pct = _rn_coverage_pct(facility_id, y, m)
        monthly_rn.append({
            "year": y,
            "month": m,
            "label": date(y, m, 1).strftime("%B %Y"),
            "coverage_pct": round(pct, 2),
        })

    # Care minutes per occupied bed day = worked_hours / OBD * 60 = worked_minutes / OBD.
    per_obd = {}
    for role, _ in ROLES:
        per_obd[role] = round(worked_minutes[role] / obd, 1) if obd else 0.0
    total_per_obd = round(sum(per_obd.values()), 1)

    worked_hours = {role: round(m / 60, 1) for role, m in worked_minutes.items()}

    return {
        "facility_id": facility_id,
        "year": year,
        "quarter": quarter,
        "label": _fy_label(year, quarter),
        "start": start,
        "end": end,
        "worked_minutes": worked_minutes,
        "worked_hours": worked_hours,
        "occupied_bed_days": obd,
        "monthly_rn_coverage": monthly_rn,
        "care_minutes_per_obd": per_obd,
        "total_care_minutes_per_obd": total_per_obd,
    }


def _active_residents_on(facility_id: int, day: date) -> int:
    return (
        db.session.query(Resident)
        .filter(
            Resident.facility_id == facility_id,
            Resident.admitted_date <= day,
            or_(Resident.discharged_date == None, Resident.discharged_date > day),  # noqa: E711
        )
        .count()
    )


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


def _rn_coverage_pct(facility_id: int, year: int, month: int) -> float:
    days = monthrange(year, month)[1]
    month_start = datetime(year, month, 1)
    month_end = month_start + timedelta(days=days)

    rows = (
        db.session.query(Shift, Staff)
        .join(Staff, Shift.staff_id == Staff.id)
        .filter(
            Shift.facility_id == facility_id,
            Staff.role == "RN",
            Shift.date >= (month_start.date() - timedelta(days=1)),
            Shift.date <= month_end.date(),
        )
        .all()
    )

    intervals = []
    for s, _ in rows:
        start_dt = datetime.combine(s.date, s.start_time)
        end_dt = datetime.combine(s.date, s.end_time)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        clipped_start = max(start_dt, month_start)
        clipped_end = min(end_dt, month_end)
        if clipped_end > clipped_start:
            intervals.append((clipped_start, clipped_end))

    intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for s, e in intervals:
        if merged and (s - merged[-1][1]).total_seconds() < 30 * 60:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    covered = sum((e - s).total_seconds() / 60 for s, e in merged)
    total = (month_end - month_start).total_seconds() / 60
    return covered / total * 100 if total else 0.0


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
        title=f"Care Minutes Performance Statement – {facility.name} – {stats['label']}",
        author="Carelog",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    muted = ParagraphStyle("muted", parent=body, textColor=colors.HexColor("#666"))

    story = []
    story.append(Paragraph("Care Minutes Performance Statement", h1))
    story.append(Paragraph(
        f"{facility.name} · {stats['label']} · "
        f"{stats['start'].strftime('%d %b %Y')} – {stats['end'].strftime('%d %b %Y')}",
        body,
    ))
    story.append(Paragraph(
        "Draft generated by Carelog for internal review. Figures derive from rostered direct-care "
        "shifts and resident occupancy in the prototype dataset. Labour costs are not tracked in "
        "Carelog and must be entered from payroll before submission. Independent audit under "
        "ASAE 3000 is required before this statement is filed with the ACFR.",
        muted,
    ))
    story.append(Spacer(1, 6))

    story.append(Paragraph("1. Labour worked hours – direct care", h2))
    story.append(_labour_hours_table(stats))
    story.append(Paragraph(
        "Hours derive from direct-care rostered shift durations. Employee vs agency split is not "
        "tracked in the prototype; all hours appear under \"Employee\".",
        muted,
    ))

    story.append(Paragraph("2. Labour costs – direct care", h2))
    story.append(_labour_costs_placeholder())
    story.append(Paragraph(
        "Carelog does not store payroll costs. Enter quarterly direct-care salary + superannuation "
        "(employee) and contract amounts (agency) from your payroll system before filing.",
        muted,
    ))

    story.append(Paragraph("3. 24/7 RN coverage – monthly", h2))
    story.append(_rn_coverage_table(stats))
    story.append(Paragraph(
        "Coverage = (minutes in month − sum of RN-on-site gaps of 30 minutes or more) ÷ minutes in "
        "month. Computed from RN shift intervals merged across gaps shorter than 30 minutes.",
        muted,
    ))

    story.append(Paragraph("4. Occupied bed days", h2))
    story.append(_obd_table(stats))
    story.append(Paragraph(
        "Occupied bed days = sum, over each day in the quarter, of residents whose admitted_date "
        "is on or before the day and whose discharged_date is empty or later than the day.",
        muted,
    ))

    story.append(Paragraph("5. Direct care minutes (worked) per occupied bed day", h2))
    story.append(_care_minutes_per_obd_table(stats, facility))
    story.append(Paragraph(
        "Per-OBD minutes = worked hours × 60 ÷ occupied bed days. EN hours are reported separately "
        "and must not be used to meet RN care-minute targets.",
        muted,
    ))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Variance against AN-ACC targets", h2))
    story.append(_variance_paragraph(stats, facility, body))

    doc.build(story)
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
        hrs = stats["worked_hours"][role]
        data.append([label, f"{hrs:,.1f}", "0.0", f"{hrs:,.1f}"])
    total = sum(stats["worked_hours"].values())
    data.append(["Total", f"{total:,.1f}", "0.0", f"{total:,.1f}"])
    t = Table(data, colWidths=[80 * mm, 30 * mm, 30 * mm, 30 * mm])
    style = _base_table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaeaea"))
    t.setStyle(style)
    return t


def _labour_costs_placeholder() -> Table:
    data = [
        ["Role", "Employee ($)", "Agency ($)", "Total ($)"],
        ["Registered Nurse", "—", "—", "—"],
        ["Enrolled Nurse", "—", "—", "—"],
        ["Personal Care Worker / AIN", "—", "—", "—"],
    ]
    t = Table(data, colWidths=[80 * mm, 30 * mm, 30 * mm, 30 * mm])
    t.setStyle(_base_table_style())
    return t


def _rn_coverage_table(stats: dict) -> Table:
    data = [["Month", "RN coverage (%)"]]
    for row in stats["monthly_rn_coverage"]:
        data.append([row["label"], f"{row['coverage_pct']:.2f}%"])
    t = Table(data, colWidths=[110 * mm, 60 * mm])
    t.setStyle(_base_table_style())
    return t


def _obd_table(stats: dict) -> Table:
    data = [
        ["Period", "Occupied bed days"],
        [
            f"{stats['start'].strftime('%d %b %Y')} – {stats['end'].strftime('%d %b %Y')}",
            f"{stats['occupied_bed_days']:,}",
        ],
    ]
    t = Table(data, colWidths=[110 * mm, 60 * mm])
    t.setStyle(_base_table_style())
    return t


def _care_minutes_per_obd_table(stats: dict, facility) -> Table:
    data = [["Role", "Care minutes per OBD", "Target"]]
    target_map = {"RN": facility.rn_target, "EN": "—", "PCA": "—"}
    for role, label in ROLES:
        target = target_map.get(role, "—")
        target_str = f"{target}" if isinstance(target, (int, float)) else target
        data.append([label, f"{stats['care_minutes_per_obd'][role]:.1f}", target_str])
    data.append(["Total", f"{stats['total_care_minutes_per_obd']:.1f}", f"{facility.ancc_target}"])
    t = Table(data, colWidths=[100 * mm, 40 * mm, 30 * mm])
    style = _base_table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaeaea"))
    t.setStyle(style)
    return t


def _variance_paragraph(stats: dict, facility, body) -> Paragraph:
    total = stats["total_care_minutes_per_obd"]
    rn = stats["care_minutes_per_obd"]["RN"]
    gap = round(total - facility.ancc_target, 1)
    rn_gap = round(rn - facility.rn_target, 1)
    if gap >= 0:
        total_msg = f"<b>{gap}</b> mins/OBD above the AN-ACC target of {facility.ancc_target}"
    else:
        total_msg = f"<b>{abs(gap)}</b> mins/OBD below the AN-ACC target of {facility.ancc_target}"
    if rn_gap >= 0:
        rn_msg = f"<b>{rn_gap}</b> mins/OBD above the RN target of {facility.rn_target}"
    else:
        rn_msg = f"<b>{abs(rn_gap)}</b> mins/OBD below the RN target of {facility.rn_target}"
    return Paragraph(
        f"Total direct-care minutes per occupied bed day: {total_msg}.<br/>"
        f"RN minutes per occupied bed day: {rn_msg}.",
        body,
    )
