"""Build-time database migration. Runs as vercel.json buildCommand.

This lives at the repository root on purpose: `.vercelignore` excludes
`scripts/` from the upload, so a build script kept there is not present when
the build runs.

Vercel runs this once per deployment, which is exactly the right moment to
bring the schema up to date: after the new code exists, before any request can
reach it. Doing it at cold start instead would repeat the work on every
instance and let two of them race on the same ALTER.

Failing the build is worse than deploying: if the database is unreachable or
DATABASE_URL is not set for this environment, this reports and exits cleanly so
the app itself can show its "not configured" page rather than nothing at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("STORAGE_DATABASE_URL")):
        print("[build] DATABASE_URL is not set for this environment. Skipping "
              "migration. The deployment will show its configuration page.")
        return 0
    # Let this script own the migration so its output reports what changed;
    # otherwise create_app() would quietly do it first and leave nothing to say.
    os.environ["AUTO_INIT_DB"] = "0"
    try:
        from carelog.app import create_app, init_db

        app = create_app()
        with app.app_context():
            added = init_db()
        if added:
            print("[build] Added columns: " + ", ".join(added))
        else:
            print("[build] Schema already up to date.")
    except Exception as e:  # never block a deploy on this
        print(f"[build] Migration skipped: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
