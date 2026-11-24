import os
import openpyxl
from datetime import datetime, timedelta
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.sheet_service import sheet_service
from app.services.session_manager import session_manager
from app.services.llm_service import llm_service, detect_caller_language
from app.services.state_machine import SessionState
from app.services.telegram_service import telegram_service
from app.core.security import verify_twilio_signature, mask_sensitive_data

def test_production_environment_forces_signature_validation(client: TestClient, monkeypatch):
    """
    Security Audit Requirement:
    In production (ENVIRONMENT="production"), Twilio signature validation MUST be strictly
    enforced and cannot accidentally be disabled, rejecting missing or invalid signatures with HTTP 403.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", False)  # Attempting to disable in config
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "prod_token_test_abc123")

    payload = {"CallSid": "CA_PROD_TEST_01", "From": "+17372212163"}

    # Missing signature in production MUST be rejected with HTTP 403
    res_missing = client.post("/voice/incoming", data=payload)
    assert res_missing.status_code == 403, (
        f"Production security violation: Request without signature returned {res_missing.status_code} instead of 403!"
    )

    # Forged/invalid signature in production MUST be rejected with HTTP 403
    res_invalid = client.post("/voice/incoming", data=payload, headers={"X-Twilio-Signature": "forged_signature_hash"})
    assert res_invalid.status_code == 403

def test_sensitive_data_masked_in_logging():
    """
    Security Audit Requirement:
    Verify patient contact numbers and credentials are masked for log output.
    """
    assert mask_sensitive_data("9829012345") == "******2345"
    assert mask_sensitive_data("+919876543210") == "*" * 9 + "3210"
    assert mask_sensitive_data("secret_token_123456") == "*" * 15 + "3456"

def test_failed_telegram_notification_does_not_undo_successful_booking(protect_production_excel, monkeypatch):
    """
    Telegram & Excel Verification Requirement:
    A failure in Telegram delivery (e.g. network timeout or API error)
    must NOT undo or roll back a confirmed appointment in Excel.
    """
    temp_excel = protect_production_excel
    call_sid = "test_telegram_fail_001"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Mock Gemini so no network calls occur
    mock_gemini = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes, confirm it please"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini)

    # Simulate Telegram network outage / exception
    def failing_send_alert(appointment_data):
        raise ConnectionError("Telegram API unreachable / network timeout")

    monkeypatch.setattr(telegram_service, "send_appointment_alert", failing_send_alert)

    # Set up session in AWAITING_EXPLICIT_CONFIRMATION
    session = session_manager.get_or_create(call_sid)
    session.set_slot("patient_name", "Kavita")
    session.set_slot("symptoms", "Fever")
    session.set_slot("appointment_date", tomorrow_str)
    session.set_slot("appointment_time", "11:00 AM")
    session.set_slot("phone_number", "9829012345")
    session.state = "AWAITING_EXPLICIT_CONFIRMATION"
    session.explicit_confirmation_requested = True

    # Caller confirms
    reply, is_done = llm_service.process_turn(call_sid, "Yes, confirm it please")

    # Booking must still be written to Excel successfully!
    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()

    assert len(rows) == 2, "Booking was rolled back due to Telegram failure!"
    assert rows[1][2] == "Kavita"
    assert rows[1][5] == "11:00 AM"

def test_excel_write_failure_preserves_backup_and_recovers(protect_production_excel, monkeypatch):
    """
    Excel Hardening Requirement:
    If a disk write fails during workbook save, the previous backup must remain intact
    and allow complete recovery.
    """
    temp_excel = protect_production_excel
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Successful initial booking
    b1 = sheet_service.save_appointment(
        patient_name="Baseline Patient",
        phone_number="9829011111",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Cold",
        call_sid="CA_BACKUP_BASE_01"
    )
    assert b1["status"] == "Confirmed"

    backup_file = temp_excel.parent / (temp_excel.stem + "_backup.xlsx")
    assert backup_file.exists()
    initial_backup_size = backup_file.stat().st_size

    # 2. Simulate catastrophic disk/OS write error during second write
    real_os_replace = os.replace
    def faulty_replace(src, dst):
        raise OSError("Simulated disk write I/O failure")

    monkeypatch.setattr(os, "replace", faulty_replace)

    try:
        sheet_service.save_appointment(
            patient_name="Crash Patient",
            phone_number="9829022222",
            date_str=tomorrow_str,
            time_slot="11:30 AM",
            symptoms="Headache",
            call_sid="CA_CRASH_TEST_02",
            raise_on_error=True
        )
    except OSError:
        pass  # Expected simulated failure

    # Verify that the backup file is completely intact and matches initial state
    assert backup_file.exists()
    wb_backup = openpyxl.load_workbook(backup_file)
    rows = list(wb_backup.active.iter_rows(values_only=True))
    wb_backup.close()
    assert len(rows) == 2
    assert rows[1][2] == "Baseline Patient"

def test_consultation_fee_question_after_failed_booking(protect_production_excel, monkeypatch):
    """
    Regression Incident E:
    Consultation fee question after failed booking: answer ₹500 without retrying the previous booking.
    """
    call_sid = "CA_FEE_AFTER_FAIL_01"
    session = session_manager.get_or_create(call_sid, "+919829012345")
    session.language = "ENGLISH"

    # Pre-fill details, but appointment_time was cleared after failed Sunday validation
    session.set_slot("patient_name", "Rahul")
    session.set_slot("symptoms", "Fever")
    session.set_slot("appointment_date", "2026-11-29")  # Sunday
    session.set_slot("appointment_time", None)  # Cleared after Sunday rejection

    # Mock Gemini to answer the FAQ question
    def mock_gemini_faq(messages, system_prompt, include_tools=True):
        return {
            "candidates": [{
                "content": {
                    "parts": [{"text": "The consultation fee for Dr. Sharma is ₹500."}]
                }
            }]
        }
    monkeypatch.setattr(llm_service, "_call_gemini_api", mock_gemini_faq)

    # Caller asks about fee
    reply, is_done = llm_service.process_turn(call_sid, "What is the consultation fee?")

    # 1. Reply includes the fee (500 or ₹500)
    assert "500" in reply

    # 2. Did NOT retry the failed booking or enter completed state
    assert not session.is_completed
    assert session.state != "BOOKING_COMPLETED"

    # 3. Excel sheet remains untouched
    wb = openpyxl.load_workbook(protect_production_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 1, "Failed booking must not write to Excel when asking FAQ!"

def test_post_booking_fees_inquiry_does_not_revalidate_or_invalidate_booking(protect_production_excel):
    """
    Regression Test:
    Caller completes booking for Devendra at 07:00 PM.
    Caller then asks 'fees bata do' (as invited by Maya).
    - Maya must reply directly with the ₹500 fee.
    - Maya must NOT attempt to re-validate the slot against Excel.
    - Maya must NOT say '07 PM is already booked'.
    - Existing booking and time slot must remain intact and completed.
    """
    call_sid = "CA_POST_BOOK_FEE_01"
    session = session_manager.get_or_create(call_sid, "+918824466229")
    session.set_slot("patient_name", "Devendra")
    session.set_slot("symptoms", "Cough")
    session.set_slot("appointment_date", "2026-11-25")
    session.set_slot("appointment_time", "07:00 PM")
    session.set_slot("phone_number", "8824466229")
    session.is_completed = True
    session.state = SessionState.BOOKING_COMPLETED

    reply, is_done = llm_service.process_turn(call_sid, "fees bata do", confidence=0.78)

    # 1. Directly answers the fee
    assert "500" in reply, f"Fee should be answered, got: {reply}"
    assert "already booked" not in reply.lower()
    assert "time slot" not in reply.lower()
    assert not is_done

    # 2. Appointment time remains intact
    assert session.slots["appointment_time"] == "07:00 PM"
    assert session.is_completed is True
    assert session.state == SessionState.BOOKING_COMPLETED

def test_post_booking_wrapup_with_nahi_or_no_hangs_up_cleanly(protect_production_excel):
    """
    Regression Test:
    When caller says 'Nahi, bas itna hi' after booking, Maya delivers warm farewell and hangs up.
    """
    call_sid = "CA_POST_BOOK_WRAP_02"
    session = session_manager.get_or_create(call_sid, "+918824466229")
    session.is_completed = True
    session.state = SessionState.BOOKING_COMPLETED

    reply, is_done = llm_service.process_turn(call_sid, "Nahi, bas itna hi", confidence=0.85)

    assert is_done is True
    assert session.state == SessionState.CALL_WRAPUP
    assert any(w in reply.lower() for w in ("shukriya", "dhanyawad", "thank you", "goodbye", "namaste"))

def test_dual_gate_stt_low_confidence_with_meaningful_speech_not_dropped(protect_production_excel, monkeypatch):
    """
    Pillar 4 Regression Test:
    Caller speaks with low confidence (0.24) e.g. on Indian noisy line.
    Because the speech contains meaningful entities ('6:20 per book kar do'),
    system MUST NOT drop it with 'line clear nahi aayi', but process it!
    """
    call_sid = "CA_DUAL_GATE_01"
    session = session_manager.get_or_create(call_sid, "+918824466229")

    # Low confidence 0.24 for meaningful speech
    reply, is_done = llm_service.process_turn(call_sid, "6:20 per book kar do", confidence=0.24)

    # Must NOT ask to repeat line
    assert "line clear nahi aayi" not in reply.lower()
    assert "trouble hearing" not in reply.lower()

def test_immediate_slot_validation_offers_smart_alternatives(protect_production_excel, monkeypatch):
    """
    Pillar 2 & 3 Regression Test:
    Caller requests 5:00 PM (afternoon break).
    Maya MUST immediately explain doctor unavailable and offer real available alternatives
    (e.g. 06:30 PM, 07:00 PM) WITHOUT waiting for Name and Symptoms!
    """
    call_sid = "CA_IMMEDIATE_ALT_01"
    session = session_manager.get_or_create(call_sid, "+918824466229")
    session.language = "HINGLISH"

    # Mock Gemini returning candidate time 05:00 PM
    def mock_gemini(messages, system_prompt, include_tools=True):
        return {
            "candidates": [{
                "content": {
                    "parts": [
                        {"functionCall": {"name": "book_appointment", "args": {"date": "2026-11-25", "time_slot": "05:00 PM"}}},
                        {"text": "Ji 5 baje."}
                    ]
                }
            }]
        }
    monkeypatch.setattr(llm_service, "_call_gemini_api", mock_gemini)

    reply, is_done = llm_service.process_turn(call_sid, "Aaj sham ko 5:00 Baje", confidence=0.75)

    # Immediately explains unavailable and offers alternatives
    assert "unavailable" in reply.lower() or "break" in reply.lower() or "samay" in reply.lower()
    assert any(slot in reply for slot in ("06:00", "06:30", "07:00", "07:30"))
    assert session.slots["appointment_time"] is None  # Invalid slot cleared immediately

def test_sticky_language_anchor_preserves_hinglish_on_english_loanwords():
    """
    Pillar 5 Regression Test:
    Caller in Hinglish saying '6:20 per book kar do' must NOT flip to English due to loanword 'book'.
    """
    lang = detect_caller_language("6:20 per book kar do", current_lang="HINGLISH")
    assert lang == "HINGLISH", f"Expected HINGLISH, got {lang}"

def test_slot_conflict_bypassed_for_testing_demo(protect_production_excel):
    """
    Demo Testing Requirement:
    When ENABLE_SLOT_CONFLICT_CHECK is False (default), multiple bookings at the same valid clinic time
    (e.g. 06:00 PM) succeed smoothly for testing and demos without throwing collision errors.
    """
    temp_excel = protect_production_excel
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # First booking at 06:00 PM
    b1 = sheet_service.save_appointment(
        patient_name="Test Caller 1",
        phone_number="9829011111",
        date_str=tomorrow_str,
        time_slot="06:00 PM",
        symptoms="Cold"
    )
    assert b1.get("status") == "Confirmed"

    # Second booking at the EXACT SAME slot (06:00 PM)
    b2 = sheet_service.save_appointment(
        patient_name="Test Caller 2",
        phone_number="9829022222",
        date_str=tomorrow_str,
        time_slot="06:00 PM",
        symptoms="Fever"
    )
    assert b2.get("status") == "Confirmed"

def test_hindi_speech_with_zero_confidence_not_dropped(protect_production_excel):
    """
    Telephone STT Requirement:
    Twilio en-IN STT returns confidence 0.00 when caller speaks Roman Hindi ('Main Hindi mein bol raha hun.').
    System must NOT drop the utterance with 'line clear nahi aayi', and must detect HINGLISH!
    """
    call_sid = "CA_ZERO_CONF_HINDI_01"
    session = session_manager.get_or_create(call_sid, "+918824466229")

    reply, is_done = llm_service.process_turn(call_sid, "Main Hindi mein bol raha hun.", confidence=0.0)

    # Must NOT ask caller to repeat due to line noise
    assert "trouble hearing" not in reply.lower()
    assert "line clear nahi aayi" not in reply.lower()

    # Session language must be updated to HINGLISH
    assert session.language == "HINGLISH"
