"""Deterministic conversion of spoken form answers into field values. Ambiguity is rejected, never guessed.

Prisma writes every language in Latin letters, and typed answers may be in native script, so every word list
has romanised spellings and native-script forms. A rejected answer raises InvalidValue whose only argument is a
reason key (see validate.REASONS); the rejected personal value never appears in an exception or a log.
"""

import re
import unicodedata
from datetime import date

from bolos.forms import indian
from bolos.forms.fields import Field
from bolos.responses import is_yes


class InvalidValue(ValueError):
    """Only a reason key, never the rejected personal value."""

    @property
    def reason(self) -> str:
        return str(self.args[0]) if self.args else "format"


_DIGIT_GROUPS = (
    "zero oh shunya sunya shunyo shunno sunna sonne sonnay poojyam pujyam poojiyam sifar sifr "
    "शून्य ಸೊನ್ನೆ பூஜ்யம் பூஜ்ஜியம் సున్నా പൂജ്യം শূন্য શૂન્ય ਸਿਫਰ ਸਿਫ਼ਰ ज़ीरो जीरो",
    "one ek एक ondu ಒಂದು ondru onnu ஒன்று okati okkati ఒకటి ഒന്ന് এক એક ikk ik ਇੱਕ",
    "two do दो don दोन eradu yeradu ಎರಡು rendu irandu இரண்டு రెండు randu രണ്ട് dui দুই be બે ਦੋ",
    "three teen tin तीन muru mooru ಮೂರು moonru moonu மூன்று moodu మూడు moonnu മൂന്ന് তিন tran ત્રણ tinn ਤਿੰਨ",
    "four chaar char चार naalku nalku ನಾಲ್ಕು naalu nalu நான்கு nalugu naalugu నాలుగు നാല് চার ચાર ਚਾਰ",
    "five paanch panch पाँच पांच paach pach पाच aidu ಐದು aindhu anju ஐந்து ఐదు anchu അഞ്ച് পাঁচ પાંચ panj ਪੰਜ",
    "six chhe chhah chah cheh छह छः saha सहा aaru ಆರು ஆறு ఆరు ആറ് chhoy chhoi ছয় chha છ ਛੇ",
    "seven saat sat सात elu ಏಳು ezhu yezhu ஏழு edu yedu ఏడు ഏഴ് সাত સાત satt ਸੱਤ",
    "eight aath ath आठ entu yentu ಎಂಟು ettu எட்டு enimidi ఎనిమిది എട്ട് aat আট આઠ atth ਅੱਠ",
    "nine nau नौ nauu nav नऊ ombattu ಒಂಬತ್ತು onbadhu onbathu ombodhu ஒன்பது tommidi తొమ్మిది onpathu ഒമ്പത് "
    "noy nay নয় નવ ਨੌਂ ਨੌ",
)
_DIGITS = {word: str(i) for i, words in enumerate(_DIGIT_GROUPS) for word in words.casefold().split()}
# "double five" -> 55, "triple zero" -> 000
_REPEAT = {w: 2 for w in "double dabal dubal dubble dabbal डबल ಡಬಲ್ டபுள் డబుల్ ഡബിൾ ডাবল ડબલ ਡਬਲ".split()}
_REPEAT |= {w: 3 for w in "triple tripal tribble ट्रिपल ಟ್ರಿಪಲ್ ட்ரிபிள் ట్రిపుల్ ട്രിപ്പിൾ ট্রিপল ટ્રિપલ ਟ੍ਰਿਪਲ".split()}
_MULTIPLIERS = {
    **dict.fromkeys("hundred sau sow सौ nooru ನೂರು nooru நூறு vanda వంద nooru നൂറ് sho শো so સો ਸੌ".split(), 100),
    **dict.fromkeys("thousand hazaar hazar hajar hajaar saavira savira ayiram aayiram veyyi vela "
                    "हज़ार हजार ಸಾವಿರ ஆயிரம் వెయ్యి ആയിരം হাজার હજાર ਹਜ਼ਾਰ".split(), 1000),
    **dict.fromkeys("lakh lac lakhs laakh laksha latcham laksham लाख ಲಕ್ಷ லட்சம் లక్ష ലക്ഷം লাখ લાખ ਲੱਖ".split(),
                    100000),
    **dict.fromkeys("crore crores karod karor koti கோடி करोड़ करोड ಕೋಟಿ కోటి കോടി কোটি કરોડ ਕਰੋੜ".split(), 10000000),
}
_AMOUNT_FILLERS = set("rs rupees rupee rupaye rupaiye rupaya rupay rupayi rupai रुपये रुपए ರೂಪಾಯಿ ரூபாய் "
                      "రూపాయలు രൂപ টাকা રૂપિયા ਰੁਪਏ per year annual saal salana".split())
_NO_ANSWERS = {"no", "nahi", "nahin", "नहीं", "नही", "beda", "illa", "ಬೇಡ", "ಇಲ್ಲ", "illai", "vendam",
               "இல்லை", "வேண்டாம்", "ledu", "kaadu", "లేదు", "కాదు", "nako", "नको", "नाही", "അല്ല",
               "ഇല്ല", "না", "নয়", "ના", "નહીં", "ਨਹੀਂ"}
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,.\-/]+", unicodedata.normalize("NFC", text).casefold().strip()) if w]


def _number_word(word: str) -> str | None:
    if word in _DIGITS:
        return _DIGITS[word]
    try:
        return "".join(str(unicodedata.decimal(c)) for c in word)
    except (TypeError, ValueError):
        return None


def digits(text: str) -> str:
    """Spoken or typed digits in any of the 10 languages -> '0123...'. Any other word rejects the answer."""
    result, repeat = [], 1
    for word in _words(text):
        if word in _REPEAT:
            repeat = _REPEAT[word]
            continue
        value = _number_word(word)
        if value is None:
            raise InvalidValue("digits")
        # "double five" repeats the next single digit; "double 55" is not a thing people say.
        result.append(value * repeat if len(value) == 1 else value)
        repeat = 1
    if repeat != 1:
        raise InvalidValue("digits")
    return "".join(result)


def amount(text: str) -> str:
    """'2 lakh 50 hazaar', 'do lakh', '150000 rupees' -> '250000' style whole rupees."""
    total, current = 0, None
    for word in _words(text.replace(",", "")):
        if word in _AMOUNT_FILLERS:
            continue
        if word in _MULTIPLIERS:
            if current is None:
                raise InvalidValue("amount")
            total += current * _MULTIPLIERS[word]
            current = None
            continue
        value = _number_word(word)
        if value is None:
            raise InvalidValue("amount")
        current = int(f"{current if current is not None else ''}{value}")
    total += current or 0
    if total <= 0:
        raise InvalidValue("amount")
    return str(total)


def valid_aadhaar(value: str) -> bool:
    if not re.fullmatch(r"[2-9][0-9]{11}", value):
        return False
    checksum = 0
    for i, n in enumerate(reversed(value)):
        checksum = _D[checksum][_P[i % 8][int(n)]]
    return checksum == 0


# Letter names as people spell codes aloud ("A for apple", "double A", "ess bee eye en").
_LETTER_NAMES = {
    "a": "a ay ae ए ಎ எ ఎ എ এ એ ਏ", "b": "b bee bi बी ಬಿ பி బి ബി বি બી ਬੀ", "c": "c see cee si सी ಸಿ சி సి സി সি સી ਸੀ",
    "d": "d dee di डी ಡಿ டி డి ഡി ডি ડી ਡੀ", "e": "e ee ई ಇ ஈ ఇ ഇ ই ઈ ਈ", "f": "f ef eff एफ ಎಫ್ எஃப் ఎఫ్ എഫ് এফ એફ ਐਫ",
    "g": "g gee jee ji जी ಜಿ ஜி జి ജി জি જી ਜੀ", "h": "h aitch ech etch haitch एच ಎಚ್ ஹெச் హెచ్ എച്ച് এইচ એચ ਐਚ",
    "i": "i eye ai आई ಐ ஐ ఐ ഐ আই આઈ ਆਈ", "j": "j jay je जे ಜೆ ஜே జే ജെ জে જે ਜੇ", "k": "k kay ke के ಕೆ கே కే കെ কে કે ਕੇ",
    "l": "l el ell एल ಎಲ್ எல் ఎల్ എൽ এল એલ ਐਲ", "m": "m em एम ಎಮ್ ಎಂ எம் ఎమ్ ఎం എം এম એમ ਐਮ",
    "n": "n en एन ಎನ್ என் ఎన్ എൻ এন એન ਐਨ", "o": "ओ ಓ ஓ ఓ ഓ ও ઓ ਓ", "p": "p pee pi पी ಪಿ பி పి പി পি પી ਪੀ",
    "q": "q queue cue kyu क्यू ಕ್ಯೂ க்யூ క్యూ ക്യൂ কিউ ક્યૂ ਕਿਊ", "r": "r ar are aar आर ಆರ್ ஆர் ఆర్ ആർ আর આર ਆਰ",
    "s": "s es ess एस ಎಸ್ எஸ் ఎస్ എസ് এস એસ ਐਸ", "t": "t tee ti टी ಟಿ டி టి ടി টি ટી ਟੀ",
    "u": "u you yu यू ಯು யூ యు യു ইউ યુ ਯੂ", "v": "v vee vi वी ವಿ வி వి വി ভি વી ਵੀ",
    "w": "w doubleu डब्ल्यू ಡಬ್ಲ್ಯೂ டபிள்யூ డబ్ల్యూ ഡബ്ല്യു ডাব্লিউ ડબલ્યુ ਡਬਲਯੂ",
    "x": "x ex eks एक्स ಎಕ್ಸ್ எக்ஸ் ఎక్స్ എക്സ് এক্স એક્સ ਐਕਸ", "y": "y why wai वाई ವೈ வை వై വൈ ওয়াই વાય ਵਾਈ",
    "z": "z zed zee jed ज़ेड जेड ಜೆಡ್ ஜெட் జెడ్ സെഡ് জেড ઝેડ ਜ਼ੈੱਡ",
}
_LETTERS = {name: letter.upper() for letter, names in _LETTER_NAMES.items() for name in names.casefold().split()}
_O = "\x00"  # 'oh' / 'o' is a letter or a zero: decided by the code's pattern
_FOR = {"for", "फॉर", "फ़ॉर", "ಫಾರ್", "ஃபார்", "ఫర్", "ഫോർ", "ফর", "ફોર", "ਫਾਰ", "se", "jaise"}


def spelled(text: str) -> str:
    """Letters and digits spelled aloud -> 'ABCDE1234F'. 'oh'/'o' stays ambiguous (\\x00) for the caller."""
    words = _words(text)
    out, repeat, i = [], 1, 0
    while i < len(words):
        word = words[i]
        if word in _REPEAT:
            if i + 1 < len(words) and words[i + 1] in {"u", "you", "yu"}:
                out.append("W")  # 'double u' is the letter W
                i += 2
                continue
            repeat = _REPEAT[word]
            i += 1
            continue
        if word in {"o", "oh"}:
            piece = _O
        elif word in _LETTERS:
            piece = _LETTERS[word]
        elif (number := _number_word(word)) is not None:
            piece = number
        elif re.fullmatch(r"[a-z0-9]+", word):
            piece = word.upper()
        else:
            raise InvalidValue("letters")
        if len(piece) == 1 and i + 1 < len(words) and words[i + 1] in _FOR:
            i += 2  # 'A for Apple': skip 'for' and the example word
        out.append(piece * repeat if len(piece) == 1 else piece)
        repeat = 1
        i += 1
    return "".join(out)


def _resolve_o(value: str, letter_slots: set[int]) -> str:
    return "".join(("O" if i in letter_slots else "0") if c == _O else c for i, c in enumerate(value))


_EMAIL_DIGITS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine".split())}
_EMAIL_WORDS = {
    "@": ("at the rate", "at the rate of", "attherate", "at rate", "at", "एट द रेट", "ऐट द रेट", "एट", "ऐट", "ಅಟ್",
          "ಅಟ್ ದ ರೇಟ್", "அட்", "எட்", "ఎట్", "ఎట్ ది రేట్", "അറ്റ്", "অ্যাট", "এট", "એટ", "ਐਟ"),
    ".": ("dot", "point", "डॉट", "डाट", "ಡಾಟ್", "டாட்", "డాట్", "ഡോട്ട്", "ডট", "ડોટ", "ਡਾਟ"),
    "_": ("underscore", "अंडरस्कोर", "ಅಂಡರ್‌ಸ್ಕೋರ್", "அண்டர்ஸ்கோர்", "అండర్‌స్కోర్", "അണ്ടർസ്കോർ", "আন্ডারস্কোর",
          "અંડરસ્કોર", "ਅੰਡਰਸਕੋਰ"),
    "-": ("dash", "hyphen", "minus", "डैश", "हाइफ़न"),
}


def email(text: str) -> str:
    value = " " + unicodedata.normalize("NFC", text).casefold().strip() + " "
    for symbol, words in _EMAIL_WORDS.items():
        for word in sorted(words, key=len, reverse=True):
            value = re.sub(rf"(?<=\s){re.escape(word)}(?=\s)", f" {symbol} ", value)
    # Spelled characters and spoken digits join up: 'r a m 1 2 3 at gmail dot com'.
    # Only English digit words: 'do', 'be' or 'tin' could be part of an address in other languages.
    value = "".join(_EMAIL_DIGITS.get(word, word) for word in value.split())
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+", value) or ".." in value:
        raise InvalidValue("email")
    return value


# Month names: English, romanised Hindi and the English month names as written in each Indian script.
_MONTH_NAMES = (
    "january jan janvari janwari जनवरी ಜನವರಿ ஜனவரி జనవరి ജനുവരി জানুয়ারি জানুয়ারী જાન્યુઆરી ਜਨਵਰੀ",
    "february feb farvari farwari february फ़रवरी फरवरी फेब्रुवारी ಫೆಬ್ರವರಿ பிப்ரவரி ఫిబ్రవరి ഫെബ്രുവരി ফেব্রুয়ারি "
    "ફેબ્રુઆરી ਫ਼ਰਵਰੀ ਫਰਵਰੀ",
    "march mar maarch मार्च ಮಾರ್ಚ್ மார்ச் మార్చి മാർച്ച് মার্চ માર્ચ ਮਾਰਚ",
    "april apr aprail aprel अप्रैल एप्रिल ಏಪ್ರಿಲ್ ஏப்ரல் ఏప్రిల్ ഏപ്രിൽ এপ্রিল એપ્રિલ ਅਪ੍ਰੈਲ",
    "may mai mei मई मे ಮೇ மே మే മേയ് মে મે ਮਈ",
    "june jun joon जून ಜೂನ್ ஜூன் జూన్ ജൂൺ জুন જૂન ਜੂਨ",
    "july jul julai जुलाई जुलै ಜುಲೈ ஜூலை జూలై ജൂലൈ জুলাই જુલાઈ ਜੁਲਾਈ",
    "august aug agast agust अगस्त ऑगस्ट ಆಗಸ್ಟ್ ஆகஸ்ட் ఆగస్టు ആഗസ്റ്റ് আগস্ট ઓગસ્ટ ਅਗਸਤ",
    "september sep sept sitambar sitamber सितंबर सितम्बर सप्टेंबर ಸೆಪ್ಟೆಂಬರ್ செப்டம்பர் సెప్టెంబర్ സെപ്റ്റംബർ "
    "সেপ্টেম্বর સપ્ટેમ્બર ਸਤੰਬਰ",
    "october oct aktubar aktoobar अक्टूबर ऑक्टोबर ಅಕ್ಟೋಬರ್ அக்டோபர் అక్టోబర్ ഒക്ടോബർ অক্টোবর ઓક્ટોબર ਅਕਤੂਬਰ",
    "november nov navambar navembar नवंबर नवम्बर नोव्हेंबर ನವೆಂಬರ್ நவம்பர் నవంబర్ നവംബർ নভেম্বর નવેમ્બર ਨਵੰਬਰ",
    "december dec disambar disember दिसंबर दिसम्बर डिसेंबर ಡಿಸೆಂಬರ್ டிசம்பர் డిసెంబర్ ഡിസംബർ ডিসেম্বর ડિસેમ્બર "
    "ਦਸੰਬਰ",
)
_MONTHS = {name: i + 1 for i, names in enumerate(_MONTH_NAMES) for name in names.casefold().split()}
# Month names as written in each language, in the order of _MONTH_NAMES (also understood when spoken).
_MONTH_SPOKEN = {
    "en": "January February March April May June July August September October November December",
    "hi": "जनवरी फ़रवरी मार्च अप्रैल मई जून जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर",
    "kn": "ಜನವರಿ ಫೆಬ್ರವರಿ ಮಾರ್ಚ್ ಏಪ್ರಿಲ್ ಮೇ ಜೂನ್ ಜುಲೈ ಆಗಸ್ಟ್ ಸೆಪ್ಟೆಂಬರ್ ಅಕ್ಟೋಬರ್ ನವೆಂಬರ್ ಡಿಸೆಂಬರ್",
    "ta": "ஜனவரி பிப்ரவரி மார்ச் ஏப்ரல் மே ஜூன் ஜூலை ஆகஸ்ட் செப்டம்பர் அக்டோபர் நவம்பர் டிசம்பர்",
    "te": "జనవరి ఫిబ్రవరి మార్చి ఏప్రిల్ మే జూన్ జూలై ఆగస్టు సెప్టెంబర్ అక్టోబర్ నవంబర్ డిసెంబర్",
    "mr": "जानेवारी फेब्रुवारी मार्च एप्रिल मे जून जुलै ऑगस्ट सप्टेंबर ऑक्टोबर नोव्हेंबर डिसेंबर",
    "ml": "ജനുവരി ഫെബ്രുവരി മാർച്ച് ഏപ്രിൽ മേയ് ജൂൺ ജൂലൈ ആഗസ്റ്റ് സെപ്റ്റംബർ ഒക്ടോബർ നവംബർ ഡിസംബർ",
    "bn": "জানুয়ারি ফেব্রুয়ারি মার্চ এপ্রিল মে জুন জুলাই আগস্ট সেপ্টেম্বর অক্টোবর নভেম্বর ডিসেম্বর",
    "gu": "જાન્યુઆરી ફેબ્રુઆરી માર્ચ એપ્રિલ મે જૂન જુલાઈ ઓગસ્ટ સપ્ટેમ્બર ઓક્ટોબર નવેમ્બર ડિસેમ્બર",
    "pa": "ਜਨਵਰੀ ਫ਼ਰਵਰੀ ਮਾਰਚ ਅਪ੍ਰੈਲ ਮਈ ਜੂਨ ਜੁਲਾਈ ਅਗਸਤ ਸਤੰਬਰ ਅਕਤੂਬਰ ਨਵੰਬਰ ਦਸੰਬਰ",
}
for _months in _MONTH_SPOKEN.values():
    _MONTHS.update({name.casefold(): i + 1 for i, name in enumerate(_months.split())})
_TENS_EN = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
            "ninety": 90}
_TEENS_EN = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19}
_ORDINAL_DAY = re.compile(r"^(\d{1,2})(st|nd|rd|th)?$")
_DATE_FILLERS = {"of", "the"}


def _english_number(words: list[str]) -> int | None:
    """'nineteen eighty five' -> 1985, 'two thousand three' -> 2003, 'twenty one' -> 21."""
    words = [w for w in words if w not in {"and", "of", "the"}]
    if not words:
        return None
    units = {w: int(d) for w, d in _DIGITS.items() if re.fullmatch(r"[a-z]+", w) and w in
             {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}}

    def under_100(ws):
        if len(ws) == 1:
            return _TEENS_EN.get(ws[0], _TENS_EN.get(ws[0], units.get(ws[0])))
        if len(ws) == 2 and ws[0] in _TENS_EN and ws[1] in units:
            return _TENS_EN[ws[0]] + units[ws[1]]
        return None

    if "thousand" in words:
        k = words.index("thousand")
        head, tail = under_100(words[:k]), words[k + 1:]
        rest = under_100(tail) if tail else 0
        return head * 1000 + rest if head is not None and rest is not None else None
    if len(words) >= 2:
        for split in (1, 2):
            high, low = under_100(words[:split]), under_100(words[split:])
            if high is not None and low is not None and 10 <= high <= 99:
                return high * 100 + low
    return under_100(words)


def parse_date(text: str) -> date:
    """Numeric day/month/year (the Indian order), ISO year-month-day, or '15 August 1985' style."""
    value = unicodedata.normalize("NFC", text).casefold().strip()
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            year, month, day = map(int, value.split("-"))
            return date(year, month, day)
        if re.fullmatch(r"\d{1,2}[ /.-]+\d{1,2}[ /.-]+\d{4}", value):
            day, month, year = map(int, re.split(r"[ /.-]+", value))
            return date(year, month, day)
        # '15th of August 1985', 'August the 15th 1985'
        words = [w for w in _words(value) if w not in _DATE_FILLERS]
        month_at = [i for i, w in enumerate(words) if w in _MONTHS]
        if len(month_at) != 1:
            raise ValueError
        m = month_at[0]
        before, after = words[:m], words[m + 1:]
        # '15 August 1985' or 'August 15 1985'
        if before:
            day_words, year_words = before, after
        else:
            day_words, year_words = after[:1], after[1:]
        day = _day(day_words)
        year = _year(year_words)
        if day is None or year is None:
            raise ValueError
        return date(year, _MONTHS[words[m]], day)
    except (ValueError, TypeError):
        raise InvalidValue("date") from None


def _day(words: list[str]) -> int | None:
    if len(words) == 1 and (match := _ORDINAL_DAY.match(words[0])):
        return int(match.group(1))
    try:
        number = digits(" ".join(words))
        return int(number) if number and len(number) <= 2 else _english_number(words)
    except InvalidValue:
        day = _english_number(words) or indian.hindi_number(words)  # 'fifteenth', 'pandrah'
        return day if day is not None and day <= 31 else None


def _year(words: list[str]) -> int | None:
    if not words:
        return None
    try:
        number = digits(" ".join(words))
        if len(number) == 4:
            return int(number)
    except InvalidValue:
        pass
    year = _english_number(words) or indian.hindi_number(words)  # 'unnis sau pachasi', 'do hazaar teen'
    return year if year and year >= 1000 else None


_NAME_PREFIX = re.compile(r"^(my (full )?name is|name is|mera (poora |pura )?naa?m|mera nam|nanna hesaru|en peyar|"
                          r"naa peru|na peru|maza nav|majha nav|maajha naav|ente peru|ente per|amar naam|aamar nam|"
                          r"maru naam|maaru naam|mera naam|ਮੇਰਾ ਨਾਮ|मेरा नाम|माझे नाव)\s+", re.IGNORECASE)
_NAME_SUFFIX = re.compile(r"\s+(hai|he|h|hain|aahe|ahe|ide|aagide|ga|aanu|anu|ache|chhe|che|है|आहे)$", re.IGNORECASE)


def person_name(text: str) -> str:
    value = re.sub(r"\s+", " ", text.strip().strip(".!?।"))
    value = _NAME_SUFFIX.sub("", _NAME_PREFIX.sub("", value))
    if not value or any(c.isdigit() for c in value) or len(value) > 120:
        raise InvalidValue("name")
    # Prisma writes names in lower-case Latin letters; forms want them capitalised as on the ID card.
    return indian.polish_name(value)


# Spoken words for common choices, mapped to a canonical meaning that matches the option text.
_CHOICE_SYNONYMS = {
    "male": "male man purush purus aadmi mard gandu ganDu aan aanu purushan purushudu manushya chhele chhelo "
            "chokro mund admi पुरुष मर्द आदमी ಗಂಡು ಪುರುಷ ஆண் పురుషుడు పురుషుడ పురుషుడు പുരുഷൻ ആൺ পুরুষ પુરુષ ਮਰਦ ਪੁਰਸ਼",
    "female": "female woman mahila aurat stree stri istri hennu pen penn sthree streelu mahile meye "
              "mahilaa bai ladki ਔਰਤ महिला औरत स्त्री ಹೆಣ್ಣು ಮಹಿಳೆ பெண் స్త్రీ మహిళ സ്ത്രീ മഹിള মহিলা নারী સ્ત્રી મહિલા ਇਸਤਰੀ",
    "married": "married shaadishuda shadishuda shaadi-shuda vivahit vivaahit vivahita",
    "single": "single unmarried avivahit avivaahit kunwara kunwari kuwara kuwari kunvara kunvari",
    "other": "other others anya itar itare thirunangai transgender kinnar अन्य इतर ಇತರೆ மற்றவை ఇతరులు മറ്റുള്ളവ "
             "অন্যান্য અન્ય ਹੋਰ",
}
_SYNONYM = {w.casefold(): meaning for meaning, words in _CHOICE_SYNONYMS.items() for w in words.split()}
_OPTION_MEANING = {
    "male": r"^(male|man|m|पुरुष|ಪುರುಷ|ಗಂಡು|ஆண்|పురుషుడు|പുരുഷൻ|পুরুষ|પુરુષ|ਮਰਦ|ਪੁਰਸ਼)$",
    "female": r"^(female|woman|f|महिला|स्त्री|ಮಹಿಳೆ|ಹೆಣ್ಣು|பெண்|స్త్రీ|మహిళ|സ്ത്രീ|মহিলা|নারী|સ્ત્રી|મહિલા|ਔਰਤ|ਇਸਤਰੀ)$",
    "other": r"^(other|others|transgender|third gender|अन्य|ಇತರೆ|மற்றவை|ఇతరులు|മറ്റുള്ളവ|অন্যান্য|અન્ય|ਹੋਰ)$",
    "married": r"^(married|विवाहित|शादीशुदा|ವಿವಾಹಿತ|திருமணமானவர்|వివాహిత|विवाहित|വിവാഹിതൻ|বিবাহিত|પરિણીત|ਵਿਆਹੁਤਾ)$",
    "single": r"^(single|unmarried|never married|अविवाहित|ಅವಿವಾಹಿತ|திருமணமாகாதவர்|అవివాహిత|അവിവാഹിതൻ|অবিবাহিত|"
              r"અપરિણીત|ਅਣਵਿਆਹਿਆ)$",
}
_ORDINALS = (
    "first 1st pehla pahla pehli pahli pahila modalane modalu mudhal mudhalaavadhu modati modatidi onnamathe "
    "prothom pratham pahelu pehlo pehla पहला पहली पहिला ಮೊದಲನೇ முதல் మొదటి ഒന്നാമത്തെ প্রথম પહેલું ਪਹਿਲਾ",
    "second 2nd doosra dusra doosri dusri eradane irandaavadhu rendava randamathe ditiyo dwitiyo biju bijo dooja "
    "दूसरा दूसरी दुसरा ಎರಡನೇ இரண்டாவது రెండవ രണ്ടാമത്തെ দ্বিতীয় બીજું ਦੂਜਾ",
    "third 3rd teesra tisra teesri tisri mooraneya moonraavadhu mudava moonnamathe tritiyo trijo teeja "
    "तीसरा तीसरी तिसरा ಮೂರನೇ மூன்றாவது మూడవ മൂന്നാമത്തെ তৃতীয় ત્રીજું ਤੀਜਾ",
    "fourth 4th chautha chauthi naalkane naangaavadhu naalugava naalamathe chaturtho chothu chautha "
    "चौथा चौथी ನಾಲ್ಕನೇ நான்காவது నాలుగవ നാലാമത്തെ চতুর্থ ચોથું ਚੌਥਾ",
    "fifth 5th paanchva panchva paanchvi aidane ainthaavadhu aidava anchamathe pancham paanchmu panjva "
    "पाँचवाँ पांचवा ಐದನೇ ஐந்தாவது ఐదవ അഞ്ചാമത്തെ পঞ্চম પાંચમું ਪੰਜਵਾਂ",
)
_ORDINAL = {w.casefold(): i for i, words in enumerate(_ORDINALS) for w in words.split()}
# \b does not see Indic vowel signs as part of a word ('चुनें' ends in one), so the word end is spelled out.
_PLACEHOLDER = re.compile("^\\W*(select|choose|please select|--|chuniye|चुनें|चुनिए)(?![\\wऀ-෿])",
                          re.IGNORECASE)


def _plain(text: str) -> str:
    """Lower-case words separated by single spaces. Letters, digits and combining marks (Indic vowel signs and
    viramas, which regex \\w does not cover) stay inside a word, as do the zero-width (non-)joiners."""
    text = unicodedata.normalize("NFC", text).casefold()
    kept = (c if unicodedata.category(c)[0] in "LNM" or c in "‌‍" else " " for c in text)
    return " ".join("".join(kept).split())


def real_options(options) -> list[str]:
    """Drop placeholder entries such as '-- Select --' so 'the first one' means the first real choice."""
    return [o for o in options if o.strip() and not _PLACEHOLDER.match(o.strip())]


def choose(options, text: str) -> str:
    options = real_options(options)
    said = _plain(text)
    exact = [o for o in options if _plain(o) == said]
    if len(exact) == 1:
        return exact[0]
    words = said.split()
    # 'the second one', 'doosra wala'
    ordinals = {_ORDINAL[w] for w in words if w in _ORDINAL}
    if len(ordinals) == 1 and len(words) <= 3:
        index = ordinals.pop()
        if index < len(options):
            return options[index]
    meanings = {_SYNONYM[w] for w in words if w in _SYNONYM}
    if len(meanings) == 1 and len(words) <= 3:
        meaning = meanings.pop()
        matches = [o for o in options if re.match(_OPTION_MEANING[meaning], _plain(o))]
        if len(matches) == 1:
            return matches[0]
    raise InvalidValue("choice")


def _date_format(field: Field) -> str:
    hint = f"{field.label} {getattr(field, 'help', '')}".casefold()
    if re.search(r"dd\s*/\s*mm\s*/\s*yyyy", hint):
        return "%d/%m/%Y"
    if re.search(r"dd\s*-\s*mm\s*-\s*yyyy", hint):
        return "%d-%m-%Y"
    if re.search(r"mm\s*/\s*dd\s*/\s*yyyy", hint):
        return "%m/%d/%Y"
    return "%Y-%m-%d"


def normalize(field: Field, text: str, concept: str | None = None) -> str:
    """The value to enter into `field` for the spoken or typed answer `text`, or InvalidValue(reason)."""
    value = unicodedata.normalize("NFC", text).strip()
    if field.protected or field.kind in {"secret", "file", "manual"}:
        raise InvalidValue("protected")
    if not value or len(value) > 2000 or any(ord(c) < 32 for c in value):
        raise InvalidValue("empty_or_long")
    if field.kind == "checkbox":
        if is_yes(value):
            return "true"
        if value.casefold().strip(".!?। ") in _NO_ANSWERS:
            return "false"
        raise InvalidValue("yes_no")
    if field.options:
        try:
            return choose(field.options, value)
        except InvalidValue:
            # "karnataka" for "कर्नाटक", "kisaan" for "किसान", "ek se paanch lakh" for "1 से 5 लाख"
            options = real_options(field.options)
            picked = indian.state_option(options, value) or indian.option_for(options, value)
            if picked is None:
                raise
            return picked
    kind = field.kind
    if kind in {"aadhaar", "postal", "mobile", "account"}:
        if kind == "mobile":
            # '+91 98765 43210' or 'plus nine one ...': the plus only marks the country code checked below
            value = re.sub(r"^(\+|plus\s|प्लस\s)\s*", "", value, flags=re.IGNORECASE)
        value = digits(value)
        if kind == "mobile" and len(value) == 12 and value.startswith("91"):
            value = value[2:]  # '+91 98765 43210' is the same number
        elif kind == "mobile" and len(value) == 11 and value.startswith("0"):
            value = value[1:]
        checks = {
            "aadhaar": (r"[0-9]{12}", "aadhaar_length"), "postal": (r"[1-9][0-9]{5}", "postal"),
            "mobile": (r"[0-9]{10}", "mobile_length"), "account": (r"[0-9]{9,18}", "account"),
        }
        pattern, reason = checks[kind]
        if not re.fullmatch(pattern, value):
            raise InvalidValue(reason)
        if kind == "aadhaar" and not valid_aadhaar(value):
            raise InvalidValue("aadhaar_checksum")
        if kind == "mobile" and value[0] not in "6789":
            raise InvalidValue("mobile_start")
    elif kind in {"pan", "ifsc"}:
        try:
            value = spelled(value)
        except InvalidValue:
            raise InvalidValue(kind) from None
        if kind == "pan":
            value = _resolve_o(value, {0, 1, 2, 3, 4, 9})
            if not re.fullmatch(r"[A-Z]{3}[ABCFGHJLPT][A-Z][0-9]{4}[A-Z]", value):
                raise InvalidValue("pan")
        else:
            value = _resolve_o(value, {0, 1, 2, 3})
            if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", value):
                raise InvalidValue("ifsc")
    elif kind == "email":
        value = email(value)
    elif kind == "date":
        parsed = parse_date(value)
        if not 1900 <= parsed.year <= 2100:
            raise InvalidValue("date_range")
        if re.search(r"birth|dob|जन्म|ಜನ್ಮ|ಹುಟ್ಟಿದ|பிறந்த|పుట్టిన|ജനന|জন্ম|જન્મ|ਜਨਮ", field.label.casefold()):
            today = date.today()
            if parsed > today or today.year - parsed.year > 120:
                raise InvalidValue("date_range")
        value = parsed.strftime(_date_format(field))
    elif concept in {"name", "relative_name", "nominee"}:
        value = person_name(value)
    elif concept == "gender":
        meanings = {_SYNONYM[w] for w in _plain(value).split() if w in _SYNONYM}
        if len(meanings) != 1:
            raise InvalidValue("choice")
        value = meanings.pop().capitalize()
    elif concept in {"place", "address"}:
        value = indian.polish_place(value)
    elif concept == "income":
        value = amount(value)
    return value
