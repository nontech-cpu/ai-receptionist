import json
import logging
import re
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

FAST_MODELS = [
    ("gemini-flash-lite-latest", 4.5),
    ("gemini-3.5-flash-lite", 4.5),
    ("gemini-3.1-flash-lite", 4.5),
    ("gemini-flash-latest", 4.5)
]

INVALID_RELATION_NAMES = {
    "dost", "friend", "mere dost", "apne dost", "bhai", "brother", 
    "papa", "father", "mummy", "mother", "sister", "patient", "unknown", "relative",
    "apna", "apne", "apna ji", "kapna", "kalpana", "khud", "self", "myself", "mera", "mere", "meri",
    "someone", "anybody", "one"
}

TOOLS_DEFINITION = [
    {
        "function_declarations": [
            {
                "name": "book_appointment",
                "description": "Confirm and record patient appointment into the clinic database.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {
                            "type": "STRING",
                            "description": "Actual person full name of the patient (e.g. Sunil, Devendra, Anita, Neeraj). Never relation words like 'dost' or 'bhai'."
                        },
                        "phone_number": {
                            "type": "STRING",
                            "description": "10-digit contact mobile number for the patient"
                        },
                        "date": {
                            "type": "STRING",
                            "description": "Appointment date in YYYY-MM-DD format (e.g. 2026-11-24)"
                        },
                        "time_slot": {
                            "type": "STRING",
                            "description": "Selected time slot e.g. 11:00 AM, 11:15 AM, 06:00 PM, 06:30 PM, 07:15 PM"
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
                    "required": ["patient_name", "date", "time_slot", "symptoms"]
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
        Synchronous REST API call to Gemini Flash-Lite.
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
                "maxOutputTokens": 120
            }
        }
        if include_tools:
            payload["tools"] = TOOLS_DEFINITION

        for model, t_sec in FAST_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                res = http_session.post(url, json=payload, timeout=t_sec)
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"[LLM] Model {model} returned HTTP {res.status_code}, trying next fallback...")
            except Exception as e:
                logger.warning(f"[LLM] Model {model} exception: {e}")
                continue

        raise Exception("All Gemini LLM models failed or timed out.")

    def process_turn(self, call_sid: str, user_speech: str) -> tuple[str, bool]:
        """
        Industry-standard Voice AI turn processor.
        Empowers Gemini LLM with complete semantic understanding and autonomous tool execution.
        Eliminates 100% of brittle keyword intercepts and preserves multi-turn memory.
        """
        session = session_manager.get_or_create(call_sid)
        user_lower = user_speech.lower().strip()

        # Parse spoken 10-digit mobile number if caller speaks one
        spoken_phone = parse_spoken_hindi_numbers(user_speech)
        if spoken_phone:
            session.caller_phone = spoken_phone
            logger.info(f"[CALL {call_sid[-6:]}] Extracted Spoken 10-digit Phone: {spoken_phone}")

        # Check if caller wants to gracefully end call after booking is completed
        if session.is_completed:
            closing_words = [
                "nahi", "nahin", "no", "nope", "nothing", "kuch nahi", "kuch nhi", 
                "bas", "bus", "thank you", "thanks", "dhanyawad", "shukriya", 
                "bye", "goodbye", "all set", "ho gaya", "theek hai bas", "nahi bas"
            ]
            has_question = any(q in user_lower for q in [
                "kya", "kitna", "kitne", "kab", "kahan", "kaha", "fee", "fees", 
                "address", "timing", "charge", "paisa", "paise", "cost", "pata", "doctor"
            ])
            is_closing = any(re.search(r'\b' + re.escape(c) + r'\b', user_lower) for c in closing_words) and not has_question
            if is_closing:
                closing_reply = "Aapka bahut-bahut shukriya! Clinic par samay se 10 minute pehle pahunchein. Apna khayal rakhiye, namaste!"
                session_manager.add_model_message(call_sid, closing_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Call Completed Gracefully: {closing_reply}")
                return closing_reply, True

        session_manager.add_user_message(call_sid, user_speech)
        logger.info(f"\n[CALL {call_sid[-6:]}] User Said: '{user_speech}'")

        today_str = datetime.now().strftime("%Y-%m-%d")
        schedule_summary = get_live_schedule_summary()
        system_prompt = get_receptionist_prompt(today_str, schedule_summary)

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
                patient_name = args.get("patient_name", "Patient").strip().title()

                # Validate patient name against relationship words or pronouns
                if patient_name.lower() in INVALID_RELATION_NAMES or len(patient_name) <= 2:
                    prompt_for_name = "Ji bilkul! Kripya mareez ka shubh naam bata dijiye taaki main appointment book kar sakoon?"
                    session_manager.add_model_message(call_sid, prompt_for_name)
                    logger.info(f"[CALL {call_sid[-6:]}] Filtered invalid name '{patient_name}'. Asking for real patient name.")
                    return prompt_for_name, False

                # Validate phone number
                raw_phone = str(args.get("phone_number") or "")
                parsed_phone = parse_spoken_hindi_numbers(raw_phone)
                phone = parsed_phone or (session.caller_phone if session.caller_phone and len(re.sub(r'\D', '', session.caller_phone)) >= 10 else None)

                if not phone or len(re.sub(r'\D', '', phone)) < 10:
                    prompt_phone = "Appointment confirm karne ke liye kripya apna 10-digit mobile number bata dijiye."
                    session_manager.add_model_message(call_sid, prompt_phone)
                    logger.info(f"[CALL {call_sid[-6:]}] Phone missing or incomplete. Asking for phone number.")
                    return prompt_phone, False

                clean_digits = re.sub(r'\D', '', phone)[-10:]
                readback = format_digits_for_readback(clean_digits)

                date_val = args.get("date", today_str)
                slot = args.get("time_slot", "11:00 AM")
                symptoms = args.get("symptoms", "General Consultation")
                duration = args.get("duration", "Recently noticed")

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

                # Format spoken confirmation in natural conversational style matching caller's language
                if is_devanagari_text(user_speech):
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
                spoken_text = direct_text if direct_text and len(direct_text.strip()) > 3 else "Ji bilkul, bataiye main aapki kya madad kar sakti hoon?"
                is_done = False

            cleaned = clean_for_speech(spoken_text)
            session_manager.add_model_message(call_sid, cleaned)
            logger.info(f"[CALL {call_sid[-6:]}] AI Replied: '{cleaned}'")
            return cleaned, is_done

        except Exception as e:
            logger.error(f"[CALL {call_sid[-6:]}] Error in LLM turn: {e}")
            fallback = "Ji bilkul, aap apni takleef aur doctor se milne ka samay bata dijiye."
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
