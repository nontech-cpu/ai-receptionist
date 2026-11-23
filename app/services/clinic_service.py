import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "clinic_profile.json"

class LocationInfo(BaseModel):
    address: str = "Main Market Road"
    landmark: str = "Opposite City Hospital, 2nd Floor"
    city: str = "Jaipur"
    state: str = "Rajasthan"
    pincode: str = "302001"
    directions_summary: str = "Main Market Road par, City Hospital ke theek samne, doosri manzil par"

class TimingsInfo(BaseModel):
    days_summary: str = "Monday to Saturday"
    working_hours: str = "10:00 AM to 07:00 PM"
    morning_slot: str = "10:00 AM to 02:00 PM"
    evening_slot: str = "04:00 PM to 07:00 PM"
    closed_days: List[str] = ["Sunday"]
    slot_duration_minutes: int = 30

class PricingInfo(BaseModel):
    consultation_fee: int = 500
    followup_fee: int = 300
    currency: str = "INR"
    currency_symbol: str = "₹"
    payment_modes: str = "Cash and UPI"

class EmergencyInfo(BaseModel):
    helpline: str = "108"
    emergency_protocol: str = "Ye emergency lag rahi hai, please turant nearest hospital emergency room ya 108 par call karein."

class ClinicProfile(BaseModel):
    clinic_id: str = "clinic_sharma_01"
    clinic_name: str = "Dr. Sharma Care Clinic"
    doctor_name: str = "Dr. Sharma"
    doctor_qualification: str = "MBBS, MD"
    specialization: str = "General Medicine, Eye Care & Diabetes"
    receptionist_name: str = "Maya"
    location: LocationInfo = Field(default_factory=LocationInfo)
    timings: TimingsInfo = Field(default_factory=TimingsInfo)
    pricing: PricingInfo = Field(default_factory=PricingInfo)
    emergency: EmergencyInfo = Field(default_factory=EmergencyInfo)
    faqs: Dict[str, str] = Field(default_factory=dict)

class ClinicService:
    def __init__(self, profile_file: Path = PROFILE_PATH):
        self.profile_file = profile_file
        self._profile: Optional[ClinicProfile] = None
        self.load_profile()

    def load_profile(self) -> ClinicProfile:
        if self.profile_file.exists():
            try:
                with open(self.profile_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._profile = ClinicProfile(**data)
                logger.info(f"[CLINIC SERVICE] Loaded profile: {self._profile.clinic_name} ({self._profile.doctor_name})")
            except Exception as e:
                logger.error(f"[CLINIC SERVICE] Error reading profile: {e}, using default fallback.")
                self._profile = ClinicProfile()
        else:
            self._profile = ClinicProfile()
            self.save_profile()
        return self._profile

    @property
    def profile(self) -> ClinicProfile:
        if self._profile is None:
            self.load_profile()
        return self._profile

    def save_profile(self) -> None:
        self.profile_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.profile_file, "w", encoding="utf-8") as f:
            f.write(self.profile.model_dump_json(indent=2))
        logger.info(f"[CLINIC SERVICE] Persisted profile to {self.profile_file}")

    def update_profile(self, updates: Dict[str, Any]) -> ClinicProfile:
        curr_dict = self.profile.model_dump()
        for k, v in updates.items():
            if isinstance(v, dict) and k in curr_dict and isinstance(curr_dict[k], dict):
                curr_dict[k].update(v)
            else:
                curr_dict[k] = v
        self._profile = ClinicProfile(**curr_dict)
        self.save_profile()
        return self._profile

    def get_address_response(self, lang: str = "HINDI") -> str:
        loc = self.profile.location
        if lang == "HINDI":
            return f"जी, क्लिनिक {loc.address} पर, {loc.landmark} में स्थित है। क्या और कोई सवाल है?"
        return f"The clinic is located at {loc.address}, {loc.landmark}, {loc.city}."

    def get_fees_response(self, lang: str = "HINDI") -> str:
        fee = self.profile.pricing
        doc = self.profile.doctor_name
        if lang == "HINDI":
            return f"{doc} की कंसल्टेशन फीस {fee.consultation_fee} रुपये है, जो क्लिनिक पर देनी होती है। क्या और कोई सवाल है?"
        return f"The consultation fee for {doc} is {fee.currency_symbol}{fee.consultation_fee}."

    def get_timings_response(self, lang: str = "HINDI") -> str:
        timings = self.profile.timings
        doc = self.profile.doctor_name
        if lang == "HINDI":
            return f"{doc} {timings.days_summary} सुबह {timings.working_hours} तक क्लिनिक पर बैठते हैं। क्या और कोई सवाल है?"
        return f"{doc} is available {timings.days_summary} from {timings.working_hours}."

    def check_faq(self, user_speech: str) -> Optional[str]:
        lower = user_speech.lower()
        for key, answer in self.profile.faqs.items():
            if key in lower:
                return answer
        return None

clinic_service = ClinicService()
