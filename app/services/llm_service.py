import json
import logging
import re
import time
import requests
from datetime import datetime, timedelta
from typing import AsyncGenerator, Optional
from app.core.config import settings
from app.core.prompt import get_receptionist_prompt
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.services.session_manager import session_manager
from app.services.clinic_service import clinic_service
from app.utils.phonetics import parse_spoken_hindi_numbers, format_digits_for_readback

logger = logging.getLogger(__name__)

# Reusable HTTP Session with Connection: close to eliminate zombie socket delays
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
                "description": "Confirm and record patient appointment into the clinic database. ONLY call when patient full name, symptoms, date & time slot within working hours, and 10-digit mobile number have all been verbally provided by caller.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {
                            "type": "STRING",
                            "description": "Actual human full name of the patient (e.g. Sunil, Devendra, Anita, Neeraj). Never food, drink, brand, or illness words like 'Nescafe' or 'Coffee'."
                        },
                        "phone_number": {
                            "type": "STRING",
                            "description": "10-digit contact mobile number verbally provided by the caller. REQUIRED before booking."
                        },
                        "date": {
                            "type": "STRING",
                            "description": "Appointment date in YYYY-MM-DD format (e.g. 2026-11-24). ONLY fill if explicitly chosen or agreed by caller. Do not guess."
                        },
                        "time_slot": {
                            "type": "STRING",
                            "description": "Selected time slot within clinic sessions: 11:00 AM - 02:00 PM or 06:00 PM - 08:00 PM (e.g. 11:00 AM, 11:15 AM, 06:00 PM, 06:30 PM, 07:30 PM). ONLY fill if explicitly chosen or agreed by caller. Do not guess."
                        },
                        "symptoms": {
                            "type": "STRING",
                            "description": "Specific health symptoms or complaint (e.g. loose motion, fever, cough, stomach pain, checkup)"
                        },
                        "duration": {
                            "type": "STRING",
                            "description": "Duration of symptoms e.g. '10 days', '2 days', 'since yesterday'"
                        }
                    },
                    "required": ["patient_name", "date", "time_slot", "phone_number", "symptoms"]
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
    Industry-standard language detection with persistent session memory.
    Short slot replies (numbers, times, symptoms, names) inherit current session language.
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

    # Ambiguous, digits, times, names, or short answers inherit active conversation language!
    return current_lang

def get_live_schedule_summary() -> str:
    """Generates real-time 3-day continuous schedule summary for prompt injection."""
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
        Synchronous REST API call to Gemini Flash-Lite governed by a strict 3.2-second budget.
        Retains up to the last 10 messages of conversation history to preserve full call context.
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
                logger.warning(f"[LLM] Model {model} returned HTTP {res.status_code}, checking remaining budget...")
            except Exception as e:
                logger.warning(f"[LLM] Model {model} error or timeout ({timeout_sec:.2f}s budget): {e}")
                continue

        raise TimeoutError("Gemini LLM exceeded strict 3.2s latency budget.")

    def process_turn(self, call_sid: str, user_speech: str) -> tuple[str, bool]:
        """
        Industry-standard Voice AI turn processor.
        Empowers Gemini LLM with complete semantic understanding and autonomous tool execution.
        Preserves multi-turn sticky session language and enforces real patient data collection.
        """
        session = session_manager.get_or_create(call_sid)
        user_lower = user_speech.lower().strip()

        # Sticky multi-turn session language memory
        current_lang = getattr(session, "language", "ENGLISH")
        session.language = detect_caller_language(user_speech, current_lang)
        caller_lang = session.language

        # Parse spoken 10-digit mobile number if caller speaks one (never store Twilio trunk)
        spoken_phone = parse_spoken_hindi_numbers(user_speech)
        if spoken_phone and not spoken_phone.endswith("7372212163"):
            session.slots["phone_number"] = spoken_phone
            session.caller_phone = spoken_phone
            logger.info(f"[CALL {call_sid[-6:]}] Extracted Spoken 10-digit Phone: {spoken_phone}")

        # Check if caller wants to gracefully end call after booking is completed
        if session.is_completed:
            closing_cues = ["nahi", "no", "bas", "bus", "thank", "dhanyawad", "shukriya", "bye", "goodbye", "done", "all set", "that's all", "nothing"]
            has_question = any(q in user_lower for q in ["kya", "kitna", "kitne", "kab", "kahan", "kaha", "fee", "fees", "address", "timing", "cost", "pata", "doctor", "where", "how much"])
            is_closing = any(c in user_lower for c in closing_cues) and not has_question
            if is_closing:
                if caller_lang == "ENGLISH":
                    closing_reply = "Thank you very much! Please arrive at the clinic 10 minutes prior to your slot. Take care and goodbye!"
                elif caller_lang == "HINDI_DEVANAGARI":
                    closing_reply = "आपका बहुत-बहुत धन्यवाद! क्लिनिक पर समय से 10 मिनट पहले पधारें। अपना ख्याल रखियेगा, नमस्ते!"
                else:
                    closing_reply = "Aapka bahut-bahut shukriya! Clinic par samay se 10 minute pehle pahunchein. Apna khayal rakhiye, namaste!"
                session_manager.add_model_message(call_sid, closing_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Call Completed Gracefully: {closing_reply}")
                return closing_reply, True

        session_manager.add_user_message(call_sid, user_speech)
        logger.info(f"\n[CALL {call_sid[-6:]}] User Said ({caller_lang}): '{user_speech}'")

        today_str = datetime.now().strftime("%Y-%m-%d")
        schedule_summary = get_live_schedule_summary()

        # Build persistent known slots summary from conversation memory
        known_items = []
        if session.slots.get("patient_name"):
            known_items.append(f"- Patient Name: {session.slots['patient_name']}")
        if session.slots.get("symptoms"):
            known_items.append(f"- Symptoms / Reason: {session.slots['symptoms']}")
        if session.slots.get("date") or session.slots.get("time_slot"):
            known_items.append(f"- Preferred Slot: {session.slots.get('date', '')} {session.slots.get('time_slot', '')}".strip())
        if session.slots.get("phone_number"):
            known_items.append(f"- Contact Phone: {session.slots['phone_number']}")
        known_slots_summary = "\n".join(known_items)

        lang_directive = f"\nCRITICAL DIRECTIVE: The caller is speaking in {caller_lang}. You MUST reply STRICTLY in {caller_lang}! If ENGLISH, reply 100% in polished English without any Hindi words. If HINGLISH, reply in conversational Roman Hinglish."
        system_prompt = get_receptionist_prompt(today_str, schedule_summary, known_slots_summary=known_slots_summary) + lang_directive

        include_tools = not session.is_completed

        try:
            api_resp = self._call_gemini_api(session.messages, system_prompt, include_tools=include_tools)
            candidate = api_resp.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])

            func_call = None
            direct_text = ""

            for p in parts:
                if "functionCall" in p and include_tools:
                    func_call = p["functionCall"]
                elif "text" in p:
                    direct_text = p["text"]

            if func_call:
                args = func_call.get("args", {})
                
                # Extract and preserve patient name without any hardcoded blacklist
                raw_name = args.get("patient_name", "").strip().title()
                if raw_name and raw_name not in ("Patient", "None"):
                    patient_name = raw_name
                else:
                    patient_name = session.slots.get("patient_name") or "Patient"
                session.slots["patient_name"] = patient_name

                # Extract and preserve symptoms across all turns
                raw_symptoms = args.get("symptoms", "").strip()
                if raw_symptoms and raw_symptoms not in ("General Consultation", "None"):
                    symptoms = raw_symptoms
                else:
                    symptoms = session.slots.get("symptoms") or "General Consultation"
                session.slots["symptoms"] = symptoms

                # Extract and preserve date and time slot
                date_val = args.get("date") or session.slots.get("date") or today_str
                slot = args.get("time_slot") or session.slots.get("time_slot") or "11:00 AM"
                session.slots["date"] = date_val
                session.slots["time_slot"] = slot
                duration = args.get("duration", "Recently noticed")

                # Clean contact number & strictly guard against Twilio trunk bot numbers
                raw_phone = str(args.get("phone_number") or "")
                clean_digits = "".join(ch for ch in raw_phone if ch.isdigit())
                if (len(clean_digits) < 10 or clean_digits.endswith("7372212163")) and session.slots.get("phone_number"):
                    clean_digits = session.slots["phone_number"]
                if clean_digits.endswith("7372212163"):
                    clean_digits = ""

                # If 10-digit phone number is missing, prompt caller verbally
                if len(clean_digits) < 10:
                    logger.warning(f"[CALL {call_sid[-6:]}] Booking requested but 10-digit phone missing. Asking caller.")
                    if caller_lang == "ENGLISH":
                        ask_phone = "Could you please share your 10-digit contact mobile number to confirm the booking?"
                    elif caller_lang == "HINDI_DEVANAGARI":
                        ask_phone = "अपॉइंटमेंट पक्का करने के लिए कृपया अपना 10 अंकों का मोबाइल नंबर बताइये।"
                    else:
                        ask_phone = "Appointment confirm karne ke liye kripya apna 10-digit mobile number bata dijiye."
                    session_manager.add_model_message(call_sid, ask_phone)
                    return ask_phone, False

                session.slots["phone_number"] = clean_digits
                readback = " ".join(clean_digits)

                booking = sheet_service.save_appointment(
                    patient_name=patient_name,
                    phone_number=clean_digits,
                    date_str=date_val,
                    time_slot=slot,
                    symptoms=symptoms,
                    duration=duration
                )

                telegram_service.send_appointment_alert(booking)
                session_manager.mark_completed(call_sid, booking)

                try:
                    dt = datetime.strptime(date_val, "%Y-%m-%d")
                    friendly_date = dt.strftime("%A, %B %d")
                except Exception:
                    friendly_date = date_val

                clean_slot = slot.replace(":00", "")

                # Format spoken confirmation strictly in active conversation language
                if caller_lang == "ENGLISH":
                    doc = clinic_service.profile.doctor_name
                    spoken_text = (
                        f"Certainly! The appointment for {patient_name} with {doc} is confirmed for "
                        f"{friendly_date} at {slot}. Registered contact number is {readback}. "
                        "Would you like to know the clinic address or consultation fees?"
                    )
                elif caller_lang == "HINDI_DEVANAGARI":
                    spoken_text = (
                        f"बिल्कुल! {patient_name} जी का अपॉइंटमेंट {friendly_date} को "
                        f"{clean_slot} बजे पक्का हो गया है। आपका फ़ोन नंबर {readback} नोट कर लिया गया है। "
                        "क्या क्लिनिक का पता या फीस जाननी है?"
                    )
                else:
                    spoken_text = (
                        f"Bilkul! {patient_name} ji ka appointment {friendly_date} ko "
                        f"{clean_slot} baje confirm ho gaya hai. Phone number {readback} note kar liya hai. "
                        "Kya clinic ka address ya fees ke baare mein kuch poochna hai?"
                    )

                logger.info(f"[CALL {call_sid[-6:]}] Confirmed Booking: {booking['booking_id']} for {patient_name}")
                is_done = False

            else:
                default_opener = "Certainly! How may I assist you today?" if caller_lang == "ENGLISH" else "Ji bilkul, bataiye main aapki kya madad kar sakti hoon?"
                spoken_text = direct_text if direct_text and len(direct_text.strip()) > 3 else default_opener
                is_done = False

            cleaned = clean_for_speech(spoken_text)
            session_manager.add_model_message(call_sid, cleaned)
            logger.info(f"[CALL {call_sid[-6:]}] AI Replied ({caller_lang}): '{cleaned}'")
            return cleaned, is_done

        except Exception as e:
            logger.warning(f"[CALL {call_sid[-6:]}] LLM budget or error ({e}). Returning natural dynamic listening cue.")
            cues = FALLBACK_LISTENING_CUES.get(caller_lang, FALLBACK_LISTENING_CUES["ENGLISH"])
            idx = getattr(session, "fallback_idx", 0)
            fallback = cues[idx % len(cues)]
            session.fallback_idx = idx + 1
            session_manager.add_model_message(call_sid, fallback)
            return fallback, False

    async def stream_turn(self, call_sid: str, user_speech: str) -> AsyncGenerator[tuple[str, bool], None]:
        """
        Streaming wrapper for real-time WebSockets / SSE.
        Yields sentence chunks so caller hears audio immediately.
        """
        reply_text, is_done = self.process_turn(call_sid, user_speech)
        sentences, _ = split_sentences(reply_text)
        if not sentences:
            sentences = [reply_text]

        for idx, sentence in enumerate(sentences):
            cleaned = clean_for_speech(sentence)
            if cleaned:
                # Last chunk conveys is_done state
                last_chunk = (idx == len(sentences) - 1)
                yield cleaned, (is_done if last_chunk else False)

llm_service = LLMService()
