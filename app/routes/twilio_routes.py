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
from app.utils.phonetics import normalize_for_indian_voice

router = APIRouter()
logger = logging.getLogger(__name__)

VOICE_NAME = "Polly.Kajal-Neural"
VOICE_LANG = "en-IN"
SPEECH_HINTS = (
    "fees, fee, paise, rupees, consultation, charge, cost, address, location, landmark, timing, "
    "parking, appointment, booking, doctor, clinic, Devendra, Rahul, Sharma, "
    "today, tomorrow, number, phone, bhai, bukhar, khansi, checkup"
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
    return f"{proto}://{host}"

def create_gather() -> Gather:
    """
    Standard listening window supporting speech + keypad DTMF input:
    - speech_timeout='auto' prevents Twilio from cutting off caller prematurely during natural conversational pauses.
    - Uses relative action='/voice/respond' so Twilio stays on the active HTTPS connection without 301 redirects.
    """
    return Gather(
        input="speech dtmf",
        num_digits=10,
        finish_on_key="#",
        action="/voice/respond",
        method="POST",
        speech_timeout="auto",
        timeout="8",
        hints=SPEECH_HINTS,
        language=VOICE_LANG
    )

def is_hindi_phrase(text: str) -> bool:
    """Detects if text contains Hindi words or Devanagari script for proper pronunciation."""
    if any('\u0900' <= char <= '\u097f' for char in text):
        return True
    hindi_cues = [
        "ji", "bilkul", "aapka", "aapke", "mein", "hai", "hain", "dard", "paon", 
        "kal", "aaj", "baje", "takleef", "bukhar", "khansi", "kya", "sharma", 
        "clinic", "din", "se", "naam", "unka", "unki", "swagat", "theek", "hoon", "dost"
    ]
    lower = text.lower()
    return any(w in lower for w in hindi_cues)

def speak_or_play(parent_element, text: str, base_url: str):
    """
    Delivers instant (<1.2s), authentic Indian bilingual speech directly inside Twilio.
    Uses Polly.Kajal-Neural with dynamic Hindi (hi-IN) and Indian English (en-IN) switching.
    Zero network latency, zero MP3 download delay, zero foreigner accent!
    """
    engine = getattr(settings, "VOICE_ENGINE", "polly_kajal").lower()

    if engine == "elevenlabs" and settings.ELEVENLABS_API_KEY:
        audio_url = elevenlabs_service.get_audio_url(text, base_url)
        if audio_url:
            print(f"[TWILIO AUDIO] Using ElevenLabs Voice: {audio_url}")
            parent_element.play(audio_url)
            return

    # Instant Twilio Native Indian Bilingual Voice (Kajal-Neural) with calm human prosody
    # If text is Hindi/Hinglish, normalize Roman words to Devanagari for flawless Indian phonetics.
    if is_hindi_phrase(text):
        clean_text = normalize_for_indian_voice(text)
        lang = "hi-IN"
    else:
        clean_text = text
        lang = "en-IN"

    print(f"[TWILIO AUDIO] Using Instant Polly.Kajal-Neural (lang={lang}) | Text: '{clean_text}'")
    say = parent_element.say(voice="Polly.Kajal-Neural", language=lang)
    say.prosody(clean_text, rate="93%")

@router.post("/voice/incoming")
@router.post("/twilio/voice")
@router.post("/")
async def voice_incoming(request: Request):
    """
    Call answers -> Speaks greeting in English -> waits 10s.
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown_call")
    caller_phone = form_data.get("From", "")

    base_url = get_base_url(request)
    print(f"\n[CALL ANSWERED] CallSid={call_sid[-6:]}, From={caller_phone}")

    session = session_manager.get_or_create(call_sid, caller_phone)
    greeting = get_initial_greeting()
    session.silence_count = 0
    session_manager.add_model_message(call_sid, greeting)

    if settings.ENABLE_STREAMING and settings.DEEPGRAM_API_KEY:
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "tightly-barbell-creamed.ngrok-free.dev"))
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
    gather = create_gather()
    resp.append(gather)

    # If initial 8s silence expires, Twilio falls through to voice/respond automatically
    resp.redirect("/voice/respond", method="POST")

    return Response(content=str(resp), media_type="text/xml")

@router.post("/voice/respond")
async def voice_respond(request: Request):
    """
    Handles speech input or the 10-second silence gap state machine:
    - 10s gap without speech -> Says: 'You are not audible. Please go ahead.'
    - Another 10s gap without speech -> Says: 'You are still not audible. Goodbye.' -> Hangs up!
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown_call")
    user_speech = form_data.get("SpeechResult", "").strip()
    digits = form_data.get("Digits", "").strip()
    confidence = form_data.get("Confidence", "1.0")

    if digits:
        if user_speech:
            user_speech = f"{user_speech} {digits}".strip()
        else:
            user_speech = digits

    base_url = get_base_url(request)
    session = session_manager.get_or_create(call_sid)
    p = clinic_service.profile

    resp = VoiceResponse()

    # Case 1: USER DID NOT SPEAK (10s Silence Triggered)
    if not user_speech:
        # If booking is already done, user stayed silent to finish -> Graceful wrap-up!
        if session.is_completed:
            print(f"[CALL {call_sid[-6:]}] User silent after confirmation. Ending call gracefully.")
            speak_or_play(resp, f"{p.clinic_name} में कॉल करने के लिए धन्यवाद। अपना ख्याल रखियेगा, नमस्ते!", base_url)
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")

        session.silence_count += 1
        print(f"[CALL {call_sid[-6:]}] Silence detected! Consecutive count = {session.silence_count}")

        if session.silence_count == 1:
            # First 8s silence warning
            silence_msg = "माफ़ कीजियेगा, आपकी आवाज़ नहीं आई। कृपया बताइये मैं आपकी क्या सहायता करूँ?"
            speak_or_play(resp, silence_msg, base_url)
            gather = create_gather()
            resp.append(gather)
            resp.redirect("/voice/respond", method="POST")
            return Response(content=str(resp), media_type="text/xml")

        else:
            # Second silence (Total ~16s of no audio) -> Hangup!
            print(f"[CALL {call_sid[-6:]}] Second silence reached. Ending call.")
            speak_or_play(resp, f"आपकी आवाज़ नहीं आ रही है। {p.clinic_name} में कॉल करने के लिए धन्यवाद। नमस्ते!", base_url)
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
        gather = create_gather()
        resp.append(gather)
        # If user stays silent after reply, falls through to silence handler
        resp.redirect("/voice/respond", method="POST")

    return Response(content=str(resp), media_type="text/xml")

@router.post("/api/make-call")
async def trigger_outbound_call(to_number: str = "+918824466229", webhook_url: str = ""):
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured."}

    # Clean and sanitize target phone number
    clean_to = to_number.strip().replace(" ", "").replace("-", "")

    # Dynamic Trial Mapping: Automatically uses the assigned Twilio number per tester
    TRIAL_FROM_MAP = {
        "+919549408914": "+17372212163",  # Sir's assigned Twilio trial number
        "+918824466229": "+17372508034",  # Primary tester's assigned Twilio trial number
    }
    from_number = TRIAL_FROM_MAP.get(clean_to, settings.TWILIO_PHONE_NUMBER)

    # Clean and sanitize target webhook URL
    raw = (webhook_url or "").strip().rstrip(". ").rstrip("/")
    if not raw:
        raw = "https://web-production-eedb2a06.up.railway.app"
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"https://{raw}"
    raw = raw.rstrip(". ").rstrip("/")

    if not raw.endswith("/voice/incoming") and not raw.endswith("/twilio/voice"):
        target_url = f"{raw}/voice/incoming"
    else:
        target_url = raw

    try:
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=clean_to,
            from_=from_number,
            url=target_url
        )
        logger.info(f"[OUTBOUND CALL] Triggered call to {clean_to} from {from_number} with webhook: {target_url} (SID: {call.sid})")
        return {"status": "success", "call_sid": call.sid, "from": from_number, "target_url": target_url, "message": f"Calling {clean_to} now from {from_number}..."}
    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return {"error": str(e)}

@router.websocket("/voice/stream")
async def voice_stream_endpoint(websocket: WebSocket):
    await streaming_voice_service.handle_websocket(websocket)
