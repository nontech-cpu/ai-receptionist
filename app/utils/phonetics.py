import re
from typing import Dict, Optional

# Comprehensive phonetic dictionary mapping Roman Hinglish to pure Devanagari script.
# This prevents Polly.Kajal-Neural from applying English phonetic rules (e.g. pronouncing "pet" as household pet animal).
HINGLISH_TO_DEVANAGARI_MAP: Dict[str, str] = {
    # Medical Complaints & Symptoms
    "pet": "पेट",
    "paet": "पेट",
    "dard": "दर्द",
    "drd": "दर्द",
    "bukhar": "बुखार",
    "bukhaar": "बुखार",
    "khansi": "खांसी",
    "khaansi": "खांसी",
    "sardi": "सर्दी",
    "zukham": "जुकाम",
    "jukham": "जुकाम",
    "sir": "सिर",
    "sar": "सिर",
    "aankh": "आँख",
    "ankh": "आँख",
    "aankhon": "आँखों",
    "ankho": "आँखों",
    "aankhe": "आँखें",
    "chashma": "चश्मा",
    "chashme": "चश्मे",
    "gale": "गले",
    "gala": "गला",
    "paon": "पाँव",
    "pair": "पैर",
    "kamar": "कमर",
    "dast": "दस्त",
    "ulti": "उल्टी",
    "chhati": "छाती",
    "jalan": "जलन",
    "kamzori": "कमज़ोरी",
    "takleef": "तकलीफ",
    "dikkat": "दिक्कत",
    "pareshaani": "परेशानी",
    "pareshani": "परेशानी",
    "bimari": "बीमारी",
    "dawa": "दवा",
    "dawai": "दवाई",
    "goli": "गोली",

    # Dates, Times & Numbers
    "kal": "कल",
    "aaj": "आज",
    "parso": "परसों",
    "subah": "सुबह",
    "dopahar": "दोपहर",
    "shaam": "शाम",
    "raat": "रात",
    "baje": "बजे",
    "bje": "बजे",
    "din": "दिन",
    "ghante": "घंटे",
    "hafte": "हफ्ते",
    "mahine": "महीने",
    "somwar": "सोमवार",
    "mangalwar": "मंगलवार",
    "budhwar": "बुधवार",
    "guruwar": "गुरुवार",
    "shukrawar": "शुक्रवार",
    "shaniwar": "शनिवार",
    "raviwar": "रविवार",
    "ek": "एक",
    "do": "दो",
    "teen": "तीन",
    "chaar": "चार",
    "char": "चार",
    "paanch": "पाँच",
    "panch": "पाँच",
    "chhe": "छह",
    "saat": "सात",
    "aath": "आठ",
    "nau": "नौ",
    "das": "दस",
    "gyarah": "ग्यारह",
    "barah": "बारह",

    # Clinic, Politeness & Conversational Openers
    "ji": "जी",
    "bilkul": "बिल्कुल",
    "theek": "ठीक",
    "thik": "ठीक",
    "haan": "हाँ",
    "han": "हाँ",
    "nahi": "नहीं",
    "nahin": "नहीं",
    "namaste": "नमस्ते",
    "shukriya": "शुक्रिया",
    "dhanyawad": "धन्यवाद",
    "doctor": "डॉक्टर",
    "sahab": "साहब",
    "dr": "डॉ.",
    "clinic": "क्लिनिक",
    "hospital": "अस्पताल",
    "fees": "फीस",
    "fee": "फीस",
    "paisa": "पैसे",
    "paise": "पैसे",
    "rupaye": "रुपये",
    "rupees": "रुपये",
    "appointment": "अपॉइंटमेंट",
    "booking": "बुकिंग",
    "checkup": "चेकअप",
    "check": "चेक",
    "karwana": "करवाना",
    "karwani": "करवानी",
    "dikhana": "दिखाना",
    "dikhaana": "दिखाना",
    "milna": "मिलना",
    "aana": "आना",

    # Pronouns, Relations, Grammar & Helpers
    "aur": "और",
    "mein": "में",
    "me": "में",
    "se": "से",
    "ko": "को",
    "ka": "का",
    "ki": "की",
    "ke": "के",
    "hai": "है",
    "hain": "हैं",
    "tha": "था",
    "thi": "थी",
    "hoga": "होगा",
    "hogi": "होगी",
    "ho": "हो",
    "gaya": "गया",
    "gayi": "गई",
    "kya": "क्या",
    "kab": "कब",
    "kahan": "कहाँ",
    "kaha": "कहाँ",
    "kaise": "कैसे",
    "kitna": "कितना",
    "kitne": "कितने",
    "kitni": "कितनी",
    "aap": "आप",
    "aapka": "आपका",
    "aapke": "आपके",
    "aapki": "आपकी",
    "mera": "मेरा",
    "mere": "मेरे",
    "meri": "मेरी",
    "unka": "उनका",
    "unke": "उनके",
    "unki": "उनकी",
    "dost": "दोस्त",
    "bhai": "भाई",
    "naam": "नाम",
    "shubh": "शुभ",
    "number": "नंबर",
    "phone": "फ़ोन",
    "liye": "लिए",
    "chahiye": "चाहिए",
    "kripya": "कृपया",
    "batayein": "बताइए",
    "bataiye": "बताइए",
    "samajh": "समझ",
    "pukka": "पक्का",
    "pakka": "पक्का",
    "rahega": "रहेगा",
    "rahegi": "रहेगी",
    "karen": "करें",
    "kijiye": "कीजिए"
}

# Pre-compiled word boundary regex for microsecond execution
WORD_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in HINGLISH_TO_DEVANAGARI_MAP.keys()) + r')\b',
    flags=re.IGNORECASE
)

# Compound Hindi spoken number dictionary for fast spoken digit recognition
HINDI_SPOKEN_NUMBERS: Dict[str, str] = {
    'shunya': '0', 'zero': '0',
    'ek': '1', 'do': '2', 'teen': '3', 'char': '4', 'chaar': '4', 'paanch': '5', 'panch': '5',
    'chhe': '6', 'che': '6', 'saat': '7', 'sat': '7', 'aath': '8', 'ath': '8', 'nau': '9', 'no': '9',
    'das': '10', 'gyarah': '11', 'barah': '12', 'terah': '13', 'chaudah': '14', 'pandrah': '15',
    'solah': '16', 'satrah': '17', 'atharah': '18', 'unnees': '19', 'bees': '20',
    'ikkees': '21', 'baees': '22', 'teees': '23', 'chaubees': '24', 'chaubis': '24',
    'pachchees': '25', 'chhabbees': '26', 'sattaees': '27', 'atthaees': '28', 'untees': '29', 'untis': '29',
    'tees': '30', 'ikttees': '31', 'battees': '32', 'tentees': '33', 'chautees': '34', 'paintees': '35',
    'chhattees': '36', 'saintees': '37', 'adhtees': '38', 'untalees': '39', 'chaalees': '40',
    'iktaalees': '41', 'bayaalees': '42', 'tentaalees': '43', 'chauwaalees': '44', 'paintaalees': '45',
    'chhiyaalees': '46', 'chhiyalis': '46', 'saintaalees': '47', 'adhtaalees': '48', 'unchaas': '49',
    'pachaas': '50', 'ikkyawan': '51', 'baawan': '52', 'tirpan': '53', 'chauwan': '54',
    'pachpan': '55', 'chhappan': '56', 'sattawan': '57', 'atthaawan': '58', 'unsath': '59',
    'saath': '60', 'iksath': '61', 'baasath': '62', 'basath': '62', 'tirsath': '63', 'chaunsath': '64',
    'painsath': '65', 'chhiyaasath': '66', 'sadsath': '67', 'adhsath': '68', 'unhattar': '69',
    'sattar': '70', 'ikhattar': '71', 'bahattar': '72', 'tihattar': '73', 'chauhattar': '74',
    'pachhattar': '75', 'pachattar': '75', 'chhihattar': '76', 'satattar': '77', 'athattar': '78',
    'uniyasi': '79', 'assi': '80', 'ikyaasi': '81', 'bayaasi': '82', 'tiraasi': '83', 'chauraasi': '84',
    'pachaasi': '85', 'chhiyaasi': '86', 'sattaasi': '87', 'atthaasi': '88', 'atthasi': '88',
    'navaasi': '89', 'nabbe': '90', 'ikyaanwe': '91', 'baanwe': '92', 'tiraanwe': '93', 'chauraanwe': '94',
    'pachaanwe': '95', 'chhiyaanwe': '96', 'sattaanwe': '97', 'atthaanwe': '98', 'ninyaanwe': '99'
}

def parse_spoken_hindi_numbers(text: str) -> Optional[str]:
    """
    Parses fast Hindi spoken compound numbers (e.g. 'atthasi chaubees chhiyalis basath untees')
    or spaced digits ('88 24 46 62 29') into a normalized 10-digit mobile number.
    Returns 10-digit string if found, otherwise None.
    """
    if not text:
        return None

    # First check if there is an explicit 10 digit number already
    clean_digits_only = re.sub(r'[^0-9]', '', text)
    match_direct = re.search(r'\b[6-9]\d{9}\b', clean_digits_only)
    if match_direct:
        return match_direct.group(0)

    # Parse spoken words into digits
    words = re.split(r'[\s\-]+', text.lower())
    digits_found = []
    for w in words:
        w_clean = re.sub(r'[^a-z0-9]', '', w)
        if w_clean.isdigit():
            digits_found.append(w_clean)
        elif w_clean in HINDI_SPOKEN_NUMBERS:
            digits_found.append(HINDI_SPOKEN_NUMBERS[w_clean])

    joined = ''.join(digits_found)
    match_compound = re.search(r'\b[6-9]\d{9}\b', joined)
    if match_compound:
        return match_compound.group(0)
    
    match_any_10 = re.search(r'\d{10}', joined)
    if match_any_10:
        return match_any_10.group(0)

    return None

def format_digits_for_readback(phone_number: str) -> str:
    """
    Formats a 10-digit number with spaces between each digit so TTS reads each digit clearly:
    e.g. '8824466229' -> '8 8 2 4 4 6 6 2 2 9'
    """
    clean = re.sub(r'\D', '', phone_number)
    return ' '.join(list(clean))

def normalize_for_indian_voice(text: str) -> str:
    """
    Normalizes mixed English/Hinglish text into native Devanagari phonetics.
    Eliminates the American/foreign accent completely when spoken by Polly.Kajal-Neural.
    """
    if not text:
        return text

    # Clean redundant slot formats e.g. "10:00 AM baje" -> "10:00 बजे"
    text = re.sub(r'(\d{1,2}):00\s*(?:AM|PM|am|pm)\s*baje', r'\1:00 baje', text)
    text = re.sub(r'(\d{1,2})\s*(?:AM|PM|am|pm)\s*baje', r'\1 baje', text)
    text = re.sub(r'(\d{1,2}):00\s*(?:AM|am)', r'\1:00 बजे', text)
    text = re.sub(r'(\d{1,2}):00\s*(?:PM|pm)', r'\1:00 बजे', text)

    # Convert common Roman Hinglish words to Devanagari
    def _replacer(match: re.Match) -> str:
        word = match.group(0).lower()
        return HINGLISH_TO_DEVANAGARI_MAP.get(word, match.group(0))

    converted = WORD_PATTERN.sub(_replacer, text)

    # Clean double spaces
    converted = re.sub(r'\s+', ' ', converted).strip()
    return converted
