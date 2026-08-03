"""Copy an existing SQLite database (and its retained upload evidence) into
Postgres and object storage.

Run once, when moving off the single-volume SQLite deployment:

    # 1. get the live database off the Fly volume
    fly ssh sftp get /data/vero.db ./vero.db -a veros

    # 2. copy it into the new Postgres, preserving every id
    DATABASE_URL='postgres://...' python migrate_to_postgres.py ./vero.db

    # 3. optionally push the retained audit files into object storage
    DATABASE_URL='postgres://...' BLOB_READ_WRITE_TOKEN='...' \
        python migrate_to_postgres.py ./vero.db --uploads ./uploads

Safe to re-run: rows that already exist (same primary key) are skipped, so an
interrupted run can simply be repeated.
"""

import argparse
import os
import sqlite3
import sys

# Order matters: parents before the rows that reference them.
TABLES = [
    "app_settings",
    "organizations",
    "users",
    "facilities",
    "residents",
    "staff",
    "format_mappings",
    "import_receipts",
    "shifts",
    "import_jobs",
    "integration_configs",
    "audit_logs",
]


def _coerce(value, type_name: str):
    """SQLite keeps booleans as 0/1 and dates/times as strings; Postgres wants
    real types. Convert based on the destination column."""
    if value is None:
        return None
    t = type_name.upper()
    if "BOOL" in t:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes")
        return bool(value)
    if isinstance(value, str):
        from datetime import datetime

        if "TIMESTAMP" in t or "DATETIME" in t:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                        "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
        elif t.startswith("DATE"):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                pass
        elif t.startswith("TIME"):
            for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
                try:
                    return datetime.strptime(value, fmt).time()
                except ValueError:
                    continue
    return value


def _rows(conn: sqlite3.Connection, table: str):
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
    except sqlite3.OperationalError:
        return [], []  # table predates this version of the schema
    return [d[0] for d in cur.description], cur.fetchall()


def copy_database(sqlite_path: str) -> dict:
    from sqlalchemy import inspect, text

    from app import app, init_db
    from carelog.models import db

    if not os.path.exists(sqlite_path):
        sys.exit(f"No such SQLite file: {sqlite_path}")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    counts = {}

    with app.app_context():
        if db.engine.dialect.name == "sqlite":
            sys.exit("DATABASE_URL is not set to a Postgres database — refusing to "
                     "copy SQLite onto itself.")
        init_db()
        target_tables = set(inspect(db.engine).get_table_names())

        for table in TABLES:
            if table not in target_tables:
                continue
            cols, rows = _rows(src, table)
            if not rows:
                counts[table] = 0
                continue
            # Only copy columns the destination actually has.
            dest = {c["name"]: str(c["type"]) for c in inspect(db.engine).get_columns(table)}
            use = [c for c in cols if c in dest]
            placeholders = ", ".join(f":{c}" for c in use)
            collist = ", ".join(f'"{c}"' for c in use)
            pk = inspect(db.engine).get_pk_constraint(table)["constrained_columns"]
            conflict = f" ON CONFLICT ({', '.join(pk)}) DO NOTHING" if pk else ""
            stmt = text(f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders}){conflict}')

            written = 0
            for row in rows:
                db.session.execute(stmt, {c: _coerce(row[c], dest[c]) for c in use})
                written += 1
            db.session.commit()
            counts[table] = written

        _resync_sequences(db)

    src.close()
    return counts


def _resync_sequences(db):
    """Rows were inserted with explicit ids, so Postgres' identity sequences
    still start at 1 and the next insert would collide. Fast-forward them."""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    for table in TABLES:
        if table not in set(insp.get_table_names()):
            continue
        pks = insp.get_pk_constraint(table)["constrained_columns"]
        if len(pks) != 1:
            continue
        col = pks[0]
        coltype = next((c["type"] for c in insp.get_columns(table) if c["name"] == col), None)
        if coltype is None or "INT" not in str(coltype).upper():
            continue  # text primary keys (import_jobs) have no sequence
        db.session.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM \"{table}\"), 1), true)"
        ))
    db.session.commit()


def copy_uploads(uploads_dir: str) -> int:
    """Push retained evidence files into the configured object storage and
    repoint each receipt at its new prefix."""
    from app import app
    from carelog.models import ImportReceipt, db
    from carelog.storage import build_storage

    if not os.path.isdir(uploads_dir):
        sys.exit(f"No such uploads directory: {uploads_dir}")

    moved = 0
    with app.app_context():
        store = build_storage(app.config["UPLOADS_DIR"])
        for receipt in ImportReceipt.query.all():
            if not receipt.source_path:
                continue
            source = receipt.source_path.rstrip("/")
            if os.path.isabs(source):
                # Legacy layout: /data/uploads/<job> — uploads_dir is a copy of
                # that parent directory.
                folder = os.path.basename(source)
                local, prefix = os.path.join(uploads_dir, folder), f"imports/{folder}"
            else:
                # Already a storage prefix (imports/job-x); mirror it verbatim.
                local, prefix = os.path.join(uploads_dir, source), source
            if not os.path.isdir(local):
                continue
            for name in sorted(os.listdir(local)):
                path = os.path.join(local, name)
                if not os.path.isfile(path):
                    continue
                with open(path, "rb") as fh:
                    store.put(f"{prefix}/{name}", fh.read())
                moved += 1
            receipt.source_path = prefix
        db.session.commit()
    return moved


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sqlite_path", help="path to the exported SQLite database")
    ap.add_argument("--uploads", help="directory of retained upload evidence to push to storage")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("Set DATABASE_URL to the destination Postgres connection string.")

    counts = copy_database(args.sqlite_path)
    print("Copied rows:")
    for table, n in counts.items():
        print(f"  {table:<22} {n}")

    if args.uploads:
        moved = copy_uploads(args.uploads)
        print(f"\nCopied {moved} evidence file(s) into object storage.")

    print("\nDone. Check /debug/storage on the new deployment to verify.")


if __name__ == "__main__":
    main()
