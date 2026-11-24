import re
from typing import Dict, Optional

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

ENGLISH_SPOKEN_NUMBERS: Dict[str, str] = {
    'zero': '0', 'oh': '0',
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13', 'fourteen': '14',
    'fifteen': '15', 'sixteen': '16', 'seventeen': '17', 'eighteen': '18', 'nineteen': '19',
    'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50',
    'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'
}

def parse_spoken_hindi_numbers(text: str) -> Optional[str]:
    """
    Parses spoken Hindi & English compound numbers, word digits, or raw digits
    into a normalized 10-digit mobile number.
    Returns 10-digit string if found, otherwise None.
    """
    if not text:
        return None

    # First check if there is an explicit 10 digit number already
    raw_digits = "".join(ch for ch in text if ch.isdigit())
    if len(raw_digits) == 10 and raw_digits[0] in "6789":
        return raw_digits
    if len(raw_digits) >= 10:
        for idx in range(len(raw_digits) - 9):
            candidate = raw_digits[idx:idx+10]
            if candidate[0] in "6789":
                return candidate

    # Parse spoken words (Hindi + English)
    words = text.lower().replace("-", " ").replace(".", " ").split()
    digits_found = []
    i = 0
    while i < len(words):
        w = words[i]
        clean_w = "".join(ch for ch in w if ch.isalnum())
        
        # Handle "double" and "triple" (e.g. "double eight" -> "88")
        if clean_w in ("double", "triple") and i + 1 < len(words):
            next_w = "".join(ch for ch in words[i+1] if ch.isalnum())
            digit_val = None
            if next_w.isdigit():
                digit_val = next_w
            elif next_w in ENGLISH_SPOKEN_NUMBERS:
                digit_val = ENGLISH_SPOKEN_NUMBERS[next_w]
            elif next_w in HINDI_SPOKEN_NUMBERS:
                digit_val = HINDI_SPOKEN_NUMBERS[next_w]
            
            if digit_val:
                repeat_count = 2 if clean_w == "double" else 3
                digits_found.append(digit_val * repeat_count)
                i += 2
                continue

        if clean_w.isdigit():
            digits_found.append(clean_w)
        elif clean_w in ENGLISH_SPOKEN_NUMBERS:
            digits_found.append(ENGLISH_SPOKEN_NUMBERS[clean_w])
        elif clean_w in HINDI_SPOKEN_NUMBERS:
            digits_found.append(HINDI_SPOKEN_NUMBERS[clean_w])
        i += 1

    joined = "".join(digits_found)
    if len(joined) == 10 and joined[0] in "6789":
        return joined
    if len(joined) >= 10:
        for idx in range(len(joined) - 9):
            candidate = joined[idx:idx+10]
            if candidate[0] in "6789":
                return candidate

    return None

parse_spoken_phone_number = parse_spoken_hindi_numbers

def format_digits_for_readback(phone_number: str) -> str:
    """
    Formats a 10-digit number with spaces between each digit for clear TTS speech:
    e.g. '9876543210' -> '9 8 7 6 5 4 3 2 1 0'
    """
    clean = "".join(ch for ch in phone_number if ch.isdigit())
    return " ".join(clean)
