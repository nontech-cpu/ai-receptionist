import hashlib
import logging
import os
from pathlib import Path
import requests
from app.core.config import settings

logger = logging.getLogger(__name__)

AUDIO_CACHE_DIR = Path("data/audio_cache")
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Sarah - Mature, Reassuring, Confident (Perfect clinic receptionist voice)
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
MODEL_ID = "eleven_multilingual_v2"

class ElevenLabsService:
    def __init__(self):
        self.session = requests.Session()

    def get_audio_url(self, text: str, base_url: str) -> str | None:
        """
        Generates and caches MP3 audio for given text using ElevenLabs.
        Returns the public URL for Twilio <Play> or None on error.
        """
        if not settings.ELEVENLABS_API_KEY:
            return None

        # Clean text for hash
        text_clean = text.strip()
        hash_key = hashlib.md5(f"{text_clean}_{VOICE_ID}_{MODEL_ID}".encode()).hexdigest()
        filename = f"{hash_key}.mp3"
        file_path = AUDIO_CACHE_DIR / filename

        # Return cached if exists
        if file_path.exists() and file_path.stat().st_size > 1000:
            return f"{base_url}/api/audio/{filename}"

        # Otherwise generate from ElevenLabs
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=mp3_22050_32"
        headers = {
            "xi-api-key": settings.ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text_clean,
            "model_id": MODEL_ID,
            "voice_settings": {
                "stability": 0.65,
                "similarity_boost": 0.8,
                "use_speaker_boost": True
            }
        }

        try:
            res = self.session.post(url, headers=headers, json=payload, timeout=6.0)
            if res.status_code == 200 and len(res.content) > 1000:
                with open(file_path, "wb") as f:
                    f.write(res.content)
                logger.info(f"[ELEVENLABS] Audio generated and cached: {filename} ({len(res.content)} bytes)")
                return f"{base_url}/api/audio/{filename}"
            else:
                logger.error(f"[ELEVENLABS] API error: {res.status_code} {res.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"[ELEVENLABS] Exception generating speech: {e}")
            return None

elevenlabs_service = ElevenLabsService()
