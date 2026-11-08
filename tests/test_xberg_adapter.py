import io
import zipfile

import pytest
from openpyxl import Workbook

from carelog.ingestion.fingerprint import fingerprint
from carelog.ingestion.reader import FileReadError, read_upload, rebuild_with_header
from carelog.ingestion.xberg_adapter import XBERG_VERSION


def test_xberg_retains_raw_grid_and_physical_source_rows():
    book = Workbook()
    book.active.title = "Empty worksheet"
    sheet = book.create_sheet("Worked time")
    sheet.append(["Payroll export"])
    sheet.append([])
    sheet.append(["Employee", "Work Date", "Actual Start", "Actual Finish"])
    sheet.append(["E1", "2026-07-01", "07:00", "15:00"])
    sheet.append([])
    sheet.append(["E2", "2026-07-01", "08:00", "16:00"])
    output = io.BytesIO()
    book.save(output)
    parsed = read_upload("worked.xlsx", output.getvalue())[0]
    assert parsed.name == "Worked time"
    assert parsed.raw_rows[0][0] == "Payroll export"
    corrected = rebuild_with_header(parsed, 2)
    assert corrected.source_rows == [4, 6]
    assert corrected.source_file == "worked.xlsx"
    assert corrected.parser == f"xberg/{XBERG_VERSION}"
    assert corrected.rows[1]["Employee"] == "E2"
    old = fingerprint([corrected])
    corrected.parser = "older-parser"
    assert fingerprint([corrected]) != old


def test_xberg_text_delimiter_and_structured_fallback():
    table = read_upload("export.txt", b"Employee\tDate\tMinutes\nE1\t2026-07-01\t60\n")[
        0
    ]
    assert table.rows[0]["Minutes"] == "60"
    assert table.parser.startswith("xberg/")
    table = read_upload("export.json", b'[{"Employee":"E1","Minutes":60}]')[0]
    assert table.rows[0]["Minutes"] == "60"
    assert table.parser == "caremin/structured-v1"
    assert table.source_file == "export.json"


def test_archive_rejects_nested_archives():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("inner.zip", b"anything")
    with pytest.raises(FileReadError, match="Nested archives"):
        read_upload("source.zip", output.getvalue())


def test_document_without_structured_tables_fails_closed():
    with pytest.raises(FileReadError):
        read_upload("broken.pdf", b"%PDF-1.4\ninvalid document")


def test_concurrent_first_extractions_load_the_real_native_parser():
    """A cold process exercises first import and independent extraction loops."""
    import subprocess
    import sys
    probe = '''
from concurrent.futures import ThreadPoolExecutor
from carelog.ingestion.reader import read_upload

def parse(index):
    data = f"Employee,Date,Minutes\\nE{index},2026-08-01,60\\n".encode()
    sheet = read_upload(f"source-{index}.csv", data)[0]
    assert sheet.rows[0]["Employee"] == f"E{index}"
    assert sheet.parser == "xberg/1.3.9"

with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(parse, range(16)))
'''
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
