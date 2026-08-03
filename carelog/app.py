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

from carelog.domain import exports as export_builders
from carelog.domain.care_minutes import average, daily_stats, quarter_bounds, range_stats
from carelog.domain.compliance import (
    CALC_VERSION,
    compliance_pct,
    detect_gaps,
    forecast_quarter,
    monthly_breakdown,
    range_breakdown,
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
from carelog.ingestion.analyzer import AnalyzerError, DEFAULT_MODEL, ai_ready, resolve_model
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.reader import FileReadError
from carelog.integrations.registry import BY_KEY, PLATFORMS
from carelog.integrations.sync import SyncError, sync_platform
from carelog.models import (
    AuditLog,
    Facility,
    ImportJob,
    FormatMapping,
    ImportReceipt,
    IntegrationConfig,
    Organization,
    Resident,
    Shift,
    Staff,
    User,
    db,
)
from carelog.domain.reports import available_quarters, build_quarterly_pdf
from carelog.storage import LocalStorage, StorageError, build_storage

# Repository root — public/ and sample_data/ live beside the package, not in it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app() -> Flask:
    app = Flask(__name__)
    uri, db_path = _database_uri()
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_options(uri)
    app.config["UPLOADS_DIR"] = os.environ.get(
        "UPLOADS_DIR", os.path.join(os.path.dirname(db_path) or ".", "uploads")
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
    db.init_app(app)
    auth.init_app(app)

    # Creating tables on every cold start is slow and races with itself, so in
    # production the schema is created once by `flask init-db` (see setup.sh).
    # Local SQLite keeps the zero-setup behaviour.
    if _auto_init_db(uri):
        with app.app_context():
            init_db()

    @app.cli.command("init-db")
    def init_db_command():
        """Create missing tables (and patch legacy SQLite columns)."""
        init_db()
        print(f"Schema ready on {db.engine.url.render_as_string(hide_password=True)}")

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
        print(f"Administrator {email} created — must change password at first sign-in.")
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
        facility = current_facility()
        if facility and Shift.query.filter_by(facility_id=facility.id).first():
            return redirect(url_for("dashboard"))
        return redirect(url_for("universal_import"))

    @app.route("/settings", methods=["GET", "POST"])
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
            ai_ready=ai_ready(),
            current_model=resolve_model(),
            default_model=DEFAULT_MODEL,
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
                known_formats=_org_formats(),
                ai_ready=ai_ready(),
            )

        errors = []
        name = (request.form.get("facility_name") or "").strip()
        files = [f for f in request.files.getlist("data_files") if f and f.filename]

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
                known_formats=_org_formats(),
                ai_ready=ai_ready(),
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
        mappings = {m.id: m for m in _org_formats()}
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
            mappings=mappings,
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

    @app.route("/style.css")
    def stylesheet():
        # On Vercel the CDN serves public/ before a request reaches this
        # function; this fallback is what serves it everywhere else.
        return send_from_directory(
            os.path.join(ROOT, "public"), "style.css"
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
        )

    @app.route("/facility", methods=["GET"])
    @require("view_dashboard")
    def facility_view():
        facility = current_facility()
        if not facility:
            return redirect(url_for("universal_import"))
        residents = Resident.query.filter_by(facility_id=facility.id).order_by(Resident.name).all()
        staff = Staff.query.filter_by(facility_id=facility.id).order_by(Staff.role, Staff.name).all()
        return render_template("facility.html", facility=facility, residents=residents, staff=staff)

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
        """Delete this organization's care data. Scoped to the caller's own
        facilities — this route used to empty every table for every tenant."""
        org_id = current_organization_id()
        facility_ids = [f.id for f in Facility.query.filter_by(organization_id=org_id)]
        if facility_ids:
            for model in (Shift, Staff, Resident, ImportReceipt):
                model.query.filter(model.facility_id.in_(facility_ids)).delete(
                    synchronize_session=False)
            ImportJob.query.filter(ImportJob.facility_id.in_(facility_ids)).delete(
                synchronize_session=False)
            Facility.query.filter(Facility.id.in_(facility_ids)).delete(
                synchronize_session=False)
        auth.record("data_cleared", "organization", org_id,
                    f"{len(facility_ids)} facility/facilities")
        db.session.commit()
        flash("All care data for your organization has been deleted.")
        return redirect(url_for("universal_import"))

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
        name = facility.name
        for model in (Shift, Staff, Resident, ImportReceipt):
            model.query.filter_by(facility_id=facility.id).delete(synchronize_session=False)
        ImportJob.query.filter_by(facility_id=facility.id).delete(synchronize_session=False)
        db.session.delete(facility)
        auth.record("facility_deleted", "facility", facility_id, name)
        db.session.commit()
        session.pop("facility_id", None)
        flash(f"{name} and all of its care data have been deleted.")
        return redirect(url_for("facilities_view"))

    # ------------------------------------------------- platform owner console

    @app.route("/platform")
    @auth.superuser_required
    def platform_console():
        """Cross-organization view. This is the only place in the application
        that deliberately reads across the tenant boundary."""
        rows = []
        for org in Organization.query.order_by(Organization.name).all():
            facility_ids = [
                f.id for f in Facility.query.filter_by(organization_id=org.id)
            ]
            latest = (
                ImportReceipt.query.filter_by(organization_id=org.id)
                .order_by(ImportReceipt.imported_at.desc()).first()
            )
            rows.append({
                "org": org,
                "users": User.query.filter_by(organization_id=org.id, is_active=True).count(),
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
                    organization_id=org.id, role="administrator", is_active=True).all(),
            })
        return render_template(
            "platform.html",
            rows=rows,
            roles=ROLES,
            total_users=User.query.filter_by(is_active=True).count(),
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
        org = auth.act_as_organization(request.form.get("organization_id", type=int))
        db.session.commit()
        flash(f"You are now working inside {org.name}.")
        return redirect(url_for("dashboard"))

    @app.route("/platform/act/stop", methods=["POST"])
    @auth.superuser_required
    def platform_stop_acting():
        auth.act_as_organization(None)
        db.session.commit()
        flash("Returned to your own organisation.")
        return redirect(url_for("platform_console"))

    # ------------------------------------------------------------- admin UI

    @app.route("/admin/users")
    @require("manage_users")
    def admin_users():
        org_id = current_organization_id()
        users = (
            User.query.filter_by(organization_id=org_id)
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
            id=user_id, organization_id=current_organization_id()
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

    @app.route("/admin/security-log")
    @require("view_security_log")
    def admin_security_log():
        entries = (
            AuditLog.query
            .filter_by(organization_id=current_organization_id())
            .order_by(AuditLog.created_at.desc())
            .limit(500)
            .all()
        )
        return render_template("admin_security_log.html", entries=entries)

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
    if url:
        # Managed providers still hand out the legacy postgres:// scheme, and
        # psycopg3 is the driver that installs cleanly on serverless images.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url, ""
    db_path = os.environ.get("DATABASE_PATH", os.path.join(ROOT, "caremin.db"))
    return f"sqlite:///{db_path}", db_path


def _engine_options(uri: str) -> dict:
    if uri.startswith("sqlite"):
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


def _auto_init_db(uri: str) -> bool:
    flag = (os.environ.get("AUTO_INIT_DB") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return uri.startswith("sqlite")  # dev convenience only


def init_db():
    """Create any missing tables, then patch legacy SQLite databases.

    Fresh databases (including Postgres) get everything from create_all();
    the column patching only matters for the original SQLite file, which
    predates several columns.
    """
    db.create_all()
    if db.engine.dialect.name == "sqlite":
        _migrate_sqlite(db)


def _migrate_sqlite(db):
    """create_all() never alters existing tables, so add columns introduced
    after the first deploy by hand. Safe to run repeatedly. SQLite only —
    Postgres databases are created complete by create_all()."""
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
            ("organization_id", "INTEGER"),
            ("imported_by_user_id", "INTEGER"),
        ],
        # Multi-tenancy: existing rows keep NULL until `bootstrap-org
        # --adopt-existing` claims them for the first organization.
        "facilities": [("organization_id", "INTEGER")],
        "format_mappings": [("organization_id", "INTEGER")],
        "integration_configs": [("organization_id", "INTEGER")],
        "import_jobs": [
            ("organization_id", "INTEGER"),
            ("started_by_user_id", "INTEGER"),
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
