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

def parse_spoken_hindi_numbers(text: str) -> Optional[str]:
    """
    Parses spoken Hindi compound numbers or raw digits into a normalized 10-digit mobile number.
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

    # Parse spoken compound words
    words = text.lower().replace("-", " ").split()
    digits_found = []
    for w in words:
        clean_w = "".join(ch for ch in w if ch.isalnum())
        if clean_w.isdigit():
            digits_found.append(clean_w)
        elif clean_w in HINDI_SPOKEN_NUMBERS:
            digits_found.append(HINDI_SPOKEN_NUMBERS[clean_w])

    joined = "".join(digits_found)
    if len(joined) == 10:
        return joined
    if len(joined) >= 10:
        for idx in range(len(joined) - 9):
            candidate = joined[idx:idx+10]
            if candidate[0] in "6789":
                return candidate

    return None

def format_digits_for_readback(phone_number: str) -> str:
    """
    Formats a 10-digit number with spaces between each digit for clear TTS speech:
    e.g. '8824466229' -> '8 8 2 4 4 6 6 2 2 9'
    """
    clean = "".join(ch for ch in phone_number if ch.isdigit())
    return " ".join(clean)
