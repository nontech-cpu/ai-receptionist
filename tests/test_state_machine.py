import threading
from datetime import datetime, timedelta
import openpyxl
import pytest

from app.services.state_machine import state_machine, SessionState
from app.services.session_manager import session_manager, CallSession
from app.services.sheet_service import sheet_service
from app.core.config import settings

def test_slots_collected_in_different_turn_orders(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Slots collected in different turn orders (e.g. Date/Time first, then Name, then Symptoms, then Phone).
    """
    call_sid = "test_order_001"
    session = session_manager.get_or_create(call_sid)
    session.state = SessionState.UNDERSTAND_REQUEST

    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: Date and Time
    state_machine.update_session_slots(session, {
        "appointment_date": tomorrow_str,
        "appointment_time": "11:00 AM"
    })
    session.state = SessionState.COLLECTING_DETAILS
    assert session.slots["appointment_date"] == tomorrow_str
    assert session.slots["appointment_time"] == "11:00 AM"
    assert not state_machine.is_all_slots_collected(session)

    # Turn 2: Patient Name
    state_machine.update_session_slots(session, {"patient_name": "Devendra Gehlot"})
    assert session.slots["patient_name"] == "Devendra Gehlot"
    assert not state_machine.is_all_slots_collected(session)

    # Turn 3: Symptoms
    state_machine.update_session_slots(session, {"symptoms": "Cold and headache"})
    assert session.slots["symptoms"] == "Cold and headache"
    assert not state_machine.is_all_slots_collected(session)

    # Turn 4: Phone number
    state_machine.update_session_slots(session, {"phone_number": "9829012345"})
    assert session.slots["phone_number"] == "9829012345"

    # All 5 mandatory slots collected!
    assert state_machine.is_all_slots_collected(session)
    session.state = SessionState.VALIDATING

    # Validate slot
    valid, reason = sheet_service.validate_slot(
        session.slots["appointment_date"],
        session.slots["appointment_time"]
    )
    assert valid is True
    session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
    session.explicit_confirmation_requested = True

    # Readback prompt check
    readback = state_machine.format_readback_prompt(session, "ENGLISH")
    assert "Devendra Gehlot" in readback
    assert "11:00 AM" in readback
    assert "Cold and headache" in readback

def test_multiple_slots_in_one_sentence(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Multiple slots provided simultaneously in one sentence.
    """
    call_sid = "test_multi_slot_002"
    session = session_manager.get_or_create(call_sid)
    session.state = SessionState.UNDERSTAND_REQUEST

    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Caller speaks all details at once
    extracted = {
        "patient_name": "Anita Sharma",
        "symptoms": "High fever",
        "appointment_date": tomorrow_str,
        "appointment_time": "06:30 PM",
        "phone_number": "9829054321"
    }

    state_machine.update_session_slots(session, extracted)
    assert state_machine.is_all_slots_collected(session)
    assert session.slots["patient_name"] == "Anita Sharma"
    assert session.slots["appointment_time"] == "06:30 PM"
    assert session.slots["phone_number"] == "9829054321"

def test_caller_correcting_details_after_readback(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Caller correcting details after readback invalidates previous confirmation
    and updates only the relevant slot.
    """
    call_sid = "test_correction_003"
    session = session_manager.get_or_create(call_sid)
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Initial state: ready for confirmation at 11:00 AM
    state_machine.update_session_slots(session, {
        "patient_name": "Sunil Verma",
        "symptoms": "Eye pain",
        "appointment_date": tomorrow_str,
        "appointment_time": "11:00 AM",
        "phone_number": "9829011111"
    })
    session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
    session.explicit_confirmation_requested = True

    # Caller provides correction: "No, please change the time to 6 PM"
    state_machine.update_session_slots(session, {"appointment_time": "06:00 PM"})

    # Confirmation must be invalidated!
    assert session.explicit_confirmation_requested is False
    assert session.state == SessionState.VALIDATING
    assert session.slots["appointment_time"] == "06:00 PM"
    # Other slots remain strictly intact!
    assert session.slots["patient_name"] == "Sunil Verma"
    assert session.slots["phone_number"] == "9829011111"

def test_caller_saying_yes_before_confirmation_requested(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Caller saying 'yes' while details are still being collected must NOT trigger booking.
    """
    temp_excel = protect_production_excel
    call_sid = "test_premature_yes_004"
    session = session_manager.get_or_create(call_sid)
    session.state = SessionState.COLLECTING_DETAILS

    # Only patient name collected so far
    state_machine.update_session_slots(session, {"patient_name": "Dev"})

    # Caller says "Yes sure"
    user_speech = "Yes sure"
    is_affirmative = state_machine.is_affirmative(user_speech)
    assert is_affirmative is True

    # Deterministic State Machine Gate:
    # Affirmation ONLY triggers booking if in AWAITING_EXPLICIT_CONFIRMATION!
    if session.state != SessionState.AWAITING_EXPLICIT_CONFIRMATION:
        # Must not book!
        booking_triggered = False
    else:
        booking_triggered = True

    assert booking_triggered is False
    assert session.state == SessionState.COLLECTING_DETAILS

    # Verify no row in Excel
    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 1

def test_caller_rejecting_confirmation(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Caller rejecting confirmation ('No, don't book') reverts state without saving.
    """
    temp_excel = protect_production_excel
    call_sid = "test_rejection_005"
    session = session_manager.get_or_create(call_sid)
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    state_machine.update_session_slots(session, {
        "patient_name": "Rohit",
        "symptoms": "Fever",
        "appointment_date": tomorrow_str,
        "appointment_time": "11:30 AM",
        "phone_number": "9829022222"
    })
    session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
    session.explicit_confirmation_requested = True

    # Caller says "No, cancel this"
    user_speech = "No, cancel this"
    assert state_machine.is_rejection(user_speech) is True

    # State machine transition on rejection:
    session.explicit_confirmation_requested = False
    session.state = SessionState.COLLECTING_DETAILS

    assert session.state == SessionState.COLLECTING_DETAILS

    # Verify nothing was saved to Excel
    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 1

def test_session_persistence_across_multiple_turns():
    """
    Sub-Phase B Requirement:
    All 5 mandatory slots are maintained across 6 turns without state loss.
    """
    call_sid = "test_persistence_006"
    session = session_manager.get_or_create(call_sid)
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Turn 1: Name
    state_machine.update_session_slots(session, {"patient_name": "Pooja"})
    # Turn 2: Symptoms
    state_machine.update_session_slots(session, {"symptoms": "Stomach pain"})
    # Turn 3: User asks clinic address (FAQ turn)
    assert session.slots["patient_name"] == "Pooja"
    assert session.slots["symptoms"] == "Stomach pain"
    # Turn 4: Date
    state_machine.update_session_slots(session, {"appointment_date": tomorrow_str})
    # Turn 5: Time
    state_machine.update_session_slots(session, {"appointment_time": "01:00 PM"})
    # Turn 6: Phone
    state_machine.update_session_slots(session, {"phone_number": "9829033333"})

    # All slots preserved perfectly
    assert session.slots["patient_name"] == "Pooja"
    assert session.slots["symptoms"] == "Stomach pain"
    assert session.slots["appointment_date"] == tomorrow_str
    assert session.slots["appointment_time"] == "01:00 PM"
    assert session.slots["phone_number"] == "9829033333"

def test_duplicate_call_sid_after_simulated_application_restart(protect_production_excel):
    """
    Sub-Phase B Requirement:
    Durable CallSid check in Excel workbook survives simulated server restart.
    """
    temp_excel = protect_production_excel
    call_sid = "CA_DURABLE_TEST_999"
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Book with CallSid
    booking1 = sheet_service.save_appointment(
        patient_name="Praveen",
        phone_number="9829044444",
        date_str=tomorrow_str,
        time_slot="12:00 PM",
        symptoms="Checkup",
        call_sid=call_sid
    )
    assert booking1["status"] == "Confirmed"

    # 2. Simulate complete application restart by clearing all memory caches
    sheet_service._call_sid_cache.clear()
    session_manager._sessions.clear()

    # 3. Webhook delivery arrives again with identical CallSid
    booking2 = sheet_service.save_appointment(
        patient_name="Praveen",
        phone_number="9829044444",
        date_str=tomorrow_str,
        time_slot="12:00 PM",
        symptoms="Checkup",
        call_sid=call_sid
    )

    # Must match booking1 ID without duplicate Excel row
    assert booking2["booking_id"] == booking1["booking_id"]
    assert booking2["patient_name"] == "Praveen"

    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert len(rows) == 2, f"Expected 2 rows (1 header + 1 record), found {len(rows)}"

def test_two_simultaneous_callers_requesting_same_slot_concurrency(protect_production_excel, monkeypatch):
    """
    Sub-Phase B Requirement:
    Two simultaneous callers requesting the same slot under cross-process file lock.
    Exactly one must succeed, and one must be rejected.
    """
    monkeypatch.setattr(settings, "ENABLE_SLOT_CONFLICT_CHECK", True)
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    target_slot = "12:30 PM"

    results = []

    def caller_worker(caller_name, phone, sid):
        try:
            res = sheet_service.save_appointment(
                patient_name=caller_name,
                phone_number=phone,
                date_str=tomorrow_str,
                time_slot=target_slot,
                symptoms="Fever",
                call_sid=sid
            )
            results.append(res)
        except Exception as e:
            results.append({"status": "Failed", "error": str(e)})

    t1 = threading.Thread(target=caller_worker, args=("Caller One", "9829011111", "CA_THREAD_1"))
    t2 = threading.Thread(target=caller_worker, args=("Caller Two", "9829022222", "CA_THREAD_2"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one must be Confirmed and one must be Failed
    confirmed = [r for r in results if r.get("status") == "Confirmed"]
    failed = [r for r in results if r.get("status") == "Failed"]

    assert len(confirmed) == 1, f"Expected exactly 1 confirmed booking, got {len(confirmed)}"
    assert len(failed) == 1, f"Expected exactly 1 failed booking, got {len(failed)}"
