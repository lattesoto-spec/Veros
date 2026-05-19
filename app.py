import csv
import io
import os
from datetime import date, datetime, time, timedelta

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from care_minutes import average, daily_stats, quarter_bounds, range_stats
from models import Facility, ImportReceipt, Resident, Shift, Staff, db
from reports import available_quarters, build_quarterly_pdf

REQUIRED_RESIDENT_COLS = {"resident_id", "name", "ancc_class", "admitted_date", "discharged_date"}
REQUIRED_SHIFT_COLS = {"staff_id", "staff_name", "role", "date", "start_time", "end_time", "is_direct_care"}


def create_app() -> Flask:
    app = Flask(__name__)
    db_path = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "carelog.db"))
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.route("/")
    def index():
        facility = Facility.query.first()
        if facility and Shift.query.first():
            return redirect(url_for("dashboard"))
        return redirect(url_for("upload"))

    @app.route("/upload", methods=["GET", "POST"])
    def upload():
        if request.method == "GET":
            facility = Facility.query.first()
            return render_template(
                "upload.html",
                facility=facility,
                errors=[],
                last_receipt=_latest_receipt(facility),
            )

        errors = []
        name = (request.form.get("facility_name") or "").strip()
        try:
            ancc_target = float(request.form.get("ancc_target") or 215)
            rn_target = float(request.form.get("rn_target") or 44)
        except ValueError:
            ancc_target, rn_target = 215.0, 44.0
            errors.append("Targets must be numbers.")

        residents_file = request.files.get("residents_csv")
        shifts_file = request.files.get("shifts_csv")

        if not name:
            errors.append("Facility name is required.")
        if not residents_file or not residents_file.filename:
            errors.append("Residents CSV is required.")
        if not shifts_file or not shifts_file.filename:
            errors.append("Shifts CSV is required.")

        if errors:
            facility = Facility.query.first()
            return render_template(
                "upload.html",
                facility=facility,
                errors=errors,
                last_receipt=_latest_receipt(facility),
            ), 400

        residents_text = residents_file.read().decode("utf-8-sig")
        shifts_text = shifts_file.read().decode("utf-8-sig")

        r_reader = csv.DictReader(io.StringIO(residents_text))
        s_reader = csv.DictReader(io.StringIO(shifts_text))

        missing_r = REQUIRED_RESIDENT_COLS - set(r_reader.fieldnames or [])
        missing_s = REQUIRED_SHIFT_COLS - set(s_reader.fieldnames or [])
        if missing_r:
            errors.append(f"Residents CSV missing columns: {', '.join(sorted(missing_r))}")
        if missing_s:
            errors.append(f"Shifts CSV missing columns: {', '.join(sorted(missing_s))}")

        if errors:
            facility = Facility.query.first()
            return render_template(
                "upload.html",
                facility=facility,
                errors=errors,
                last_receipt=_latest_receipt(facility),
            ), 400

        facility = Facility.query.first()
        if facility:
            facility.name = name
            facility.ancc_target = ancc_target
            facility.rn_target = rn_target
        else:
            facility = Facility(name=name, ancc_target=ancc_target, rn_target=rn_target)
            db.session.add(facility)
        db.session.flush()

        # Replace shifts on every upload (the ingestion contract).
        Shift.query.filter_by(facility_id=facility.id).delete()

        r_imported, r_skipped, r_errors = _ingest_residents(facility, r_reader)
        s_imported, s_skipped, s_errors, first_d, last_d = _ingest_shifts(facility, s_reader)

        receipt = ImportReceipt(
            facility_id=facility.id,
            residents_imported=r_imported,
            residents_skipped=r_skipped,
            shifts_imported=s_imported,
            shifts_skipped=s_skipped,
            first_shift_date=first_d,
            last_shift_date=last_d,
        )
        db.session.add(receipt)
        db.session.commit()

        return render_template(
            "upload_summary.html",
            facility=facility,
            receipt=receipt,
            resident_errors=r_errors,
            shift_errors=s_errors,
        )

    @app.route("/report/quarter.pdf")
    def quarterly_report():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("upload"))
        q = (request.args.get("q") or "").strip()
        try:
            year_str, q_str = q.split("-Q")
            year = int(year_str)
            quarter = int(q_str)
            if quarter not in (1, 2, 3, 4):
                raise ValueError
        except (ValueError, AttributeError):
            flash("Pick a valid quarter to generate a report.")
            return redirect(url_for("dashboard"))

        pdf_bytes = build_quarterly_pdf(facility, year, quarter)
        safe_name = "".join(c if c.isalnum() else "-" for c in facility.name).strip("-").lower()
        filename = f"care-minutes-statement_{safe_name}_{year}-Q{quarter}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/samples/<path:filename>")
    def sample_file(filename):
        if filename not in ("residents.csv", "shifts.csv"):
            return ("Not found", 404)
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "sample_data"),
            filename,
            as_attachment=True,
        )

    @app.route("/dashboard")
    def dashboard():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("upload"))

        latest_shift = Shift.query.filter_by(facility_id=facility.id).order_by(Shift.date.desc()).first()
        today = latest_shift.date if latest_shift else date.today()

        today_stats = daily_stats(facility.id, today, facility.ancc_target, facility.rn_target)

        last_14_start = today - timedelta(days=13)
        last_14 = range_stats(facility.id, last_14_start, today, facility.ancc_target, facility.rn_target)

        q_start, q_end = quarter_bounds(today)
        q_rows = range_stats(facility.id, q_start, q_end, facility.ancc_target, facility.rn_target)
        q_avg = average(q_rows)
        q_gap = round(q_avg["care_per_resident"] - facility.ancc_target, 1)
        q_gap_pct = round((q_gap / facility.ancc_target) * 100, 1) if facility.ancc_target else 0

        chart_data = {
            "labels": [r["date"].strftime("%d %b") for r in last_14],
            "values": [r["care_per_resident"] for r in last_14],
            "colors": [_color_for(r["status"]) for r in last_14],
            "target": facility.ancc_target,
        }

        resident_count = (
            db.session.query(Resident)
            .filter(Resident.facility_id == facility.id, Resident.discharged_date == None)  # noqa: E711
            .count()
        )

        return render_template(
            "dashboard.html",
            facility=facility,
            today=today,
            today_stats=today_stats,
            last_14=list(reversed(last_14)),
            chart_data=chart_data,
            q_avg=q_avg,
            q_gap=q_gap,
            q_gap_pct=q_gap_pct,
            q_start=q_start,
            resident_count=resident_count,
            last_receipt=_latest_receipt(facility),
            quarters=available_quarters(facility.id),
        )

    @app.route("/facility", methods=["GET"])
    def facility_view():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("upload"))
        residents = Resident.query.filter_by(facility_id=facility.id).order_by(Resident.name).all()
        staff = Staff.query.filter_by(facility_id=facility.id).order_by(Staff.role, Staff.name).all()
        return render_template("facility.html", facility=facility, residents=residents, staff=staff)

    @app.route("/facility/targets", methods=["POST"])
    def update_targets():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("upload"))
        try:
            facility.ancc_target = float(request.form.get("ancc_target") or facility.ancc_target)
            facility.rn_target = float(request.form.get("rn_target") or facility.rn_target)
            db.session.commit()
        except ValueError:
            flash("Targets must be numbers.")
        return redirect(url_for("dashboard"))

    @app.route("/clear", methods=["POST"])
    def clear():
        Shift.query.delete()
        Staff.query.delete()
        Resident.query.delete()
        ImportReceipt.query.delete()
        Facility.query.delete()
        db.session.commit()
        return redirect(url_for("upload"))

    return app


def _parse_date(value):
    if not value or not str(value).strip():
        return None
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _parse_time(value):
    return datetime.strptime(str(value).strip(), "%H:%M").time()


def _color_for(status: str) -> str:
    return {"on_track": "#2f9e44", "at_risk": "#f08c00", "behind": "#c92a2a"}.get(status, "#888")


def _latest_receipt(facility):
    if not facility:
        return None
    return (
        ImportReceipt.query.filter_by(facility_id=facility.id)
        .order_by(ImportReceipt.imported_at.desc())
        .first()
    )


def _ingest_residents(facility, reader):
    imported = 0
    skipped = 0
    errors = []
    for idx, row in enumerate(reader, start=2):  # row 1 is header
        try:
            rid = (row.get("resident_id") or "").strip()
            if not rid:
                raise ValueError("resident_id is blank")
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError("name is blank")
            adm = _parse_date(row.get("admitted_date"))
            dis = _parse_date(row.get("discharged_date"))
        except ValueError as e:
            skipped += 1
            errors.append(f"Row {idx}: {e}")
            continue

        existing = Resident.query.filter_by(facility_id=facility.id, resident_id=rid).first()
        if existing:
            existing.name = name
            existing.ancc_class = (row.get("ancc_class") or "").strip()
            existing.admitted_date = adm
            existing.discharged_date = dis
        else:
            db.session.add(Resident(
                facility_id=facility.id,
                resident_id=rid,
                name=name,
                ancc_class=(row.get("ancc_class") or "").strip(),
                admitted_date=adm,
                discharged_date=dis,
            ))
        imported += 1
    return imported, skipped, errors


def _ingest_shifts(facility, reader):
    imported = 0
    skipped = 0
    errors = []
    first_d = None
    last_d = None
    staff_cache = {s.staff_id: s for s in Staff.query.filter_by(facility_id=facility.id).all()}

    for idx, row in enumerate(reader, start=2):
        try:
            sid = (row.get("staff_id") or "").strip()
            staff_name = (row.get("staff_name") or "").strip()
            role = (row.get("role") or "").strip().upper()
            if not sid:
                raise ValueError("staff_id is blank")
            if not role:
                raise ValueError("role is blank")
            d = _parse_date(row.get("date"))
            if d is None:
                raise ValueError("date is blank")
            start = _parse_time(row.get("start_time") or "")
            end = _parse_time(row.get("end_time") or "")
        except ValueError as e:
            skipped += 1
            errors.append(f"Row {idx}: {e}")
            continue

        staff = staff_cache.get(sid)
        if staff:
            if staff_name:
                staff.name = staff_name
            staff.role = role
        else:
            staff = Staff(
                facility_id=facility.id,
                staff_id=sid,
                name=staff_name or sid,
                role=role,
            )
            db.session.add(staff)
            db.session.flush()
            staff_cache[sid] = staff

        db.session.add(Shift(
            staff_id=staff.id,
            facility_id=facility.id,
            date=d,
            start_time=start,
            end_time=end,
            is_direct_care=str(row.get("is_direct_care", "true")).strip().lower() in ("true", "1", "yes"),
        ))
        imported += 1
        first_d = d if first_d is None or d < first_d else first_d
        last_d = d if last_d is None or d > last_d else last_d

    return imported, skipped, errors, first_d, last_d


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=8080)
