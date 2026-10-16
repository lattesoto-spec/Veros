"""JSON API, independently served from the static React frontend."""

from flask import Blueprint

bp = Blueprint("api", __name__, url_prefix="/api")


def register_api(app):
    from importlib import import_module

    for module in (
        "diagnostics",
        "eligibility",
        "facilities",
        "imports",
        "integrations",
        "monitoring",
        "platform",
        "reports",
        "users",
    ):
        import_module(f"carelog.api.{module}")
    app.register_blueprint(bp)


@bp.get("/session")
def session_info():
    from .responses import resource

    return resource("session")
