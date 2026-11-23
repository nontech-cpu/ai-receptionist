import urllib.request
import urllib.parse
import json
import logging
import threading
import time
from pathlib import Path
from app.core.config import settings, BASE_DIR
from app.services.clinic_service import clinic_service

logger = logging.getLogger(__name__)

class TelegramService:
    def __init__(self):
        self.bot_token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._cached_chat_id = self.chat_id or None
        self._pending_alerts = []
        # Start background listener to detect chat ID as soon as user opens @Frontt_Desk_Bot
        threading.Thread(target=self._start_chat_id_poller, daemon=True).start()

    def _start_chat_id_poller(self):
        """Continuously polls Telegram until user sends /start, then immediately connects!"""
        while not self._cached_chat_id:
            try:
                self.get_latest_chat_id()
                if self._cached_chat_id:
                    print(f"🎉 [TELEGRAM CONNECTED] Bot successfully linked to Chat ID: {self._cached_chat_id}")
                    # Send welcome message
                    self._sync_send(
                        "🏥 <b>DR. SHARMA CARE CLINIC BOT CONNECTED</b>\n\n"
                        "Namaste! Your Telegram is now linked to the Voice AI Receptionist.\n"
                        "All live appointment reports from phone calls will be sent directly here! 📲"
                    )
                    # Send any pending alerts
                    for alert in self._pending_alerts:
                        self.send_appointment_alert(alert)
                    self._pending_alerts.clear()
                    break
            except Exception as e:
                pass
            time.sleep(3)

    def get_latest_chat_id(self, force_refresh: bool = False) -> str | None:
        """Fetches the latest active chat ID from bot updates and permanently persists it."""
        if self._cached_chat_id and not force_refresh:
            return self._cached_chat_id

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
                            self._persist_chat_id(detected_id)
                            return detected_id
        except Exception as e:
            logger.debug(f"Telegram getUpdates check: {e}")

        return None

    def _persist_chat_id(self, chat_id: str):
        env_path = BASE_DIR / ".env"
        try:
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                if "TELEGRAM_CHAT_ID=" in content:
                    lines = []
                    for line in content.splitlines():
                        if line.startswith("TELEGRAM_CHAT_ID="):
                            lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
                        else:
                            lines.append(line)
                    env_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to persist chat_id to .env: {e}")

    def _sync_send(self, text: str, chat_id: str = None):
        target_chat_id = chat_id or self._cached_chat_id or self.get_latest_chat_id()
        if not target_chat_id:
            print(
                "\n⚠️ [TELEGRAM ACTION REQUIRED] You haven't pressed START on @Frontt_Desk_Bot yet!\n"
                "👉 Open Telegram -> search @Frontt_Desk_Bot -> tap 'START'\n"
            )
            return

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
                print(f"[TELEGRAM] Report successfully delivered to Chat ID: {target_chat_id}")
        except Exception as e:
            print(f"[TELEGRAM ERROR] Failed to send message: {e}")

    def send_appointment_alert(self, appointment: dict):
        """Asynchronously dispatches the rich clinical appointment report."""
        if not self._cached_chat_id:
            # Queue alert to send as soon as user opens bot
            self._pending_alerts.append(appointment)

        p = clinic_service.profile
        doc_name = appointment.get('doctor') or p.doctor_name
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
        threading.Thread(target=self._sync_send, args=(message,), daemon=True).start()

telegram_service = TelegramService()
