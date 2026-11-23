import os
from pathlib import Path
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from app.core.config import settings

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

    def __init__(self, file_path: Path = None):
        self.file_path = file_path or settings.EXCEL_FILE_PATH
        self._ensure_file_exists()

    def _ensure_file_exists(self):
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
                "Status"
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

    @staticmethod
    def is_time_within_doctor_hours(time_str: str) -> bool:
        """Validates if any requested time string falls within Doctor's active sessions (11 AM-2 PM or 6 PM-8 PM)."""
        time_str = time_str.strip().upper()
        for fmt in ["%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I%p"]:
            try:
                parsed = datetime.strptime(time_str, fmt).time()
                m_start = datetime.strptime("11:00 AM", "%I:%M %p").time()
                m_end = datetime.strptime("02:00 PM", "%I:%M %p").time()
                e_start = datetime.strptime("06:00 PM", "%I:%M %p").time()
                e_end = datetime.strptime("08:00 PM", "%I:%M %p").time()
                return (m_start <= parsed <= m_end) or (e_start <= parsed <= e_end)
            except ValueError:
                continue
        return True

    def is_slot_available(self, date_str: str, time_str: str) -> bool:
        """Returns True if the time is within doctor working hours and not already booked."""
        if not self.is_time_within_doctor_hours(time_str):
            return False
        booked = self.get_booked_slots(date_str)
        return time_str.strip() not in booked

    def get_booked_slots(self, date_str: str) -> list[str]:
        """Returns list of time slots already booked for a given date (YYYY-MM-DD)."""
        self._ensure_file_exists()
        wb = openpyxl.load_workbook(self.file_path, data_only=True)
        ws = wb["Appointments"]

        booked = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 6:
                continue
            row_date = str(row[4]).strip() if row[4] else ""
            row_time = str(row[5]).strip() if row[5] else ""
            status = str(row[9]).strip().lower() if len(row) > 9 and row[9] else ""

            if row_date == date_str.strip() and status != "cancelled":
                booked.append(row_time)

        wb.close()
        return booked

    def get_available_slots(self, date_str: str) -> list[str]:
        booked = self.get_booked_slots(date_str)
        available = [slot for slot in self.DEFAULT_SLOTS if slot not in booked]
        return available

    def save_appointment(
        self,
        patient_name: str,
        phone_number: str,
        date_str: str,
        time_slot: str,
        symptoms: str,
        duration: str = "Recently started"
    ) -> dict:
        """Appends a new confirmed clinical appointment with symptoms and duration."""
        self._ensure_file_exists()
        wb = openpyxl.load_workbook(self.file_path)
        ws = wb["Appointments"]

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        booking_id = f"APT-{datetime.now().strftime('%y%m%d%H%M%S')}"

        row_data = [
            booking_id,
            timestamp,
            patient_name.title(),
            phone_number,
            date_str,
            time_slot,
            symptoms,
            duration,
            settings.DOCTOR_NAME,
            "Confirmed"
        ]
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

        wb.save(self.file_path)
        wb.close()

        return {
            "booking_id": booking_id,
            "timestamp": timestamp,
            "patient_name": patient_name.title(),
            "phone_number": phone_number,
            "date": date_str,
            "time_slot": time_slot,
            "symptoms": symptoms,
            "duration": duration,
            "doctor": settings.DOCTOR_NAME,
            "status": "Confirmed"
        }

sheet_service = SheetService()
