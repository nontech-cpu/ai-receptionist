import openpyxl
from datetime import datetime, timedelta
import pytest
from unittest.mock import MagicMock

from app.services.llm_service import llm_service
from app.services.session_manager import session_manager
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.services.state_machine import SessionState

def test_complete_english_booking_flow(monkeypatch, protect_production_excel, mock_external_services):
    """
    Sub-Phase C Requirement: Complete English booking flow with explicit confirmation.
    """
    temp_excel = protect_production_excel
    call_sid = "test_eng_flow_001"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: Caller provides all 5 details in English
    mock_gemini_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Rahul Verma",
                            "symptoms": "Severe headache",
                            "date": tomorrow_str,
                            "time_slot": "11:00 AM",
                            "phone_number": "9829012345"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn1)

    reply1, is_done1 = llm_service.process_turn(
        call_sid,
        f"Hi, I want to book an appointment for Rahul Verma on {tomorrow_str} at 11 AM for severe headache, my phone is 9829012345"
    )

    # Must ask for explicit confirmation in English
    assert is_done1 is False
    assert "Rahul Verma" in reply1
    assert "11 AM" in reply1 or "11:00 AM" in reply1
    assert "confirm" in reply1.lower()
    
    # Must NOT be written to Excel yet!
    wb = openpyxl.load_workbook(temp_excel)
    assert len(list(wb.active.iter_rows(values_only=True))) == 1
    wb.close()
    assert len(mock_external_services["sent_alerts"]) == 0

    # Turn 2: Caller says "Yes, please confirm"
    mock_gemini_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes, please confirm."}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn2)

    reply2, is_done2 = llm_service.process_turn(call_sid, "Yes, please confirm")

    # Must confirm booking in English
    assert "confirmed" in reply2.lower()
    assert "Dr. Sharma" in reply2

    # Now Excel MUST have exactly 1 record!
    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2
    assert rows[1][2] == "Rahul Verma"
    assert rows[1][3] == "9829012345"
    assert rows[1][4] == tomorrow_str
    assert rows[1][5] == "11:00 AM"

    # Exactly 1 Telegram alert sent
    assert len(mock_external_services["sent_alerts"]) == 1

def test_complete_hindi_booking_flow(monkeypatch, protect_production_excel, mock_external_services):
    """
    Sub-Phase C Requirement: Complete Hindi booking flow in Devanagari script.
    """
    temp_excel = protect_production_excel
    call_sid = "test_hindi_flow_002"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: Devanagari speech
    mock_gemini_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "राहुल शर्मा",
                            "symptoms": "तेज़ बुखार",
                            "date": tomorrow_str,
                            "time_slot": "11:30 AM",
                            "phone_number": "9829054321"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn1)

    reply1, _ = llm_service.process_turn(
        call_sid,
        f"नमस्ते, मुझे राहुल शर्मा के लिए कल {tomorrow_str} को 11:30 बजे तेज़ बुखार के लिए अपॉइंटमेंट चाहिए, नंबर 9829054321 है।"
    )

    # Devanagari readback
    assert "अपॉइंटमेंट" in reply1 or "पक्का" in reply1
    assert "9 8 2 9 0 5 4 3 2 1" in reply1

    # Turn 2: Affirmation in Hindi
    mock_gemini_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "हाँ, पक्का कर दीजिये।"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn2)

    reply2, _ = llm_service.process_turn(call_sid, "हाँ, पक्का कर दीजिये।")
    assert "पक्का" in reply2 or "बिल्कुल" in reply2

    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2
    assert rows[1][2] == "राहुल शर्मा"

def test_complete_hinglish_booking_flow(monkeypatch, protect_production_excel, mock_external_services):
    """
    Sub-Phase C Requirement: Complete Roman Hinglish booking flow.
    """
    temp_excel = protect_production_excel
    call_sid = "test_hinglish_flow_003"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    mock_gemini_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Devendra",
                            "symptoms": "Pet dard",
                            "date": tomorrow_str,
                            "time_slot": "06:00 PM",
                            "phone_number": "9829099999"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn1)

    reply1, _ = llm_service.process_turn(
        call_sid,
        f"Dr Sharma se milna hai kal sham 6 baje Devendra ke liye pet dard hai, number 9829099999"
    )

    assert "Devendra" in reply1
    assert "confirm" in reply1.lower()

    # Turn 2: Hinglish confirmation
    mock_gemini_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Haanji bilkul confirm kardo"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_gemini_turn2)

    reply2, _ = llm_service.process_turn(call_sid, "Haanji bilkul confirm kardo")
    assert "confirm ho gaya" in reply2.lower() or "bilkul" in reply2.lower()

def test_correction_after_readback_and_regeneration(monkeypatch, protect_production_excel, mock_external_services):
    """
    Sub-Phase C Requirement:
    Caller correcting a detail after readback invalidates previous confirmation,
    updates only that slot, and presents a fresh readback.
    """
    temp_excel = protect_production_excel
    call_sid = "test_correction_turn_004"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: 11:00 AM
    mock_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Suresh",
                            "symptoms": "Cold",
                            "date": tomorrow_str,
                            "time_slot": "11:00 AM",
                            "phone_number": "9829012345"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn1)
    reply1, _ = llm_service.process_turn(call_sid, "Book Suresh tomorrow 11 AM cold 9829012345")
    assert "11 AM" in reply1 or "11:00 AM" in reply1

    # Turn 2: Caller says "No, actually please make it 6 PM instead"
    mock_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "time_slot": "06:00 PM"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn2)
    reply2, _ = llm_service.process_turn(call_sid, "No, actually please make it 6 PM instead")

    # Fresh readback must mention 06:00 PM and NOT be saved yet
    assert "06:00 PM" in reply2 or "6:00 PM" in reply2 or "6 PM" in reply2
    wb = openpyxl.load_workbook(temp_excel)
    assert len(list(wb.active.iter_rows(values_only=True))) == 1
    wb.close()

    # Turn 3: Caller confirms
    mock_turn3 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes that is correct"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn3)
    reply3, _ = llm_service.process_turn(call_sid, "Yes that is correct")

    assert "confirmed" in reply3.lower()
    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2
    assert rows[1][5] == "06:00 PM"  # Saved with the corrected time!

def test_availability_changing_before_confirmation(monkeypatch, protect_production_excel):
    """
    Sub-Phase C Requirement:
    If another patient books the slot while caller is awaiting confirmation,
    revalidation before booking catches the collision and offers alternatives.
    """
    temp_excel = protect_production_excel
    call_sid = "test_avail_race_005"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: Caller receives readback for 11:00 AM
    mock_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Late Caller",
                            "symptoms": "Fever",
                            "date": tomorrow_str,
                            "time_slot": "11:00 AM",
                            "phone_number": "9829012345"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn1)
    llm_service.process_turn(call_sid, "Book Late Caller tomorrow 11 AM fever 9829012345")

    # RACE CONDITION SIMULATION:
    # Another caller takes 11:00 AM right before Late Caller confirms!
    sheet_service.save_appointment(
        patient_name="Early Bird",
        phone_number="9829088888",
        date_str=tomorrow_str,
        time_slot="11:00 AM",
        symptoms="Checkup"
    )

    # Turn 2: Late Caller says "Yes, confirm it!"
    mock_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes, confirm it!"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn2)
    reply2, _ = llm_service.process_turn(call_sid, "Yes, confirm it!")

    # Must detect that slot was taken and NOT book!
    assert "already booked" in reply2.lower() or "overlaps" in reply2.lower() or "another" in reply2.lower()
    
    # In Excel, only the Early Bird appointment exists!
    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2
    assert rows[1][2] == "Early Bird"

def test_gemini_timeout_handled_gracefully(monkeypatch, protect_production_excel):
    """
    Sub-Phase C Requirement:
    Gemini timeout or malformed response falls back to natural listening cue
    without crashing or losing session slots.
    """
    call_sid = "test_timeout_006"
    session = session_manager.get_or_create(call_sid)
    session.set_slot("patient_name", "Sunita")

    # Simulate Gemini timeout exception
    def raise_timeout(*args, **kwargs):
        raise TimeoutError("Gemini LLM exceeded 3.2s latency budget.")
    
    monkeypatch.setattr(llm_service, "_call_gemini_api", raise_timeout)

    reply, is_done = llm_service.process_turn(call_sid, "Hello, can you hear me?")

    # Natural fallback cue returned
    assert len(reply) > 5
    assert is_done is False
    # Slot preserved!
    assert session.slots["patient_name"] == "Sunita"

def test_duplicate_webhook_no_duplicate_booking_or_telegram(monkeypatch, protect_production_excel, mock_external_services):
    """
    Sub-Phase C Requirement:
    Duplicate webhook deliveries for the same CallSid do not produce duplicate bookings
    or duplicate Telegram alerts.
    """
    temp_excel = protect_production_excel
    call_sid = "test_webhook_dup_007"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Initial Turn 1:
    mock_turn1 = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "book_appointment",
                        "args": {
                            "patient_name": "Idempotent User",
                            "symptoms": "Cough",
                            "date": tomorrow_str,
                            "time_slot": "12:00 PM",
                            "phone_number": "9829012345"
                        }
                    }
                }]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn1)
    llm_service.process_turn(call_sid, "Details")

    # Confirmation Turn 2:
    mock_turn2 = {
        "candidates": [{
            "content": {
                "parts": [{"text": "Yes please confirm"}]
            }
        }]
    }
    monkeypatch.setattr(llm_service, "_call_gemini_api", lambda *args, **kwargs: mock_turn2)
    reply_a, _ = llm_service.process_turn(call_sid, "Yes please confirm")
    assert "confirmed" in reply_a.lower()

    # Network retry: Webhook delivery arrives again with identical call_sid and user speech:
    reply_b, _ = llm_service.process_turn(call_sid, "Yes please confirm")

    # Exactly 1 row in Excel
    wb = openpyxl.load_workbook(temp_excel)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2, f"Expected 2 rows (header + 1 record), found {len(rows)}"

    # Exactly 1 Telegram alert
    assert len(mock_external_services["sent_alerts"]) == 1
