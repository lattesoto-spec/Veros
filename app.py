import json
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

import exports as export_builders
from care_minutes import average, daily_stats, quarter_bounds, range_stats
from compliance import (
    CALC_VERSION,
    compliance_pct,
    detect_gaps,
    forecast_quarter,
    monthly_breakdown,
    range_breakdown,
    rn_coverage,
)
from ingestion import jobs as import_jobs
from ingestion.analyzer import AnalyzerError, DEFAULT_MODEL, ai_ready, resolve_model
from ingestion.mapping import MappingError
from ingestion.reader import FileReadError
from integrations.registry import BY_KEY, PLATFORMS
from integrations.sync import SyncError, sync_platform
from models import (
    Facility,
    FormatMapping,
    ImportReceipt,
    IntegrationConfig,
    Resident,
    Shift,
    Staff,
    db,
    get_setting,
    set_setting,
)
from reports import available_quarters, build_quarterly_pdf

def create_app() -> Flask:
    app = Flask(__name__)
    db_path = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "caremin.db"))
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOADS_DIR"] = os.path.join(os.path.dirname(db_path) or ".", "uploads")
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.template_filter("fromjson")(json.loads)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _migrate_sqlite(db)

    @app.route("/")
    def index():
        facility = Facility.query.first()
        if facility and Shift.query.first():
            return redirect(url_for("dashboard"))
        return redirect(url_for("universal_import"))

    @app.route("/upload", methods=["GET", "POST"])
    def upload():
        # Legacy fixed-template upload path, superseded by Universal Import.
        return redirect(url_for("universal_import"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        facility = Facility.query.first()

        if request.method == "POST":
            section = request.form.get("section")
            if section == "facility":
                name = (request.form.get("facility_name") or "").strip()
                if not name:
                    flash("Facility name is required.")
                    return redirect(url_for("settings"))
                if not facility:
                    facility = Facility(name=name)
                    db.session.add(facility)
                facility.name = name
                try:
                    facility.ancc_target = float(request.form.get("ancc_target") or facility.ancc_target or 215)
                    facility.rn_target = float(request.form.get("rn_target") or facility.rn_target or 44)
                except ValueError:
                    flash("Targets must be numbers.")
                    return redirect(url_for("settings"))
                db.session.commit()
                flash("Facility settings saved.")
            elif section == "api":
                key = (request.form.get("anthropic_api_key") or "").strip()
                if request.form.get("clear_key"):
                    set_setting("anthropic_api_key", None)
                    flash("API key removed.")
                elif key:
                    if not key.startswith("sk-ant-"):
                        flash("That doesn't look like an Anthropic API key (they start with sk-ant-).")
                        return redirect(url_for("settings"))
                    set_setting("anthropic_api_key", key)
                    flash("API key saved — new file formats can now be learned.")
                model = (request.form.get("analyzer_model") or "").strip()
                set_setting("analyzer_model", model or None)
                db.session.commit()
            return redirect(url_for("settings"))

        stored_key = get_setting("anthropic_api_key")
        env_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        return render_template(
            "settings.html",
            facility=facility,
            key_masked=(stored_key[:10] + "…" + stored_key[-4:]) if stored_key else None,
            env_key=env_key,
            ai_ready=ai_ready(),
            current_model=resolve_model(),
            default_model=DEFAULT_MODEL,
            model_setting=get_setting("analyzer_model") or "",
        )

    @app.route("/import", methods=["GET", "POST"])
    def universal_import():
        facility = Facility.query.first()
        if request.method == "GET":
            return render_template(
                "universal_import.html",
                facility=facility,
                errors=[],
                known_formats=FormatMapping.query.order_by(FormatMapping.created_at.desc()).all(),
                ai_ready=ai_ready(),
            )

        errors = []
        name = (request.form.get("facility_name") or "").strip()
        files = [f for f in request.files.getlist("data_files") if f and f.filename]
        if not name and not facility:
            errors.append("Facility name is required for the first import.")
        if not files:
            errors.append("Upload at least one file.")
        if errors:
            return render_template(
                "universal_import.html",
                facility=facility,
                errors=errors,
                known_formats=FormatMapping.query.order_by(FormatMapping.created_at.desc()).all(),
                ai_ready=ai_ready(),
            ), 400

        if facility:
            if name:
                facility.name = name
        else:
            facility = Facility(name=name)
            db.session.add(facility)
        # Commit now: the background worker needs the facility id, and the
        # request must not hold an open transaction while the job runs.
        db.session.commit()

        # The slow work (AI format learning, extraction) happens in a
        # background job so this request returns immediately — no gunicorn or
        # proxy timeout can kill an import, however large the file.
        payloads = [(f.filename, f.read()) for f in files]
        job_id = import_jobs.start_job(app, facility.id, payloads, app.config["UPLOADS_DIR"])
        return redirect(url_for("import_status", job_id=job_id))

    @app.route("/import/status/<job_id>")
    def import_status(job_id):
        job = import_jobs.get_job(job_id)
        if not job:
            flash(
                "That import job is no longer tracked (the server may have "
                "restarted). Check the audit trail for the result."
            )
            return redirect(url_for("universal_import"))
        return render_template("import_status.html", job=job)

    @app.route("/import/status/<job_id>.json")
    def import_status_json(job_id):
        job = import_jobs.get_job(job_id)
        if not job:
            return {"status": "gone"}, 404
        return {k: job[k] for k in ("id", "status", "error", "receipt_id", "files")}

    @app.route("/import/summary/<int:receipt_id>")
    def import_summary(receipt_id):
        receipt = ImportReceipt.query.get_or_404(receipt_id)
        outcomes = json.loads(receipt.summary_json) if receipt.summary_json else []
        return render_template(
            "import_summary.html",
            facility=Facility.query.first(),
            outcomes=outcomes,
            receipt=receipt,
        )

    def _latest_data_date(facility):
        latest = Shift.query.filter_by(facility_id=facility.id).order_by(Shift.date.desc()).first()
        return latest.date if latest else date.today()

    @app.route("/compliance")
    def compliance_view():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        today = _latest_data_date(facility)
        fc = forecast_quarter(facility, today)
        return render_template(
            "compliance.html",
            facility=facility,
            today=today,
            forecast=fc,
            alerts=detect_gaps(facility, today),
            coverage=rn_coverage(facility.id, today),
            months=monthly_breakdown(facility.id, fc["q_start"], today) if fc else [],
            week=range_breakdown(facility.id, today - timedelta(days=6), today),
            calc_version=CALC_VERSION,
        )

    @app.route("/scenarios", methods=["GET", "POST"])
    def scenarios():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        today = _latest_data_date(facility)
        base = forecast_quarter(facility, today)

        params = {"rn_shifts_removed": 0, "agency_removed": False, "occupancy_delta": 0}
        result = None
        if request.method == "POST" and base:
            try:
                params["rn_shifts_removed"] = max(int(request.form.get("rn_shifts_removed") or 0), 0)
                params["occupancy_delta"] = int(request.form.get("occupancy_delta") or 0)
            except ValueError:
                flash("Scenario inputs must be whole numbers.")
            params["agency_removed"] = request.form.get("agency_removed") == "on"
            result = forecast_quarter(facility, today, adjustments=params)

        return render_template(
            "scenarios.html",
            facility=facility, today=today, base=base,
            params=params, result=result,
        )

    @app.route("/export/daily.csv")
    def export_daily_csv():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        start, end = _export_range(facility)
        text = export_builders.daily_csv(facility, start, end)
        return Response(
            text, mimetype="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=care-minutes-daily_{start}_{end}.csv"},
        )

    @app.route("/export/summary.xlsx")
    def export_summary_xlsx():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        start, end = _export_range(facility)
        blob = export_builders.summary_xlsx(facility, start, end)
        return Response(
            blob,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f"attachment; filename=care-minutes-summary_{start}_{end}.xlsx"},
        )

    @app.route("/report/board.pdf")
    def board_report():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        blob = export_builders.board_pdf(facility, _latest_data_date(facility))
        return Response(
            blob, mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=board-care-minutes-report.pdf"},
        )

    def _export_range(facility):
        def _parse(name, fallback):
            try:
                return datetime.strptime(request.args.get(name, ""), "%Y-%m-%d").date()
            except ValueError:
                return fallback
        latest = _latest_data_date(facility)
        return _parse("start", latest - timedelta(days=89)), _parse("end", latest)

    @app.route("/audit")
    def audit():
        facility = Facility.query.first()
        if not facility:
            return redirect(url_for("universal_import"))
        receipts = (
            ImportReceipt.query.filter_by(facility_id=facility.id)
            .order_by(ImportReceipt.imported_at.desc()).all()
        )
        mappings = {m.id: m for m in FormatMapping.query.all()}
        files_by_receipt = {}
        for r in receipts:
            if r.source_path and os.path.isdir(r.source_path):
                files_by_receipt[r.id] = sorted(os.listdir(r.source_path))
        with_files = sum(1 for r in receipts if r.id in files_by_receipt)
        return render_template(
            "audit.html",
            facility=facility,
            receipts=receipts,
            mappings=mappings,
            files_by_receipt=files_by_receipt,
            readiness=round(with_files / len(receipts) * 100) if receipts else None,
            calc_version=CALC_VERSION,
        )

    @app.route("/audit/file/<int:receipt_id>/<path:filename>")
    def audit_file(receipt_id, filename):
        receipt = ImportReceipt.query.get_or_404(receipt_id)
        if not receipt.source_path:
            return ("Not found", 404)
        safe = os.path.basename(filename)
        return send_from_directory(receipt.source_path, safe, as_attachment=True)

    @app.route("/integrations", methods=["GET", "POST"])
    def integrations_view():
        facility = Facility.query.first()
        configs = {c.platform: c for c in IntegrationConfig.query.all()}

        if request.method == "POST":
            action = request.form.get("action")
            platform = request.form.get("platform", "")
            if platform not in BY_KEY:
                flash("Unknown platform.")
                return redirect(url_for("integrations_view"))

            if action == "configure":
                cfg = configs.get(platform) or IntegrationConfig(platform=platform)
                cfg.config_json = json.dumps({"url": (request.form.get("url") or "").strip()})
                db.session.add(cfg)
                db.session.commit()
                flash(f"{BY_KEY[platform].name} configured.")
            elif action == "sync":
                cfg = configs.get(platform)
                if not cfg:
                    flash("Configure the connection first.")
                elif not facility:
                    flash("Create a facility (run one import) before syncing.")
                else:
                    try:
                        outcomes = sync_platform(facility, cfg)
                        db.session.commit()
                        o = outcomes[0]
                        flash(f"Synced {o.filename}: {o.shifts_imported} shifts, "
                              f"{o.residents_imported} residents.")
                    except (SyncError, FileReadError, MappingError, AnalyzerError) as e:
                        db.session.rollback()
                        cfg = IntegrationConfig.query.filter_by(platform=platform).first()
                        if cfg:
                            cfg.last_result = f"Failed: {e}"
                            db.session.commit()
                        flash(f"Sync failed: {e}")
            return redirect(url_for("integrations_view"))

        return render_template(
            "integrations.html",
            platforms=PLATFORMS,
            configs=configs,
            facility=facility,
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

        # Phase 6 additions
        pct = compliance_pct(facility, today)
        coverage = rn_coverage(facility.id, today)
        alerts = detect_gaps(facility, today)
        last_receipt = _latest_receipt(facility)
        data_age_days = (
            (date.today() - last_receipt.imported_at.date()).days if last_receipt else None
        )
        receipts = ImportReceipt.query.filter_by(facility_id=facility.id).all()
        with_files = sum(
            1 for r in receipts if r.source_path and os.path.isdir(r.source_path)
        )
        audit_readiness = round(with_files / len(receipts) * 100) if receipts else None

        return render_template(
            "dashboard.html",
            compliance_pct=pct,
            coverage=coverage,
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


def _migrate_sqlite(db):
    """create_all() never alters existing tables, so add columns introduced
    after the first deploy by hand. Safe to run on every startup."""
    from sqlalchemy import text

    additions = {
        "shifts": [
            ("break_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("is_agency", "BOOLEAN NOT NULL DEFAULT 0"),
            ("import_receipt_id", "INTEGER"),
            ("source_row", "INTEGER"),
        ],
        "import_receipts": [
            ("source_path", "TEXT"),
            ("imported_by", "TEXT"),
            ("mapping_ids", "TEXT"),
            ("calc_version", "TEXT"),
            ("summary_json", "TEXT"),
        ],
    }
    for table, cols in additions.items():
        existing = {
            row[1] for row in db.session.execute(text(f"PRAGMA table_info({table})"))
        }
        for name, ddl in cols:
            if name not in existing:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    db.session.commit()


def _parse_date(value):
    if not value or not str(value).strip():
        return None
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()



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




app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=8080)

