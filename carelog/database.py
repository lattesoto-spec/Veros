"""Database configuration and additive schema migrations."""

import os

from carelog import auth
from carelog.models import db


def _database_uri() -> str:
    """Choose the configured PostgreSQL database; no local fallback.

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
            value
            for value in candidates
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
            missing += [
                f"{table.name}.{c.name}" for c in table.columns if c.name not in have
            ]
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
    mfa_key = (os.environ.get("MFA_ENCRYPTION_KEY") or "").strip()
    if os.environ.get("VERCEL") and not mfa_key:
        problems.append(
            "MFA_ENCRYPTION_KEY is not set. CareMin cannot safely store authenticator "
            "secrets without a dedicated encryption key. Set the same valid Fernet key "
            "for Preview and Production before deploying mandatory two-step verification."
        )
    elif mfa_key and not auth.valid_mfa_encryption_key(mfa_key):
        problems.append(
            "MFA_ENCRYPTION_KEY is not a valid Fernet key. Generate a URL-safe base64 "
            "32-byte key and update the environment without exposing the value in logs."
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
    for table in db.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=db.engine, checkfirst=True)
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
            clause = (
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f"{column.type.compile(dialect)}"
            )
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
