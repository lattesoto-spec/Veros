import csv
import io
import os
from datetime import date, datetime, time, timedelta

from flask import Flask, flash, redirect, render_template, request, url_for

from care_minutes import average, daily_stats, quarter_bounds, range_stats
from models import Facility, Resident, Shift, Staff, db

REQUIRED_RESIDENT_COLS = {"resident_id", "name", "ancc_class", "admitted_date", "discharged_date"}
REQUIRED_SHIFT_COLS = {"staff_id", "staff_name", "role", "date", "start_time", "end_time", "is_direct_care"}


def create_app() -> Flask:
    app = Flask(__name__)
    db_path = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "vero.db"))
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
            return render_template("upload.html", facility=facility, errors=[])

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
            return render_template("upload.html", facility=Facility.query.first(), errors=errors), 400

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
            return render_template("upload.html", facility=Facility.query.first(), errors=errors), 400

        facility = Facility.query.first()
        if facility:
            facility.name = name
            facility.ancc_target = ancc_target
            facility.rn_target = rn_target
        else:
            facility = Facility(name=name, ancc_target=ancc_target, rn_target=rn_target)
            db.session.add(facility)
        db.session.flush()

        for row in r_reader:
            rid = row["resident_id"].strip()
            existing = Resident.query.filter_by(facility_id=facility.id, resident_id=rid).first()
            adm = _parse_date(row.get("admitted_date"))
            dis = _parse_date(row.get("discharged_date"))
            if existing:
                existing.name = row["name"].strip()
                existing.ancc_class = row.get("ancc_class", "").strip()
                existing.admitted_date = adm
                existing.discharged_date = dis
            else:
                db.session.add(Resident(
                    facility_id=facility.id,
                    resident_id=rid,
                    name=row["name"].strip(),
                    ancc_class=row.get("ancc_class", "").strip(),
                    admitted_date=adm,
                    discharged_date=dis,
                ))

        staff_cache = {s.staff_id: s for s in Staff.query.filter_by(facility_id=facility.id).all()}

        for row in s_reader:
            sid = row["staff_id"].strip()
            staff = staff_cache.get(sid)
            if staff:
                staff.name = row["staff_name"].strip()
                staff.role = row["role"].strip().upper()
            else:
                staff = Staff(
                    facility_id=facility.id,
                    staff_id=sid,
                    name=row["staff_name"].strip(),
                    role=row["role"].strip().upper(),
                )
                db.session.add(staff)
                db.session.flush()
                staff_cache[sid] = staff

            db.session.add(Shift(
                staff_id=staff.id,
                facility_id=facility.id,
                date=_parse_date(row["date"]),
                start_time=_parse_time(row["start_time"]),
                end_time=_parse_time(row["end_time"]),
                is_direct_care=str(row.get("is_direct_care", "true")).strip().lower() in ("true", "1", "yes"),
            ))

        db.session.commit()
        return redirect(url_for("dashboard"))

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


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=8080)
