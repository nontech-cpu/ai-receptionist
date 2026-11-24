import hashlib
from pathlib import Path
import openpyxl
import pytest
from app.core.config import BASE_DIR

REAL_EXCEL_PATH = BASE_DIR / "data/appointments.xlsx"

def get_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def test_production_excel_file_exists_and_preserved():
    """
    Verifies that the real data/appointments.xlsx file exists and preserves
    its historical records (at least 45 appointments).
    """
    assert REAL_EXCEL_PATH.exists(), f"Production appointments file missing at {REAL_EXCEL_PATH}"
    
    wb = openpyxl.load_workbook(REAL_EXCEL_PATH, read_only=True)
    ws = wb.active
    row_count = sum(1 for _ in ws.iter_rows())
    wb.close()

    # 1 header row + 44 records = 45 rows
    assert row_count >= 45, f"Expected at least 45 rows (1 header + 44 records), found {row_count}"
