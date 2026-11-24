import re
import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, date, timedelta

from app.core.config import settings
from app.services.clinic_service import clinic_service
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.utils.phonetics import parse_spoken_hindi_numbers

logger = logging.getLogger(__name__)

class SessionState(str, Enum):
    GREETING = "GREETING"
    UNDERSTAND_REQUEST = "UNDERSTAND_REQUEST"
    COLLECTING_DETAILS = "COLLECTING_DETAILS"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    VALIDATING = "VALIDATING"
    AWAITING_EXPLICIT_CONFIRMATION = "AWAITING_EXPLICIT_CONFIRMATION"
    BOOKING_COMPLETED = "BOOKING_COMPLETED"
    CALL_WRAPUP = "CALL_WRAPUP"

MANDATORY_SLOTS = [
    "patient_name",
    "symptoms",
    "appointment_date",
    "appointment_time",
    "phone_number"
]

AFFIRMATIVE_WORDS = {
    "yes", "yeah", "yep", "sure", "correct", "confirm", "confirm it", "haan",
    "haanji", "hanji", "sahi", "theek", "theek hai", "bilkul", "kar do",
    "kardo", "kar dijiye", "pakka", "pakka kar do", "all good", "perfect",
    "right", "ok", "okay", "done", "please book", "book it",
    # Devanagari
    "हाँ", "जी हाँ", "हाँजी", "पक्का", "पक्का कर दीजिये", "कर दीजिये", "ज़रूर",
    "ठीक", "ठीक है", "सही है", "बिल्कुल"
}

REJECTION_WORDS = {
    "no", "nope", "nahi", "nahin", "na", "galat", "wrong", "change", "badal",
    "badlo", "mat", "mat karo", "cancel", "wait", "ruko", "not this",
    # Devanagari
    "नहीं", "ना", "मत", "मत करो", "गलत", "बदलो"
}

WRAPUP_WORDS = {
    "thanks", "thank you", "bye", "goodbye", "bas", "bus", "nothing",
    "nothing else", "all set", "dhanyawad", "shukriya", "that's all", "thats all",
    "no", "nope", "nahi", "nahin", "na", "kuch nahi", "nahi chahiye", "kuch nahi chahiye", "nahi bas",
    # Devanagari
    "धन्यवाद", "शुक्रिया", "बस", "नमस्ते", "नहीं", "कुछ नहीं", "ना", "नहीं चाहिए"
}

class ReceptionistStateMachine:
    """
    Deterministic Finite State Machine governing the appointment lifecycle:
    GREETING -> UNDERSTAND_REQUEST -> COLLECTING_DETAILS -> VALIDATING
    -> AWAITING_EXPLICIT_CONFIRMATION -> BOOKING_COMPLETED -> CALL_WRAPUP
    """

    @staticmethod
    def is_affirmative(text: str) -> bool:
        clean = re.sub(r'[,.?!।\-\'\"`~#*;:\(\)]', ' ', text).lower().strip()
        words = set(clean.split())
        if words & AFFIRMATIVE_WORDS:
            return True
        return any(phrase in clean for phrase in [
            "kar dijiye", "kar do", "theek hai", "sahi hai", "confirm it",
            "please book", "पक्का कर दीजिये", "पक्का कर दो", "हाँ", "जी हाँ"
        ])

    @staticmethod
    def is_rejection(text: str) -> bool:
        clean = re.sub(r'[,.?!।\-\'\"`~#*;:\(\)]', ' ', text).lower().strip()
        words = set(clean.split())
        if words & REJECTION_WORDS:
            return True
        return any(phrase in clean for phrase in [
            "mat karo", "galat hai", "wrong number", "change time", "change date",
            "मत करो", "गलत है", "नहीं"
        ])

    @staticmethod
    def is_wrapup(text: str) -> bool:
        clean = re.sub(r'[,.?!।\-\'\"`~#*;:\(\)]', ' ', text).lower().strip()
        words = set(clean.split())
        if words & WRAPUP_WORDS:
            return True
        return any(phrase in clean for phrase in [
            "thank you", "that's all", "thats all", "kuch nahi", "nahi chahiye",
            "kuch nahi chahiye", "call end", "call disconnect", "bas itna hi",
            "कुछ नहीं", "नहीं चाहिए", "बस इतना ही"
        ])

    @staticmethod
    def clean_slot_value(slot_name: str, value: Any) -> Optional[str]:
        if value is None:
            return None
        val_str = str(value).strip()
        if not val_str or val_str.lower() in ("none", "null", "undefined"):
            return None

        if slot_name == "patient_name":
            # Strip titles and generic placeholders
            cleaned = val_str.title()
            lower = cleaned.lower()
            if lower in ("patient", "user", "someone", "caller", "doctor", "dr"):
                return None
            # Requirement 4: Medical symptoms must NEVER automatically become a patient name
            symptom_words = {
                "cough", "cold", "fever", "headache", "sore throat", "chest pain", "stomach pain",
                "vomiting", "nausea", "body pain", "allergy", "khansi", "bukhar", "sardi", "jukam",
                "pet dard", "sir dard", "ulti", "dast", "kamzori"
            }
            if lower in symptom_words:
                logger.warning(f"[STATE MACHINE] Rejecting symptom '{cleaned}' from patient_name slot.")
                return None
            # Requirement 4: Brand names (e.g. Nescafé, Nice Cafe) require explicit confirmation
            if lower in ("nescafe", "nescafé", "nice cafe", "coffee"):
                return None
            return cleaned

        elif slot_name == "phone_number":
            digits = "".join(c for c in val_str if c.isdigit())
            if len(digits) >= 10:
                digits = digits[-10:]
            if len(digits) == 10 and not digits.endswith("7372212163"):
                return digits
            return None

        elif slot_name == "appointment_date":
            # Match YYYY-MM-DD
            try:
                dt = datetime.strptime(val_str, "%Y-%m-%d").date()
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                return None

        elif slot_name == "appointment_time":
            parsed = sheet_service.parse_time_str(val_str)
            if parsed:
                return sheet_service.format_canonical_time(parsed)
            return None

        elif slot_name == "symptoms":
            lower_s = val_str.lower()
            if lower_s in ("general consultation", "routine", "none"):
                return None
            # Requirement 4: Brands or beverages must never become symptoms
            if lower_s in ("nescafe", "nescafé", "nice cafe", "coffee", "tea", "hotel", "cafe"):
                logger.warning(f"[STATE MACHINE] Rejecting non-symptom brand '{val_str}' from symptoms slot.")
                return None
            return val_str

        return val_str

    def update_session_slots(self, session, candidate_slots: Dict[str, Any]) -> List[str]:
        """
        Updates session slots with candidate extractions.
        Returns list of slot keys that were updated/changed.
        """
        changed = []
        for key in MANDATORY_SLOTS:
            raw_val = candidate_slots.get(key)
            if raw_val is None:
                continue

            cleaned_val = self.clean_slot_value(key, raw_val)
            if cleaned_val is None:
                continue

            current_val = session.slots.get(key)
            if current_val != cleaned_val:
                session.slots[key] = cleaned_val
                changed.append(key)
                logger.info(f"[STATE MACHINE] Slot '{key}' updated -> '{cleaned_val}' (CallSid={session.call_sid[-6:]})")

        # Also preserve auxiliary duration
        if "duration" in candidate_slots and candidate_slots["duration"]:
            session.slots["duration"] = str(candidate_slots["duration"]).strip()

        # If a slot changed while awaiting confirmation or validating, invalidate confirmation!
        if changed and session.state in (SessionState.AWAITING_EXPLICIT_CONFIRMATION, SessionState.VALIDATING):
            logger.info(f"[STATE MACHINE] Slot correction detected ({changed}). Invalidating previous confirmation.")
            session.explicit_confirmation_requested = False
            session.state = SessionState.VALIDATING

        return changed

    def get_missing_slots(self, session) -> List[str]:
        """Returns mandatory slot names that have not yet been collected."""
        missing = []
        for slot in MANDATORY_SLOTS:
            val = session.slots.get(slot)
            if not val:
                missing.append(slot)
        return missing

    def is_all_slots_collected(self, session) -> bool:
        return len(self.get_missing_slots(session)) == 0

    def format_readback_prompt(self, session, lang: str = "ENGLISH") -> str:
        """Constructs explicit read-back confirmation message."""
        p = clinic_service.profile
        doc_name = p.doctor_name
        name = session.slots.get("patient_name", "Patient")
        symptoms = session.slots.get("symptoms", "consultation")
        date_str = session.slots.get("appointment_date", "")
        time_str = session.slots.get("appointment_time", "")
        phone = session.slots.get("phone_number", "")
        spaced_phone = " ".join(phone) if phone else ""

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            friendly_date = dt.strftime("%A, %B %d")
        except Exception:
            friendly_date = date_str

        clean_time = time_str.replace(":00", "")

        if lang == "ENGLISH":
            return (
                f"I have noted an appointment for {name} with {doc_name} on {friendly_date} at {time_str} "
                f"for {symptoms}. Contact number is {spaced_phone}. Shall I go ahead and confirm this booking?"
            )
        elif lang == "HINDI_DEVANAGARI":
            return (
                f"मैंने {name} जी के लिए {friendly_date} को {clean_time} बजे {symptoms} के लिए "
                f"{doc_name} से अपॉइंटमेंट नोट कर लिया है। फ़ोन नंबर {spaced_phone} है। क्या मैं यह अपॉइंटमेंट पक्का कर दूँ?"
            )
        else:  # HINGLISH
            return (
                f"Maine {name} ji ke liye {friendly_date} ko {clean_time} baje {symptoms} ke liye "
                f"{doc_name} se appointment note kar liya hai. Contact number {spaced_phone} hai. Kya main ise confirm kar doon?"
            )

    def format_confirmation_success(self, session, booking: dict, lang: str = "ENGLISH") -> str:
        p = clinic_service.profile
        doc_name = p.doctor_name
        name = booking.get("patient_name", "")
        date_str = booking.get("date", "")
        time_str = booking.get("time_slot", "")
        phone = booking.get("phone_number", "")
        spaced_phone = " ".join(phone)

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            friendly_date = dt.strftime("%A, %B %d")
        except Exception:
            friendly_date = date_str

        clean_time = time_str.replace(":00", "")

        if lang == "ENGLISH":
            return (
                f"Certainly! The appointment for {name} with {doc_name} is confirmed for {friendly_date} "
                f"at {time_str}. Registered phone number is {spaced_phone}. Would you like to know the clinic address or consultation fees?"
            )
        elif lang == "HINDI_DEVANAGARI":
            return (
                f"बिल्कुल! {name} जी का {doc_name} के साथ अपॉइंटमेंट {friendly_date} को {clean_time} बजे पक्का हो गया है। "
                f"फ़ोन नंबर {spaced_phone} नोट कर लिया है। क्या आपको क्लिनिक का पता या परामर्श शुल्क जानना है?"
            )
        else:  # HINGLISH
            return (
                f"Bilkul! {name} ji ka {doc_name} ke sath appointment {friendly_date} ko {clean_time} baje confirm ho gaya hai. "
                f"Phone number {spaced_phone} note kar liya hai. Kya aapko clinic ka address ya consultation fees ke baare mein poochna hai?"
            )

state_machine = ReceptionistStateMachine()
