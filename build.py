"""Run additive database migrations before deploying the Flask API."""

import os


def main() -> int:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("STORAGE_DATABASE_URL")):
        print(
            "[build] No database configured. API will report unavailable at /healthz."
        )
        return 0
    os.environ["AUTO_INIT_DB"] = "0"
    from carelog.app import create_app
    from carelog.database import init_db

    app = create_app()
    try:
        with app.app_context():
            added = init_db()
        print(
            "[build] Added columns: " + ", ".join(added)
            if added
            else "[build] Schema is current."
        )
    except Exception as error:
        # Do not replace a working deployment with code expecting absent columns.
        print(
            f"[build] Migration failed ({type(error).__name__}). Deployment must stop."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
