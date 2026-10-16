import json

from flask import (
    flash,
    request,
    url_for,
)

from carelog.api import bp
from carelog.api.responses import form_data, navigate, resource
from carelog.auth import (
    current_organization_id,
    require,
)
from carelog.ingestion.analyzer import AnalyzerError
from carelog.ingestion.mapping import MappingError
from carelog.ingestion.reader import FileReadError
from carelog.integrations.registry import BY_KEY, PLATFORMS
from carelog.integrations.sync import SyncError, sync_platform
from carelog.models import (
    IntegrationConfig,
    db,
)
from carelog.services.workspace import current_facility


@bp.route("/integrations", methods=["GET", "POST"])
@require("manage_integrations")
def integrations_view():
    facility = current_facility()
    configs = {
        c.platform: c
        for c in IntegrationConfig.query.filter_by(
            organization_id=current_organization_id()
        ).all()
    }

    if request.method == "POST":
        action = form_data().get("action")
        platform = form_data().get("platform", "")
        if platform not in BY_KEY:
            flash("Unknown platform.")
            return navigate(url_for("api.integrations_view"))

        if action == "configure":
            cfg = configs.get(platform) or IntegrationConfig(
                platform=platform, organization_id=current_organization_id()
            )
            cfg.config_json = json.dumps(
                {"url": (form_data().get("url") or "").strip()}
            )
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
                    flash(
                        f"Synced {o.filename}: {o.shifts_imported} shifts, "
                        f"{o.residents_imported} residents."
                    )
                except (SyncError, FileReadError, MappingError, AnalyzerError) as e:
                    db.session.rollback()
                    cfg = IntegrationConfig.query.filter_by(
                        platform=platform, organization_id=current_organization_id()
                    ).first()
                    if cfg:
                        cfg.last_result = f"Failed: {e}"
                        db.session.commit()
                    flash(f"Sync failed: {e}")
        return navigate(url_for("api.integrations_view"))

    return resource(
        "integrations",
        platforms=PLATFORMS,
        configs=configs,
        facility=facility,
    )
