from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

@dataclass
class CallSession:
    call_sid: str
    caller_phone: str
    slots: dict = field(default_factory=lambda: {
        "patient_name": None,
        "symptoms": None,
        "duration": None,
        "time_slot": None,
        "date": None,
        "phone_number": None
    })
    messages: List[dict] = field(default_factory=list)
    is_completed: bool = False
    appointment_data: Optional[dict] = None
    silence_count: int = 0  # Tracks consecutive silence gaps
    language: str = "HINDI"  # Persists caller conversation language
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)

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
                if len(digits) >= 10:
                    session.slots["phone_number"] = digits[-10:]
            self._sessions[call_sid] = session
        else:
            session = self._sessions[call_sid]
            session.last_active = datetime.now()
            if caller_phone and not session.caller_phone:
                session.caller_phone = caller_phone
                digits = "".join(c for c in caller_phone if c.isdigit())
                if len(digits) >= 10 and not session.slots.get("phone_number"):
                    session.slots["phone_number"] = digits[-10:]
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
        if appointment_data:
            session.appointment_data = appointment_data

    def get_session(self, call_sid: str) -> Optional[CallSession]:
        return self._sessions.get(call_sid)

session_manager = SessionManager()
