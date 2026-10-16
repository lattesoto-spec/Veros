"""Flask API application factory. Page rendering belongs to the separate caremin-frontend repository."""

import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from carelog import auth
from carelog.database import (
    _auto_init_db,
    _config_problems,
    _database_uri,
    _engine_options,
    _schema_drift,
    init_db,
)
from carelog.models import db
from carelog.storage import find_blob_token


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    root = Path(__file__).resolve().parents[1]
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOADS_DIR=os.environ.get("UPLOADS_DIR", str(root / "uploads")),
        PUBLIC_APP_ORIGINS=tuple(
            value.strip().rstrip("/")
            for value in os.environ.get("PUBLIC_APP_ORIGINS", "").split(",")
            if value.strip()
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(
            os.environ.get("VERCEL") or os.environ.get("FORCE_HTTPS")
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=int(
            os.environ.get(
                "MAX_UPLOAD_BYTES",
                4_000_000 if os.environ.get("VERCEL") else 50_000_000,
            )
        ),
    )
    for name in (
        "SUPPORT",
        "HELP",
        "ADMIN",
        "SECURITY",
        "PRIVACY",
        "BILLING",
        "COMPLIANCE",
        "DEMO",
    ):
        app.config[f"{name}_EMAIL"] = os.environ.get(
            f"{name}_EMAIL", f"{name.lower()}@caremin.care"
        )
    if test_config:
        app.config.update(test_config)
    app.config["MFA_ENCRYPTION_KEY"] = auth.resolve_mfa_encryption_key(app.secret_key)
    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or _database_uri()
    problems = _config_problems(uri)
    if uri.startswith("sqlite") and not app.testing:
        problems.append("Postgres is required outside isolated tests.")
    if not problems:
        app.config["SQLALCHEMY_DATABASE_URI"] = uri
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_options(uri)
        db.init_app(app)
        if _auto_init_db(uri):
            with app.app_context():
                try:
                    init_db()
                except Exception as error:
                    app.logger.error(
                        "Database initialization failed: %s", type(error).__name__
                    )
                    problems.append("The database could not be reached or initialized.")

    @app.get("/healthz")
    @app.get("/api/healthz")
    def healthz():
        from carelog.ingestion.jobs import worker_mode
        from carelog.ingestion.xberg_adapter import parser_status

        drift = _schema_drift() if uri and not problems else None
        parser = parser_status()
        ready = not problems and not drift and parser["ready"]
        return jsonify(
            ok=ready,
            problems=problems,
            schema_drift=drift,
            database="configured" if uri else "missing",
            storage="vercel_blob" if find_blob_token()[0] else "local",
            worker=worker_mode(),
            parser=parser,
        ), 200 if ready else 503

    @app.before_request
    def configuration_gate():
        if problems and request.endpoint != "healthz":
            return jsonify(error="Service unavailable", problems=problems), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = error.get_response()
        response.data = app.json.dumps(
            {"error": error.description, "status": error.code}
        )
        response.content_type = "application/json"
        return response

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if app.testing:
            raise error
        db.session.rollback()
        app.logger.exception("Unhandled API error")
        return jsonify(
            error="CareMin could not finish the request. Check the relevant records before retrying."
        ), 500

    if not problems:
        from carelog.api import register_api
        from carelog.cli import register_cli

        auth.init_app(app)
        register_api(app)
        register_cli(app)

    @app.after_request
    def api_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return app
