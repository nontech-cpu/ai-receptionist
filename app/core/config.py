import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

class Settings:
    PROJECT_NAME: str = "AI Clinic Receptionist"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Security & Administration
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")
    ALLOWED_ORIGINS: str = os.getenv("ALLOWED_ORIGINS", "")
    WEBHOOK_BASE_URL: str = os.getenv("WEBHOOK_BASE_URL", "")
    TWILIO_VALIDATE_SIGNATURE: bool = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() in ("true", "1", "yes")

    # Speech Recognition & Confidence Thresholds
    STT_HIGH_CONFIDENCE_THRESHOLD: float = float(os.getenv("STT_HIGH_CONFIDENCE_THRESHOLD", "0.80"))
    STT_MEDIUM_CONFIDENCE_THRESHOLD: float = float(os.getenv("STT_MEDIUM_CONFIDENCE_THRESHOLD", "0.55"))
    STT_LOW_CONFIDENCE_THRESHOLD: float = float(os.getenv("STT_LOW_CONFIDENCE_THRESHOLD", "0.25"))

    # Slot Conflict & Overlap Enforcement (Default False for doctor testing/demos)
    ENABLE_SLOT_CONFLICT_CHECK: bool = os.getenv("ENABLE_SLOT_CONFLICT_CHECK", "false").lower() in ("true", "1")

    # Twilio
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "+17372212163")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")

    # Clinic
    CLINIC_NAME: str = os.getenv("CLINIC_NAME", "Dr. Sharma's Care Clinic")
    DOCTOR_NAME: str = os.getenv("DOCTOR_NAME", "Dr. Sharma")

    # Streaming Voice AI (Twilio Media Streams + Deepgram + Cartesia/ElevenLabs)
    ENABLE_STREAMING: bool = os.getenv("ENABLE_STREAMING", "false").lower() in ("true", "1", "yes")
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    CARTESIA_API_KEY: str = os.getenv("CARTESIA_API_KEY", "")
    ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")

    # Voice Engine Selection ("polly_kajal" for instant Indian voice, "elevenlabs" for ElevenLabs)
    VOICE_ENGINE: str = os.getenv("VOICE_ENGINE", "polly_kajal")

    # Data
    EXCEL_FILE_PATH: Path = BASE_DIR / os.getenv("EXCEL_FILE_PATH", "data/appointments.xlsx")

settings = Settings()
