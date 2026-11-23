import asyncio
import base64
import json
import logging
import aiohttp
from fastapi import WebSocket, WebSocketDisconnect
from app.core.config import settings
from app.services.session_manager import session_manager
from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

# Deepgram Nova-2 Full-Duplex Real-Time STT WebSocket URL with VAD and Multilingual support
DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen?"
    "encoding=mulaw&sample_rate=8000&channels=1&"
    "endpointing=300&interim_results=true&punctuate=true&smart_format=true&"
    "model=nova-2&language=multi&vad_events=true"
)

INITIAL_GREETING = (
    f"Namaste! Welcome to {settings.CLINIC_NAME}. "
    "Main Maya hoon, batayein main aapki kya madad kar sakti hoon?"
)

class StreamingVoiceService:
    def __init__(self):
        self.active_streams = set()

    async def generate_tts_mulaw(self, text: str) -> bytes:
        """
        Generates 8kHz mu-law audio bytes for Twilio Media Streams.
        Engine Priority:
          1. ElevenLabs Turbo v2.5 (Sub-500ms ultra-realistic speech in ulaw_8000)
          2. Cartesia Sonic (Sub-100ms ultra-low latency)
          3. Deepgram Aura (Native mulaw 8000Hz fallback)
        """
        cleaned_text = text.strip()
        if not cleaned_text:
            return b""

        # 1. ElevenLabs Turbo v2.5 (Industry Standard Ultra-Low Latency Human Voice)
        if settings.ELEVENLABS_API_KEY:
            voice_id = "EXAVITQu4vr4xnSDxMaL"  # Sarah (Warm, Reassuring clinic receptionist)
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=ulaw_8000"
            headers = {
                "xi-api-key": settings.ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "text": cleaned_text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75
                }
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        logger.warning(f"[TTS] ElevenLabs Turbo returned HTTP {resp.status}: {await resp.text()}")
            except Exception as e:
                logger.warning(f"[TTS] ElevenLabs Turbo exception: {e}")

        # 2. Cartesia Sonic TTS
        if settings.CARTESIA_API_KEY:
            url = "https://api.cartesia.ai/tts/bytes"
            headers = {
                "X-API-Key": settings.CARTESIA_API_KEY,
                "Cartesia-Version": "2024-06-10",
                "Content-Type": "application/json"
            }
            payload = {
                "model_id": "sonic-multilingual",
                "transcript": cleaned_text,
                "voice": {"mode": "id", "id": "a0e99841-438c-4a64-b679-ae501e7d6091"},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_mulaw",
                    "sample_rate": 8000
                }
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        logger.warning(f"[TTS] Cartesia returned HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"[TTS] Cartesia exception: {e}")

        # 3. Deepgram Aura TTS (Zero-config fallback with Deepgram API key)
        if settings.DEEPGRAM_API_KEY:
            url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=mulaw&sample_rate=8000"
            headers = {
                "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {"text": cleaned_text}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        logger.warning(f"[TTS] Deepgram Aura returned HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"[TTS] Deepgram Aura exception: {e}")

        return b""

    async def handle_websocket(self, twilio_ws: WebSocket):
        """
        Manages full-duplex bi-directional WebSocket audio between Twilio Media Streams,
        Deepgram Nova-2 STT, Streaming Gemini Flash-Lite LLM, and ElevenLabs Turbo TTS.
        Features sub-100ms Barge-In (interruption detection) and zero-memory-leak task lifecycle.
        """
        await twilio_ws.accept()
        logger.info("[STREAM WS] Twilio media stream accepted.")

        stream_sid = None
        call_sid = None
        caller_phone = None

        interrupted = asyncio.Event()
        is_maya_speaking = False
        active_playback_task = None
        active_turn_task = None

        if not settings.DEEPGRAM_API_KEY:
            logger.error("[STREAM WS] DEEPGRAM_API_KEY is not configured in .env. Closing WebSocket.")
            await twilio_ws.close()
            return

        deepgram_headers = {"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"}

        async with aiohttp.ClientSession() as http_sess:
            try:
                async with http_sess.ws_connect(DEEPGRAM_WS_URL, headers=deepgram_headers) as deepgram_ws:
                    logger.info("[STREAM WS] Deepgram Nova-2 Multilingual WebSocket connected.")

                    async def send_twilio_clear():
                        """Sends Twilio clear event to instantly flush buffered audio on the caller's ear."""
                        if stream_sid:
                            try:
                                clear_msg = {"event": "clear", "streamSid": stream_sid}
                                await twilio_ws.send_text(json.dumps(clear_msg))
                                logger.info(f"[BARGE-IN] Sent CLEAR event to Twilio for stream {stream_sid[-6:]}")
                            except Exception as e:
                                logger.warning(f"[BARGE-IN] Error sending clear event: {e}")

                    async def play_audio_chunk(audio_bytes: bytes) -> bool:
                        """
                        Streams 8kHz mu-law audio in 20ms frames (160 bytes) with instant cancellation check.
                        Returns True if playback completed, False if interrupted.
                        """
                        nonlocal is_maya_speaking
                        if not audio_bytes or not stream_sid:
                            return False

                        chunk_size = 160  # 160 bytes = 20ms of 8000Hz 8-bit mu-law audio
                        is_maya_speaking = True

                        for i in range(0, len(audio_bytes), chunk_size):
                            if interrupted.is_set():
                                logger.info("[STREAM AUDIO] Playback aborted mid-chunk due to user interruption.")
                                is_maya_speaking = False
                                return False

                            chunk = audio_bytes[i:i + chunk_size]
                            payload = base64.b64encode(chunk).decode("utf-8")
                            media_msg = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": payload}
                            }
                            try:
                                await twilio_ws.send_text(json.dumps(media_msg))
                                await asyncio.sleep(0.019)  # 20ms real-time audio frame pacing
                            except Exception:
                                is_maya_speaking = False
                                return False

                        is_maya_speaking = False
                        return True

                    async def process_user_turn(user_transcript: str):
                        """
                        Streaming LLM + Sentence-by-sentence TTS pipeline:
                        As Gemini tokens arrive, sentence chunks are synthesized and played immediately.
                        """
                        nonlocal active_playback_task, is_maya_speaking
                        logger.info(f"[STREAM TURN] Processing user input: '{user_transcript}'")

                        interrupted.clear()

                        try:
                            async for sentence, is_done in llm_service.stream_turn(call_sid or "stream_call", user_transcript):
                                if interrupted.is_set():
                                    logger.info("[STREAM TURN] Aborting LLM turn due to caller interruption.")
                                    break

                                logger.info(f"[STREAM TURN] Synthesizing sentence chunk: '{sentence}'")
                                audio_bytes = await self.generate_tts_mulaw(sentence)

                                if interrupted.is_set():
                                    logger.info("[STREAM TURN] Aborting playback after TTS synthesis due to interruption.")
                                    break

                                if audio_bytes and stream_sid:
                                    active_playback_task = asyncio.create_task(play_audio_chunk(audio_bytes))
                                    completed = await active_playback_task
                                    if not completed:
                                        break

                                if is_done:
                                    logger.info("[STREAM TURN] Call marked completed. Closing after brief pause.")
                                    await asyncio.sleep(1.0)
                                    await twilio_ws.close()
                                    return

                        except Exception as e:
                            logger.error(f"[STREAM TURN] Error during streaming turn: {e}")

                    async def stream_twilio_to_deepgram():
                        """Pumps inbound caller audio packets from Twilio into Deepgram STT."""
                        nonlocal stream_sid, call_sid, caller_phone, active_playback_task
                        try:
                            while True:
                                raw_msg = await twilio_ws.receive_text()
                                msg = json.loads(raw_msg)
                                event = msg.get("event")

                                if event == "start":
                                    start_info = msg.get("start", {})
                                    stream_sid = msg.get("streamSid") or start_info.get("streamSid")
                                    call_sid = start_info.get("callSid", "call_stream")
                                    custom_params = start_info.get("customParameters", {})
                                    caller_phone = custom_params.get("From", "")

                                    logger.info(f"[STREAM WS] Call started. StreamSid={stream_sid}, CallSid={call_sid[-6:] if call_sid else ''}, From={caller_phone}")
                                    session_manager.get_or_create(call_sid, caller_phone)

                                    # Stream initial warm greeting immediately
                                    initial_audio = await self.generate_tts_mulaw(INITIAL_GREETING)
                                    if initial_audio and stream_sid:
                                        active_playback_task = asyncio.create_task(play_audio_chunk(initial_audio))

                                elif event == "media":
                                    media_info = msg.get("media", {})
                                    payload = media_info.get("payload")
                                    if payload and deepgram_ws:
                                        audio_bytes = base64.b64decode(payload)
                                        await deepgram_ws.send_bytes(audio_bytes)

                                elif event == "stop":
                                    logger.info(f"[STREAM WS] Stop event from Twilio. CallSid={call_sid}")
                                    break

                        except WebSocketDisconnect:
                            logger.info("[STREAM WS] Twilio WebSocket disconnected.")
                        except Exception as e:
                            logger.warning(f"[STREAM WS] Exception in twilio_to_deepgram: {e}")

                    transcript_buffer = []

                    async def handle_deepgram_messages():
                        """
                        Receives STT transcription and VAD events from Deepgram.
                        Triggers instant Barge-In on user speech start, and dispatches turn upon speech completion.
                        """
                        nonlocal active_playback_task, active_turn_task, is_maya_speaking, transcript_buffer
                        try:
                            async for dg_msg in deepgram_ws:
                                if dg_msg.type == aiohttp.WSMsgType.TEXT:
                                    res = json.loads(dg_msg.data)
                                    msg_type = res.get("type", "")

                                    channel = res.get("channel", {})
                                    alternatives = channel.get("alternatives", [{}])
                                    transcript = alternatives[0].get("transcript", "").strip() if alternatives else ""
                                    is_final = res.get("is_final", False)
                                    speech_final = res.get("speech_final", False)

                                    # --- ⚡ INSTANT BARGE-IN / INTERRUPTION HANDLING ⚡ ---
                                    # Deepgram emits SpeechStarted when caller begins speaking
                                    user_started_speaking = (
                                        msg_type == "SpeechStarted" or 
                                        (bool(transcript) and len(transcript.split()) >= 1)
                                    )

                                    if user_started_speaking and (is_maya_speaking or (active_playback_task and not active_playback_task.done())):
                                        logger.info(f"[BARGE-IN] Caller interrupted Maya! Event={msg_type}, Words='{transcript}' -> Flushing audio immediately!")
                                        interrupted.set()
                                        if active_playback_task and not active_playback_task.done():
                                            active_playback_task.cancel()
                                        if active_turn_task and not active_turn_task.done():
                                            active_turn_task.cancel()
                                        is_maya_speaking = False
                                        transcript_buffer.clear()
                                        await send_twilio_clear()

                                    # Accumulate finalized words
                                    if is_final and transcript and transcript not in transcript_buffer:
                                        transcript_buffer.append(transcript)

                                    # --- 🧠 DISPATCH USER TURN ON ENDPOINTING 🧠 ---
                                    if speech_final or (is_final and not res.get("from_finalize", False) and len(transcript_buffer) > 0):
                                        full_speech = " ".join(transcript_buffer).strip()
                                        if not full_speech:
                                            full_speech = transcript
                                        transcript_buffer.clear()

                                        if len(full_speech) >= 2:
                                            logger.info(f"[STREAM STT] Finalized User Speech: '{full_speech}'")
                                            # Cancel any old turn and start fresh
                                            if active_turn_task and not active_turn_task.done():
                                                active_turn_task.cancel()
                                            active_turn_task = asyncio.create_task(process_user_turn(full_speech))

                                elif dg_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    logger.info("[STREAM WS] Deepgram WebSocket connection closed.")
                                    break

                        except Exception as e:
                            logger.error(f"[STREAM WS] Exception in handle_deepgram_messages: {e}")

                    # Run both bi-directional streaming workers concurrently
                    twilio_task = asyncio.create_task(stream_twilio_to_deepgram())
                    dg_task = asyncio.create_task(handle_deepgram_messages())

                    done, pending = await asyncio.wait(
                        [twilio_task, dg_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # Gracefully cancel pending tasks on call finish
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

            except Exception as e:
                logger.error(f"[STREAM WS] Deepgram connection error: {e}")

            finally:
                interrupted.set()
                if active_playback_task and not active_playback_task.done():
                    active_playback_task.cancel()
                if active_turn_task and not active_turn_task.done():
                    active_turn_task.cancel()
                logger.info("[STREAM WS] Session cleaned up successfully.")

streaming_voice_service = StreamingVoiceService()
