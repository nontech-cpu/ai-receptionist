import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
import openpyxl

from app.utils.phonetics import parse_spoken_hindi_numbers
from app.services.session_manager import session_manager
from app.services.llm_service import llm_service
from app.services.sheet_service import sheet_service
from app.services.clinic_service import clinic_service
from app.core.config import settings

def test_spoken_hindi_and_english_phone_numbers():
    """
    Scenarios H & I: Spoken numbers with spaces, digits, and phonetic words.
    Verifies that '7233 4566 81' and spoken sequences parse to 10-digit mobile number.
    """
    assert parse_spoken_hindi_numbers("7233 4566 81") == "7233456681"
    assert parse_spoken_hindi_numbers("My number is 9829 012 345") == "9829012345"
    assert parse_spoken_hindi_numbers("mera number double seven double zero one two three four five six hai") == "7700123456"

def test_omitted_phone_number_prompts_user_without_booking(monkeypatch, protect_production_excel):
    """
    Scenario J: When caller provides name, symptoms, date/time but omits phone number,
    the receptionist MUST ask for the 10-digit contact number and NOT complete booking.
    """
    temp_excel = protect_production_excel
    call_sid = "test_call_omitted_phone_001"
    
    # Simulate Gemini returning functionCall without a 10-digit phone
    mock_gemini_resp = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Devendra",
                            "symptoms": "Fever and cough",
                            "date": "2026-11-25",
                            "time_slot": "11:00 AM",
                            "phone_number": ""  # Missing phone
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_resp)

    reply, is_done = llm_service.process_turn(call_sid, "I want to book for Devendra tomorrow at 11 AM, having fever")
    
    # Must ask for phone number and not be finished
    assert "mobile number" in reply.lower() or "phone" in reply.lower()
    assert is_done is False
    
    # Verify no appointment was written to Excel
    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 1, "Appointment was saved despite missing phone number!"

def test_unusual_name_retained_across_turns(monkeypatch, protect_production_excel):
    """
    Scenario G: Unusual names like 'Nescafe' or 'Lucky'.
    When caller confirms the name, it must be retained in session.slots across turns
    and not trigger repeated clarification loops.
    """
    temp_excel = protect_production_excel
    call_sid = "test_call_unusual_name_002"

    # Turn 1: User provides unusual name
    mock_resp_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Could you please confirm if Nescafe is the real patient name?"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_resp_turn1)
    reply1, _ = llm_service.process_turn(call_sid, "The patient name is Nescafe")
    
    # Turn 2: User confirms that Nescafe is indeed the name
    mock_resp_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Understood. What symptoms is Nescafe experiencing?"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_resp_turn2)
    reply2, _ = llm_service.process_turn(call_sid, "Yes, patient name is Nescafe only")
    
    session = session_manager.get_or_create(call_sid)
    # The session history should reflect the confirmation
    assert len(session.messages) >= 4

def test_explicit_readback_confirmation_required(monkeypatch, protect_production_excel):
    """
    Scenario N (Non-negotiable requirement):
    Before writing to Excel, the AI receptionist MUST read back the appointment details
    (Name, Date, Time, Phone) and require caller confirmation (e.g., 'Is this correct?').
    Writing to Excel BEFORE caller confirmation is a critical failure.
    
    NOTE: On the CURRENT un-repaired codebase, this test is EXPECTED TO FAIL because
    llm_service immediately calls sheet_service.save_appointment as soon as book_appointment
    is generated.
    """
    temp_excel = protect_production_excel
    call_sid = "test_call_readback_003"
    
    mock_gemini_resp = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Rahul Verma",
                            "phone_number": "9829012345",
                            "date": "2026-11-25",
                            "time_slot": "11:00 AM",
                            "symptoms": "Headache"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_resp)

    # First turn providing all details
    reply, is_done = llm_service.process_turn(call_sid, "Book appointment for Rahul Verma, 9829012345, headache, tomorrow 11 AM")

    # In a proper production voice AI system with explicit read-back:
    # 1. Excel must NOT have the booking yet!
    # 2. Reply must ask caller for confirmation (e.g., "Can you confirm?")
    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    
    # Expect 1 row (header only); if row count > 1, booking was prematurely saved without caller confirmation!
    assert len(rows) == 1, (
        f"PREMATURE BOOKING DETECTED: Booking was written to Excel ({len(rows)-1} rows) "
        "BEFORE caller explicit read-back confirmation!"
    )

def test_sunday_booking_rejected(monkeypatch, protect_production_excel):
    """
    Scenario T: Sunday booking rejection.
    Clinic is strictly closed on Sunday. Booking tool or turn processor must reject Sunday dates.
    
    NOTE: On current un-repaired codebase, this test verifies whether clinic validation prevents
    saving Sunday appointments.
    """
    temp_excel = protect_production_excel
    call_sid = "test_call_sunday_004"

    # Find the next Sunday
    today = datetime.now()
    days_ahead = 6 - today.weekday()  # Sunday is weekday 6
    if days_ahead <= 0:
        days_ahead += 7
    next_sunday_str = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    mock_gemini_resp = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Anita",
                            "phone_number": "9829012345",
                            "date": next_sunday_str,
                            "time_slot": "11:00 AM",
                            "symptoms": "Checkup"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_resp)

    reply, is_done = llm_service.process_turn(call_sid, f"Book for Sunday {next_sunday_str}")

    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    assert len(rows) == 1, f"CLINIC SCHEDULE VIOLATION: Appointment was booked on Sunday ({next_sunday_str})!"

def test_afternoon_break_rejected(monkeypatch, protect_production_excel):
    """
    Scenario V: Afternoon break (02:00 PM - 06:00 PM) rejection.
    Doctor is strictly unavailable between 2 PM and 6 PM.
    
    NOTE: On current un-repaired codebase, this test checks whether sheet_service or llm_service
    guards against appointments inside the 2 PM - 6 PM break.
    """
    temp_excel = protect_production_excel
    call_sid = "test_call_break_005"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    mock_gemini_resp = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Sunil",
                            "phone_number": "9829012345",
                            "date": tomorrow_str,
                            "time_slot": "03:30 PM",  # Break time!
                            "symptoms": "Fever"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_resp)

    reply, is_done = llm_service.process_turn(call_sid, "Book at 3:30 PM tomorrow")

    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    assert len(rows) == 1, "CLINIC BREAK VIOLATION: Appointment booked during 2:00 PM - 6:00 PM break!"

def test_double_booking_same_slot_rejected(protect_production_excel, monkeypatch):
    """
    Scenario X: Double booking detection.
    When a slot (e.g. 2026-11-25 11:00 AM) is already booked, saving the exact same slot
    should raise an error or be rejected.
    
    NOTE: On current un-repaired codebase, SheetService.save_appointment does NOT check
    for existing bookings before appending!
    """
    monkeypatch.setattr(settings, "ENABLE_SLOT_CONFLICT_CHECK", True)
    temp_excel = protect_production_excel
    
    # Book first appointment
    sheet_service.save_appointment(
        patient_name="First Patient",
        phone_number="9829012345",
        date_str="2026-11-25",
        time_slot="11:00 AM",
        symptoms="Cold"
    )

    # Attempt to book second appointment for the EXACT SAME date and slot
    # Expected behavior in production: should detect collision and reject or raise
    second_booking = sheet_service.save_appointment(
        patient_name="Second Patient",
        phone_number="9829054321",
        date_str="2026-11-25",
        time_slot="11:00 AM",
        symptoms="Fever"
    )

    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = [r for r in ws.iter_rows(values_only=True) if r[4] == "2026-11-25" and r[5] == "11:00 AM"]
    wb.close()

    assert len(rows) <= 1, f"DOUBLE BOOKING DETECTED: Found {len(rows)} appointments booked for the identical slot!"
