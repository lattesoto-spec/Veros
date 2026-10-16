import os

from flask import (
    abort,
    request,
)

from carelog.api import bp
from carelog.auth import (
    current_user,
)
from carelog.ingestion import jobs as import_jobs
from carelog.models import (
    db,
)
from carelog.services.workspace import _storage


def _debug_allowed() -> bool:
    """Diagnostics expose configuration and spend real API calls, so they
    are for the operator only: a signed-in superuser, or a caller holding
    DEBUG_TOKEN (which is how the setup script checks a fresh deploy)."""
    token = os.environ.get("DEBUG_TOKEN")
    if token and request.headers.get("x-debug-token") == token:
        return True
    user = current_user()
    return bool(user and user.is_superuser)


@bp.route("/debug/network")
def debug_network():
    """Server-side connectivity self-test for the Anthropic API — visit
    after a deploy to see what this machine's network can actually reach.
    Sends no credentials; every probe is capped at a few seconds."""
    if not _debug_allowed():
        abort(404)
    import socket
    import time as _time

    import httpx

    report = {
        "env": {
            k: os.environ[k]
            for k in (
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "ANTHROPIC_BASE_URL",
            )
            if os.environ.get(k)
        }
    }

    def timed(fn):
        t = _time.monotonic()
        try:
            detail = fn()
            return {
                "ok": True,
                "detail": detail,
                "seconds": round(_time.monotonic() - t, 2),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "seconds": round(_time.monotonic() - t, 2),
            }

    def dns():
        infos = socket.getaddrinfo("api.anthropic.com", 443, proto=socket.IPPROTO_TCP)
        return sorted({i[4][0] for i in infos})

    report["dns"] = timed(dns)

    report["tcp"] = {}
    for addr in report["dns"].get("detail") or []:
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

    report["https_default"] = timed(
        lambda: https("https://api.anthropic.com/v1/models")
    )
    report["https_ipv4_pinned"] = timed(
        lambda: https("https://api.anthropic.com/v1/models", ipv4_only=True)
    )
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
                r = c.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=headers,
                )
                body = r.text[:120] if r.status_code != 200 else "OK"
                return f"HTTP {r.status_code}: {body}"

        report["messages_post"] = timed(lambda: messages_post(streaming=False))
        report["messages_post_stream"] = timed(lambda: messages_post(streaming=True))
    return report


@bp.route("/debug/storage")
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
            return {
                "ok": True,
                "detail": fn(),
                "seconds": round(_time.monotonic() - t, 2),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "seconds": round(_time.monotonic() - t, 2),
            }

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
            "round_trip": "OK"
            if got == payload
            else f"MISMATCH ({len(got)} bytes back)",
            "listing_works": bool(listed),
        }

    report["database"] = timed(database)
    report["storage"] = timed(storage_roundtrip)
    return report
