import json
import logging
import re
import time
import requests
from datetime import datetime, timedelta
from typing import AsyncGenerator, Optional, Tuple, Dict, Any

from app.core.config import settings
from app.core.prompt import get_receptionist_prompt
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.services.session_manager import session_manager
from app.services.state_machine import state_machine, SessionState, MANDATORY_SLOTS
from app.services.clinic_service import clinic_service
from app.services.speech_normalizer import speech_normalizer
from app.utils.phonetics import parse_spoken_hindi_numbers

logger = logging.getLogger(__name__)

# Reusable HTTP Session with Connection: close
http_session = requests.Session()
http_session.headers.update({"Connection": "close"})

TOTAL_LLM_TIMEOUT_BUDGET = 3.2

FAST_MODELS = [
    ("gemini-flash-lite-latest", 3.0)
]

FALLBACK_LISTENING_CUES = {
    "HINGLISH": [
        "Haanji sun rahi hoon, boliye.",
        "Ji bilkul, sun rahi hoon.",
        "Haanji, main note kar rahi hoon, kahiye.",
        "Ji boliye, main sun rahi hoon."
    ],
    "ENGLISH": [
        "Yes, I am listening. Please go ahead.",
        "Certainly, please continue.",
        "I am listening, please go ahead.",
        "Yes, please tell me."
    ],
    "HINDI_DEVANAGARI": [
        "जी हाँ, मैं सुन रही हूँ। कहिये।",
        "जी बिल्कुल, कहिये।",
        "जी, मैं सुन रही हूँ।"
    ]
}

TOOLS_DEFINITION = [
    {
        "function_declarations": [
            {
                "name": "book_appointment",
                "description": "Extract patient appointment details verbally stated by caller. Only fill parameters that were explicitly provided by the caller.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {
                            "type": "STRING",
                            "description": "Actual full name of the patient explicitly provided by caller."
                        },
                        "phone_number": {
                            "type": "STRING",
                            "description": "10-digit contact mobile number explicitly provided by caller."
                        },
                        "date": {
                            "type": "STRING",
                            "description": "Appointment date in YYYY-MM-DD format explicitly chosen by caller."
                        },
                        "time_slot": {
                            "type": "STRING",
                            "description": "Appointment time slot explicitly chosen by caller (e.g. 11:00 AM, 06:30 PM)."
                        },
                        "symptoms": {
                            "type": "STRING",
                            "description": "Health symptoms or reason for visit stated by caller."
                        },
                        "duration": {
                            "type": "STRING",
                            "description": "Duration of symptoms stated by caller."
                        }
                    }
                }
            }
        ]
    }
]

def clean_for_speech(text: str) -> str:
    """Cleans punctuation, asterisks, and formatting for clean voice output."""
    if not text:
        return ""
    text = re.sub(r'[*_#`~]', '', text)
    text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'(\d{1,2}):00\s*(AM|PM|am|pm)', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def is_devanagari_text(text: str) -> bool:
    """Detects if text contains native Devanagari script."""
    return any('\u0900' <= char <= '\u097f' for char in text)

def detect_caller_language(text: str, current_lang: str = "ENGLISH") -> str:
    """
    Industry-standard language detection with persistent sticky session memory.
    Only shifts language when clear intent/markers in the target language are detected.
    """
    if not text:
        return current_lang
    if is_devanagari_text(text):
        return "HINDI_DEVANAGARI"

    clean = text.lower().strip()
    words = set(clean.replace('-', ' ').replace(',', ' ').replace('.', ' ').split())

    hinglish_markers = {
        "hai", "hain", "karna", "chahiye", "mera", "meri", "mere", "mujhe", "baje",
        "naam", "theek", "nahi", "nahin", "kripya", "aaj", "kal", "subah", "shaam",
        "sham", "bhai", "shukriya", "dost", "samay", "kijiye", "bataiye", "hoga",
        "bhi", "kuch", "kitna", "kitne", "kahan", "kaha", "dast", "dard", "pet", "bukhar",
        "namaste", "namaskar", "batao", "milna", "chahata", "chahati", "dhanyawad"
    }

    english_markers = {
        "i", "want", "need", "appointment", "doctor", "fever", "cough", "cold",
        "tomorrow", "today", "morning", "evening", "hello", "hi", "hey", "what",
        "where", "how", "please", "book", "is", "my", "name", "can", "could", "would",
        "at", "pm", "am", "yes", "no", "yeah", "yep", "sure", "fine", "okay", "ok",
        "number", "phone", "for", "the", "thanks", "thank", "clinic", "visit", "checkup"
    }

    has_hinglish = bool(words & hinglish_markers)
    has_english = bool(words & english_markers)

    if has_hinglish and not has_english:
        return "HINGLISH"
    elif has_english and not has_hinglish:
        return "ENGLISH"
    elif has_hinglish and has_english:
        h_count = sum(1 for w in words if w in hinglish_markers)
        e_count = sum(1 for w in words if w in english_markers)
        return "HINGLISH" if h_count > e_count else "ENGLISH"

    return current_lang

def get_live_schedule_summary() -> str:
    """Generates continuous schedule summary for prompt injection."""
    today = datetime.now()
    schedule_lines = []
    for i in range(3):
        day = today + timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        day_name = day.strftime("%A")
        booked = sheet_service.get_booked_slots(day_str)
        label = "Today" if i == 0 else ("Tomorrow" if i == 1 else day_name)
        booked_info = f"Booked times: [{', '.join(booked)}]" if booked else "All times available within active sessions"
        schedule_lines.append(f"- {label} ({day_name}, {day_str}): Morning (11:00 AM to 02:00 PM) & Evening (06:00 PM to 08:00 PM). {booked_info}")
    return "\n".join(schedule_lines)

def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Splits text stream into distinct sentence chunks for micro-TTS synthesis."""
    pattern = r'(?<!\bDr)(?<!\bMr)(?<!\bMrs)(?<!\bMs)[.?!।\n]+(?:\s+|$)'
    sentences = []
    last_end = 0
    for match in re.finditer(pattern, buffer):
        sentence = buffer[last_end:match.end()].strip()
        if sentence:
            sentences.append(sentence)
        last_end = match.end()
    remaining = buffer[last_end:]
    return sentences, remaining

class LLMService:
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY

    def _call_gemini_api(self, contents: list, system_prompt: str, include_tools: bool = True) -> dict:
        """
        Synchronous REST API call to Gemini Flash-Lite with 3.2-second budget.
        """
        recent_messages = contents[-10:] if len(contents) > 10 else contents
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": recent_messages,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 100
            }
        }
        if include_tools:
            payload["tools"] = TOOLS_DEFINITION

        start_time = time.time()
        deadline = start_time + TOTAL_LLM_TIMEOUT_BUDGET

        for model, preferred_timeout in FAST_MODELS:
            remaining = deadline - time.time()
            if remaining <= 0.35:
                break
            timeout_sec = min(preferred_timeout, remaining)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                res = http_session.post(url, json=payload, timeout=timeout_sec)
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"[LLM] Model {model} returned HTTP {res.status_code}")
            except Exception as e:
                logger.warning(f"[LLM] Model {model} error or timeout: {e}")
                continue

        raise TimeoutError("Gemini LLM exceeded strict 3.2s latency budget.")

    def process_turn(self, call_sid: str, user_speech: str, confidence: Optional[float] = None) -> Tuple[str, bool]:
        """
        Deterministic, state-machine governed Voice AI turn processor.
        - Preserves raw transcripts for audit and confidence verification.
        - Checks for clinical emergency red flags before standard booking.
        - Handles phonetic ambiguities via explicit caller clarification without silent rewrites.
        - Gates validation, explicit read-back confirmation, and Excel booking.
        """
        session = session_manager.get_or_create(call_sid)
        user_lower = user_speech.lower().strip()

        # Audit log of raw transcript with timestamp & confidence
        session.raw_transcripts.append({
            "turn": len(session.raw_transcripts) + 1,
            "raw": user_speech,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat()
        })
        session_manager.add_user_message(call_sid, user_speech)

        # 1. Update Sticky Session Language
        current_lang = getattr(session, "language", "ENGLISH")
        session.language = detect_caller_language(user_speech, current_lang)
        caller_lang = session.language

        # 2. Patient Safety: Immediate Emergency Red-Flag Check (Requirement 8)
        is_emerg, emerg_msg = speech_normalizer.check_emergency_symptom(user_speech, caller_lang)
        if is_emerg:
            cleaned = clean_for_speech(emerg_msg)
            session_manager.add_model_message(call_sid, cleaned)
            return cleaned, False

        # 3. Handle Active Clarification State (Requirement 3: Never silently rewrite)
        if session.state == SessionState.AWAITING_CLARIFICATION and session.pending_clarification:
            pending = session.pending_clarification
            is_aff = state_machine.is_affirmative(user_speech)
            is_rej = state_machine.is_rejection(user_speech)

            if is_aff:
                # Caller confirmed the interpreted candidate!
                target_slot = pending.get("target_slot")
                suggested_val = pending.get("suggested_value")
                session.slots[target_slot] = suggested_val
                session.slot_sources[target_slot] = {
                    "raw_phrase": pending.get("raw_phrase"),
                    "suggested": suggested_val,
                    "confirmed": True,
                    "source": "acoustic_clarification_confirmed"
                }
                logger.info(f"[CLARIFICATION] Caller confirmed {target_slot} -> '{suggested_val}' (from '{pending.get('raw_phrase')}')")
                session.pending_clarification = None

                # Check if all mandatory slots are now collected
                if state_machine.is_all_slots_collected(session):
                    session.state = SessionState.VALIDATING
                    date_val = session.slots["appointment_date"]
                    time_val = session.slots["appointment_time"]
                    valid, reason = sheet_service.validate_slot(date_val, time_val)
                    if valid:
                        session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
                        session.explicit_confirmation_requested = True
                        readback = state_machine.format_readback_prompt(session, caller_lang)
                        cleaned = clean_for_speech(readback)
                        session_manager.add_model_message(call_sid, cleaned)
                        return cleaned, False

                session.state = SessionState.COLLECTING_DETAILS
                missing = state_machine.get_missing_slots(session)
                next_missing = missing[0]
                if caller_lang == "ENGLISH":
                    ack = f"Got it, noted {suggested_val}. " + self._get_missing_slot_prompt(next_missing, caller_lang)
                elif caller_lang == "HINDI_DEVANAGARI":
                    ack = f"जी धन्यवाद, {suggested_val} नोट कर लिया है। " + self._get_missing_slot_prompt(next_missing, caller_lang)
                else:
                    ack = f"Theek hai, {suggested_val} note kar liya hai. " + self._get_missing_slot_prompt(next_missing, caller_lang)
                cleaned = clean_for_speech(ack)
                session_manager.add_model_message(call_sid, cleaned)
                return cleaned, False

            elif is_rej:
                # Caller rejected the interpretation!
                logger.info(f"[CLARIFICATION] Caller rejected interpretation of '{pending.get('raw_phrase')}'")
                session.pending_clarification = None
                session.state = SessionState.COLLECTING_DETAILS
                if caller_lang == "ENGLISH":
                    rej_prompt = "Understood, my mistake. Could you please state your detail or symptom again?"
                elif caller_lang == "HINDI_DEVANAGARI":
                    rej_prompt = "माफ़ कीजियेगा, मैंने गलत समझा। कृपया अपनी परेशानी या नाम दोबारा स्पष्ट बताइये।"
                else:
                    rej_prompt = "Maaf kariyega, maine galat samjha. Kripya apni pareshani ya naam dobara clearly batayein."
                cleaned = clean_for_speech(rej_prompt)
                session_manager.add_model_message(call_sid, cleaned)
                return cleaned, False

            else:
                # Neither clear yes nor no: repeat clarification prompt
                clarify_prompt = pending["prompts"].get(caller_lang, pending["prompts"]["ENGLISH"])
                cleaned = clean_for_speech(clarify_prompt)
                session_manager.add_model_message(call_sid, cleaned)
                return cleaned, False

        # 4. Low Confidence Gate (Requirement 5)
        low_threshold = getattr(settings, "STT_LOW_CONFIDENCE_THRESHOLD", 0.35)
        if confidence is not None and confidence < low_threshold:
            logger.warning(f"[STT CONFIDENCE] Low confidence score {confidence:.2f} for '{user_speech}'. Asking caller to repeat.")
            if caller_lang == "ENGLISH":
                low_msg = "I am having a little trouble hearing you clearly. Could you please repeat that?"
            elif caller_lang == "HINDI_DEVANAGARI":
                low_msg = "माफ़ कीजियेगा, फ़ोन लाइन में आवाज़ स्पष्ट नहीं आई। क्या आप दोबारा कह सकते हैं?"
            else:
                low_msg = "Maaf kariyega, line clear nahi aayi. Kya aap dobara bata sakte hain?"
            cleaned = clean_for_speech(low_msg)
            session_manager.add_model_message(call_sid, cleaned)
            return cleaned, False

        # 5. Acoustic Confusion Detection before Normal Slot Extraction (Requirement 3 & 4)
        confusion = speech_normalizer.detect_acoustic_confusion(user_speech, confidence)
        if confusion:
            logger.info(f"[SPEECH NORMALIZER] Detected acoustic confusion '{confusion['raw_phrase']}' -> candidate '{confusion['suggested_value']}'")
            session.state = SessionState.AWAITING_CLARIFICATION
            session.pending_clarification = confusion
            clarify_prompt = confusion["prompts"].get(caller_lang, confusion["prompts"]["ENGLISH"])
            cleaned = clean_for_speech(clarify_prompt)
            session_manager.add_model_message(call_sid, cleaned)
            return cleaned, False

        # 6. Extract Spoken Phone Digits
        spoken_phone = parse_spoken_hindi_numbers(user_speech)
        if spoken_phone and not spoken_phone.endswith("7372212163"):
            state_machine.update_session_slots(session, {"phone_number": spoken_phone})
            session.caller_phone = spoken_phone

        # 7. Post-Booking Call Wrap-Up
        if session.is_completed or session.state == SessionState.BOOKING_COMPLETED:
            if state_machine.is_wrapup(user_lower):
                session.state = SessionState.CALL_WRAPUP
                if caller_lang == "ENGLISH":
                    farewell = "Thank you very much for calling Dr. Sharma Care Clinic. Please arrive 10 minutes prior to your slot. Take care and goodbye!"
                elif caller_lang == "HINDI_DEVANAGARI":
                    farewell = "डॉक्टर शर्मा क्लिनिक में कॉल करने के लिए बहुत धन्यवाद। समय से 10 मिनट पहले पधारें। अपना ख्याल रखियेगा, नमस्ते!"
                else:
                    farewell = "Dr. Sharma clinic mein call karne ke liye shukriya. Samay se 10 minute pehle pahunchein. Apna khayal rakhiye, namaste!"
                session_manager.add_model_message(call_sid, farewell)
                return farewell, True

        logger.info(f"\n[CALL {call_sid[-6:]}] User Said ({caller_lang}, State={session.state}): '{user_speech}'")

        # 4. Handle Active Awaiting Explicit Confirmation State
        if session.state == SessionState.AWAITING_EXPLICIT_CONFIRMATION:
            is_aff = state_machine.is_affirmative(user_speech)
            is_rej = state_machine.is_rejection(user_speech)

            # Check if caller is speaking a correction
            # Call LLM to see if any new slot values are being corrected
            extracted_slots, direct_text = self._extract_slots_from_llm(session, user_speech, caller_lang)
            changed_slots = state_machine.update_session_slots(session, extracted_slots) if extracted_slots else []

            if changed_slots:
                # Caller corrected a field! Invalidation already triggered in update_session_slots
                logger.info(f"[CALL {call_sid[-6:]}] Caller corrected slot(s): {changed_slots}")
                # Revalidate new slot
                valid, reason = sheet_service.validate_slot(
                    session.slots["appointment_date"],
                    session.slots["appointment_time"]
                )
                if not valid:
                    session.set_slot("appointment_time", None)
                    session.state = SessionState.COLLECTING_DETAILS
                    msg = f"I apologize, {reason}. What other time slot would you prefer?" if caller_lang == "ENGLISH" else f"Maaf kariyega, {reason}. Aap doosra kaunsa time prefer karenge?"
                    session_manager.add_model_message(call_sid, msg)
                    return msg, False

                # Valid correction -> Generate fresh readback!
                session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
                session.explicit_confirmation_requested = True
                readback = state_machine.format_readback_prompt(session, caller_lang)
                session_manager.add_model_message(call_sid, readback)
                return readback, False

            elif is_rej:
                # Caller rejected the readback
                session.explicit_confirmation_requested = False
                session.state = SessionState.COLLECTING_DETAILS
                if caller_lang == "ENGLISH":
                    rej_msg = "No problem! Which detail would you like to change — the date, time, or patient name?"
                elif caller_lang == "HINDI_DEVANAGARI":
                    rej_msg = "कोई बात नहीं! आप कौन सी जानकारी बदलना चाहेंगे — तारीख, समय, या मरीज़ का नाम?"
                else:
                    rej_msg = "Koi baat nahi! Aap kaunsi detail badalna chahenge — date, time, ya patient ka naam?"
                session_manager.add_model_message(call_sid, rej_msg)
                return rej_msg, False

            elif is_aff:
                # Explicit affirmative confirmation received!
                # Revalidate availability under lock right before execution
                date_val = session.slots["appointment_date"]
                time_val = session.slots["appointment_time"]
                valid, reason = sheet_service.validate_slot(date_val, time_val)
                if not valid:
                    # Slot was taken while awaiting confirmation
                    session.explicit_confirmation_requested = False
                    session.set_slot("appointment_time", None)
                    session.state = SessionState.COLLECTING_DETAILS
                    if caller_lang == "ENGLISH":
                        conflict_msg = f"I apologize, but {reason}. Would you like to pick another time slot?"
                    elif caller_lang == "HINDI_DEVANAGARI":
                        conflict_msg = f"माफ़ कीजियेगा, {reason}। क्या आप कोई अन्य समय चुनना चाहेंगे?"
                    else:
                        conflict_msg = f"Maaf kariyega, {reason}। Kya aap koi doosra time slot select karna chahenge?"
                    session_manager.add_model_message(call_sid, conflict_msg)
                    return conflict_msg, False

                # Single Deterministic Booking Path:
                booking = sheet_service.save_appointment(
                    patient_name=session.slots["patient_name"],
                    phone_number=session.slots["phone_number"],
                    date_str=date_val,
                    time_slot=time_val,
                    symptoms=session.slots["symptoms"],
                    duration=session.slots.get("duration", "Recently started"),
                    call_sid=call_sid
                )

                if booking.get("status") == "Failed":
                    # Error writing
                    err_msg = booking.get("error", "Unable to save appointment.")
                    session.state = SessionState.COLLECTING_DETAILS
                    return err_msg, False

                try:
                    telegram_service.send_appointment_alert(booking)
                except Exception as e:
                    logger.error(f"[TELEGRAM] Notification delivery failed: {e}")

                session_manager.mark_completed(call_sid, booking)

                confirm_reply = state_machine.format_confirmation_success(session, booking, caller_lang)
                cleaned = clean_for_speech(confirm_reply)
                session_manager.add_model_message(call_sid, cleaned)
                return cleaned, False

            else:
                # Unclear reply while awaiting confirmation
                prompt_again = state_machine.format_readback_prompt(session, caller_lang)
                session_manager.add_model_message(call_sid, prompt_again)
                return prompt_again, False

        # 5. Extract Candidate Slots via LLM and Clinical Domain Vocabulary
        extracted_slots, direct_text = self._extract_slots_from_llm(session, user_speech, caller_lang)
        if not extracted_slots:
            extracted_slots = {}
        if "symptoms" not in extracted_slots or not extracted_slots["symptoms"]:
            known_symptom = speech_normalizer.match_known_symptom(user_speech)
            if known_symptom:
                extracted_slots["symptoms"] = known_symptom
                logger.info(f"[SPEECH NORMALIZER] Matched domain symptom '{known_symptom}' from speech: '{user_speech}'")
        if extracted_slots:
            state_machine.update_session_slots(session, extracted_slots)

        # 6. Check Premature Affirmative ("yes" while slots are still missing)
        if state_machine.is_affirmative(user_speech) and not state_machine.is_all_slots_collected(session):
            logger.info(f"[CALL {call_sid[-6:]}] Premature 'yes' ignored. Missing mandatory slots remain.")

        # 7. Check if All 5 Mandatory Slots are Collected
        if state_machine.is_all_slots_collected(session):
            session.state = SessionState.VALIDATING
            date_val = session.slots["appointment_date"]
            time_val = session.slots["appointment_time"]

            # Validate against clinic rules and existing bookings
            valid, reason = sheet_service.validate_slot(date_val, time_val)
            if not valid:
                logger.warning(f"[CALL {call_sid[-6:]}] Slot validation failed: {reason}")
                session.set_slot("appointment_time", None)
                session.state = SessionState.COLLECTING_DETAILS
                if caller_lang == "ENGLISH":
                    conflict_reply = f"I apologize, {reason}. Could you please choose another available time or date?"
                elif caller_lang == "HINDI_DEVANAGARI":
                    conflict_reply = f"माफ़ कीजियेगा, {reason}। कृपया कोई अन्य समय या तारीख बताइये।"
                else:
                    conflict_reply = f"Maaf kariyega, {reason}। Kripya koi doosra samay ya tarikh bata dijiye."
                cleaned = clean_for_speech(conflict_reply)
                session_manager.add_model_message(call_sid, cleaned)
                return cleaned, False

            # Valid -> Enter Awaiting Explicit Confirmation!
            session.state = SessionState.AWAITING_EXPLICIT_CONFIRMATION
            session.explicit_confirmation_requested = True
            readback = state_machine.format_readback_prompt(session, caller_lang)
            cleaned = clean_for_speech(readback)
            session_manager.add_model_message(call_sid, cleaned)
            logger.info(f"[CALL {call_sid[-6:]}] Read-back generated: '{cleaned}'")
            return cleaned, False

        # 8. Still Missing Slots -> Ask Caller Naturally
        session.state = SessionState.COLLECTING_DETAILS
        missing = state_machine.get_missing_slots(session)
        logger.info(f"[CALL {call_sid[-6:]}] Still missing slots: {missing}")

        # If LLM answered a FAQ question (e.g. fees, address), use LLM response and prompt for next missing slot
        if direct_text and len(direct_text.strip()) > 3:
            spoken_text = direct_text
        else:
            # Deterministic missing slot prompts
            next_missing = missing[0]
            spoken_text = self._get_missing_slot_prompt(next_missing, caller_lang)

        cleaned = clean_for_speech(spoken_text)
        session_manager.add_model_message(call_sid, cleaned)
        return cleaned, False

    def _extract_slots_from_llm(self, session, user_speech: str, caller_lang: str) -> Tuple[Dict[str, Any], str]:
        """
        Calls Gemini to extract candidate slots and natural conversational text.
        Never executes booking.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        schedule_summary = get_live_schedule_summary()

        known_items = []
        for k in MANDATORY_SLOTS:
            val = session.slots.get(k)
            if val:
                known_items.append(f"- {k.replace('_', ' ').title()}: {val}")
        known_slots_summary = "\n".join(known_items)

        lang_directive = f"\nCRITICAL DIRECTIVE: The caller is speaking in {caller_lang}. You MUST reply STRICTLY in {caller_lang}! If ENGLISH, reply 100% in polished English. If HINGLISH, reply in Roman Hinglish."
        system_prompt = get_receptionist_prompt(today_str, schedule_summary, known_slots_summary=known_slots_summary) + lang_directive

        try:
            api_resp = self._call_gemini_api(session.messages, system_prompt, include_tools=True)
            candidate = api_resp.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])

            extracted_slots = {}
            direct_text = ""

            for p in parts:
                if "functionCall" in p:
                    args = p["functionCall"].get("args", {})
                    if "patient_name" in args:
                        extracted_slots["patient_name"] = args["patient_name"]
                    if "phone_number" in args:
                        extracted_slots["phone_number"] = args["phone_number"]
                    if "date" in args:
                        extracted_slots["appointment_date"] = args["date"]
                    if "time_slot" in args:
                        extracted_slots["appointment_time"] = args["time_slot"]
                    if "symptoms" in args:
                        extracted_slots["symptoms"] = args["symptoms"]
                    if "duration" in args:
                        extracted_slots["duration"] = args["duration"]
                elif "text" in p:
                    direct_text = p["text"]

            return extracted_slots, direct_text

        except Exception as e:
            logger.warning(f"[LLM] Error calling Gemini: {e}")
            return {}, ""

    def _get_missing_slot_prompt(self, missing_slot: str, lang: str) -> str:
        """Deterministic prompts for missing mandatory booking fields."""
        prompts = {
            "patient_name": {
                "ENGLISH": "Could you please tell me the patient's full name?",
                "HINGLISH": "Kripya patient ka poora naam bata dijiye?",
                "HINDI_DEVANAGARI": "कृपया मरीज़ का पूरा नाम बताइये?"
            },
            "symptoms": {
                "ENGLISH": "Could you please share the health symptoms or reason for the visit?",
                "HINGLISH": "Kripya takleef ya bimari ke lakshan bata dijiye?",
                "HINDI_DEVANAGARI": "कृपया अपनी तकलीफ़ या लक्षण बताइये?"
            },
            "appointment_date": {
                "ENGLISH": "Which date would you prefer for the appointment?",
                "HINGLISH": "Aap kis date ko appointment lena chahenge?",
                "HINDI_DEVANAGARI": "आप किस तारीख को अपॉइंटमेंट लेना चाहेंगे?"
            },
            "appointment_time": {
                "ENGLISH": "What time slot would you prefer within our clinic hours?",
                "HINGLISH": "Aap subah 11 se 2 ya shaam 6 se 8 ke beech kaunsa time prefer karenge?",
                "HINDI_DEVANAGARI": "आप सुबह 11 से 2 या शाम 6 से 8 के बीच कौनसा समय पसंद करेंगे?"
            },
            "phone_number": {
                "ENGLISH": "Could you please share your 10-digit contact mobile number so I can confirm the booking?",
                "HINGLISH": "Appointment confirm karne ke liye kripya apna 10-digit mobile number bata dijiye.",
                "HINDI_DEVANAGARI": "अपॉइंटमेंट पक्का करने के लिए कृपया अपना 10 अंकों का मोबाइल नंबर बताइये।"
            }
        }
        slot_dict = prompts.get(missing_slot, prompts["phone_number"])
        return slot_dict.get(lang, slot_dict["ENGLISH"])

    async def stream_turn(self, call_sid: str, user_speech: str) -> AsyncGenerator[Tuple[str, bool], None]:
        """Streaming wrapper for WebSockets / SSE."""
        reply_text, is_done = self.process_turn(call_sid, user_speech)
        sentences, _ = split_sentences(reply_text)
        if not sentences:
            sentences = [reply_text]

        for idx, sentence in enumerate(sentences):
            cleaned = clean_for_speech(sentence)
            if cleaned:
                last_chunk = (idx == len(sentences) - 1)
                yield cleaned, (is_done if last_chunk else False)

llm_service = LLMService()
