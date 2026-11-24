import urllib.request
import urllib.parse
import json
import logging
import threading
from pathlib import Path
from app.core.config import settings
from app.services.clinic_service import clinic_service

logger = logging.getLogger(__name__)

def mask_phone_for_logs(phone: str) -> str:
    """Masks phone number for privacy e.g. ******2345."""
    if not phone or len(phone) < 4:
        return "****"
    return "*" * (len(phone) - 4) + phone[-4:]

class TelegramService:
    def __init__(self):
        self.bot_token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._cached_chat_id = self.chat_id or None
        self._pending_alerts = []

    def get_latest_chat_id(self, force_refresh: bool = False) -> str | None:
        """Fetches active chat ID from bot updates without modifying runtime environment files."""
        if self._cached_chat_id and not force_refresh:
            return self._cached_chat_id

        if not self.bot_token:
            return None

        try:
            url = f"{self.base_url}/getUpdates"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=4) as response:
                data = json.loads(response.read().decode())
                results = data.get("result", [])
                if results:
                    for item in reversed(results):
                        msg = item.get("message") or item.get("channel_post") or item.get("my_chat_member")
                        if msg and "chat" in msg:
                            detected_id = str(msg["chat"]["id"])
                            self._cached_chat_id = detected_id
                            return detected_id
        except Exception as e:
            logger.debug(f"[TELEGRAM] getUpdates check error: {e}")

        return None

    def _sync_send(self, text: str, chat_id: str = None) -> bool:
        """Synchronously dispatches a formatted message to Telegram."""
        if not self.bot_token:
            return False

        target_chat_id = chat_id or self._cached_chat_id or self.get_latest_chat_id()
        if not target_chat_id:
            logger.warning("[TELEGRAM] No chat ID configured or detected to deliver alert.")
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": target_chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                logger.info(f"[TELEGRAM] Report successfully delivered to Chat ID: {target_chat_id}")
                return True
        except Exception as e:
            logger.error(f"[TELEGRAM ERROR] Failed to deliver notification: {e}")
            return False

    def send_appointment_alert(self, appointment: dict):
        """Asynchronously dispatches clinical appointment report."""
        if not self.bot_token:
            return

        p = clinic_service.profile
        doc_name = appointment.get('doctor') or p.doctor_name
        masked_phone = mask_phone_for_logs(appointment.get('phone_number', ''))

        message = (
            f"🏥 <b>{p.clinic_name.upper()} - APPOINTMENT REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 <b>Booking ID:</b> <code>{appointment.get('booking_id')}</code>\n"
            f"👤 <b>Patient Name:</b> {appointment.get('patient_name')}\n"
            f"📞 <b>Contact Number:</b> {appointment.get('phone_number')}\n"
            f"📅 <b>Appointment Date:</b> {appointment.get('date')}\n"
            f"⏰ <b>Time Slot:</b> {appointment.get('time_slot')}\n"
            f"🩺 <b>Symptoms:</b> {appointment.get('symptoms')}\n"
            f"⏳ <b>Duration of Symptoms:</b> {appointment.get('duration')}\n"
            f"👨‍⚕️ <b>Consulting Doctor:</b> {doc_name} ({p.specialization})\n"
            f"🕒 <b>Booked At:</b> {appointment.get('timestamp')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <i>Location: {p.location.address}, {p.location.landmark}, {p.location.city}</i>\n"
            f"💰 <i>Consultation Fee: {p.pricing.currency_symbol}{p.pricing.consultation_fee}</i>\n"
            f"✅ <i>Status: Confirmed via Voice AI Receptionist</i>"
        )
        logger.info(f"[TELEGRAM ALERT] Dispatching alert for booking {appointment.get('booking_id')} (phone={masked_phone})")
        threading.Thread(target=self._sync_send, args=(message,), daemon=True).start()

telegram_service = TelegramService()
