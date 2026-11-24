import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import openpyxl
from fastapi.testclient import TestClient

from app.core.config import settings, BASE_DIR
from app.services.sheet_service import sheet_service, SheetService
from app.services.session_manager import session_manager
from app.services.telegram_service import telegram_service
from app.services.elevenlabs_service import elevenlabs_service
from app.services.llm_service import llm_service
from app.main import app

REAL_EXCEL_PATH = (BASE_DIR / "data/appointments.xlsx").resolve()

@pytest.fixture(autouse=True)
def protect_production_excel(monkeypatch, tmp_path):
    """
    CRITICAL SAFETY FIXTURE:
    Strictly forbids reading/writing the production appointments.xlsx file during tests.
    Every test automatically runs against an isolated temporary workbook in tmp_path.
    """
    temp_excel_file = tmp_path / "test_appointments.xlsx"
    
    # Initialize empty appointment sheet with standard headers in temp directory
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Appointments"
    headers = [
        "Booking ID",
        "Booking Timestamp",
        "Patient Name",
        "Contact Number",
        "Appointment Date",
        "Time Slot",
        "Symptoms / Reason",
        "Symptom Duration",
        "Doctor Name",
        "Status"
    ]
    ws.append(headers)
    wb.save(temp_excel_file)
    wb.close()

    # Monkeypatch settings and sheet_service to point strictly to the temporary workbook
    monkeypatch.setattr(settings, "EXCEL_FILE_PATH", temp_excel_file)
    monkeypatch.setattr(sheet_service, "file_path", temp_excel_file)
    monkeypatch.setattr(sheet_service, "_lock_path", Path(str(temp_excel_file) + ".lock"))
    sheet_service._call_sid_cache.clear()

    # Monkeypatch openpyxl.load_workbook and Workbook.save to ensure real Excel is NEVER touched
    original_save = openpyxl.Workbook.save
    def safe_save(self, filename):
        target = Path(filename).resolve()
        if target == REAL_EXCEL_PATH:
            raise PermissionError(
                f"FATAL TEST SAFETY VIOLATION: Attempted to write to production Excel file: {REAL_EXCEL_PATH}"
            )
        return original_save(self, filename)
    monkeypatch.setattr(openpyxl.Workbook, "save", safe_save)

    # Reset in-memory sessions between tests
    session_manager._sessions.clear()

    yield temp_excel_file

@pytest.fixture(autouse=True)
def mock_external_services(monkeypatch):
    """
    Mocks all third-party external services:
    - Telegram alerts (no network calls, no bot polling)
    - ElevenLabs audio generation (no paid quota consumption)
    - Twilio outbound calls
    """
    sent_alerts = []
    def fake_send_alert(booking_data):
        sent_alerts.append(booking_data)
        return True
    
    monkeypatch.setattr(telegram_service, "send_appointment_alert", fake_send_alert)
    monkeypatch.setattr(telegram_service, "_sync_send", lambda text, chat_id=None: True)
    monkeypatch.setattr(telegram_service, "get_latest_chat_id", lambda force_refresh=False: "1234828592")
    
    # Mock Elevenlabs
    monkeypatch.setattr(elevenlabs_service, "get_audio_url", lambda text, base_url: None)

    yield {
        "sent_alerts": sent_alerts
    }

@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)
