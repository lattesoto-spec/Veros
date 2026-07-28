"""Readers for structured (non-tabular) uploads: JSON, JSONL, SQL dumps and
SQLite database files.

Each one is flattened into the same Sheet structure the CSV/Excel readers
produce, so fingerprinting, the AI analyzer and the mapping engine work on
them unchanged — a JSON export is just another format to learn.
"""

import json
import re

from .reader import FileReadError, Sheet, _cell_to_str

# A JSON payload usually wraps its records: {"data": {"shifts": [...]}}.
# Walk this deep looking for arrays of objects.
MAX_DEPTH = 6


# ------------------------------------------------------------------ JSON


def read_json(filename: str, data: bytes) -> list[Sheet]:
    from .reader import _decode

    text = _decode(data).strip()
    if not text:
        raise FileReadError("File is empty.")

    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = _read_jsonl(text, filename)

    tables = []
    _collect_tables(doc, filename or "json", tables, depth=0)
    sheets = [_sheet_from_records(name, records) for name, records in tables]
    sheets = [s for s in sheets if s.headers and s.rows]
    if not sheets:
        raise FileReadError(
            "No records found in this JSON file — expected an array of objects "
            "(or an object containing one), e.g. [{\"staff_id\": ...}, ...]."
        )
    return sheets


def _read_jsonl(text: str, filename: str) -> list:
    """Newline-delimited JSON: one record per line."""
    records = []
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise FileReadError(
                f"Could not parse {filename or 'file'} as JSON or JSON Lines "
                f"(line {i}: {e.msg})."
            ) from e
    if not records:
        raise FileReadError("File is empty.")
    return records


def _collect_tables(node, name: str, out: list, depth: int):
    """Find every array-of-objects in the document; each becomes a sheet."""
    if depth > MAX_DEPTH:
        return
    if isinstance(node, list):
        records = [r for r in node if isinstance(r, dict)]
        if records:
            out.append((name, records))
        return
    if isinstance(node, dict):
        # An object whose values are all scalars is itself a single record.
        nested = {k: v for k, v in node.items() if isinstance(v, (dict, list))}
        if not nested:
            out.append((name, [node]))
            return
        for key, value in nested.items():
            _collect_tables(value, key, out, depth + 1)


def _flatten(record: dict, prefix: str = "") -> dict:
    """Nested objects become dotted columns; lists of scalars are joined;
    anything else is left as compact JSON so no data is silently dropped."""
    flat = {}
    for key, value in record.items():
        col = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{col}."))
        elif isinstance(value, list):
            if all(not isinstance(v, (dict, list)) for v in value):
                flat[col] = ", ".join(_cell_to_str(v) for v in value)
            else:
                flat[col] = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, bool):
            flat[col] = "true" if value else "false"
        else:
            flat[col] = _cell_to_str(value)
    return flat


def _sheet_from_records(name: str, records: list[dict]) -> Sheet:
    flat_rows = [_flatten(r) for r in records]
    # Records may carry different keys; the header is their ordered union.
    headers: list[str] = []
    seen = set()
    for row in flat_rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                headers.append(key)
    rows = [{h: row.get(h, "") for h in headers} for row in flat_rows]
    return Sheet(name=name, headers=headers, rows=rows)


# ------------------------------------------------------------------ SQL


_CREATE_RE = re.compile(
    r"CREATE\s+(?:TEMP\w*\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)\s*\((.*)\)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([^\s(]+)\s*(?:\(([^)]*)\))?\s*VALUES\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINTS = (
    "primary", "unique", "key", "constraint", "foreign", "index", "check", "fulltext",
)


def read_sql(filename: str, data: bytes) -> list[Sheet]:
    """Parse a SQL dump (mysqldump / pg_dump / sqlite .dump): CREATE TABLE
    supplies the columns, INSERT statements supply the rows."""
    from .reader import _decode

    text = _decode(data)
    columns: dict[str, list[str]] = {}
    rows: dict[str, list[list[str]]] = {}
    order: list[str] = []

    for statement in _split_statements(text):
        m = _CREATE_RE.match(statement)
        if m:
            table = _unquote(m.group(1))
            cols = _parse_column_defs(m.group(2))
            if cols:
                columns[table] = cols
                if table not in order:
                    order.append(table)
            continue

        m = _INSERT_RE.match(statement)
        if not m:
            continue
        table = _unquote(m.group(1))
        insert_cols = (
            [_unquote(c) for c in _split_top_level(m.group(2))] if m.group(2) else None
        )
        tuples = _parse_value_tuples(m.group(3))
        if not tuples:
            continue
        if insert_cols:
            # Columns named on the INSERT win: a dump may insert a subset.
            if columns.get(table) != insert_cols:
                columns[table] = insert_cols
        elif table not in columns:
            columns[table] = [f"column_{i + 1}" for i in range(len(tuples[0]))]
        rows.setdefault(table, []).extend(tuples)
        if table not in order:
            order.append(table)

    sheets = []
    for table in order:
        table_rows = rows.get(table)
        if not table_rows:
            continue  # schema-only table, nothing to import
        headers = _dedupe(columns.get(table) or [])
        # Pad/trim so a malformed row can never shift the whole table.
        while len(headers) < max(len(t) for t in table_rows):
            headers.append(f"column_{len(headers) + 1}")
        sheets.append(Sheet(
            name=table,
            headers=headers,
            rows=[
                {h: (t[i] if i < len(t) else "") for i, h in enumerate(headers)}
                for t in table_rows
            ],
        ))

    if not sheets:
        raise FileReadError(
            "No INSERT statements with data were found in this SQL file — "
            "export the table contents (not just the schema) and try again."
        )
    return sheets


def _split_statements(text: str):
    """Split on semicolons that sit outside string literals and comments."""
    buf, i, n = [], 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:  # MySQL backslash escape
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:  # '' escape
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str, quote = True, ch
            buf.append(ch)
        elif ch == "-" and text.startswith("--", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        elif ch == "#" and text.startswith("#", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        elif ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                yield statement
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside quotes and nested parentheses."""
    parts, buf, depth, i, n = [], [], 0, 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str, quote = True, ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _parse_column_defs(body: str) -> list[str]:
    cols = []
    for part in _split_top_level(body):
        if not part:
            continue
        first = part.split()[0] if part.split() else ""
        if first.lower().strip("`\"[]") in _CONSTRAINTS:
            continue
        name = _unquote(first)
        if name:
            cols.append(name)
    return cols


def _parse_value_tuples(body: str) -> list[list[str]]:
    """Read the `(...), (...)` list that follows VALUES."""
    tuples = []
    for chunk in _split_top_level(body.strip().rstrip(";")):
        chunk = chunk.strip()
        if not (chunk.startswith("(") and chunk.endswith(")")):
            continue
        tuples.append([_sql_literal(v) for v in _split_top_level(chunk[1:-1])])
    return tuples


def _sql_literal(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.upper() == "NULL":
        return ""
    if raw[0] in ("'", '"', "`") and len(raw) > 1 and raw[-1] == raw[0]:
        q = raw[0]
        inner = raw[1:-1].replace(q + q, q)
        return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r", "0": ""}.get(m.group(1), m.group(1)), inner)
    return raw


def _unquote(name: str) -> str:
    """`hr`.`roster` -> roster. The schema split has to happen before quotes
    are stripped, or the dot inside `x`.`y` cuts the identifier in half."""
    name = name.strip().strip(";")
    segments, buf, i, n = [], [], 0, len(name)
    in_str = False
    closer = ""
    while i < n:
        ch = name[i]
        if in_str:
            if ch == closer:
                in_str = False
            else:
                buf.append(ch)
        elif ch in ("`", '"', "["):
            in_str, closer = True, "]" if ch == "[" else ch
        elif ch == ".":
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments[-1].strip()


def _dedupe(names: list[str]) -> list[str]:
    out, seen = [], {}
    for i, n in enumerate(names):
        n = n or f"column_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 1
        out.append(n)
    return out


# --------------------------------------------------------------- SQLite


def read_sqlite(data: bytes) -> list[Sheet]:
    """Read an actual SQLite database file — one sheet per table."""
    import os
    import sqlite3
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise FileReadError(f"Could not open SQLite database: {e}") from e
        try:
            conn.text_factory = lambda b: b.decode("utf-8", "replace")
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            sheets = []
            for table in tables:
                cur = conn.execute(f'SELECT * FROM "{table}"')
                headers = _dedupe([d[0] for d in cur.description])
                rows = [
                    {h: _cell_to_str(v) for h, v in zip(headers, record)}
                    for record in cur.fetchall()
                ]
                if rows:
                    sheets.append(Sheet(name=table, headers=headers, rows=rows))
        finally:
            conn.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not sheets:
        raise FileReadError("This SQLite database contains no tables with data.")
    return sheets
