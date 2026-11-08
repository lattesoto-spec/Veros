"""Pinned, local Xberg extraction translated into CareMin's lossless table boundary."""

import asyncio
import csv
import importlib.metadata
import json
import threading
import importlib
from pathlib import Path

XBERG_VERSION = "1.3.9"
_PARSER_IMPORT_LOCK = threading.Lock()
_parser = None


def load_parser():
    """Initialise the native extension once before concurrent extractions."""
    global _parser
    with _PARSER_IMPORT_LOCK:
        if _parser is None:
            _parser = importlib.import_module("xberg")
        return _parser
DOCUMENT_EXTENSIONS = (
    ".csv",
    ".tsv",
    ".txt",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".xlsb",
    ".ods",
    ".pdf",
    ".docx",
    ".odt",
    ".html",
    ".htm",
)


def parser_status():
    try:
        version = importlib.metadata.version("xberg")
        return {
            "engine": "xberg",
            "version": version,
            "ready": version == XBERG_VERSION,
        }
    except importlib.metadata.PackageNotFoundError:
        return {"engine": "xberg", "version": None, "ready": False}


def extract_tables(filename, data):
    from .reader import FileReadError, _build_sheet, _find_header_row

    try:
        parser = load_parser()
        CsvConfig, ExtractInput = parser.CsvConfig, parser.ExtractInput
        ExtractionConfig, SecurityLimits = parser.ExtractionConfig, parser.SecurityLimits
        extract = parser.extract
    except ImportError as error:
        raise FileReadError(
            "The Xberg parser is unavailable. Install the pinned backend requirements."
        ) from error
    if not parser_status()["ready"]:
        raise FileReadError(
            f"CareMin requires the validated Xberg version {XBERG_VERSION}."
        )
    suffix = Path(filename).suffix.lower()
    mime = None
    csv_config = None
    if suffix in (".csv", ".tsv", ".txt"):
        # Delimiter detection is metadata only; Xberg still reads every cell.
        from .reader import _decode

        text = _decode(data)
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if suffix == ".tsv" else ","
        csv_config = CsvConfig(delimiter=delimiter)
        mime = "text/csv"
        data = text.encode("utf-8")
    config = ExtractionConfig(
        use_cache=False,
        enable_quality_processing=False,
        disable_ocr=True,
        strict_parse=True,
        extraction_timeout_secs=90,
        max_concurrent_extractions=1,
        csv=csv_config,
        security_limits=SecurityLimits(
            max_archive_size=100_000_000,
            max_compression_ratio=100,
            max_files_in_archive=2000,
            max_table_cells=1_000_000,
            max_content_size=100_000_000,
            max_pages=500,
        ),
    )

    async def run():
        return await extract(
            ExtractInput(kind="bytes", bytes=data, filename=filename, mime_type=mime),
            config,
        )

    try:
        output = asyncio.run(run())
    except Exception as error:
        raise FileReadError(
            f"Xberg could not safely extract {filename}: {type(error).__name__}."
        ) from error
    if output.errors:
        raise FileReadError(
            f"Xberg reported an extraction error in {filename}; no records were imported."
        )
    sheets = []
    for document in output.results:
        if document.processing_warnings:
            raise FileReadError(
                f"Xberg reported incomplete extraction in {filename}; review the source before importing."
            )
        if document.metadata.ocr_used:
            raise FileReadError(
                "OCR-derived records need human verification. Supply a reviewed structured export."
            )
        fmt = document.metadata.format
        metadata = json.loads(str(fmt)) if fmt is not None else {}
        names = metadata.get("sheet_names", [])
        for index, table in enumerate(document.tables):
            raw = [
                [str(cell) if cell is not None else "" for cell in row]
                for row in table.cells
            ]
            if not raw or not any(any(cell.strip() for cell in row) for row in raw):
                continue
            if names:
                # Xberg omits empty sheets; page_number is the original
                # one-based worksheet ordinal, not a compacted table index.
                ordinal = table.page_number
                if not ordinal or not 1 <= ordinal <= len(names):
                    raise FileReadError(
                        "Xberg did not identify the source worksheet reliably."
                    )
                name = names[ordinal - 1]
            else:
                name = (
                    filename
                    if len(document.tables) == 1
                    else f"{filename}:table-{index + 1}"
                )
            sheet = _build_sheet(name, raw, _find_header_row(raw))
            sheet.parser = f"xberg/{XBERG_VERSION}"
            sheet.source_file = filename
            sheet.source_page = (
                None
                if names or suffix in (".csv", ".tsv", ".txt")
                else table.page_number or None
            )
            sheet.notes.append(
                f"Extracted locally by Xberg {XBERG_VERSION}; original grid retained for header review"
            )
            sheets.append(sheet)
    if not sheets:
        raise FileReadError(
            "No structured tables were extracted. Supply a CSV, spreadsheet or document containing an explicit table; prose is not transcribed into care records."
        )
    return sheets
