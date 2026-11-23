import json
import logging
import re
import aiohttp
import requests
from datetime import datetime, timedelta
from typing import AsyncGenerator
from app.core.config import settings
from app.core.prompt import get_receptionist_prompt
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.services.session_manager import session_manager
from app.services.clinic_service import clinic_service
from app.utils.phonetics import normalize_for_indian_voice, parse_spoken_hindi_numbers, format_digits_for_readback

logger = logging.getLogger(__name__)

# Reusable HTTP Session with Connection: close to eliminate zombie socket delays
http_session = requests.Session()
http_session.headers.update({"Connection": "close"})

FAST_MODELS = [
    ("gemini-3.5-flash-lite", 3.2),
    ("gemini-3.1-flash-lite", 2.2)
]

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
                            "description": "Actual person full name of the patient (e.g. Sunil, Devendra, Anita). Never relations like 'dost' or 'bhai'."
                        },
                        "phone_number": {
                            "type": "STRING",
                            "description": "Contact phone number for the patient"
                        },
                        "date": {
                            "type": "STRING",
                            "description": "Appointment date in YYYY-MM-DD format (e.g. 2026-11-17)"
                        },
                        "time_slot": {
                            "type": "STRING",
                            "description": "Selected time slot e.g. 10:00 AM, 02:00 PM, 07:00 PM"
                        },
                        "symptoms": {
                            "type": "STRING",
                            "description": "Specific health symptoms or complaint (e.g. leg pain, fever, cough)"
                        },
                        "duration": {
                            "type": "STRING",
                            "description": "Duration of symptoms e.g. 'since 2 days', 'for 3 days', 'from yesterday'"
                        }
                    },
                    "required": ["patient_name", "date", "time_slot", "symptoms"]
                }
            }
        ]
    }
]

def clean_for_speech(text: str, is_hindi: bool = False) -> str:
    """Cleans punctuation, asterisks, formats times and normalizes to native Indian phonetics if Hindi."""
    text = re.sub(r'[*_#`~]', '', text)
    text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'(\d{1,2}):00\s*(AM|PM|am|pm)', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)
    if is_hindi:
        text = normalize_for_indian_voice(text)
    return text.strip()

def detect_user_language(text: str, current_lang: str = "HINDI") -> str:
    """
    Enterprise language detection with session stickiness.
    Prevents single-word utterances, digits or phone numbers from flipping conversation language.
    """
    hindi_keywords = [
        "bhai", "mera", "mere", "meri", "karna", "karana", "kardo", "kijiye", "hai", "hain", 
        "kal", "aaj", "baje", "takleef", "bukhar", "khansi", "dost", "theek", "haan",
        "nahi", "nahin", "unka", "unki", "batayein", "chahiye", "dard", "paon", "kitne",
        "dopahar", "subah", "shaam", "lena", "milna", "dikhaana", "dikhana", "naam", "sir",
        "kya", "kab", "kahan", "kaise", "samajh", "aaya", "aayi", "bolo", "batao", "sunai"
    ]
    english_keywords = [
        "i want", "i need", "appointment for", "my name is", "doctor sharma", "how are you",
        "can i get", "is doctor available", "could you", "please tell me", "what is the fee",
        "hello", "good morning", "good evening", "thank you"
    ]
    lower = text.lower().strip()

    # If the turn is purely numbers/digits (e.g. '78 6342 51 22', '8824466229') or short neutral noise:
    letters_only = re.sub(r'[\d\s\-\+\(\)\#\*]', '', lower)
    if len(letters_only) <= 2:
        return current_lang

    if any(w in lower for w in hindi_keywords):
        return "HINDI"

    if any(w in lower for w in english_keywords):
        return "ENGLISH"

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
    """
    Splits text stream into distinct sentence chunks for micro-TTS synthesis.
    Avoids splitting on common abbreviations like Dr., Mr., Mrs.
    """
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

INVALID_RELATION_NAMES = [
    "dost", "friend", "mere dost", "apne dost", "bhai", "brother", 
    "papa", "father", "mummy", "mother", "sister", "patient", "unknown", "relative"
]

class LLMService:
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY

    async def stream_turn(self, call_sid: str, user_speech: str) -> AsyncGenerator[tuple[str, bool], None]:
        """
        Enterprise real-time streaming generator.
        Streams tokens from Gemini Flash-Lite via SSE, splits into sentence chunks,
        and yields (sentence, is_done) in <500ms so caller hears audio immediately.
        """
        session = session_manager.get_or_create(call_sid)
        user_lower = user_speech.lower().strip()
        user_lang = detect_user_language(user_speech, current_lang=session.language)
        session.language = user_lang

        # Detect spoken 10-digit phone number if present in speech
        spoken_phone = parse_spoken_hindi_numbers(user_speech)
        if spoken_phone:
            session.caller_phone = spoken_phone
            logger.info(f"[CALL {call_sid[-6:]}] Extracted Spoken 10-digit Phone: {spoken_phone}")

        session_manager.add_user_message(call_sid, user_speech)
        logger.info(f"[CALL {call_sid[-6:]}] User Said ({user_lang}): {user_speech}")

        # --- 1. ENTERPRISE IMMEDIATE ROUTING (Prevents Post-Booking Loops & Webhook Timeouts) ---
        if session.is_completed:
            appt = session.appointment_data or {}
            p_name = appt.get('patient_name', '')
            slot = appt.get('time_slot', '')
            name_str = f"{p_name} जी का " if p_name and p_name.lower() not in ("patient", "dost") else ""

            # Check for explicit closing cues (Caller actually wants to end the call)
            closing_words = [
                "nahi", "nahin", "no", "nope", "nothing", "kuch nahi", "kuch nhi", 
                "bas", "bus", "thank you", "thanks", "dhanyawad", "shukriya", 
                "bye", "goodbye", "all set", "ho gaya", "theek hai bas", "nahi bas"
            ]
            has_question_words = any(q in user_lower for q in [
                "kya", "kitna", "kitne", "kab", "kahan", "kaha", "fee", "fees", "address", 
                "timing", "charge", "paisa", "paise", "cost", "pata", "age", "location", "doctor"
            ])
            is_closing = any(re.search(r'\b' + re.escape(c) + r'\b', user_lower) for c in closing_words) and not has_question_words

            if is_closing:
                concluding_reply = f"आपका बहुत-बहुत धन्यवाद! {name_str}अपॉइंटमेंट पक्का हो गया है। क्लिनिक पर समय से 10 मिनट पहले पधारें। अपना ख्याल रखियेगा, नमस्ते!" if user_lang == "HINDI" else f"You are most welcome! {name_str}appointment is confirmed. Please arrive 10 minutes prior. Take care and goodbye!"
                session_manager.add_model_message(call_sid, concluding_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Explicit Call Closing: {concluding_reply}")
                yield concluding_reply, True
                return

            # Clarification / Repeat Request Handling
            repeat_cues = [
                "samajh nahi", "samajh nhi", "sunai nahi", "sunayi nahi", "kya bola", "kya kaha",
                "repeat", "phir se", "fir se", "dobara", "ek bar aur", "pardon", "sorry",
                "didn't understand", "did not understand", "could not hear", "say again", "once more"
            ]
            if any(cue in user_lower for cue in repeat_cues):
                if user_lang == "HINDI":
                    clarify_reply = f"जी मैं फिर से बता देती हूँ। {name_str}अपॉइंटमेंट {slot} के लिए पक्का हो गया है। क्या आपको क्लिनिक का पता या फीस जाननी है?"
                else:
                    doc = clinic_service.profile.doctor_name
                    clarify_reply = f"Certainly! The appointment for {p_name or 'the patient'} with {doc} is confirmed for {slot}. Would you like the clinic address or consultation fee details?"
                session_manager.add_model_message(call_sid, clarify_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Handled Clarification / Repeat Request: {clarify_reply}")
                yield clarify_reply, False
                return

            # Instant Fees handling (including STT mishearings like 'age kya' / 'age' which sounds like 'fees')
            if any(w in user_lower for w in ["fee", "fees", "paisa", "paise", "rupaye", "rupees", "charge", "charges", "cost", "kitna", "kitne", "rate", "age kya", "age kitna", "age"]):
                fees_reply = clinic_service.get_fees_response(user_lang)
                session_manager.add_model_message(call_sid, fees_reply)
                yield fees_reply, False
                return

            # Instant Address / Location handling
            if any(w in user_lower for w in ["address", "kahan", "kaha", "location", "kidhar", "clinic", "rasta", "road", "hospital", "pata"]):
                addr_reply = clinic_service.get_address_response(user_lang)
                session_manager.add_model_message(call_sid, addr_reply)
                yield addr_reply, False
                return

            # Instant Clinic Timings handling
            if any(w in user_lower for w in ["timing", "timings", "kab", "samay", "open", "khula", "hours"]):
                timing_reply = clinic_service.get_timings_response(user_lang)
                session_manager.add_model_message(call_sid, timing_reply)
                yield timing_reply, False
                return

            # Dynamic FAQ lookup from Clinic Profile
            faq_ans = clinic_service.check_faq(user_speech)
            if faq_ans:
                session_manager.add_model_message(call_sid, faq_ans)
                yield faq_ans, False
                return

            # Open follow-up: DO NOT HANG UP! Keep call connected (is_done=False)
            if user_lang == "HINDI":
                fallback_followup = f"जी, {name_str}अपॉइंटमेंट {slot} पर पक्का है। क्या क्लिनिक का पता या फीस के बारे में कुछ जानना चाहते हैं?"
            else:
                fallback_followup = f"Yes, {name_str}appointment is confirmed for {slot}. Would you like to know the clinic address or consultation fees?"
            session_manager.add_model_message(call_sid, fallback_followup)
            logger.info(f"[CALL {call_sid[-6:]}] Post-booking open follow-up: {fallback_followup}")
            yield fallback_followup, False
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        schedule_summary = get_live_schedule_summary()
        system_prompt = get_receptionist_prompt(today_str, schedule_summary, target_lang=user_lang)

        include_tools = not session.is_completed

        recent_messages = session.messages[-6:] if len(session.messages) > 6 else session.messages
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": recent_messages,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 300
            }
        }
        if include_tools:
            payload["tools"] = TOOLS_DEFINITION

        text_buffer = ""
        accumulated_chunks = []
        function_call_payload = None
        stream_successful = False

        # Attempt fast streaming with fallback models
        for model, t_sec in FAST_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={self.api_key}"
            try:
                async with aiohttp.ClientSession() as http_sess:
                    async with http_sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=t_sec + 1.2)) as resp:
                        if resp.status != 200:
                            logger.warning(f"[LLM STREAM] Model {model} returned HTTP {resp.status}")
                            continue

                        stream_successful = True
                        async for line_bytes in resp.content:
                            line = line_bytes.decode('utf-8', errors='replace').strip()
                            if not line or not line.startswith("data: "):
                                continue

                            json_str = line[6:].strip()
                            try:
                                data = json.loads(json_str)
                            except Exception:
                                continue

                            candidate = data.get("candidates", [{}])[0]
                            parts = candidate.get("content", {}).get("parts", [])

                            for p in parts:
                                if "functionCall" in p and include_tools:
                                    function_call_payload = p["functionCall"]
                                if "text" in p and p["text"]:
                                    token = p["text"]
                                    text_buffer += token
                                    sentences, text_buffer = split_sentences(text_buffer)
                                    for sent in sentences:
                                        cleaned = clean_for_speech(sent, is_hindi=(user_lang == "HINDI"))
                                        if cleaned:
                                            accumulated_chunks.append(cleaned)
                                            yield cleaned, False

                if stream_successful:
                    break
            except Exception as e:
                logger.warning(f"[LLM STREAM] Model {model} stream exception: {e}")
                continue

        # Handle Function Call (Booking appointment)
        if function_call_payload:
            args = function_call_payload.get("args", {})
            patient_name = args.get("patient_name", "Patient").strip().title()

            # Filter relation words from being recorded as names
            if patient_name.lower() in INVALID_RELATION_NAMES:
                if user_lang == "HINDI":
                    prompt_for_name = "जी बिल्कुल! आपके दोस्त का शुभ नाम क्या है ताकि मैं अपॉइंटमेंट बुक कर सकूँ?"
                else:
                    prompt_for_name = "Certainly! Could you please share the patient's full name so I can confirm the booking?"
                session_manager.add_model_message(call_sid, prompt_for_name)
                logger.info(f"[CALL {call_sid[-6:]}] Filtered relation '{patient_name}'. Asking for real patient name.")
                yield prompt_for_name, False
                return

            # Parse and validate 10-digit phone number
            raw_phone = str(args.get("phone_number") or "")
            parsed_phone = parse_spoken_hindi_numbers(raw_phone)
            phone = parsed_phone or (session.caller_phone if session.caller_phone and len(re.sub(r'\D', '', session.caller_phone)) >= 10 else None)

            if not phone or len(re.sub(r'\D', '', phone)) < 10:
                if user_lang == "HINDI":
                    prompt_phone = "जी बिल्कुल! अपॉइंटमेंट बुक करने के लिए कृपया अपना 10 अंकों का मोबाइल नंबर बता दीजिए।"
                else:
                    prompt_phone = "Certainly! Please share your 10-digit contact number to confirm the booking."
                session_manager.add_model_message(call_sid, prompt_phone)
                logger.info(f"[CALL {call_sid[-6:]}] Phone missing or incomplete. Asking for phone number.")
                yield prompt_phone, False
                return

            clean_digits = re.sub(r'\D', '', phone)[-10:]
            readback = format_digits_for_readback(clean_digits)

            date_val = args.get("date", today_str)
            slot = args.get("time_slot", "11:00 AM")
            symptoms = args.get("symptoms", "General Consultation")
            duration = args.get("duration", "Recently noticed")

            # 1. Save to Excel
            booking = sheet_service.save_appointment(
                patient_name=patient_name,
                phone_number=clean_digits,
                date_str=date_val,
                time_slot=slot,
                symptoms=symptoms,
                duration=duration
            )

            # 2. Real-time Telegram Alert
            telegram_service.send_appointment_alert(booking)
            session_manager.mark_completed(call_sid, booking)

            try:
                dt = datetime.strptime(date_val, "%Y-%m-%d")
                friendly_date = dt.strftime("%A, %B %d")
            except Exception:
                friendly_date = date_val

            clean_slot = slot.replace(":00", "").replace(" PM", " बजे").replace(" AM", " बजे")

            if user_lang == "HINDI":
                spoken_text = (
                    f"बिल्कुल! {patient_name} जी का अपॉइंटमेंट {friendly_date} को "
                    f"{clean_slot} पक्का हो गया है। आपका फ़ोन नंबर {readback} नोट कर लिया गया है। "
                    "क्या क्लिनिक का पता या फीस जाननी है?"
                )
            else:
                doc = clinic_service.profile.doctor_name
                spoken_text = (
                    f"Perfect! The appointment for {patient_name} with {doc} is confirmed for "
                    f"{friendly_date} at {slot} for {symptoms}. Registered number is {readback}. "
                    "Do you need clinic address or timings?"
                )

            cleaned = clean_for_speech(spoken_text, is_hindi=(user_lang == "HINDI"))
            session_manager.add_model_message(call_sid, cleaned)
            logger.info(f"[CALL {call_sid[-6:]}] Confirmed Booking: {booking['booking_id']} for {patient_name}")
            yield cleaned, False
            return

        # Flush any remaining text in the buffer
        if text_buffer.strip():
            cleaned_rem = clean_for_speech(text_buffer, is_hindi=(user_lang == "HINDI"))
            if cleaned_rem:
                accumulated_chunks.append(cleaned_rem)
                yield cleaned_rem, False

        # Store complete message in history
        if accumulated_chunks:
            full_reply = " ".join(accumulated_chunks)
            session_manager.add_model_message(call_sid, full_reply)
            logger.info(f"[CALL {call_sid[-6:]}] AI Replied: {full_reply}")
        elif not stream_successful:
            if user_lang == "HINDI":
                fallback = "Ji bilkul, aap apni takleef aur aane ka time bata dijiye."
            else:
                fallback = "Certainly! Could you please share your symptoms and preferred time to see Dr. Sharma?"
            session_manager.add_model_message(call_sid, fallback)
            yield fallback, False

    def _call_gemini_api(self, contents: list, system_prompt: str, include_tools: bool = True) -> dict:
        """Synchronous REST API call with capped sliding window for <1.5s turnaround."""
        recent_messages = contents[-6:] if len(contents) > 6 else contents
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": recent_messages,
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": 150
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
                logger.warning(f"[LLM] Model {model} returned HTTP {res.status_code}, trying next...")
            except Exception as e:
                logger.warning(f"[LLM] Model {model} exception: {e}")
                continue

        raise Exception("All LLM models failed or timed out.")

    def process_turn(self, call_sid: str, user_speech: str) -> tuple[str, bool]:
        """
        Synchronous turn processor for standard Gather webhook fallback and simulator.
        """
        session = session_manager.get_or_create(call_sid)
        user_lower = user_speech.lower().strip()
        user_lang = detect_user_language(user_speech, current_lang=session.language)
        session.language = user_lang

        # Detect spoken 10-digit phone number if present in speech
        spoken_phone = parse_spoken_hindi_numbers(user_speech)
        if spoken_phone:
            session.caller_phone = spoken_phone
            logger.info(f"[CALL {call_sid[-6:]}] Extracted Spoken 10-digit Phone: {spoken_phone}")

        session_manager.add_user_message(call_sid, user_speech)
        logger.info(f"\n[CALL {call_sid[-6:]}] User Said ({user_lang}): {user_speech}")

        # --- 1. ENTERPRISE IMMEDIATE ROUTING (Prevents Post-Booking Loops & Timeouts) ---
        if session.is_completed:
            appt = session.appointment_data or {}
            p_name = appt.get('patient_name', '')
            slot = appt.get('time_slot', '')
            name_str = f"{p_name} जी का " if p_name and p_name.lower() not in ("patient", "dost") else ""

            # Check for explicit closing cues (Caller actually wants to end the call)
            closing_words = [
                "nahi", "nahin", "no", "nope", "nothing", "kuch nahi", "kuch nhi", 
                "bas", "bus", "thank you", "thanks", "dhanyawad", "shukriya", 
                "bye", "goodbye", "all set", "ho gaya", "theek hai bas", "nahi bas"
            ]
            has_question_words = any(q in user_lower for q in [
                "kya", "kitna", "kitne", "kab", "kahan", "kaha", "fee", "fees", "address", 
                "timing", "charge", "paisa", "paise", "cost", "pata", "age", "location", "doctor"
            ])
            is_closing = any(re.search(r'\b' + re.escape(c) + r'\b', user_lower) for c in closing_words) and not has_question_words

            if is_closing:
                concluding_reply = f"आपका बहुत-बहुत धन्यवाद! {name_str}अपॉइंटमेंट पक्का हो गया है। क्लिनिक पर समय से 10 मिनट पहले पधारें। अपना ख्याल रखियेगा, नमस्ते!" if user_lang == "HINDI" else f"You are most welcome! {name_str}appointment is confirmed. Please arrive 10 minutes prior. Take care and goodbye!"
                session_manager.add_model_message(call_sid, concluding_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Explicit Call Closing: {concluding_reply}")
                return concluding_reply, True

            # Clarification / Repeat Request Handling
            repeat_cues = [
                "samajh nahi", "samajh nhi", "sunai nahi", "sunayi nahi", "kya bola", "kya kaha",
                "repeat", "phir se", "fir se", "dobara", "ek bar aur", "pardon", "sorry",
                "didn't understand", "did not understand", "could not hear", "say again", "once more"
            ]
            if any(cue in user_lower for cue in repeat_cues):
                if user_lang == "HINDI":
                    clarify_reply = f"जी मैं फिर से बता देती हूँ। {name_str}अपॉइंटमेंट {slot} के लिए पक्का हो गया है। क्या आपको क्लिनिक का पता या फीस जाननी है?"
                else:
                    doc = clinic_service.profile.doctor_name
                    clarify_reply = f"Certainly! The appointment for {p_name or 'the patient'} with {doc} is confirmed for {slot}. Would you like the clinic address or consultation fee details?"
                session_manager.add_model_message(call_sid, clarify_reply)
                logger.info(f"[CALL {call_sid[-6:]}] Handled Clarification / Repeat Request: {clarify_reply}")
                return clarify_reply, False

            # Instant Fees handling (including STT mishearings like 'age kya' / 'age' which sounds like 'fees')
            if any(w in user_lower for w in ["fee", "fees", "paisa", "paise", "rupaye", "rupees", "charge", "charges", "cost", "kitna", "kitne", "rate", "age kya", "age kitna", "age"]):
                fees_reply = clinic_service.get_fees_response(user_lang)
                session_manager.add_model_message(call_sid, fees_reply)
                return fees_reply, False

            # Instant Address / Location handling
            if any(w in user_lower for w in ["address", "kahan", "kaha", "location", "kidhar", "clinic", "rasta", "road", "hospital", "pata"]):
                addr_reply = clinic_service.get_address_response(user_lang)
                session_manager.add_model_message(call_sid, addr_reply)
                return addr_reply, False

            # Instant Clinic Timings handling
            if any(w in user_lower for w in ["timing", "timings", "kab", "samay", "open", "khula", "hours"]):
                timing_reply = clinic_service.get_timings_response(user_lang)
                session_manager.add_model_message(call_sid, timing_reply)
                return timing_reply, False

            # Dynamic FAQ lookup from Clinic Profile
            faq_ans = clinic_service.check_faq(user_speech)
            if faq_ans:
                session_manager.add_model_message(call_sid, faq_ans)
                return faq_ans, False

            # Open follow-up: DO NOT HANG UP! Keep call connected (is_done=False)
            if user_lang == "HINDI":
                fallback_followup = f"जी, {name_str}अपॉइंटमेंट {slot} पर पक्का है। क्या क्लिनिक का पता या फीस के बारे में कुछ जानना चाहते हैं?"
            else:
                fallback_followup = f"Yes, {name_str}appointment is confirmed for {slot}. Would you like to know the clinic address or consultation fees?"
            session_manager.add_model_message(call_sid, fallback_followup)
            logger.info(f"[CALL {call_sid[-6:]}] Post-booking open follow-up: {fallback_followup}")
            return fallback_followup, False

        today_str = datetime.now().strftime("%Y-%m-%d")
        schedule_summary = get_live_schedule_summary()
        system_prompt = get_receptionist_prompt(today_str, schedule_summary, target_lang=user_lang)

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

                if patient_name.lower() in INVALID_RELATION_NAMES:
                    if user_lang == "HINDI":
                        prompt_for_name = "जी बिल्कुल! आपके दोस्त का शुभ नाम क्या है ताकि मैं अपॉइंटमेंट बुक कर सकूँ?"
                    else:
                        prompt_for_name = "Certainly! Could you please share the patient's full name so I can confirm the booking?"
                    session_manager.add_model_message(call_sid, prompt_for_name)
                    logger.info(f"[CALL {call_sid[-6:]}] Filtered relation '{patient_name}'. Asking for real patient name.")
                    return prompt_for_name, False

                # Parse and validate 10-digit phone number
                raw_phone = str(args.get("phone_number") or "")
                parsed_phone = parse_spoken_hindi_numbers(raw_phone)
                phone = parsed_phone or (session.caller_phone if session.caller_phone and len(re.sub(r'\D', '', session.caller_phone)) >= 10 else None)

                if not phone or len(re.sub(r'\D', '', phone)) < 10:
                    if user_lang == "HINDI":
                        prompt_phone = "जी बिल्कुल! अपॉइंटमेंट बुक करने के लिए कृपया अपना 10 अंकों का मोबाइल नंबर बता दीजिए।"
                    else:
                        prompt_phone = "Certainly! Please share your 10-digit contact number to confirm the booking."
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

                clean_slot = slot.replace(":00", "").replace(" PM", " बजे").replace(" AM", " बजे")

                if user_lang == "HINDI":
                    spoken_text = (
                        f"बिल्कुल! {patient_name} जी का अपॉइंटमेंट {friendly_date} को "
                        f"{clean_slot} पक्का हो गया है। आपका फ़ोन नंबर {readback} नोट कर लिया गया है। "
                        "क्या क्लिनिक का पता या फीस जाननी है?"
                    )
                else:
                    doc = clinic_service.profile.doctor_name
                    spoken_text = (
                        f"Perfect! The appointment for {patient_name} with {doc} is confirmed for "
                        f"{friendly_date} at {slot} for {symptoms}. Registered contact number is {readback}. "
                        "Do you need clinic address or timings?"
                    )

                logger.info(f"[CALL {call_sid[-6:]}] Confirmed Booking: {booking['booking_id']} for {patient_name}")
                is_done = False

            else:
                if direct_text and len(direct_text.strip()) > 3:
                    spoken_text = direct_text
                else:
                    doc = clinic_service.profile.doctor_name
                    if user_lang == "HINDI":
                        spoken_text = "जी बिल्कुल! बताइए मैं आपकी क्या मदद कर सकती हूँ?"
                    else:
                        spoken_text = f"Certainly! How may I assist you with your appointment with {doc} today?"
                is_done = False

            cleaned = clean_for_speech(spoken_text, is_hindi=(user_lang == "HINDI"))
            session_manager.add_model_message(call_sid, cleaned)
            logger.info(f"[CALL {call_sid[-6:]}] AI Replied: {cleaned}")

            return cleaned, is_done

        except Exception as e:
            logger.error(f"[CALL {call_sid[-6:]}] Error in LLM turn: {e}")
            doc = clinic_service.profile.doctor_name
            # Smart context-aware conversational fallback
            if any(t in user_lower for t in ["6:30", "11:30", "12:30", "1:30", "7:30", "6", "7", "11", "12", "baje", "pm", "am", "time", "slot"]):
                if user_lang == "HINDI":
                    fallback = "जी समझ गई। अपॉइंटमेंट पक्का करने के लिए कृपया अपना 10 अंकों का मोबाइल नंबर बता दीजिए।"
                else:
                    fallback = "Understood. Please share your 10-digit contact number so I can confirm your slot."
            else:
                if user_lang == "HINDI":
                    fallback = "जी बिल्कुल, आप अपनी समस्या और आने का समय बता दीजिए।"
                else:
                    fallback = f"Certainly! Could you please share your symptoms and preferred time to see {doc}?"
            session_manager.add_model_message(call_sid, fallback)
            return fallback, False

llm_service = LLMService()
