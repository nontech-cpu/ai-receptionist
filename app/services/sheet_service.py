import os
import shutil
import logging
from pathlib import Path
from datetime import datetime, date, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from filelock import FileLock

from app.core.config import settings

logger = logging.getLogger(__name__)

class SheetService:
    DEFAULT_SLOTS = [
        "11:00 AM",
        "11:30 AM",
        "12:00 PM",
        "12:30 PM",
        "01:00 PM",
        "01:30 PM",
        "06:00 PM",
        "06:30 PM",
        "07:00 PM",
        "07:30 PM"
    ]

    SLOT_DURATION_MINUTES = 30

    def __init__(self, file_path: Path = None):
        self.file_path = file_path or settings.EXCEL_FILE_PATH
        self._lock_path = Path(str(self.file_path) + ".lock")
        self._call_sid_cache: Dict[str, dict] = {}
        self._ensure_file_exists()

    @property
    def lock(self) -> FileLock:
        return FileLock(str(self._lock_path), timeout=10)

    @staticmethod
    def sanitize_cell_value(val: Any) -> Any:
        """
        Guards against Formula/CSV injection in Excel.
        Prepends a single quote to any text starting with =, +, -, or @.
        """
        if val is None:
            return ""
        if isinstance(val, str):
            s = val.strip()
            if s.startswith(("=", "+", "-", "@")):
                return "'" + s
            return s
        return val

    @staticmethod
    def parse_time_str(time_str: str) -> Optional[time]:
        """Parses various user/AI time formats into datetime.time."""
        if not time_str:
            return None
        clean_time = time_str.strip().upper()
        for fmt in ["%I:%M %p", "%I %p", "%I:%M%p", "%I%p", "%H:%M"]:
            try:
                return datetime.strptime(clean_time, fmt).time()
            except ValueError:
                continue
        return None

    @staticmethod
    def format_canonical_time(t: time) -> str:
        """Formats time consistently as '01:30 PM'."""
        return t.strftime("%I:%M %p")

    def _ensure_file_exists(self):
        """Ensures the directory and workbook exist with proper headers."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Appointments"

            headers = [
                "Booking ID",
                "Booking Timestamp",
                "Patient Name",
                "Contact Number",
                "Appointment Date",
                "Time Slot",
                "Symptoms / Reason",
                "Symptom Duration",
                "Doctor Name",
                "Status",
                "CallSid"
            ]
            ws.append(headers)

            # Style header row
            header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
            header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
            thin_border = Border(
                left=Side(style="thin", color="D9D9D9"),
                right=Side(style="thin", color="D9D9D9"),
                top=Side(style="thin", color="D9D9D9"),
                bottom=Side(style="thin", color="D9D9D9")
            )

            for col_num, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_num)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = thin_border
                ws.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = max(len(header) + 6, 16)

            wb.save(self.file_path)
            wb.close()

    def _ensure_call_sid_header(self, ws) -> int:
        """
        Ensures 'CallSid' column exists in header row without overwriting existing data.
        Returns the 1-based column index of CallSid.
        """
        header_vals = [cell.value for cell in ws[1]]
        for idx, val in enumerate(header_vals, 1):
            if val and str(val).strip().lower() == "callsid":
                return idx
        
        # Add CallSid header at next available column
        new_col = len(header_vals) + 1
        ws.cell(row=1, column=new_col, value="CallSid")
        return new_col

    def _validate_slot_with_ws(self, ws, date_str: str, time_str: str) -> Tuple[bool, str]:
        """
        Validates complete 30-minute appointment interval against the live workbook instance
        under the file lock.
        """
        if not date_str:
            return False, "Appointment date is required."
        if not time_str:
            return False, "Appointment time slot is required."

        # 1. Parse Date
        try:
            req_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            return False, f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD."

        today_date = datetime.now().date()
        if req_date < today_date:
            return False, f"Cannot book an appointment for a past date ({date_str})."

        # 2. Sunday Closure
        if req_date.weekday() == 6:  # Sunday
            return False, f"The clinic is closed on Sundays ({date_str}). Please choose Monday through Saturday."

        # 3. Parse Time and Check 30-Minute Interval
        req_time = self.parse_time_str(time_str)
        if not req_time:
            return False, f"Invalid time format: '{time_str}'. Please select a valid time like '11:00 AM' or '06:00 PM'."

        slot_delta = timedelta(minutes=self.SLOT_DURATION_MINUTES)
        req_start = datetime(2000, 1, 1, req_time.hour, req_time.minute)
        req_end = req_start + slot_delta

        # Check past time for today's date
        if req_date == today_date:
            now_dt = datetime.now()
            req_full_start = datetime.combine(req_date, req_time)
            if req_full_start < now_dt - timedelta(minutes=5):
                return False, f"Cannot book for a past time ({time_str}) today."

        # Session boundaries
        m_start = datetime(2000, 1, 1, 11, 0)
        m_end = datetime(2000, 1, 1, 14, 0)
        e_start = datetime(2000, 1, 1, 18, 0)
        e_end = datetime(2000, 1, 1, 20, 0)

        in_morning = (req_start >= m_start and req_end <= m_end)
        in_evening = (req_start >= e_start and req_end <= e_end)

        if not (in_morning or in_evening):
            if req_start >= m_end and req_start < e_start:
                return False, "Doctor is unavailable during the afternoon break (02:00 PM to 06:00 PM). Clinic reopens at 06:00 PM."
            return False, "Requested time is outside clinic hours. Working hours are Morning 11:00 AM to 02:00 PM and Evening 06:00 PM to 08:00 PM."

        # 4. Overlap & Double Booking Detection from live worksheet (Bypassable for testing/demos)
        if getattr(settings, "ENABLE_SLOT_CONFLICT_CHECK", False):
            target_date = date_str.strip()
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not any(row):
                    continue

                raw_date = row[4]
                if isinstance(raw_date, (datetime, date)):
                    row_date = raw_date.strftime("%Y-%m-%d")
                else:
                    row_date = str(raw_date or "").strip()

                raw_time = row[5]
                if isinstance(raw_time, time):
                    row_time = raw_time.strftime("%I:%M %p")
                else:
                    row_time = str(raw_time or "").strip()

                status = str(row[9] or "").strip().lower() if len(row) > 9 else "confirmed"

                if row_date == target_date and status != "cancelled":
                    b_time = self.parse_time_str(row_time)
                    if not b_time:
                        continue
                    b_start = datetime(2000, 1, 1, b_time.hour, b_time.minute)
                    b_end = b_start + slot_delta
                    # Overlap condition: (StartA < EndB) and (EndA > StartB)
                    if (req_start < b_end) and (req_end > b_start):
                        return False, f"Time slot {self.format_canonical_time(req_time)} is already booked or overlaps with an existing appointment ({row_time})."

        return True, "Available"

    def validate_slot(self, date_str: str, time_str: str) -> Tuple[bool, str]:
        """External slot validator locking the file for read consistency."""
        self._ensure_file_exists()
        with self.lock:
            wb = openpyxl.load_workbook(self.file_path, data_only=True)
            ws = wb["Appointments"]
            result = self._validate_slot_with_ws(ws, date_str, time_str)
            wb.close()
            return result

    def is_slot_available(self, date_str: str, time_str: str) -> bool:
        """Returns True if the slot passes all schedule and availability checks."""
        valid, _ = self.validate_slot(date_str, time_str)
        return valid

    def get_booked_slots_detailed(self, date_str: str) -> List[Dict[str, str]]:
        """Returns detailed records of all active bookings for a given date."""
        self._ensure_file_exists()
        with self.lock:
            wb = openpyxl.load_workbook(self.file_path, data_only=True)
            ws = wb["Appointments"]

            target_date = date_str.strip()
            booked = []

            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not any(row):
                    continue

                raw_date = row[4]
                if isinstance(raw_date, (datetime, date)):
                    row_date = raw_date.strftime("%Y-%m-%d")
                else:
                    row_date = str(raw_date or "").strip()

                raw_time = row[5]
                if isinstance(raw_time, time):
                    row_time = raw_time.strftime("%I:%M %p")
                else:
                    row_time = str(raw_time or "").strip()

                status = str(row[9] or "").strip().lower() if len(row) > 9 else "confirmed"

                if row_date == target_date and status != "cancelled":
                    booked.append({
                        "booking_id": str(row[0] or ""),
                        "patient_name": str(row[2] or ""),
                        "phone_number": str(row[3] or ""),
                        "date": row_date,
                        "time_slot": row_time,
                        "status": status
                    })

            wb.close()
            return booked

    def get_booked_slots(self, date_str: str) -> List[str]:
        """Returns list of time strings booked for a given date."""
        details = self.get_booked_slots_detailed(date_str)
        return [d["time_slot"] for d in details]

    def get_available_slots(self, date_str: str) -> List[str]:
        """Returns default slots that have zero overlap with existing bookings."""
        available = []
        for slot in self.DEFAULT_SLOTS:
            is_avail, _ = self.validate_slot(date_str, slot)
            if is_avail:
                available.append(slot)
        return available

    def get_smart_alternative_slots(self, date_str: str, requested_time: str = "") -> List[str]:
        """
        Industry-grade alternative slot finder:
        Returns the top 1 or 2 closest available slots to the requested time.
        If requested time is in afternoon break or booked evening, prioritizes closest evening slots.
        """
        avail = self.get_available_slots(date_str)
        if not avail:
            return []

        req_t = self.parse_time_str(requested_time) if requested_time else None
        if not req_t:
            return avail[:2]

        req_minutes = req_t.hour * 60 + req_t.minute

        def proximity(slot_str: str) -> int:
            t = self.parse_time_str(slot_str)
            if not t:
                return 9999
            slot_minutes = t.hour * 60 + t.minute
            diff = slot_minutes - req_minutes
            # Strongly prefer upcoming slots (diff >= 0) over past slots
            if diff >= 0:
                return diff
            return abs(diff) + 300

        sorted_slots = sorted(avail, key=proximity)
        return sorted_slots[:2]

    def format_alternative_slot_prompt(self, reason: str, alternatives: List[str], lang: str = "ENGLISH") -> str:
        """Formats polite conflict explanation with actionable real available slots."""
        if not alternatives:
            if lang == "HINDI_DEVANAGARI":
                return f"माफ़ कीजियेगा, {reason}। इस तारीख पर कोई स्लॉट उपलब्ध नहीं है। कृपया कोई अन्य तारीख बताइये।"
            elif lang == "HINGLISH":
                return f"Maaf kariyega, {reason}। Is date par koi slot available nahi hai. Kripya koi doosri date bataiye."
            return f"I apologize, {reason}. No other slots are available on this date. Could you please select another date?"

        slots_text = " ya ".join(alternatives) if lang in ("HINDI_DEVANAGARI", "HINGLISH") else " or ".join(alternatives)

        if lang == "HINDI_DEVANAGARI":
            return f"माफ़ कीजियेगा, {reason}। इसके बजाय {slots_text} का समय उपलब्ध है। क्या आप इनमें से कोई समय चुनना चाहेंगे?"
        elif lang == "HINGLISH":
            return f"Maaf kariyega, {reason}। Iske bajaye shaam ko {slots_text} available hai. Kya aap isme se koi time prefer karenge?"
        return f"I apologize, {reason}. However, {slots_text} is available today. Would one of those times work for you?"

    def _create_backup(self):
        """Creates a timestamped and stable backup copy of the workbook before writing."""
        try:
            if self.file_path.exists() and self.file_path.stat().st_size > 0:
                backup_file = self.file_path.parent / (self.file_path.stem + "_backup.xlsx")
                shutil.copy2(self.file_path, backup_file)
        except Exception as e:
            logger.warning(f"[SHEET SERVICE] Backup creation failed: {e}")

    def save_appointment(
        self,
        patient_name: str,
        phone_number: str,
        date_str: str,
        time_slot: str,
        symptoms: str,
        duration: str = "Recently started",
        call_sid: str = "",
        raise_on_error: bool = False
    ) -> Dict[str, Any]:
        """
        Atomically saves a new appointment with:
        - Cross-process file locking.
        - Durable CallSid idempotency checked directly in Excel.
        - Atomic validation & append inside the SAME lock window.
        - Formula injection prevention.
        - Pre-write backup.
        """
        # Fast in-memory check
        if call_sid and call_sid in self._call_sid_cache:
            logger.info(f"[SHEET SERVICE] Returning in-memory cached booking for CallSid={call_sid[-6:]}")
            return self._call_sid_cache[call_sid]

        self._ensure_file_exists()

        with self.lock:
            wb = openpyxl.load_workbook(self.file_path)
            ws = wb["Appointments"]

            call_sid_col_idx = self._ensure_call_sid_header(ws)

            # 1. Durable CallSid Check from Excel (survives app restarts)
            if call_sid:
                for r_idx in range(2, ws.max_row + 1):
                    row_sid = ws.cell(row=r_idx, column=call_sid_col_idx).value
                    if row_sid and str(row_sid).strip() == call_sid.strip():
                        logger.info(f"[SHEET SERVICE] Durable CallSid match found in Excel row {r_idx}")
                        existing_record = {
                            "booking_id": str(ws.cell(row=r_idx, column=1).value or ""),
                            "timestamp": str(ws.cell(row=r_idx, column=2).value or ""),
                            "patient_name": str(ws.cell(row=r_idx, column=3).value or "").lstrip("'"),
                            "phone_number": str(ws.cell(row=r_idx, column=4).value or "").lstrip("'"),
                            "date": str(ws.cell(row=r_idx, column=5).value or "").lstrip("'"),
                            "time_slot": str(ws.cell(row=r_idx, column=6).value or "").lstrip("'"),
                            "symptoms": str(ws.cell(row=r_idx, column=7).value or "").lstrip("'"),
                            "duration": str(ws.cell(row=r_idx, column=8).value or "").lstrip("'"),
                            "doctor": str(ws.cell(row=r_idx, column=9).value or "").lstrip("'"),
                            "status": str(ws.cell(row=r_idx, column=10).value or "Confirmed"),
                            "call_sid": call_sid
                        }
                        wb.close()
                        self._call_sid_cache[call_sid] = existing_record
                        return existing_record

            # 2. Strict Schedule & Interval Validation inside the same lock
            valid, reason = self._validate_slot_with_ws(ws, date_str, time_slot)
            if not valid:
                wb.close()
                logger.warning(f"[SHEET SERVICE] Appointment rejected under lock: {reason}")
                if raise_on_error:
                    raise ValueError(reason)
                return {
                    "status": "Failed",
                    "error": reason,
                    "patient_name": patient_name,
                    "date": date_str,
                    "time_slot": time_slot
                }

            # 3. Canonical formatting & Sanitization
            parsed_t = self.parse_time_str(time_slot)
            canonical_slot = self.format_canonical_time(parsed_t) if parsed_t else time_slot.strip()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            booking_id = f"APT-{datetime.now().strftime('%y%m%d%H%M%S')}"

            clean_name = self.sanitize_cell_value(patient_name.strip().title())
            clean_phone = self.sanitize_cell_value(phone_number.strip())
            clean_date = self.sanitize_cell_value(date_str.strip())
            clean_slot = self.sanitize_cell_value(canonical_slot)
            clean_symptoms = self.sanitize_cell_value(symptoms.strip())
            clean_duration = self.sanitize_cell_value(duration.strip())
            clean_doctor = self.sanitize_cell_value(settings.DOCTOR_NAME)
            clean_sid = self.sanitize_cell_value(call_sid.strip()) if call_sid else ""

            row_data = [
                booking_id,
                timestamp,
                clean_name,
                clean_phone,
                clean_date,
                clean_slot,
                clean_symptoms,
                clean_duration,
                clean_doctor,
                "Confirmed",
                clean_sid
            ]

            # Pre-write backup
            self._create_backup()

            ws.append(row_data)

            # Style data row
            row_idx = ws.max_row
            thin_border = Border(
                left=Side(style="thin", color="E0E0E0"),
                right=Side(style="thin", color="E0E0E0"),
                top=Side(style="thin", color="E0E0E0"),
                bottom=Side(style="thin", color="E0E0E0")
            )
            for col_idx in range(1, len(row_data) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.font = Font(name="Arial", size=10)
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")

            # Atomic write via temporary file
            tmp_file = self.file_path.with_suffix(".tmp")
            wb.save(tmp_file)
            wb.close()
            os.replace(tmp_file, self.file_path)

        booking_record = {
            "booking_id": booking_id,
            "timestamp": timestamp,
            "patient_name": clean_name.lstrip("'"),
            "phone_number": clean_phone.lstrip("'"),
            "date": clean_date.lstrip("'"),
            "time_slot": clean_slot.lstrip("'"),
            "symptoms": clean_symptoms.lstrip("'"),
            "duration": clean_duration.lstrip("'"),
            "doctor": clean_doctor.lstrip("'"),
            "status": "Confirmed",
            "call_sid": call_sid
        }

        if call_sid:
            self._call_sid_cache[call_sid] = booking_record

        logger.info(f"[SHEET SERVICE] Successfully booked {booking_id} for {clean_name} at {clean_date} {clean_slot}")
        return booking_record

sheet_service = SheetService()
