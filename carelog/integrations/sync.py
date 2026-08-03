"""Connector execution. Currently one live connector: url_fetch.

A sync is just a fetch that feeds the normal ingestion pipeline, so synced
data gets the same fingerprinting, mapping reuse, quality warnings, and audit
lineage as a manual upload.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime

from carelog.models import IntegrationConfig, db

MAX_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = 30


class SyncError(Exception):
    pass


def sync_platform(facility, config: IntegrationConfig):
    """Run a sync for a configured platform. Returns a list of ImportOutcomes."""
    from ingestion.pipeline import ingest_file

    if config.platform != "url_fetch":
        raise SyncError(
            "This platform's direct API needs vendor credentials and is not active yet. "
            "Export from the platform and use Universal Import, or configure a "
            "scheduled file fetch."
        )

    cfg = json.loads(config.config_json or "{}")
    url = (cfg.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise SyncError("Configure a valid http(s) export URL first.")

    filename = url.rsplit("/", 1)[-1].split("?")[0] or "export.csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CareMin/1.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise SyncError(f"Fetch failed: {e}") from e
    if len(data) > MAX_BYTES:
        raise SyncError("Export exceeds the 20 MB sync limit.")

    outcome = ingest_file(facility, filename, data)
    config.last_sync_at = datetime.utcnow()
    config.last_result = (
        f"OK: {outcome.shifts_imported} shifts, {outcome.residents_imported} residents "
        f"({'known format' if outcome.mapping_reused else 'new format learned'})"
    )
    db.session.flush()
    return [outcome]
