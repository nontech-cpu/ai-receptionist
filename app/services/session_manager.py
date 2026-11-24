from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from app.services.state_machine import SessionState, MANDATORY_SLOTS

@dataclass
class CallSession:
    call_sid: str
    caller_phone: str
    state: SessionState = SessionState.GREETING
    explicit_confirmation_requested: bool = False
    slots: Dict[str, Optional[str]] = field(default_factory=lambda: {
        "patient_name": None,
        "symptoms": None,
        "appointment_date": None,
        "appointment_time": None,
        "phone_number": None,
        "duration": "Recently noticed",
        # Aliases for backward compatibility
        "date": None,
        "time_slot": None
    })
    messages: List[dict] = field(default_factory=list)
    is_completed: bool = False
    appointment_data: Optional[dict] = None
    silence_count: int = 0  # Tracks consecutive silence gaps
    language: str = "ENGLISH"  # Persists caller conversation language (sticky session memory)
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)

    def set_slot(self, key: str, value: Optional[str]):
        """Sets a slot and maintains backward-compatible aliases."""
        self.slots[key] = value
        if key == "appointment_date":
            self.slots["date"] = value
        elif key == "date":
            self.slots["appointment_date"] = value
        elif key == "appointment_time":
            self.slots["time_slot"] = value
        elif key == "time_slot":
            self.slots["appointment_time"] = value

class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, CallSession] = {}

    def get_or_create(self, call_sid: str, caller_phone: str = "") -> CallSession:
        if call_sid not in self._sessions:
            session = CallSession(
                call_sid=call_sid,
                caller_phone=caller_phone
            )
            if caller_phone:
                digits = "".join(c for c in caller_phone if c.isdigit())
                # Never assign Twilio bot trunk number to patient phone slot
                if len(digits) >= 10 and not digits.endswith("7372212163"):
                    clean_phone = digits[-10:]
                    session.set_slot("phone_number", clean_phone)
            self._sessions[call_sid] = session
        else:
            session = self._sessions[call_sid]
            session.last_active = datetime.now()
            if caller_phone and not session.caller_phone:
                session.caller_phone = caller_phone
                digits = "".join(c for c in caller_phone if c.isdigit())
                if len(digits) >= 10 and not digits.endswith("7372212163") and not session.slots.get("phone_number"):
                    clean_phone = digits[-10:]
                    session.set_slot("phone_number", clean_phone)
        return self._sessions[call_sid]

    def add_user_message(self, call_sid: str, text: str):
        session = self.get_or_create(call_sid)
        session.messages.append({
            "role": "user",
            "parts": [{"text": text}]
        })
        session.silence_count = 0  # Reset silence counter whenever user speaks!
        session.last_active = datetime.now()

    def add_model_message(self, call_sid: str, text: str):
        session = self.get_or_create(call_sid)
        session.messages.append({
            "role": "model",
            "parts": [{"text": text}]
        })
        session.last_active = datetime.now()

    def mark_completed(self, call_sid: str, appointment_data: dict = None):
        session = self.get_or_create(call_sid)
        session.is_completed = True
        session.state = SessionState.BOOKING_COMPLETED
        if appointment_data:
            session.appointment_data = appointment_data

    def get_session(self, call_sid: str) -> Optional[CallSession]:
        return self._sessions.get(call_sid)

session_manager = SessionManager()
