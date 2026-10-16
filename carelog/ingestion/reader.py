"""Read arbitrary uploads — CSV, Excel, JSON, SQL dumps, SQLite — into a
uniform table structure.

Every file, regardless of source system, becomes a list of Sheet objects:
headers (from the detected header row) + rows of stringified cells.
Downstream code (fingerprinting, mapping, the LLM analyzer) only ever sees
this structure, never the raw file.
"""

import io
from dataclasses import dataclass, field


# How many raw rows to keep for the analyzer to inspect. Enough to see a
# title banner, a subtitle, a blank line and the real header.
PREVIEW_ROWS = 12


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[dict]  # header -> cell value (str)
    header_row_index: int = 0
    notes: list[str] = field(default_factory=list)
    # The grid as it was before a header row was chosen. Kept so the choice can
    # be revisited: header detection is a guess, and a guess the analyzer can
    # see is a guess it can correct.
    raw_rows: list[list[str]] = field(default_factory=list)
    source_rows: list[int] = field(default_factory=list)
    source_file: str = ""
    source_page: int | None = None
    parser: str = "caremin/structured-v1"

    @property
    def preview(self) -> list[list[str]]:
        return self.raw_rows[:PREVIEW_ROWS]


def rebuild_with_header(sheet: Sheet, header_row_index: int) -> Sheet:
    """Re-interpret a sheet using a different row as the header."""
    if not sheet.raw_rows or not (0 <= header_row_index < len(sheet.raw_rows)):
        return sheet
    rebuilt = _build_sheet(sheet.name, sheet.raw_rows, header_row_index)
    rebuilt.source_file = sheet.source_file
    rebuilt.source_page = sheet.source_page
    rebuilt.parser = sheet.parser
    rebuilt.notes = list(sheet.notes) + [
        f"Header taken from row {header_row_index + 1} on the analyzer's instruction"
    ]
    return rebuilt


class FileReadError(Exception):
    pass


JSON_EXTENSIONS = (".json", ".jsonl", ".ndjson")
SQL_EXTENSIONS = (".sql",)
SQLITE_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
from .xberg_adapter import DOCUMENT_EXTENSIONS
UNPACKABLE_EXTENSIONS = DOCUMENT_EXTENSIONS + JSON_EXTENSIONS + SQL_EXTENSIONS + SQLITE_EXTENSIONS
SUPPORTED_EXTENSIONS = UNPACKABLE_EXTENSIONS + (".zip",)

# SQLite files start with this regardless of extension.
SQLITE_MAGIC = b"SQLite format 3\x00"


def read_upload(filename: str, data: bytes) -> list[Sheet]:
    sheets = _read_upload(filename, data)
    for sheet in sheets:
        if not sheet.source_file:
            sheet.source_file = filename
        if sheet.parser == "caremin/structured-v1" and not sheet.source_rows:
            sheet.source_rows = list(range(1, len(sheet.rows) + 1))
            sheet.notes.append("Source rows are logical record ordinals, not text line numbers")
    return sheets


def _read_upload(filename: str, data: bytes) -> list[Sheet]:
    """Parse an uploaded file into sheets. Raises FileReadError on unusable files."""
    name = (filename or "").lower()
    if name.endswith(".zip"):
        return _read_zip(data)

    from . import structured

    # Sniff the magic bytes first: .db files are routinely named anything.
    if data[:16] == SQLITE_MAGIC or name.endswith(SQLITE_EXTENSIONS):
        return structured.read_sqlite(data)
    if name.endswith(JSON_EXTENSIONS):
        return structured.read_json(filename, data)
    if name.endswith(SQL_EXTENSIONS):
        return structured.read_sql(filename, data)
    # Extension-less or oddly named uploads: fall back to content sniffing so
    # a JSON body doesn't get mangled by the CSV parser.
    head = data.lstrip()[:1]
    if head in (b"[", b"{"):
        return structured.read_json(filename, data)
    # Default: treat as delimited text (csv/tsv/txt).
    from .xberg_adapter import extract_tables
    return extract_tables(filename, data)


def _read_zip(data: bytes) -> list[Sheet]:
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [i for i in archive.infolist() if not i.is_dir() and not i.filename.rsplit("/", 1)[-1].startswith((".", "~"))]
            if len(entries) > 100 or sum(i.file_size for i in entries) > 50_000_000:
                raise FileReadError("Archive exceeds the 100-file or 50 MB expanded limit.")
            if any(i.file_size > max(i.compress_size, 1) * 100 for i in entries):
                raise FileReadError("Archive compression ratio is unsafe.")
            sheets = []
            for entry in entries:
                if entry.filename.lower().endswith(".zip"):
                    raise FileReadError("Nested archives are not supported. Upload their data files directly.")
                if not entry.filename.lower().endswith(UNPACKABLE_EXTENSIONS):
                    continue
                for sheet in read_upload(entry.filename, archive.read(entry)):
                    sheet.name = f"{entry.filename}:{sheet.name}"
                    sheet.source_file = entry.filename
                    sheets.append(sheet)
    except zipfile.BadZipFile as error:
        raise FileReadError("Could not open zip archive.") from error
    if not sheets:
        raise FileReadError("Zip archive contains no supported data files.")
    return sheets


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise FileReadError("Could not decode file as text.")


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    # datetimes/times/dates stringify sensibly via isoformat
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ") if hasattr(value, "hour") and hasattr(value, "year") else value.isoformat()
    return str(value).strip()


def _find_header_row(raw_rows: list[list[str]], scan: int = 10) -> int:
    """Exports often carry title/preamble lines before the real header.
    Pick the row that looks most header-like: many non-empty, mostly
    non-numeric cells, followed by rows of consistent width."""
    best_idx, best_score = 0, -1.0
    for i, row in enumerate(raw_rows[:scan]):
        cells = [(c or "").strip() for c in row]
        non_empty = [c for c in cells if c]
        if len(non_empty) < 2:
            continue
        non_numeric = sum(1 for c in non_empty if not _looks_numeric(c))
        # Count distinct labels, not filled cells: a merged title banner is
        # copied into every column it spans, so it looks as wide as the real
        # header. A header names each column differently.
        score = len(set(non_empty)) + 2.0 * (non_numeric / len(non_empty))
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


def _looks_numeric(s: str) -> bool:
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _build_sheet(name: str, raw_rows: list[list[str]], header_idx: int) -> Sheet:
    headers_raw = [(c or "").strip() for c in raw_rows[header_idx]]
    headers, seen = [], {}
    for j, h in enumerate(headers_raw):
        h = h or f"column_{j + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
        headers.append(h)

    rows, source_rows = [], []
    for source_row, raw in enumerate(raw_rows[header_idx + 1:], start=header_idx + 2):
        if not any((cell or "").strip() for cell in raw):
            continue
        source_rows.append(source_row)
        row = {}
        for j, h in enumerate(headers):
            row[h] = (raw[j] or "").strip() if j < len(raw) else ""
        rows.append(row)

    notes = []
    if header_idx > 0:
        preamble = "; ".join(
            " ".join(c for c in r if (c or "").strip()) for r in raw_rows[:header_idx]
        )
        notes.append(f"Skipped {header_idx} preamble row(s): {preamble[:200]}")
    return Sheet(name=name, headers=headers, rows=rows, header_row_index=header_idx,
                 notes=notes, raw_rows=raw_rows, source_rows=source_rows)
