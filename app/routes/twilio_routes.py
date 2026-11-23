import logging
from fastapi import APIRouter, Request, Form, Response, WebSocket
from twilio.twiml.voice_response import VoiceResponse, Gather, Connect, Stream, Parameter
from twilio.rest import Client
from app.core.config import settings
from app.services.session_manager import session_manager
from app.services.llm_service import llm_service
from app.services.elevenlabs_service import elevenlabs_service
from app.services.streaming_voice_service import streaming_voice_service
from app.services.clinic_service import clinic_service

router = APIRouter()
logger = logging.getLogger(__name__)

VOICE_NAME = "Polly.Kajal-Neural"
VOICE_LANG = "hi-IN"
SPEECH_HINTS = (
    "fees, fee, paise, rupees, consultation, charge, cost, address, location, landmark, timing, "
    "parking, appointment, booking, doctor, clinic, Devendra, Devender, Rahul, Sharma, "
    "today, tomorrow, number, phone, bhai, bukhar, khansi, checkup, "
    "dast, dast hai, ulti, pet dard, sir dard, 10 din, 2 din, 3 din, 10 days, 10 deen, "
    "kal, aaj, subah, shaam, 11 baje, 6 baje, 7 baje, loose motion, "
    "I want to book an appointment, appointment book karna hai, doctor se milna hai, checkup karwana hai"
)

def get_initial_greeting() -> str:
    p = clinic_service.profile
    return (
        f"Hello, welcome to {p.clinic_name}. "
        f"I am {p.receptionist_name}, your clinic voice assistant. How may I assist you today?"
    )

def get_base_url(request: Request) -> str:
    """Dynamically determines the public HTTPS URL from railway / reverse-proxy headers."""
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "web-production-eedb2a06.up.railway.app"))
    return f"{proto}://{host}".rstrip("/")

async def extract_request_data(request: Request) -> dict:
    """
    Extracts parameters from both GET query params and POST form/json data seamlessly.
    Guarantees 100% interoperability regardless of whether Twilio sends GET or POST.
    """
    data = dict(request.query_params)
    if request.method.upper() == "POST":
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                json_data = await request.json()
                if isinstance(json_data, dict):
                    data.update(json_data)
            except Exception:
                pass
        else:
            try:
                form = await request.form()
                data.update(dict(form))
            except Exception:
                pass
    return data

def create_gather(base_url: str = "", language: str = "en-IN") -> Gather:
    """
    Standard listening window supporting speech + keypad DTMF input:
    - speech_model='phone_call' with enhanced=True: Google Telephony model trained on 8kHz mobile audio.
    - language='en-IN': Natively transcribes English, Indian English, and Roman Hinglish with high fidelity.
    - speech_timeout='auto' with timeout='7': prevents premature cutoffs.
    - Explicit absolute action URL prevents cross-host resolution failures.
    """
    action_url = f"{base_url}/voice/respond" if base_url else "/voice/respond"
    return Gather(
        input="speech dtmf",
        num_digits=10,
        finish_on_key="#",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        timeout="7",
        hints=SPEECH_HINTS,
        language=language,
        speech_model="phone_call",
        enhanced=True
    )

def is_devanagari(text: str) -> bool:
    """Detects if text contains native Devanagari script."""
    return any('\u0900' <= char <= '\u097f' for char in text)

def speak_or_play(parent_element, text: str, base_url: str):
    """
    Delivers instant (<1.0s), authentic Indian bilingual speech directly inside Twilio.
    Uses Polly.Kajal-Neural with dynamic Hindi (hi-IN) and Indian English (en-IN) switching.
    - If Devanagari script: uses hi-IN for native Hindi.
    - If Roman script (Hinglish or English): uses en-IN for natural Indian Hinglish accent.
    """
    engine = getattr(settings, "VOICE_ENGINE", "polly_kajal").lower()

    if engine == "elevenlabs" and settings.ELEVENLABS_API_KEY:
        audio_url = elevenlabs_service.get_audio_url(text, base_url)
        if audio_url:
            print(f"[TWILIO AUDIO] Using ElevenLabs Voice: {audio_url}")
            parent_element.play(audio_url)
            return

    if is_devanagari(text):
        clean_text = text
        lang = "hi-IN"
    else:
        clean_text = text
        lang = "en-IN"

    print(f"[TWILIO AUDIO] Using Instant Polly.Kajal-Neural (lang={lang}) | Text: '{clean_text}'")
    say = parent_element.say(voice="Polly.Kajal-Neural", language=lang)
    say.prosody(clean_text, rate="95%")

@router.api_route("/voice/incoming", methods=["GET", "POST"])
@router.api_route("/twilio/voice", methods=["GET", "POST"])
@router.api_route("/voice", methods=["GET", "POST"])
async def voice_incoming(request: Request):
    """
    Call answers -> Speaks greeting in English/Hindi -> waits for user speech.
    Supports both GET and POST to handle all Twilio webhook configurations.
    """
    data = await extract_request_data(request)
    call_sid = data.get("CallSid", "unknown_call")
    caller_phone = data.get("From", "")

    base_url = get_base_url(request)
    print(f"\n[CALL ANSWERED] CallSid={call_sid[-6:]}, From={caller_phone}, Method={request.method}")

    session = session_manager.get_or_create(call_sid, caller_phone)
    greeting = get_initial_greeting()
    session.silence_count = 0
    session_manager.add_model_message(call_sid, greeting)

    if settings.ENABLE_STREAMING and settings.DEEPGRAM_API_KEY:
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "web-production-eedb2a06.up.railway.app"))
        ws_url = f"wss://{host}/voice/stream"
        print(f"[CALL {call_sid[-6:]}] Connecting to Realtime WebSocket Media Stream: {ws_url}")
        resp = VoiceResponse()
        connect = Connect()
        stream = Stream(url=ws_url)
        stream.parameter(name="From", value=caller_phone)
        connect.append(stream)
        resp.append(connect)
        return Response(content=str(resp), media_type="text/xml")

    resp = VoiceResponse()
    speak_or_play(resp, greeting, base_url)
    gather = create_gather(base_url)
    resp.append(gather)

    # If initial 8s silence expires, Twilio falls through to voice/respond automatically
    resp.redirect(f"{base_url}/voice/respond", method="POST")

    return Response(content=str(resp), media_type="text/xml")

@router.api_route("/voice/respond", methods=["GET", "POST"])
async def voice_respond(request: Request):
    """
    Handles speech input or the silence gap state machine:
    - 8s gap without speech -> Says: 'You are not audible. Please go ahead.'
    - Another 8s gap without speech -> Says: 'You are still not audible. Goodbye.' -> Hangs up!
    Supports both GET and POST to handle any Twilio redirect or probe.
    """
    data = await extract_request_data(request)
    call_sid = data.get("CallSid", "unknown_call")
    user_speech = data.get("SpeechResult", "").strip()
    digits = data.get("Digits", "").strip()
    confidence = data.get("Confidence", "1.0")

    if digits:
        if user_speech:
            user_speech = f"{user_speech} {digits}".strip()
        else:
            user_speech = digits

    base_url = get_base_url(request)
    session = session_manager.get_or_create(call_sid)
    p = clinic_service.profile

    resp = VoiceResponse()

    # Case 1: USER DID NOT SPEAK (Silence Triggered)
    if not user_speech:
        # If booking is already done, user stayed silent to finish -> Graceful wrap-up!
        if session.is_completed:
            print(f"[CALL {call_sid[-6:]}] User silent after confirmation. Ending call gracefully.")
            if getattr(session, "language", "") == "ENGLISH":
                farewell = f"Thank you for calling {p.clinic_name}. Take care and goodbye!"
            elif getattr(session, "language", "") == "HINDI_DEVANAGARI":
                farewell = f"{p.clinic_name} में कॉल करने के लिए धन्यवाद। अपना ख्याल रखियेगा, नमस्ते!"
            else:
                farewell = f"{p.clinic_name} mein call karne ke liye shukriya. Apna khayal rakhiye, namaste!"
            speak_or_play(resp, farewell, base_url)
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")

        session.silence_count += 1
        print(f"[CALL {call_sid[-6:]}] Silence detected! Consecutive count = {session.silence_count}")

        if session.silence_count == 1:
            # First silence warning
            if getattr(session, "language", "") == "ENGLISH":
                silence_msg = "Sorry, I could not hear you. Could you please repeat that?"
            elif getattr(session, "language", "") == "HINDI_DEVANAGARI":
                silence_msg = "माफ़ कीजियेगा, आपकी आवाज़ नहीं आई। कृपया बताइये मैं आपकी क्या सहायता करूँ?"
            else:
                silence_msg = "Maaf kariyega, aapki aawaz nahi aayi. Kripya batayein main aapki kya madad kar sakti hoon?"
            speak_or_play(resp, silence_msg, base_url)
            gather = create_gather(base_url)
            resp.append(gather)
            resp.redirect(f"{base_url}/voice/respond", method="POST")
            return Response(content=str(resp), media_type="text/xml")

        else:
            # Second silence -> Hangup!
            print(f"[CALL {call_sid[-6:]}] Second silence reached. Ending call.")
            if getattr(session, "language", "") == "ENGLISH":
                hangup_msg = f"I am still unable to hear you. Thank you for calling {p.clinic_name}. Goodbye!"
            elif getattr(session, "language", "") == "HINDI_DEVANAGARI":
                hangup_msg = f"आपकी आवाज़ नहीं आ रही है। {p.clinic_name} में कॉल करने के लिए धन्यवाद। नमस्ते!"
            else:
                hangup_msg = f"Aapki aawaz nahi aa rahi hai. {p.clinic_name} mein call karne ke liye shukriya. Namaste!"
            speak_or_play(resp, hangup_msg, base_url)
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")

    # Case 2: USER SPOKE (Reset silence counter and continue conversation)
    session.silence_count = 0
    print(f"\n>>> [TWILIO WEBHOOK] User Speech: '{user_speech}' (Confidence: {confidence}, CallSid: {call_sid[-6:]})")

    reply_text, is_done = llm_service.process_turn(call_sid, user_speech)
    print(f"<<< [TWILIO REPLY] AI Voice: '{reply_text}' (is_done={is_done})")

    # Deliver reply cleanly to caller first without premature microphone interruption
    speak_or_play(resp, reply_text, base_url)

    if is_done:
        # Final goodbye after completing conversation -> Hang up cleanly!
        resp.hangup()
    else:
        # Active conversation: gather next user response
        gather = create_gather(base_url)
        resp.append(gather)
        # If user stays silent after reply, falls through to silence handler
        resp.redirect(f"{base_url}/voice/respond", method="POST")

    return Response(content=str(resp), media_type="text/xml")

@router.api_route("/api/make-call", methods=["GET", "POST"])
async def trigger_outbound_call(request: Request, to_number: str = "+918824466229", webhook_url: str = ""):
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured."}

    data = await extract_request_data(request)
    raw_to = data.get("to_number", to_number)
    raw_webhook = data.get("webhook_url", webhook_url)

    # Clean and sanitize target phone number (auto prepend +91 for 10-digit Indian numbers)
    clean_to = raw_to.strip().replace(" ", "").replace("-", "")
    if clean_to and not clean_to.startswith("+"):
        if len(clean_to) == 10:
            clean_to = f"+91{clean_to}"
        else:
            clean_to = f"+{clean_to}"

    # Primary outbound number from Twilio settings
    from_number = settings.TWILIO_PHONE_NUMBER or "+17372212163"

    # Clean and sanitize target webhook URL
    raw = (raw_webhook or "").strip().rstrip(". ").rstrip("/")
    if not raw:
        raw = get_base_url(request)
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"https://{raw}"
    raw = raw.rstrip(". ").rstrip("/")

    if not raw.endswith("/voice/incoming") and not raw.endswith("/twilio/voice"):
        target_url = f"{raw}/voice/incoming"
    else:
        target_url = raw

    try:
        # Note: Do NOT pass method="POST" into calls.create because Twilio trial accounts reject it with HTTP 400
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            url=target_url
        )
        logger.info(f"[OUTBOUND CALL] Triggered call to {clean_to} from {from_number} with webhook: {target_url} (SID: {call.sid})")
        return {
            "status": "success",
            "call_sid": call.sid,
            "to": clean_to,
            "from": from_number,
            "target_url": target_url,
            "message": f"Calling {clean_to} now from {from_number}..."
        }
    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return {"error": str(e)}

@router.websocket("/voice/stream")
async def voice_stream_endpoint(websocket: WebSocket):
    await streaming_voice_service.handle_websocket(websocket)
