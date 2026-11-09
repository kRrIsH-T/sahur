"""Conservative local routing before legacy models that predate form actions.

Only whole request patterns are recognised. Ordinary form values never pass through this router.
"""

import re
import unicodedata

_FORM = (r"(?:\bform(?:s|u|um|nu|er|ta)?\b|\b(?:arji|arzi|a?avedan patra|vinnappam|darakhastu|apeksha|"
         r"abedanpatra|abedonpotro)\b|फ़?(?:ॉर्म|ार्म)|आवेदन\s*पत्र|ಫಾರ್ಮ್|ಅರ್ಜಿ|படிவ|விண்ணப்ப|"
         r"ఫారం|ఫారమ్|దరఖాస్తు|ഫോം|ഫോര്മ്|അപേക്ഷ|ফর্ম|ফরম|আবেদনপত্র|ફોર્મ|ਅਰਜ਼ੀ|ਫਾਰਮ|ਫ਼ਾਰਮ)")
_FILL = (r"(?:\bfill\b|\bfilling\b|\bbhar\w*\b|\bguide\b|\bth?umb\w*\b|\btumbu\w*\b|"
         r"\bnirapp\w*\b|\bniraipp\w*\b|\bnimp\w*\b|\bpuripp\w*\b|\bpooripp\w*\b|\bpoorthi\b|"
         r"\bpuro[nr]\w*\b|\bbhoro\w*\b|"
         r"भरो|भरने|भरना|भरें|भरू|भरण|भराय|ತುಂಬ|ಭರ್ತಿ|நிரப்ப|నింప|പൂരിപ്പ|পূরণ|ভরতে|ભર|ਭਰ)")
_EXPLAIN = r"(?:\bexplain\b|\bunderstand\b|\bneed\b|\basking\b|\bmean\b|\bdocuments\b|\bsamjha\w*\b|" \
           r"\bsamaj\w*\b|\ben(u|ide)\b|\byenu\b|\benna\b|\bemi\b|\bentha\b|\bsamj\w*\b|" \
           r"\bkya\b|\bki\b|समझ|क्या|चाहिए|समज|ಬೇಕ|ವಿವರ|ಏನು|என்ன|விளக்|ఏమి|ఏం|వివర|എന്ത്|" \
           r"വിശദ|বুঝ|কী|কি |સમજ|શું|ਸਮਝ|ਕੀ)"
_SITE = r"(?:\bsite\b|\bwebsite\b|\blink\b|वेबसाइट|साइट|ವೆಬ್|ಸೈಟ್|தளம்|இணைய|సైట్|వెబ్|" \
        r"സൈറ്റ്|വെബ്|সাইট|ওয়েব|સાઇટ|વેબ|ਸਾਈਟ|ਵੈੱਬ)"
_CHECK = r"(?:\breal\b|\bsafe\b|\btrust\b|\bgenuine\b|\bfake\b|\bofficial\b|\basli\b|\bcheck\b|" \
         r"\bnija\w*\b|\bunmai\w*\b|\bnijam\w*\b|\bsheri\w*\b|\bkhari\b|\bsachu\b|" \
         r"असली|सुरक्षित|खरी|खोटी|ನಿಜ|ಸುರಕ್ಷ|உண்மை|போலி|నిజ|సురక్ష|യഥാർത്ഥ|സുരക്ഷ|আসল|নিরাপদ|" \
         r"સાચી|અસલી|ਸਹੀ|ਅਸਲੀ|ਜਾਅਲੀ)"


def intent(command: str) -> str | None:
    text = unicodedata.normalize("NFC", command).casefold().strip()
    # A dictated value or message can mention a form; do not divert it into a session.
    if re.search(r"\b(type|write|message|send|likho|bhejo|bolo ki)\b|लिखो|भेजो|ಟೈಪ್|டைப்", text):
        return None
    if re.search(_SITE, text) and re.search(_CHECK, text):
        return "check_site"
    if re.search(_FORM, text):
        # Questions about what to enter explain the form; 'kya bharna hai' is not an instruction to
        # start collecting answers. Explicit explanation requests take precedence over 'fill'.
        if re.search(r"\b(what|which|kya|yenu|enna|emi|entha|samjhao|explain)\b|"
                     r"क्या|समझाओ|ಏನು|ವಿವರ|என்ன|விளக்|ఏమి|ఏం|ఎందు|എന്ത്|വിശദ|কী|বুঝ|શું|સમજ|ਕੀ|ਸਮਝ|समज", text):
            return "explain_form"
        if re.search(_FILL, text):
            return "fill_form"
        if re.search(_EXPLAIN, text):
            return "explain_form"
    if re.search(r"\bupload\b|अपलोड|ಅಪ್‌ಲೋಡ್|பதிவேற்|అప్‌లోడ్|അപ്‌ലോഡ്|আপলোড|અપલોડ|ਅੱਪਲੋਡ", text):
        return "upload_file"
    return None


_INLINE_VALUE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|\b[a-z]{5}\d{4}[a-z]\b|\b\d(?:[ -]?\d){5,}\b", re.I)
_VALUE_CLAUSE = re.compile(r"\b(?:with|using|mera|meri|mere)\b|(?:मेरी|मेरा|मेरे)\s", re.I)


def lookup_request(command: str) -> str:
    """Keep the form request for navigation and drop common inline answer values before planning."""
    text = unicodedata.normalize("NFC", command).strip()
    form_word = re.search(_FORM, text, re.I)
    if form_word and (clause := _VALUE_CLAUSE.search(text, form_word.end())):
        text = text[:clause.start()]
    return " ".join(_INLINE_VALUE.sub(" ", text).split())[:250]


# "Maine OTP bata diya", "paise kat gaye", "I shared my PIN": the 1930 helpline advice, in or out of a form.
_FRAUD = re.compile(
    r"\b(otp|pin|cvv|password)\b.{0,30}\b(de diya|bata diya|share|shared|gave|told|diya|bataya|bheja|"
    r"kottide|kotte|helide|sonnen|koduthen|cheppanu|ichanu|sangitla|dila|paranju|koduthu|diye diyechi|bole diyechi|"
    r"aapi didho|kahi didho|dass ditta|de ditta)\b"
    r"|\b(shared|gave|told|bata diya|de diya|bataya)\b.{0,20}\b(otp|pin|cvv|password)\b"
    r"|\b(paise|paisa|money|rupaye|amount|panam|dabbu|taka|rupiya|pese|paisay)\b.{0,30}"
    r"\b(kat gaye|kat gaya|chale gaye|gaye|lost|gone|debited|stolen|hoyitu|pochu|poyindi|gele|poyi|chole geche|"
    r"gaya|kapaya|chale gaya|kat gye)\b"
    r"|\b(fraud|scam|dhokha|thagi|cheated|vanchane|mosadi|mosam|phasavnuk|thattippu|protarona|chhetarpindi|"
    r"dhokha ho gaya)\b"
    r"|ठगी|धोखा|फ्रॉड|ओटीपी बता|पैसे कट|ವಂಚನೆ|மோசடி|మోసం|फसवणूक|തട്ടിപ്പ്|প্রতারণা|છેતરપિંડી|ਧੋਖਾ",
    re.IGNORECASE)
# "Meri jaankari mita do", "forget my details": delete the local form profile.
_FORGET = re.compile(
    r"\b(forget|delete|erase|remove|mita|hata|alisi|azhi|tudich|pus|maykk|muchhe|bhunsi|mitaa)\w*\b.{0,30}"
    r"\b(my|meri|mera|mere|nanna|en|ennoda|naa|na|maza|majhi|ente|amar|maru|mari|meri)\b.{0,30}"
    r"\b(details|data|information|info|jaankari|jankari|profile|vivara|vivaram|vivarangal|vivaralu|mahiti|"
    r"vivarangal|tathya|vigat|jaankari)\b"
    r"|\b(my|meri|mera|nanna|ennoda|naa|maza|majhi|ente|amar|maru|meri)\b.{0,30}"
    r"\b(details|data|information|jaankari|jankari|profile|vivara|vivaralu|mahiti|tathya|vigat)\b.{0,30}"
    r"\b(forget|delete|erase|remove|mita|hata|alisi|azhi|tudich|pus|maykk|muchhe|bhunsi)\w*"
    r"|जानकारी मिटा|जानकारी हटा|माहिती पुस|ಮಾಹಿತಿ ಅಳಿಸ|விவரங்களை அழி|వివరాలు తొలగ|വിവരങ്ങൾ മായ്|তথ্য মুছ|વિગતો ભૂંસ|"
    r"ਜਾਣਕਾਰੀ ਮਿਟਾ",
    re.IGNORECASE)


def fraud_report(command: str) -> bool:
    """The user says they shared an OTP/PIN or lost money: they need the 1930 helpline now."""
    return _FRAUD.search(unicodedata.normalize("NFC", command)) is not None


def forget_request(command: str) -> bool:
    """The user asks Bol OS to delete their saved form details."""
    return _FORGET.search(unicodedata.normalize("NFC", command)) is not None
