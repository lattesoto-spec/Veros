import json
import os
from datetime import date, datetime, timedelta

import click

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from carelog.domain import eligibility as el
from carelog.domain import exports as export_builders
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
    range_breakdown,
    quarterly_tests,
    rn_coverage,
)
from carelog import auth
from carelog.auth import (
    ROLES,
    current_organization_id,
    current_user,
    has_permission,
    owned_or_404,
    require,
)
from carelog.ingestion import jobs as import_jobs
from carelog.ingestion.analyzer import AnalyzerError, ai_ready, resolve_model
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.reader import FileReadError
from carelog.integrations.registry import BY_KEY, PLATFORMS
from carelog.integrations.sync import SyncError, sync_platform
from carelog.models import (
    AuditLog,
    CareEpisode,
    Facility,
    ImportJob,
    FormatMapping,
    ImportReceipt,
    IntegrationConfig,
    Organization,
    Resident,
    ResidentDay,
    Shift,
    Staff,
    User,
    db,
)
from carelog.domain.reports import available_quarters, build_quarterly_pdf
from carelog.storage import LocalStorage, StorageError, build_storage, find_blob_token

# Repository root — public/ and sample_data/ live beside the package, not in it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app() -> Flask:
    app = Flask(__name__)
    uri = _database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOADS_DIR"] = os.environ.get(
        "UPLOADS_DIR", os.path.join(ROOT, "uploads")
    )
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Cookies must not travel over plain HTTP anywhere real; local dev is
        # the only place without TLS.
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL") or os.environ.get("FORCE_HTTPS")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    app.template_filter("fromjson")(json.loads)

    # A misconfigured deployment must not take the whole function down at
    # import time: that surfaces as FUNCTION_INVOCATION_FAILED on every path,
    # including pages that never touch the database, and says nothing about
    # the cause. Diagnose first, and never initialise the database layer with
    # settings known to be wrong.
    problems = _config_problems(uri)

    def _register_health(current_problems):
        @app.route("/healthz")
        def healthz():
            """Deployment health, answerable without a working database."""
            return {
                "ok": not current_problems,
                "problems": current_problems,
                "environment": "vercel" if os.environ.get("VERCEL") else "self-hosted",
                "database": "configured" if uri else "MISSING",
                "storage": "vercel_blob" if find_blob_token()[0] else "local",
                "worker": import_jobs.worker_mode(),
                "schema_drift": _schema_drift() if uri and not current_problems else None,
            }, (200 if not current_problems else 503)

        @app.route("/style.css")
        def stylesheet():
            # On Vercel the CDN serves public/ before a request reaches this
            # function; this fallback is what serves it everywhere else.
            return send_from_directory(os.path.join(ROOT, "public"), "style.css")

        @app.route("/brand/<filename>")
        def brand_asset(filename):
            """Serve the approved CareMin brand assets in every environment."""
            if filename not in {"full.png", "simple.png", "favicon.png"}:
                abort(404)
            return send_from_directory(os.path.join(ROOT, "icons"), filename)

    if problems:
        # Stop here: no engine, no session, no models — just the diagnosis.
        _register_health(problems)

        @app.before_request
        def _configuration_gate():
            if request.endpoint in ("healthz", "stylesheet", "brand_asset"):
                return None
            return render_template("misconfigured.html", problems=problems), 503

        return app

    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_options(uri)
    db.init_app(app)

    # Creating tables on every cold start is slow and races with itself, so on
    # serverless the schema is created once by `flask init-db` (see setup.sh).
    if _auto_init_db(uri):
        with app.app_context():
            try:
                init_db()
            except Exception as e:
                problems.append(f"The database could not be reached: {type(e).__name__}: {e}")

    _register_health(problems)
    if problems:
        @app.before_request
        def _database_gate():
            if request.endpoint in ("healthz", "stylesheet", "brand_asset"):
                return None
            return render_template("misconfigured.html", problems=problems), 503

    @app.errorhandler(Exception)
    def _explain_schema_drift(error):
        """A missing column means the database predates the deployed code.

        Without this the user sees a raw 500 and a stack trace, which says
        nothing about the one command that fixes it.
        """
        from sqlalchemy.exc import ProgrammingError, OperationalError
        from werkzeug.exceptions import HTTPException

        # Preserve deliberate 400/403/404 responses. Catching Exception at
        # this level must not turn authorization failures into server errors.
        if isinstance(error, HTTPException):
            return error

        if isinstance(error, (ProgrammingError, OperationalError)) and \
                "does not exist" in str(getattr(error, "orig", error)):
            db.session.rollback()
            app.logger.error("Schema is behind the code: %s", error)
            missing = str(getattr(error, "orig", error)).split("\n")[0]
            return render_template("misconfigured.html", problems=[
                f"The database is missing something this version of the app "
                f"expects: {missing.strip()}. The schema was not migrated when "
                f"this version deployed. Run `flask --app app init-db` against "
                f"this environment's DATABASE_URL, or redeploy now that the "
                f"build step runs migrations automatically. No data is lost; "
                f"the migration only adds what is missing."
            ]), 503
        raise error

    auth.init_app(app)

    @app.cli.command("init-db")
    def init_db_command():
        """Create any missing tables and columns."""
        added = init_db()
        print(f"Schema ready on {db.engine.url.render_as_string(hide_password=True)}")
        if added:
            print("Added columns: " + ", ".join(added))

    @app.cli.command("bootstrap-org")
    @click.option("--name", required=True, help="Organization (company) name")
    @click.option("--admin-email", required=True)
    @click.option("--password", required=True, help="Temporary password; changed at first sign-in")
    @click.option("--admin-name", default="", help="Administrator's full name")
    @click.option("--superuser", is_flag=True, help="Platform operator: can support any organization")
    @click.option("--adopt-existing", is_flag=True,
                  help="Assign pre-existing single-tenant data to this organization")
    def bootstrap_org(name, admin_email, password, admin_name, superuser, adopt_existing):
        """Create an organization and its first administrator."""
        init_db()
        email = admin_email.strip().lower()
        if User.query.filter(db.func.lower(User.email) == email).first():
            raise SystemExit(f"A user with email {email} already exists.")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters.")

        org = Organization(name=name.strip())
        db.session.add(org)
        db.session.flush()

        auth.create_user(
            organization_id=org.id, email=email, name=admin_name,
            role="administrator", password=password,
            is_superuser=superuser, must_change_password=True,
        )

        adopted = {}
        if adopt_existing:
            # A database that predates multi-tenancy has exactly one customer
            # in it; claim those rows so nothing disappears after the upgrade.
            for model in (Facility, FormatMapping, ImportReceipt, ImportJob, IntegrationConfig):
                n = model.query.filter(model.organization_id.is_(None)).update(
                    {"organization_id": org.id}, synchronize_session=False)
                adopted[model.__tablename__] = n
        db.session.commit()

        print(f"Organization {org.name!r} created (id {org.id}).")
        print(f"Administrator {email} created. Password change required at first sign-in.")
        if adopted:
            print("Adopted existing rows: "
                  + ", ".join(f"{k}={v}" for k, v in adopted.items() if v))

    @app.cli.command("promote-superuser")
    @click.argument("email")
    def promote_superuser(email):
        """Make an existing account a platform owner."""
        user = User.query.filter(db.func.lower(User.email) == email.strip().lower()).first()
        if user is None:
            raise SystemExit(f"No user with email {email}.")
        user.is_superuser = True
        db.session.commit()
        print(f"{user.email} is now a platform owner (can create and enter client organisations).")

    @app.cli.command("seed-demo")
    @click.option("--name", default="Demo", help="Organisation name")
    @click.option("--admin-email", default="demo@caremin.app")
    @click.option("--password", required=True, help="Demo administrator password (12+ chars)")
    @click.option("--days", default=60, help="Days of shift history to generate")
    def seed_demo(name, admin_email, password, days):
        """Create a self-contained demo organisation with sample care data.

        Deliberately a separate tenant: demo data must never sit alongside a
        real client's records.
        """
        from carelog.demo import build_demo_organization

        init_db()
        email = admin_email.strip().lower()
        if User.query.filter(db.func.lower(User.email) == email).first():
            raise SystemExit(f"A user with email {email} already exists.")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters.")
        summary = build_demo_organization(name=name, admin_email=email,
                                          password=password, days=days)
        print(f"Demo organisation {summary['organization']!r} created.")
        print(f"  administrator: {email} (must change password at first sign-in)")
        print(f"  facility:      {summary['facility']}")
        print(f"  data:          {summary['residents']} residents, {summary['staff']} staff, "
              f"{summary['shifts']} shifts over {days} days")

    @app.route("/")
    def index():
        user = current_user()
        if user and user.is_superuser and not session.get("acting_org_id"):
            return redirect(url_for("platform_console"))
        facility = current_facility()
        if facility and Shift.query.filter_by(facility_id=facility.id).first():
            return redirect(url_for("dashboard"))
        return redirect(url_for("universal_import"))

    @app.route("/settings", methods=["GET", "POST"])
    @require("manage_facility")
    def settings():
        facility = current_facility()

        if request.method == "POST":
            if not has_permission("manage_facility"):
                abort(403)
            name = (request.form.get("facility_name") or "").strip()
            if not name:
                flash("Facility name is required.")
                return redirect(url_for("settings"))
            if not facility:
                facility = Facility(name=name, organization_id=current_organization_id())
                db.session.add(facility)
            facility.name = name
            try:
                facility.ancc_target = float(request.form.get("ancc_target") or facility.ancc_target or 215)
                facility.rn_target = float(request.form.get("rn_target") or facility.rn_target or 44)
            except ValueError:
                flash("Targets must be numbers.")
                return redirect(url_for("settings"))
            auth.record("facility_updated", "facility", facility.id,
                        f"targets {facility.ancc_target}/{facility.rn_target}")
            db.session.commit()
            flash("Facility settings saved.")
            return redirect(url_for("settings"))

        return render_template(
            "settings.html",
            facility=facility,
            organization=db.session.get(Organization, current_organization_id()),
        )

    @app.route("/import", methods=["GET", "POST"])
    @require("import_data")
    def universal_import():
        facility = current_facility()
        if request.method == "GET":
            return render_template(
                "universal_import.html",
                facility=facility,
                facilities=org_facilities(),
                errors=[],
            )

        errors = []
        name = (request.form.get("facility_name") or "").strip()
        files = [f for f in request.files.getlist("data_files") if f and f.filename]
        evidence_type = (request.form.get("evidence_type") or "unverified").strip()
        if evidence_type not in ("worked", "rostered", "unverified"):
            errors.append("Choose a valid staffing evidence type.")

        # With several homes in one organization, the upload must say which one
        # it belongs to rather than silently landing in whichever is active.
        target_id = request.form.get("facility_id", type=int)
        if target_id:
            facility = Facility.query.filter_by(
                id=target_id, organization_id=current_organization_id()).first()
            if facility is None:
                abort(404)
            session["facility_id"] = facility.id

        if not name and not facility:
            errors.append("Facility name is required for the first import.")
        if not files:
            errors.append("Upload at least one file.")
        if errors:
            return render_template(
                "universal_import.html",
                facility=facility,
                facilities=org_facilities(),
                errors=errors,
            ), 400

        if not facility:
            facility = Facility(name=name, organization_id=current_organization_id())
            db.session.add(facility)
            db.session.flush()
            session["facility_id"] = facility.id
        # Commit now: the background worker needs the facility id, and the
        # request must not hold an open transaction while the job runs.
        db.session.commit()

        # The slow work (AI format learning, extraction) happens in a
        # background job so this request returns immediately — no gateway
        # timeout can kill an import, however large the file.
        payloads = [(f.filename, f.read()) for f in files]
        user = current_user()
        job_id = import_jobs.start_job(
            app, facility.id, payloads, _storage(),
            organization_id=current_organization_id(),
            user_id=user.id if user else None,
            evidence_type=evidence_type,
        )
        auth.record("import_started", "import_job", job_id,
                    ", ".join(fname for fname, _ in payloads))
        db.session.commit()
        return redirect(url_for("import_status", job_id=job_id))

    @app.route("/import/run/<job_id>", methods=["POST"])
    def import_run(job_id):
        """Worker entrypoint. On serverless hosts the upload request cannot do
        the work itself, so it calls this to run the job in its own
        invocation."""
        expected = os.environ.get("WORKER_SECRET", "")
        if expected and request.headers.get("x-worker-secret") != expected:
            return {"error": "forbidden"}, 403
        import_jobs.run_job(job_id)
        job = import_jobs.get_job(job_id)
        return (job.as_dict() if job else {"error": "unknown job"}), (200 if job else 404)

    @app.route("/import/status/<job_id>")
    def import_status(job_id):
        job = import_jobs.get_job(job_id)
        if not job:
            flash("That import job no longer exists. Check the audit trail for the result.")
            return redirect(url_for("universal_import"))
        return render_template("import_status.html", job=owned_or_404(job).as_dict())

    @app.route("/import/status/<job_id>.json")
    def import_status_json(job_id):
        job = import_jobs.get_job(job_id)
        if not job:
            return {"status": "gone"}, 404
        return owned_or_404(job).as_dict()

    @app.route("/import/summary/<int:receipt_id>")
    @require("view_audit")
    def import_summary(receipt_id):
        receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
        outcomes = json.loads(receipt.summary_json) if receipt.summary_json else []
        return render_template(
            "import_summary.html",
            facility=current_facility(),
            outcomes=outcomes,
            receipt=receipt,
        )

    def _latest_data_date(facility):
        latest = Shift.query.filter_by(
            facility_id=facility.id, evidence_type="worked"
        ).order_by(Shift.date.desc()).first()
        if latest is None:
            latest = Shift.query.filter_by(facility_id=facility.id).order_by(Shift.date.desc()).first()
        return latest.date if latest else date.today()

    @app.route("/compliance")
    @require("view_dashboard")
    def compliance_view():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        today = _latest_data_date(facility)
        fc = forecast_quarter(facility, today)
        q_start, _ = quarter_bounds(today)
        return render_template(
            "compliance.html",
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

    @app.route("/scenarios", methods=["GET", "POST"])
    @require("run_scenarios")
    def scenarios():
        facility = current_facility()
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
    @require("export_data")
    def export_daily_csv():
        facility = current_facility()
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
    @require("export_data")
    def export_summary_xlsx():
        facility = current_facility()
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
    @require("export_data")
    def board_report():
        facility = current_facility()
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
    @require("view_audit")
    def audit():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        receipts = (
            ImportReceipt.query.filter_by(facility_id=facility.id)
            .order_by(ImportReceipt.imported_at.desc()).all()
        )
        files_by_receipt = {}
        for r in receipts:
            names = [o.name for o in _receipt_files(r)]
            if names:
                files_by_receipt[r.id] = names
        with_files = sum(1 for r in receipts if r.id in files_by_receipt)
        return render_template(
            "audit.html",
            facility=facility,
            receipts=receipts,
            files_by_receipt=files_by_receipt,
            readiness=round(with_files / len(receipts) * 100) if receipts else None,
            calc_version=CALC_VERSION,
        )

    @app.route("/audit/file/<int:receipt_id>/<path:filename>")
    @require("download_evidence")
    def audit_file(receipt_id, filename):
        receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
        safe = os.path.basename(filename)
        match = next((o for o in _receipt_files(receipt) if o.name == safe), None)
        if not match:
            return ("Not found", 404)
        try:
            blob = _storage_for(receipt).get(match.key)
        except StorageError as e:
            app.logger.warning("Audit file fetch failed: %s", e)
            return ("That evidence file could not be retrieved from storage.", 502)
        return Response(
            blob,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={safe}"},
        )

    @app.route("/integrations", methods=["GET", "POST"])
    @require("manage_integrations")
    def integrations_view():
        facility = current_facility()
        configs = {c.platform: c for c in IntegrationConfig.query.filter_by(
            organization_id=current_organization_id()).all()}

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
                        cfg = IntegrationConfig.query.filter_by(
                            platform=platform, organization_id=current_organization_id()).first()
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
    @require("export_data")
    def quarterly_report():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
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
            os.path.join(ROOT, "sample_data"),
            filename,
            as_attachment=True,
        )

    @app.route("/dashboard")
    @require("view_dashboard")
    def dashboard():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))

        latest_shift = Shift.query.filter_by(
            facility_id=facility.id, evidence_type="worked"
        ).order_by(Shift.date.desc()).first()
        if latest_shift is None:
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

        resident_count = active_residents_on(facility.id, today)

        # Phase 6 additions
        pct = compliance_pct(facility, today)
        coverage = rn_coverage(facility.id, today)
        alerts = detect_gaps(facility, today)
        last_receipt = _latest_receipt(facility)
        data_age_days = (
            (date.today() - last_receipt.imported_at.date()).days if last_receipt else None
        )
        receipts = ImportReceipt.query.filter_by(facility_id=facility.id).all()
        with_files = sum(1 for r in receipts if _receipt_files(r))
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
            evidence=evidence_summary(facility.id, q_start, today),
        )

    @app.route("/facility", methods=["GET"])
    @require("view_dashboard")
    def facility_view():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        residents = Resident.query.filter_by(facility_id=facility.id).order_by(Resident.name).all()
        staff = Staff.query.filter_by(facility_id=facility.id).order_by(Staff.role, Staff.name).all()
        return render_template(
            "facility.html", facility=facility, residents=residents, staff=staff,
            resident_day_count=ResidentDay.query.filter_by(facility_id=facility.id).count(),
            care_episode_count=CareEpisode.query.filter_by(facility_id=facility.id).count(),
        )

    @app.route("/facility/targets", methods=["POST"])
    @require("manage_facility")
    def update_targets():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        try:
            facility.ancc_target = float(request.form.get("ancc_target") or facility.ancc_target)
            facility.rn_target = float(request.form.get("rn_target") or facility.rn_target)
            db.session.commit()
        except ValueError:
            flash("Targets must be numbers.")
        return redirect(url_for("dashboard"))

    def _debug_allowed() -> bool:
        """Diagnostics expose configuration and spend real API calls, so they
        are for the operator only: a signed-in superuser, or a caller holding
        DEBUG_TOKEN (which is how the setup script checks a fresh deploy)."""
        token = os.environ.get("DEBUG_TOKEN")
        if token and request.headers.get("x-debug-token") == token:
            return True
        user = current_user()
        return bool(user and user.is_superuser)

    @app.route("/debug/network")
    def debug_network():
        """Server-side connectivity self-test for the Anthropic API — visit
        after a deploy to see what this machine's network can actually reach.
        Sends no credentials; every probe is capped at a few seconds."""
        if not _debug_allowed():
            abort(404)
        import socket
        import time as _time

        import httpx

        report = {"env": {
            k: os.environ[k]
            for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "ANTHROPIC_BASE_URL")
            if os.environ.get(k)
        }}

        def timed(fn):
            t = _time.monotonic()
            try:
                detail = fn()
                return {"ok": True, "detail": detail, "seconds": round(_time.monotonic() - t, 2)}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "seconds": round(_time.monotonic() - t, 2)}

        def dns():
            infos = socket.getaddrinfo("api.anthropic.com", 443, proto=socket.IPPROTO_TCP)
            return sorted({i[4][0] for i in infos})

        report["dns"] = timed(dns)

        report["tcp"] = {}
        for addr in (report["dns"].get("detail") or []):
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET

            def connect(addr=addr, fam=fam):
                with socket.socket(fam, socket.SOCK_STREAM) as s:
                    s.settimeout(4)
                    s.connect((addr, 443))
                return "connected"

            report["tcp"][addr] = timed(connect)

        def https(url, ipv4_only=False):
            transport = httpx.HTTPTransport(local_address="0.0.0.0") if ipv4_only else None
            with httpx.Client(timeout=6, transport=transport) as c:
                return f"HTTP {c.get(url).status_code}"

        report["https_default"] = timed(lambda: https("https://api.anthropic.com/v1/models"))
        report["https_ipv4_pinned"] = timed(lambda: https("https://api.anthropic.com/v1/models", ipv4_only=True))
        report["https_control_google"] = timed(lambda: https("https://www.google.com"))

        # The decisive probe: a real, authenticated 1-token POST /v1/messages —
        # the exact call an import makes, minus the payload. Uses the stored
        # key server-side; the key never appears in the output.
        from ingestion.analyzer import resolve_api_key, resolve_model

        api_key = resolve_api_key()
        if not api_key:
            report["messages_post"] = {"ok": False, "error": "no API key configured"}
        else:
            def messages_post(streaming):
                payload = {
                    "model": resolve_model(),
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }
                if streaming:
                    payload["stream"] = True
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
                with httpx.Client(timeout=20, transport=transport) as c:
                    r = c.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
                    body = r.text[:120] if r.status_code != 200 else "OK"
                    return f"HTTP {r.status_code}: {body}"

            report["messages_post"] = timed(lambda: messages_post(streaming=False))
            report["messages_post_stream"] = timed(lambda: messages_post(streaming=True))
        return report

    @app.route("/debug/storage")
    def debug_storage():
        """Self-test the database and object storage from inside the running
        instance. Visit after deploying to a new platform."""
        if not _debug_allowed():
            abort(404)
        import time as _time

        report = {"worker_mode": import_jobs.worker_mode()}

        def timed(fn):
            t = _time.monotonic()
            try:
                return {"ok": True, "detail": fn(), "seconds": round(_time.monotonic() - t, 2)}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "seconds": round(_time.monotonic() - t, 2)}

        def database():
            from sqlalchemy import inspect, text

            db.session.execute(text("SELECT 1"))
            tables = sorted(inspect(db.engine).get_table_names())
            missing = sorted({t.name for t in db.metadata.sorted_tables} - set(tables))
            return {
                "dialect": db.engine.dialect.name,
                "url": db.engine.url.render_as_string(hide_password=True),
                "tables": tables,
                "missing_tables": missing or None,
            }

        def storage_roundtrip():
            store = _storage()
            key = f"diagnostics/selftest-{int(_time.time())}.txt"
            payload = b"carelog storage self-test"
            store.put(key, payload, content_type="text/plain")
            got = store.get(key)
            listed = [o.name for o in store.list("diagnostics")]
            return {
                "config": store.describe(),
                "round_trip": "OK" if got == payload else f"MISMATCH ({len(got)} bytes back)",
                "listing_works": bool(listed),
            }

        report["database"] = timed(database)
        report["storage"] = timed(storage_roundtrip)
        return report

    @app.route("/clear", methods=["POST"])
    @require("manage_facility")
    def clear():
        """Delete this organization's care data, including retained evidence."""
        if not _confirmed(request):
            flash('Type "delete" to confirm. Nothing was deleted.')
            return redirect(url_for("settings"))

        org_id = current_organization_id()
        facilities = Facility.query.filter_by(organization_id=org_id).all()
        facility_ids = [f.id for f in facilities]
        receipts = (
            ImportReceipt.query.filter(ImportReceipt.facility_id.in_(facility_ids)).all()
            if facility_ids else []
        )
        files = _purge_evidence(receipts)
        if facility_ids:
            _delete_facility_data(facility_ids)
            Facility.query.filter(Facility.id.in_(facility_ids)).delete(
                synchronize_session=False)
        auth.record("data_cleared", "organization", org_id,
                    f"{len(facility_ids)} facility/facilities, {files} evidence file(s)")
        db.session.commit()
        flash(f"Deleted all care data for your organization "
              f"({len(facility_ids)} facility/facilities, {files} evidence file(s)).")
        return redirect(url_for("universal_import"))

    @app.route("/audit/receipt/<int:receipt_id>/delete", methods=["POST"])
    @require("manage_facility")
    def delete_receipt(receipt_id):
        """Delete one import: its rows, its receipt and its retained files."""
        receipt = owned_or_404(db.session.get(ImportReceipt, receipt_id))
        if not _confirmed(request):
            flash('Type "delete" to confirm. Nothing was deleted.')
            return redirect(url_for("audit"))

        files = _purge_evidence([receipt])
        shifts = Shift.query.filter_by(import_receipt_id=receipt.id).delete(
            synchronize_session=False)
        resident_days = ResidentDay.query.filter_by(import_receipt_id=receipt.id).delete(
            synchronize_session=False)
        episodes = CareEpisode.query.filter_by(import_receipt_id=receipt.id).delete(
            synchronize_session=False)
        ImportJob.query.filter_by(receipt_id=receipt.id).update(
            {"receipt_id": None}, synchronize_session=False)
        db.session.delete(receipt)
        auth.record("import_deleted", "import_receipt", receipt_id,
                    f"{shifts} shifts, {resident_days} resident days, {episodes} care episodes, "
                    f"{files} evidence file(s)")
        db.session.commit()
        flash(f"Import #{receipt_id} deleted: {shifts} shift row(s), {resident_days} "
              f"resident-day row(s), {episodes} care episode(s) and {files} evidence file(s) removed.")
        return redirect(url_for("audit"))

    # ------------------------------------------------------------ facilities

    @app.route("/facilities")
    @require("view_dashboard")
    def facilities_view():
        facilities = org_facilities()
        active = current_facility()
        summary = {}
        for f in facilities:
            latest = (
                Shift.query.filter_by(facility_id=f.id)
                .order_by(Shift.date.desc()).first()
            )
            summary[f.id] = {
                "shifts": Shift.query.filter_by(facility_id=f.id).count(),
                "residents": Resident.query.filter_by(
                    facility_id=f.id, discharged_date=None).count(),
                "latest": latest.date if latest else None,
            }
        return render_template(
            "facilities.html",
            facilities=facilities,
            active=active,
            summary=summary,
            organization=db.session.get(Organization, current_organization_id()),
        )

    @app.route("/facilities/switch", methods=["POST"])
    @require("view_dashboard")
    def facility_switch():
        facility = set_active_facility(request.form.get("facility_id", type=int))
        if facility is None:
            abort(404)
        flash(f"Now viewing {facility.name}.")
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.route("/facilities/create", methods=["POST"])
    @require("manage_facility")
    def facility_create():
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Facility name is required.")
            return redirect(url_for("facilities_view"))
        existing = Facility.query.filter_by(
            organization_id=current_organization_id(), name=name).first()
        if existing:
            flash(f"{name} already exists.")
            return redirect(url_for("facilities_view"))
        facility = Facility(name=name, organization_id=current_organization_id())
        try:
            facility.ancc_target = float(request.form.get("ancc_target") or 215)
            facility.rn_target = float(request.form.get("rn_target") or 44)
        except ValueError:
            flash("Targets must be numbers.")
            return redirect(url_for("facilities_view"))
        db.session.add(facility)
        db.session.flush()
        auth.record("facility_created", "facility", facility.id, name)
        db.session.commit()
        session["facility_id"] = facility.id
        flash(f"{name} created and selected.")
        return redirect(url_for("facilities_view"))

    @app.route("/facilities/<int:facility_id>/update", methods=["POST"])
    @require("manage_facility")
    def facility_update(facility_id):
        facility = Facility.query.filter_by(
            id=facility_id, organization_id=current_organization_id()).first()
        if facility is None:
            abort(404)
        name = (request.form.get("name") or "").strip()
        if name:
            facility.name = name
        try:
            facility.ancc_target = float(request.form.get("ancc_target") or facility.ancc_target)
            facility.rn_target = float(request.form.get("rn_target") or facility.rn_target)
        except ValueError:
            flash("Targets must be numbers.")
            return redirect(url_for("facilities_view"))
        auth.record("facility_updated", "facility", facility.id,
                    f"{facility.name} targets {facility.ancc_target}/{facility.rn_target}")
        db.session.commit()
        flash(f"{facility.name} updated.")
        return redirect(url_for("facilities_view"))

    @app.route("/facilities/<int:facility_id>/delete", methods=["POST"])
    @require("manage_facility")
    def facility_delete(facility_id):
        facility = Facility.query.filter_by(
            id=facility_id, organization_id=current_organization_id()).first()
        if facility is None:
            abort(404)
        if not _confirmed(request):
            flash('Type "delete" to confirm. Nothing was deleted.')
            return redirect(url_for("facilities_view"))

        name = facility.name
        files = _purge_evidence(
            ImportReceipt.query.filter_by(facility_id=facility.id).all())
        _delete_facility_data([facility.id])
        db.session.delete(facility)
        auth.record("facility_deleted", "facility", facility_id,
                    f"{name}, {files} evidence file(s)")
        db.session.commit()
        session.pop("facility_id", None)
        flash(f"{name} deleted, along with its care data and "
              f"{files} evidence file(s).")
        return redirect(url_for("facilities_view"))

    # ------------------------------------------------- eligibility exceptions

    @app.route("/eligibility")
    @require("view_audit")
    def eligibility_queue():
        """Staff whose minutes are being withheld until someone confirms them."""
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        rows = (
            Staff.query.filter_by(facility_id=facility.id)
            .order_by(Staff.eligibility_status, Staff.name).all()
        )
        buckets = {"pending": [], "excluded": [], "approved": [], "blocked": []}
        latest_day = _latest_data_date(facility)
        for st in rows:
            bucket = (
                "blocked" if st.eligibility_status == el.APPROVED
                and el.registration_problem(st, latest_day) else st.eligibility_status
            )
            buckets.setdefault(bucket, []).append(st)
        withheld = sum(
            b["excluded_minutes"] for b in
            range_breakdown(facility.id, _latest_data_date(facility) - timedelta(days=29),
                            _latest_data_date(facility))
        )
        return render_template(
            "eligibility.html",
            facility=facility, buckets=buckets, withheld=withheld,
            registration_problem=lambda staff: el.registration_problem(staff, latest_day),
        )

    @app.route("/eligibility/<int:staff_id>", methods=["POST"])
    @require("manage_facility")
    def eligibility_decide(staff_id):
        facility = current_facility()
        member = Staff.query.filter_by(id=staff_id, facility_id=facility.id).first() \
            if facility else None
        if member is None:
            abort(404)
        decision = (request.form.get("decision") or "").strip()
        if decision not in (el.APPROVED, el.EXCLUDED, el.PENDING):
            abort(400)

        mapped_role = (request.form.get("mapped_role") or member.role or "OTHER").strip().upper()
        if mapped_role not in ("RN", "EN", "PCW", "PCA", "AIN", "OTHER"):
            abort(400)
        if mapped_role in ("PCA", "AIN"):
            mapped_role = "PCW"

        def optional_date(field):
            value = (request.form.get(field) or "").strip()
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                abort(400)

        member.eligibility_status = decision
        member.role = mapped_role
        reason = (request.form.get("reason") or "").strip()
        if decision in (el.APPROVED, el.EXCLUDED) and not reason:
            flash("Record a basis for an approval or exclusion.")
            return redirect(url_for("eligibility_queue"))
        member.eligibility_reason = reason or f"left {decision} by an administrator"
        member.registration_number = (request.form.get("registration_number") or "").strip() \
            or member.registration_number
        member.registration_expiry = optional_date("registration_expiry")
        member.eligible_from = optional_date("eligible_from")
        member.eligible_to = optional_date("eligible_to")
        member.employment_type = (request.form.get("employment_type") or "").strip() \
            or member.employment_type

        problem = el.registration_problem(member, member.eligible_from or date.today())
        if decision == el.APPROVED and problem:
            flash(f"Cannot approve: {problem}.")
            return redirect(url_for("eligibility_queue"))
        actor = current_user()
        member.approved_by_user_id = actor.id if actor else None
        member.approved_at = datetime.utcnow()
        auth.record("eligibility_decided", "staff", member.id,
                    f"{member.name} ({member.source_role or member.role}) -> {member.role}/{decision}: "
                    f"{member.eligibility_reason}")
        db.session.commit()
        flash(f"{member.name} marked {decision}. Care minutes recalculated on next view.")
        return redirect(url_for("eligibility_queue"))

    # ------------------------------------------------- platform owner console

    def _platform_org_summary(org):
        facilities = Facility.query.filter_by(organization_id=org.id).all()
        facility_ids = [f.id for f in facilities]
        latest = (
            ImportReceipt.query.filter_by(organization_id=org.id)
            .order_by(ImportReceipt.imported_at.desc()).first()
        )
        return {
            "org": org,
            "users": User.query.filter_by(
                organization_id=org.id, is_active=True, is_superuser=False
            ).count(),
            "facilities": len(facility_ids),
            "shifts": (
                Shift.query.filter(Shift.facility_id.in_(facility_ids)).count()
                if facility_ids else 0
            ),
            "residents": (
                Resident.query.filter(
                    Resident.facility_id.in_(facility_ids),
                    Resident.discharged_date.is_(None),
                ).count() if facility_ids else 0
            ),
            "last_import": latest.imported_at if latest else None,
            "admins": User.query.filter_by(
                organization_id=org.id, role="administrator", is_active=True,
                is_superuser=False,
            ).all(),
        }

    @app.route("/platform")
    @auth.superuser_required
    def platform_console():
        """Cross-organization view. This is the only place in the application
        that deliberately reads across the tenant boundary."""
        rows = [
            _platform_org_summary(org)
            for org in Organization.query.order_by(Organization.name).all()
        ]
        return render_template(
            "platform.html",
            rows=rows,
            total_users=User.query.filter_by(
                is_active=True, is_superuser=False
            ).count(),
            total_facilities=Facility.query.count(),
            recent_entries=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all(),
        )

    @app.route("/platform/organizations/<int:organization_id>")
    @auth.superuser_required
    def platform_organization(organization_id):
        org = db.session.get(Organization, organization_id)
        if org is None:
            abort(404)
        facilities = Facility.query.filter_by(
            organization_id=org.id
        ).order_by(Facility.name).all()
        facility_rows = []
        for facility in facilities:
            latest = Shift.query.filter_by(facility_id=facility.id).order_by(
                Shift.date.desc()
            ).first()
            facility_rows.append({
                "facility": facility,
                "residents": Resident.query.filter_by(
                    facility_id=facility.id, discharged_date=None
                ).count(),
                "shifts": Shift.query.filter_by(facility_id=facility.id).count(),
                "latest": latest.date if latest else None,
            })
        return render_template(
            "platform_organization.html",
            summary=_platform_org_summary(org),
            facility_rows=facility_rows,
            users=User.query.filter_by(
                organization_id=org.id, is_superuser=False
            ).order_by(User.is_active.desc(), User.email).all(),
            imports=(
                ImportReceipt.query.filter_by(organization_id=org.id)
                .order_by(ImportReceipt.imported_at.desc()).limit(10).all()
            ),
            integrations=IntegrationConfig.query.filter_by(
                organization_id=org.id
            ).order_by(IntegrationConfig.platform).all(),
            recent_entries=(
                AuditLog.query.filter_by(organization_id=org.id)
                .order_by(AuditLog.created_at.desc()).limit(12).all()
            ),
        )

    @app.route("/platform/organizations/<int:organization_id>/update", methods=["POST"])
    @auth.superuser_required
    def platform_update_organization(organization_id):
        org = db.session.get(Organization, organization_id)
        if org is None:
            abort(404)
        name = (request.form.get("name") or "").strip()
        timezone = (request.form.get("timezone") or "").strip()
        if not name:
            flash("Organisation name is required.")
        elif Organization.query.filter(
            db.func.lower(Organization.name) == name.lower(),
            Organization.id != org.id,
        ).first():
            flash(f"An organisation called {name} already exists.")
        else:
            org.name = name
            if timezone:
                org.timezone = timezone
            auth.record("platform_org_updated", "organization", org.id, name)
            db.session.commit()
            flash(f"{org.name} updated.")
        return redirect(url_for("platform_organization", organization_id=org.id))

    @app.route("/platform/activity")
    @auth.superuser_required
    def platform_activity():
        entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(1000).all()
        organizations = {o.id: o for o in Organization.query.all()}
        return render_template(
            "platform_activity.html", entries=entries, organizations=organizations
        )

    @app.route("/platform/system")
    @auth.superuser_required
    def platform_system():
        try:
            storage_status = _storage().describe()
        except StorageError as error:
            storage_status = {"backend": "unavailable", "error": str(error)}
        return render_template(
            "platform_system.html",
            database=db.engine.url.render_as_string(hide_password=True),
            schema_drift=_schema_drift(),
            storage_status=storage_status,
            worker_mode=import_jobs.worker_mode(),
            mapping_service_ready=ai_ready(),
            mapping_model=resolve_model(),
            calc_version=CALC_VERSION,
        )

    @app.route("/platform/organizations/create", methods=["POST"])
    @auth.superuser_required
    def platform_create_org():
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("admin_email") or "").strip().lower()
        admin_name = (request.form.get("admin_name") or "").strip()
        password = request.form.get("password") or ""

        if not name:
            flash("Organisation name is required.")
        elif Organization.query.filter(db.func.lower(Organization.name) == name.lower()).first():
            flash(f"An organisation called {name} already exists.")
        elif not email or "@" not in email:
            flash("A valid administrator email is required.")
        elif User.query.filter(db.func.lower(User.email) == email).first():
            flash("A user with that email already exists.")
        elif (problem := auth.password_problem(password, password)):
            flash(problem)
        else:
            org = Organization(name=name)
            db.session.add(org)
            db.session.flush()
            user = auth.create_user(
                organization_id=org.id, email=email, name=admin_name,
                role="administrator", password=password, must_change_password=True,
            )
            db.session.flush()
            auth.record("platform_org_created", "organization", org.id,
                        f"{name} with administrator {email}")
            db.session.commit()
            flash(f"{name} created with {user.email} as its administrator. "
                  "They must change the password at first sign-in.")
        return redirect(url_for("platform_console"))

    @app.route("/platform/act", methods=["POST"])
    @auth.superuser_required
    def platform_act():
        organization_id = request.form.get("organization_id", type=int)
        if organization_id is None:
            abort(400)
        org = auth.act_as_organization(organization_id)
        db.session.commit()
        flash(f"Support workspace opened for {org.name}.")
        return redirect(url_for("index"))

    @app.route("/platform/act/stop", methods=["POST"])
    @auth.superuser_required
    def platform_stop_acting():
        auth.act_as_organization(None)
        db.session.commit()
        flash("Returned to platform administration.")
        return redirect(url_for("platform_console"))

    # ------------------------------------------------------------- admin UI

    @app.route("/admin/users")
    @require("manage_users")
    def admin_users():
        org_id = current_organization_id()
        users = (
            User.query.filter_by(organization_id=org_id, is_superuser=False)
            .order_by(User.is_active.desc(), User.email)
            .all()
        )
        return render_template(
            "admin_users.html",
            users=users,
            roles=ROLES,
            organization=db.session.get(Organization, org_id),
        )

    @app.route("/admin/users/create", methods=["POST"])
    @require("manage_users")
    def admin_create_user():
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("name") or "").strip()
        role = (request.form.get("role") or "read_only").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not email or "@" not in email:
            flash("A valid email address is required.")
        elif role not in ROLES:
            flash("Unknown role.")
        elif User.query.filter(db.func.lower(User.email) == email).first():
            flash("A user with that email already exists.")
        elif (problem := auth.password_problem(password, confirm)):
            flash(problem)
        else:
            user = auth.create_user(
                organization_id=current_organization_id(),
                email=email, name=name, role=role, password=password,
                must_change_password=True,
            )
            db.session.flush()
            auth.record("user_created", "user", user.id, f"{email} as {role}")
            db.session.commit()
            flash(f"{email} added as {ROLES[role]['label']}. They must change the password at first sign-in.")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/update", methods=["POST"])
    @require("manage_users")
    def admin_update_user(user_id):
        user = User.query.filter_by(
            id=user_id, organization_id=current_organization_id(), is_superuser=False
        ).first()
        if user is None:
            abort(404)

        actor = current_user()
        action = request.form.get("action")
        if action == "set_role":
            role = (request.form.get("role") or "").strip()
            if role not in ROLES:
                flash("Unknown role.")
            elif user.id == actor.id and role != "administrator":
                # Otherwise the last administrator can lock everyone out.
                flash("You cannot remove your own administrator access.")
            else:
                user.role = role
                auth.record("user_role_changed", "user", user.id, role)
                flash(f"{user.email} is now {ROLES[role]['label']}.")
        elif action == "deactivate":
            if user.id == actor.id:
                flash("You cannot deactivate your own account.")
            else:
                user.is_active = False
                auth.record("user_deactivated", "user", user.id)
                flash(f"{user.email} can no longer sign in.")
        elif action == "reactivate":
            user.is_active = True
            auth.record("user_reactivated", "user", user.id)
            flash(f"{user.email} can sign in again.")
        elif action == "reset_password":
            password = request.form.get("password") or ""
            problem = auth.password_problem(password, password)
            if problem:
                flash(problem)
            else:
                from werkzeug.security import generate_password_hash

                user.password_hash = generate_password_hash(password)
                user.must_change_password = True
                auth.record("password_reset", "user", user.id)
                flash(f"Password reset for {user.email}; they must change it at next sign-in.")
        db.session.commit()
        return redirect(url_for("admin_users"))

    return app


def org_facilities():
    """Every facility belonging to the signed-in user's organization."""
    return (
        Facility.query
        .filter_by(organization_id=current_organization_id())
        .order_by(Facility.name)
        .all()
    )


def current_facility():
    """The facility the user is currently working in.

    Organizations can run several homes, so the active one is held in the
    session. The id is re-checked against the organization on every request —
    a session value is user-supplied data and must never be trusted to select
    a row on its own.
    """
    facilities = org_facilities()
    if not facilities:
        return None
    chosen = session.get("facility_id")
    for f in facilities:
        if f.id == chosen:
            return f
    return facilities[0]


def set_active_facility(facility_id: int) -> Facility | None:
    facility = Facility.query.filter_by(
        id=facility_id, organization_id=current_organization_id()
    ).first()
    if facility:
        session["facility_id"] = facility.id
    return facility


def _org_formats():
    return (
        FormatMapping.query
        .filter_by(organization_id=current_organization_id())
        .order_by(FormatMapping.created_at.desc())
        .all()
    )


def _storage():
    from flask import current_app

    return build_storage(current_app.config["UPLOADS_DIR"])


def _storage_for(receipt):
    """Receipts written before object storage recorded an absolute directory
    on the machine's disk. Serve those from the filesystem so the existing
    audit trail keeps working after the migration."""
    if receipt.source_path and os.path.isabs(receipt.source_path):
        return LocalStorage(os.path.dirname(receipt.source_path))
    return _storage()


def _confirmed(req) -> bool:
    """Destructive actions require the word typed out, not just a click."""
    return (req.form.get("confirm") or "").strip().lower() == "delete"


def _delete_facility_data(facility_ids):
    """Remove care data for facilities, in an order the foreign keys allow.

    Shifts reference both staff and receipts, and import jobs reference
    receipts — so receipts cannot go first. Postgres enforces this even though
    SQLite historically did not.
    """
    Shift.query.filter(Shift.facility_id.in_(facility_ids)).delete(
        synchronize_session=False)
    ResidentDay.query.filter(ResidentDay.facility_id.in_(facility_ids)).delete(
        synchronize_session=False)
    CareEpisode.query.filter(CareEpisode.facility_id.in_(facility_ids)).delete(
        synchronize_session=False)
    ImportJob.query.filter(ImportJob.facility_id.in_(facility_ids)).delete(
        synchronize_session=False)
    ImportReceipt.query.filter(ImportReceipt.facility_id.in_(facility_ids)).delete(
        synchronize_session=False)
    for model in (Staff, Resident):
        model.query.filter(model.facility_id.in_(facility_ids)).delete(
            synchronize_session=False)


def _purge_evidence(receipts) -> int:
    """Delete retained source files for the given receipts.

    Storage failures are logged rather than raised: the database deletion is
    the part the user asked for, and a leftover blob must not block it.
    """
    from flask import current_app

    removed = 0
    for receipt in receipts:
        if not receipt.source_path:
            continue
        try:
            store = _storage_for(receipt)
            removed += store.delete([o.key for o in _receipt_files(receipt)])
        except StorageError as e:
            current_app.logger.warning(
                "Could not delete evidence for receipt %s: %s", receipt.id, e)
    return removed


def _receipt_files(receipt):
    """Evidence files retained for a receipt, whichever storage holds them."""
    if not receipt.source_path:
        return []
    prefix = (
        os.path.basename(receipt.source_path.rstrip("/"))
        if os.path.isabs(receipt.source_path)
        else receipt.source_path
    )
    try:
        return _storage_for(receipt).list(prefix)
    except StorageError:
        return []


def _database_uri() -> tuple[str, str]:
    """Choose the connected database, with a local SQLite fallback.

    Vercel's Neon Storage integration provides ``STORAGE_DATABASE_URL``. Use
    it first on Vercel so a stale manually-created ``DATABASE_URL`` cannot
    disconnect a production deployment. Other hosts continue to prefer the
    conventional ``DATABASE_URL``.
    """
    vercel_url = (os.environ.get("STORAGE_DATABASE_URL") or "").strip()
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    candidates = (
        (vercel_url, database_url)
        if os.environ.get("VERCEL")
        else (database_url, vercel_url)
    )
    url = next(
        (
            value for value in candidates
            if value and value not in {"[SENSITIVE]", "STORAGE_DATABASE_URL"}
        ),
        "",
    )
    if not url:
        return ""
    # Managed providers still hand out the legacy postgres:// scheme, and
    # psycopg3 is the driver that installs cleanly on serverless images.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _engine_options(uri: str) -> dict:
    if uri.startswith("sqlite"):
        # Test and one-off migration apps use SQLite; its driver does not
        # accept PostgreSQL's connect_timeout option.
        return {}
    opts = {
        # Connections do not survive between serverless invocations, and a
        # pooled connection can be closed by the provider at any time.
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "connect_args": {"connect_timeout": 10},
    }
    if os.environ.get("VERCEL"):
        from sqlalchemy.pool import NullPool

        opts["poolclass"] = NullPool
        opts.pop("pool_pre_ping", None)
        opts.pop("pool_recycle", None)
    return opts


def _schema_drift() -> list[str] | None:
    """Columns the models expect that the database does not have."""
    try:
        from sqlalchemy import inspect

        inspector = inspect(db.engine)
        present = set(inspector.get_table_names())
        missing = []
        for table in db.metadata.sorted_tables:
            if table.name not in present:
                missing.append(f"{table.name} (whole table)")
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
        return missing or None
    except Exception as e:
        return [f"could not inspect schema: {type(e).__name__}"]


def _config_problems(uri: str) -> list[str]:
    """Deployment mistakes worth refusing to start on, phrased as instructions.

    Postgres is the only supported database. There is deliberately no local
    file fallback: it behaved differently from production on booleans, dates
    and constraints, and on a serverless host it either crashed on a read-only
    disk or silently discarded every write.
    """
    problems = []
    if not uri:
        problems.append(
            "DATABASE_URL is not set for this environment. CareMin stores "
            "everything in Postgres. On Vercel, open Project Settings, then "
            "Environment Variables. Confirm Preview and Production are configured "
            "separately, so a variable ticked only for Production leaves preview "
            "builds without a database. Locally, run `docker compose up -d` and "
            "copy .env.example to .env."
        )
    if os.environ.get("VERCEL") and not os.environ.get("SECRET_KEY"):
        problems.append(
            "SECRET_KEY is not set, so sessions would be signed with the shared "
            "development key and anyone could forge a login. Set it in the same "
            "place as DATABASE_URL."
        )
    return problems


def _auto_init_db(uri: str) -> bool:
    """Create missing tables at start-up.

    Off by default on serverless, where every cold start would repeat the
    round trip and two instances could race; on by default locally so a fresh
    checkout works after `docker compose up`.
    """
    flag = (os.environ.get("AUTO_INIT_DB") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return not os.environ.get("VERCEL")


def init_db():
    """Create any missing tables, then add any columns the models have gained.

    `create_all()` never alters an existing table, so a deployed database keeps
    whatever shape it had when it was created. This adds the gap additively —
    it never drops, renames or retypes anything, so it cannot destroy data. A
    change that needs more than that (dropping a column, changing a type,
    backfilling with logic) needs Alembic.
    """
    db.create_all()
    added = add_missing_columns()
    return added


def add_missing_columns() -> list[str]:
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    dialect = db.engine.dialect
    present = set(inspector.get_table_names())
    added = []

    for table in db.metadata.sorted_tables:
        if table.name not in present:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" ' \
                     f'{column.type.compile(dialect)}'
            default = getattr(column.default, "arg", None)
            if default is not None and not callable(default):
                literal = f"'{default}'" if isinstance(default, str) else str(default)
                clause += f" DEFAULT {literal}"
                if not column.nullable:
                    clause += " NOT NULL"
            # A NOT NULL column with no default cannot be added to a table that
            # already has rows; leave it nullable rather than fail the deploy.
            db.session.execute(text(clause))
            added.append(f"{table.name}.{column.name}")
    if added:
        db.session.commit()
    return added


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
