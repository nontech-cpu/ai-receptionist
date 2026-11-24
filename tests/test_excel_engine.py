import openpyxl
from datetime import datetime, timedelta
import pytest

from app.services.sheet_service import sheet_service
from app.core.config import settings

def test_excel_call_sid_idempotency(protect_production_excel):
    """
    Sub-Phase A Requirement:
    Duplicate calls with identical CallSid must be idempotent and not append duplicate rows.
    """
    temp_excel = protect_production_excel
    call_sid = "CA1234567890abcdef"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # First call
    booking1 = sheet_service.save_appointment(
        patient_name="Neeraj Sharma",
        phone_number="9829012345",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Fever",
        call_sid=call_sid
    )

    # Duplicate webhook call with identical call_sid
    booking2 = sheet_service.save_appointment(
        patient_name="Neeraj Sharma",
        phone_number="9829012345",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Fever",
        call_sid=call_sid
    )

    assert booking1["booking_id"] == booking2["booking_id"]

    # Verify only 1 row was appended in Excel (header + 1 record)
    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    assert len(rows) == 2, f"Idempotency failure: Expected 2 rows in Excel, found {len(rows)}"

def test_interval_overlap_detection_30_minutes(protect_production_excel, monkeypatch):
    """
    Sub-Phase A Requirement:
    Validate complete 30-minute appointment interval against overlapping bookings.
    Booking 11:00 AM occupies 11:00 - 11:30 AM.
    Attempting to book 11:15 AM (11:15 - 11:45 AM) must be rejected due to overlap.
    Booking 11:30 AM (11:30 - 12:00 PM) must succeed.
    """
    monkeypatch.setattr(settings, "ENABLE_SLOT_CONFLICT_CHECK", True)
    temp_excel = protect_production_excel
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Book 11:00 AM
    b1 = sheet_service.save_appointment(
        patient_name="Patient One",
        phone_number="9829012345",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Headache"
    )
    assert b1.get("status") == "Confirmed"

    # 2. Attempt 11:15 AM (overlaps [11:00, 11:30))
    valid, reason = sheet_service.validate_slot(tomorrow_str, "11:15 AM")
    assert valid is False
    assert "overlaps" in reason.lower()

    b_overlap = sheet_service.save_appointment(
        patient_name="Patient Two",
        phone_number="9829054321",
        date_str=tomorrow_str,
        time_slot="11:15 AM",
        symptoms="Cold"
    )
    assert b_overlap.get("status") == "Failed"

    # 3. Book 11:30 AM (adjacent non-overlapping slot)
    valid_adj, reason_adj = sheet_service.validate_slot(tomorrow_str, "11:30 AM")
    assert valid_adj is True

    b_adj = sheet_service.save_appointment(
        patient_name="Patient Three",
        phone_number="9829099999",
        date_str=tomorrow_str,
        time_slot="11:30 AM",
        symptoms="Checkup"
    )
    assert b_adj.get("status") == "Confirmed"

def test_session_boundary_interval_overflow(protect_production_excel):
    """
    Sub-Phase A Requirement:
    A 30-minute appointment interval must finish within clinic session hours.
    Morning session ends at 02:00 PM. An appointment at 01:45 PM would end at 02:15 PM
    and must be rejected.
    Evening session ends at 08:00 PM. An appointment at 07:45 PM would end at 08:15 PM
    and must be rejected.
    """
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 01:45 PM ends at 02:15 PM (past 02:00 PM)
    valid_m, reason_m = sheet_service.validate_slot(tomorrow_str, "01:45 PM")
    assert valid_m is False

    # 07:45 PM ends at 08:15 PM (past 08:00 PM)
    valid_e, reason_e = sheet_service.validate_slot(tomorrow_str, "07:45 PM")
    assert valid_e is False

def test_backup_created_on_write(protect_production_excel):
    """
    Sub-Phase A Requirement:
    An atomic backup file must be created/updated when writing to the workbook.
    """
    temp_excel = protect_production_excel
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    sheet_service.save_appointment(
        patient_name="Backup Test",
        phone_number="9829012345",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Fever"
    )

    backup_file = temp_excel.parent / (temp_excel.stem + "_backup.xlsx")
    assert backup_file.exists(), f"Expected backup file at {backup_file}"
    assert backup_file.stat().st_size > 0
