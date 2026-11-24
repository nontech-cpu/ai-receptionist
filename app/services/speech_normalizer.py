import re
import math
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# 1. Comprehensive Clinic Domain Vocabulary
CLINICAL_SYMPTOMS_CATALOG = {
    "cough": {
        "canonical": "Cough",
        "variants": ["cough", "khansi", "khasi", "cuff", "koff", "सूखी खांसी", "खांसी"],
        "acoustic_confusions": ["amazing cup", "a cup", "nice cup", "coffee", "cop", "coff", "coughing"]
    },
    "cold": {
        "canonical": "Cold",
        "variants": ["cold", "sardi", "jukam", "zukaam", "runny nose", "सर्दी", "जुकाम"],
        "acoustic_confusions": ["code", "called", "coal", "cord", "hold"]
    },
    "fever": {
        "canonical": "Fever",
        "variants": ["fever", "bukhar", "bukhaar", "tap", "high temperature", "बुखार", "ताप"],
        "acoustic_confusions": ["fewer", "flavor", "favor", "river", "beaver"]
    },
    "headache": {
        "canonical": "Headache",
        "variants": ["headache", "sir dard", "sar dard", "head ache", "माइग्रेन", "सिर दर्द"],
        "acoustic_confusions": ["head egg", "had ache", "head deck"]
    },
    "sore throat": {
        "canonical": "Sore throat",
        "variants": ["sore throat", "gala kharab", "gale me dard", "gale mein kharash", "गले में दर्द"],
        "acoustic_confusions": ["saw throat", "show throat", "so throat", "sour throat"]
    },
    "chest pain": {
        "canonical": "Chest pain",
        "variants": ["chest pain", "seene me dard", "chhati me dard", "सीने में दर्द"],
        "acoustic_confusions": ["just pain", "chest pen", "chased pain", "chess pain"]
    },
    "stomach pain": {
        "canonical": "Stomach pain",
        "variants": ["stomach pain", "pet dard", "pet me dard", "stomach ache", "पेट दर्द"],
        "acoustic_confusions": ["paint dard", "pet dark", "star match pain"]
    },
    "vomiting": {
        "canonical": "Vomiting",
        "variants": ["vomiting", "ulti", "vomit", "उल्टी"],
        "acoustic_confusions": ["ult", "warming", "all tea"]
    },
    "nausea": {
        "canonical": "Nausea",
        "variants": ["nausea", "ji ghabrana", "ghabrahat", "उबकाई", "जी मिचलाना"],
        "acoustic_confusions": ["nose yeah", "gnaws yeah"]
    },
    "body pain": {
        "canonical": "Body pain",
        "variants": ["body pain", "badan dard", "body ache", "बदन दर्द"],
        "acoustic_confusions": ["bad and dard", "bad and hard", "body pen"]
    },
    "allergy": {
        "canonical": "Allergy",
        "variants": ["allergy", "chheenk", "chheekein", "rash", "एलर्जी"],
        "acoustic_confusions": ["a large e", "allergies"]
    },
    "breathing difficulty": {
        "canonical": "Breathing difficulty",
        "variants": ["breathing difficulty", "saans lene me takleef", "shortness of breath", "saans phoolna", "सांस लेने में तकलीफ"],
        "acoustic_confusions": ["briefing difficulty", "bleeding difficulty"]
    },
    "weakness": {
        "canonical": "Weakness",
        "variants": ["weakness", "kamzori", "kamzori lag rahi", "tiredness", "fatigue", "कमजोरी"],
        "acoustic_confusions": ["weekness", "weak ness", "come sorry"]
    }
}

# Common Names & Brand Confusion Catalog
NAME_AND_BRAND_CONFUSIONS = {
    "nice cafe": {
        "suggested": "Nescafé",
        "category": "brand_or_unusual_name",
        "prompt_en": "Just to clarify, did you say your name is Nescafé, or are you calling about something else?",
        "prompt_hi": "कृपया स्पष्ट करें, क्या आपका नाम नेस्कैफे (Nescafé) है, या आप किसी अन्य विषय में कॉल कर रहे हैं?",
        "prompt_hinglish": "Kripya clarify karein, kya aapka naam Nescafé hai, ya aap kisi aur vishay mein call kar rahe hain?"
    },
    "nescafe": {
        "suggested": "Nescafé",
        "category": "brand_or_unusual_name",
        "prompt_en": "Just to confirm, is Nescafé the patient's full name, or are you calling about something else?",
        "prompt_hi": "कृपया पुष्टि करें, क्या नेस्कैफे (Nescafé) मरीज़ का नाम है, या कुछ और?",
        "prompt_hinglish": "Kripya confirm karein, kya Nescafé patient ka naam hai, ya kuch aur?"
    }
}

# Potentially Urgent Clinical Emergency Red Flags
EMERGENCY_RED_FLAGS = [
    "chest pain", "seene me dard", "heart attack", "chhati me dard",
    "severe breathing difficulty", "saans nahi aa rahi", "breathless",
    "unconscious", "behoshi", "heavy bleeding", "stroke", "paralysis",
    "choking", "seene mein jalan aur dard"
]

class SpeechNormalizer:
    """
    Context-aware speech understanding and phonetic ambiguity resolver.
    - Flags acoustic confusion without silently rewriting uncertain speech.
    - Detects medical emergencies for immediate clinical escalation.
    - Computes WER/CER for speech recognition regression evaluation.
    """

    @staticmethod
    def parse_stt_confidence(val: Any) -> Optional[float]:
        """
        Safely parses and validates Twilio Speech Recognition Confidence score.
        - Never defaults missing confidence to 1.0.
        - Missing or empty values return None (unknown).
        - Rejects NaN, infinity, negative numbers, and values > 1.0.
        - Returns float in [0.0, 1.0] rounded to 4 decimal places.
        """
        if val is None:
            return None
        if isinstance(val, str):
            val = val.strip()
            if not val or val.lower() in ("none", "null", "undefined"):
                return None
        try:
            c = float(val)
            if math.isnan(c) or math.isinf(c):
                return None
            if 0.0 <= c <= 1.0:
                return round(c, 4)
            return None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def check_emergency_symptom(text: str, lang: str = "ENGLISH") -> Tuple[bool, Optional[str]]:
        """
        Detects urgent symptoms and returns immediate clinical emergency guidance
        rather than delaying care through booking questions.
        """
        if not text:
            return False, None

        clean = text.lower().strip()
        for flag in EMERGENCY_RED_FLAGS:
            if flag in clean:
                logger.warning(f"[SAFETY ALERT] Emergency red-flag detected: '{flag}' in caller speech: '{text}'")
                if lang == "HINDI_DEVANAGARI":
                    msg = (
                        "यह एक आपातकालीन स्थिति लग रही है। क्लिनिक में सामान्य अपॉइंटमेंट के बजाय, "
                        "कृपया तुरंत 108 पर कॉल करें या नजदीकी अस्पताल के इमरजेंसी विभाग में जाएं।"
                    )
                elif lang == "HINGLISH":
                    msg = (
                        "Yeh ek emergency sthiti lag rahi hai. Clinic appointment ke bajaye, "
                        "kripya turant 108 par call karein ya nearest hospital emergency room mein jayein."
                    )
                else:
                    msg = (
                        "This sounds like a medical emergency. Rather than scheduling a routine appointment, "
                        "please immediately call 108 or proceed to the nearest hospital emergency department."
                    )
                return True, msg

        return False, None

    @classmethod
    def detect_acoustic_confusion(cls, text: str, confidence: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Detects known telephony acoustic misrecognitions (e.g. 'amazing cup' -> 'cough', 'nice cafe' -> 'Nescafé').
        Returns clarification metadata so system can ask for confirmation instead of silently rewriting.
        """
        if not text:
            return None

        clean = text.lower().strip()
        words = set(re.findall(r'\b[a-zA-Z\']+\b', clean))

        # 1. Check Name / Brand Confusions
        for pattern, data in NAME_AND_BRAND_CONFUSIONS.items():
            pattern_regex = r'(?:\b|^)' + re.escape(pattern) + r'(?:\b|$)'
            if re.search(pattern_regex, clean):
                return {
                    "raw_phrase": pattern,
                    "suggested_value": data["suggested"],
                    "category": data["category"],
                    "target_slot": "patient_name",
                    "prompts": {
                        "ENGLISH": data["prompt_en"],
                        "HINDI_DEVANAGARI": data["prompt_hi"],
                        "HINGLISH": data["prompt_hinglish"]
                    },
                    "confidence": confidence
                }

        # 2. Check Clinical Symptoms Acoustic Confusions
        for symptom_key, info in CLINICAL_SYMPTOMS_CATALOG.items():
            confusions = info.get("acoustic_confusions", [])
            for c_phrase in confusions:
                # Match full phrase or word with boundaries
                c_regex = r'(?:\b|^)' + re.escape(c_phrase) + r'(?:\b|$)'
                if re.search(c_regex, clean):
                    canonical = info["canonical"]
                    prompt_en = f"Just to confirm, did you mean you have a {canonical.lower()}?"
                    prompt_hi = f"कृपया पुष्टि करें, क्या आपका मतलब {info['variants'][1]} ({canonical.lower()}) से है?"
                    prompt_hinglish = f"Kripya confirm karein, kya aapka matlab {canonical.lower()} ({info['variants'][1]}) se hai?"
                    return {
                        "raw_phrase": c_phrase,
                        "suggested_value": canonical,
                        "category": "symptom",
                        "target_slot": "symptoms",
                        "prompts": {
                            "ENGLISH": prompt_en,
                            "HINDI_DEVANAGARI": prompt_hi,
                            "HINGLISH": prompt_hinglish
                        },
                        "confidence": confidence
                    }

        return None

    @classmethod
    def match_known_symptom(cls, text: str) -> Optional[str]:
        """Matches raw text directly against known clinical symptoms catalog."""
        if not text:
            return None
        clean = text.lower().strip()
        for symptom_key, info in CLINICAL_SYMPTOMS_CATALOG.items():
            for v in info["variants"]:
                v_regex = r'(?:\b|^)' + re.escape(v) + r'(?:\b|$)'
                if re.search(v_regex, clean):
                    return info["canonical"]
        return None

    # --- Benchmark Evaluation Metrics (WER & CER) ---
    @staticmethod
    def calculate_levenshtein_distance(seq1: List[str], seq2: List[str]) -> int:
        """Dynamic programming Levenshtein edit distance between two sequences."""
        m, n = len(seq1), len(seq2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        for i in range(m + 1):
            dp[i][0] = i
        for j in range(n + 1):
            dp[0][j] = j
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i - 1] == seq2[j - 1]:
                    dp[i][j] = dp[i - 1][j - 1]
                else:
                    dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
        return dp[m][n]

    @classmethod
    def calculate_wer(cls, reference: str, hypothesis: str) -> float:
        """Calculates Word Error Rate (WER = EditDistance / RefLength)."""
        ref_words = reference.lower().strip().split()
        hyp_words = hypothesis.lower().strip().split()
        if not ref_words:
            return 0.0 if not hyp_words else 1.0
        distance = cls.calculate_levenshtein_distance(ref_words, hyp_words)
        return distance / len(ref_words)

    @classmethod
    def calculate_cer(cls, reference: str, hypothesis: str) -> float:
        """Calculates Character Error Rate (CER = CharEditDistance / RefCharLength)."""
        ref_chars = list(reference.lower().replace(" ", ""))
        hyp_chars = list(hypothesis.lower().replace(" ", ""))
        if not ref_chars:
            return 0.0 if not hyp_chars else 1.0
        distance = cls.calculate_levenshtein_distance(ref_chars, hyp_chars)
        return distance / len(ref_chars)

speech_normalizer = SpeechNormalizer()
