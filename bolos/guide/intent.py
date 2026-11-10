"""Local routing for "show me how" requests, and the short words that steer a running guide.

A request is a lesson when it asks to be taught or shown, or asks how to do something on the computer:

- a teach verb: "sikhao", "teach me", "step by step" (a bare "sikha"/"shikha" also needs a computer word, since
  Shikha is a name);
- a how-word with a show verb: "kaise ... dikhao", in any of the ten languages;
- a how-word with a computer word: "Bluetooth kaise on karein", "how do I change my wallpaper";
- applying for a document online: "PAN card ke liye apply karna hai".

Prisma writes what it hears, so the show and teach verbs are matched by shape as well as by spelling: "dikhao"
has arrived as dikaho, bikhau and dekhako, "sikha" as shikha (bolos history, 8 November 2026). A how-question
without a computer word stays a web search ("chai kaise banate hain"), and "photos dikhao" or "Bluetooth on
karo" stay ordinary commands.
"""

import re
import unicodedata

from bolos.responses import _norm, _words, refuses_command, script_language

# Dictated text or a message can contain these words; it must not start a lesson.
_DICTATION = re.compile(
    r"^(?:please )?(?:type|write|message|send)\b|"
    r"\b(?:likho|bhejo|bolo ki|type karo|message karo|send karo|whatsapp karo ki|type pannu|type maadi|anuppu|"
    r"kalisi|kalsi|pampu)\b|लिखो|भेजो|ಟೈಪ್|ಕಳುಹಿಸು|டைப்|அனுப்பு|పంపు")
_FORM = re.compile(r"\bforms?\b|फॉर्म|फार्म|ಫಾರ್ಮ್|படிவ|ఫారం|ఫారమ్|ഫോം|ফর্ম|ફોર્મ|ਫਾਰਮ")

_HINDI_HOW = r"kaise|kaisey|kaisay|kese|kesay|kaisa|kis tarah"
_HOW = re.compile(
    rf"\b({_HINDI_HOW}|how|hege|hegge|eppadi|epadi|epdi|ela|elaa|kasa|kase|kashi|kasha|"
    r"kibhabe|kivabe|kemon kore|kevi rite|kevirite|engane|enganeya|kiddan|kive|kiven|kivein)\b"
    r"|कैसे|किस तरह|ಹೇಗೆ|எப்படி|ఎలా|कसा|कसे|कशी|कसं|কিভাবে|কীভাবে|কেমন করে|કેવી રીતે|എങ്ങനെ|ਕਿਵੇਂ|ਕਿੱਦਾਂ")
_SHOW = re.compile(
    r"\b(show|torisu|thorisu|torsu|kaattu|kaatu|kaanbi|choopinchu|chupinchu|chupinchandi|dakhav|dakhva|"
    r"dakhava|dekhan|batavo|batav|kaanikku|kaanikkoo|kanikku)\b"
    # dikhao and how Prisma hears it: dikha, dikhaao, dikaho, dikao, dikhaiye, dekhao, dekhako, bikhau, ...
    r"|\b[bd][ie]e?kh?a{1,2}(?:h?[aou]{1,2}|ko|iye|yiye|ye|na)?\b"
    r"|दिखा|ತೋರಿಸ|காட்டு|காண்பி|చూపించ|दाखव|দেখাও|দেখান|બતાવ|കാണിക്ക|ਦਿਖਾ")
_TEACH = re.compile(
    r"\b(teach|kathukodu|kattukodu|nerpinchu|nerpu|shikav|shikva|shekhao|shikhvado|shikhvo|padippikku)\b"
    r"|\b(walk me through|show me how|show me where to click|where (?:do|should) i click|guide me|step by step)\b"
    # sikhao and its spellings: sikhaao, shikhao, sikhaiye, sikha do, sekhao ...
    r"|\bs?h?[ie]kh?a{1,2}(?:o|ao|u|au|iye|yiye|ye)\b|\bs?h?[ie]kh?a{1,2} (?:do|dijiye|de)\b"
    r"|सिखा|ಕಲಿಸಿ|ಕಲಿಸು|கற்றுக்கொடு|சொல்லிக்கொடு|నేర్పించ|शिकव|শেখাও|শেখান|શીખવ|പഠിപ്പിക്ക|ਸਿਖਾ")
# A bare "sikha" / "shikha": a teach verb only next to a computer word ("folder banana shikha").
_TEACH_WEAK = re.compile(r"\b(sikha|shikha|sikhaa|shikhaa|sekha)\b")
_COMPUTER = re.compile(
    r"\b(computer|laptop|pc|windows|settings?|internet|folders?|files?|screenshot|"
    r"wallpaper|printer|print|download|install|uninstall|update|apps?|application|software|browser|chrome|edge|"
    r"website|websites|site|e-?mail|gmail|mail|whatsapp|password|account|login|log in|sign in|online|upload|"
    r"pdf|zoom|volume|brightness|keyboard|mouse|taskbar|start menu|desktop|copy|paste|rename|pen ?drive|usb|"
    r"camera|microphone|mic|speaker|notepad|word|excel|powerpoint|youtube|bookmark|font|recycle bin|shortcut|"
    r"calculator|scientific mode|screens?|buttons?|click|tap|menus?|tabs?|dialog|scroll)\b"
    r"|कंप्यूटर|कम्प्यूटर|लैपटॉप|सेटिंग|इंटरनेट|फ़ोल्डर|फोल्डर|फ़ाइल|फाइल|स्क्रीनशॉट|"
    r"वॉलपेपर|प्रिंटर|डाउनलोड|ऐप|वेबसाइट|ईमेल|पासवर्ड|ऑनलाइन|अपलोड|कीबोर्ड|माउस|डेस्कटॉप"
    r"|ಕಂಪ್ಯೂಟರ್|ಸೆಟ್ಟಿಂಗ್|ಬ್ಲೂಟೂತ್|ವೈಫೈ|ಫೋಲ್ಡರ್|ಫೈಲ್|ಸ್ಕ್ರೀನ್‌ಶಾಟ್|ವಾಲ್‌ಪೇಪರ್"
    r"|கம்ப்யூட்டர்|செட்டிங்|ப்ளூடூத்|வைஃபை|ஃபோல்டர்|ஃபைல்|ஸ்கிரீன்ஷாட்|வால்பேப்பர்"
    r"|కంప్యూటర్|సెట్టింగ్|బ్లూటూత్|వైఫై|ఫోల్డర్|ఫైల్|స్క్రీన్‌షాట్|వాల్‌పేపర్"
    r"|কম্পিউটার|সেটিং|ব্লুটুথ|ওয়াইফাই|ফোল্ডার|ফাইল|স্ক্রিনশট|ওয়ালপেপার"
    r"|કમ્પ્યુટર|સેટિંગ|બ્લૂટૂથ|વાઇફાઇ|ફોલ્ડર|ફાઇલ|સ્ક્રીનશોટ|વોલપેપર"
    r"|കമ്പ്യൂട്ടർ|സെറ്റിങ്|ബ്ലൂടൂത്ത്|വൈഫൈ|ഫോൾഡർ|ഫയൽ|സ്ക്രീൻഷോട്ട്|വാൾപേപ്പർ"
    r"|ਕੰਪਿਊਟਰ|ਸੈਟਿੰਗ|ਬਲੂਟੁੱਥ|ਵਾਈਫਾਈ|ਫੋਲਡਰ|ਫਾਈਲ|ਸਕ੍ਰੀਨਸ਼ਾਟ|ਵਾਲਪੇਪਰ")
_SETTINGS_TOPICS = {
    "hotspot": re.compile(r"\bhot[ ‐‑-]?spot\b|हॉट[ ‐‑-]?स्पॉट"),
    "wifi": re.compile(r"\bwi[ ‐‑-]?fi\b|वा[इई][ ‐‑-]?फा[इई]"),
    "bluetooth": re.compile(r"\bblue[ ‐‑-]?tooth\b|ब्लू[ ‐‑-]?टूथ"),
    "night_light": re.compile(r"\bnight[ ‐‑-]?light\b|ना[इई]ट[ ‐‑-]?ला[इई]ट"),
}
_SETTINGS_OTHER_DEVICE = re.compile(
    r"\b(?:phones?|smartphones?|android|iphones?|ios|ipads?|tablets?|macs?|macbooks?|linux|ubuntu|chromebooks?|"
    r"routers?|modems?|mobile (?:mein|me|pe|par|ka|ke|ki)|on (?:my|your|the) mobile)\b"
    r"|फ़?ोन|आईफ़?ोन|एंड्रॉइड|मोबाइल (?:में|पर|का|के|की)")
_SETTINGS_OTHER_TASK = re.compile(
    r"\b(?:connect(?:ing|ed)?|join|pair(?:ing)?|set up|setup|(?:re)?configur\w*|change|modify|rename|"
    r"pass ?word|automatic\w*|schedule|bad(?:a)?l\w*|jor\w*|jod\w*)\b|पासवर्ड|बदल|जोड़|कनेक्ट")
_ENGLISH_SWITCH = re.compile(r"\b(?:turn|switch|toggle)\s+(?:(?!(?:on|off)\b)[\w'‐‑-]+\s+){0,7}(on|off)\b")
_SWITCH_ON = re.compile(rf"\b(?:enable|activate|chaa?l(?:u|oo)|on (?:(?:{_HINDI_HOW}) )?k(?:a)?r\w*)\b|चालू|ऑन")
_SWITCH_OFF = re.compile(
    rf"\b(?:disable|deactivate|(?:off|bandh?) (?:(?:{_HINDI_HOW}) )?k(?:a)?r\w*)\b|बंद|बन्द|ऑफ")
_SWITCH_AMBIGUOUS = re.compile(r"\b(?:on|off)\b\s*(?:/|and(?: then)?|or|then|ya|aur)\s*(?:back )?(?:on|off)\b")
_WALLPAPER = re.compile(
    r"\b(?:wall[ -]?paper|(?:desktop|windows|pc|computer|laptop) background)\b"
    r"|व[ाॉ]लपेपर|ವಾಲ್‌?ಪೇಪರ್|வால்பேப்பர்|వాల్‌?పేపర్"
    r"|ওয়ালপেপার|વોલપેપર|വാൾപേപ്പർ|ਵਾਲਪੇਪਰ")
_WALLPAPER_CHANGE = re.compile(
    r"\b(?:chang(?:e|ing)|set(?:ting)?|replac(?:e|ing)|bad(?:a)?l\w*|laga(?:na|ye(?:in|n)?|un?))\b"
    r"|बदल|लगाएँ|लगाएं|लगाना|ಬದಲ|மாற்ற|మార్చ|বদল|બદલ|മാറ്റ|ਬਦਲ")
_OTHER_BACKGROUND = re.compile(
    r"\b(?:lock[ -]?screen|screen ?saver|whats ?app|zoom|teams|google meet|chrome|firefox|browser)\b"
    r"|लॉक स्क्रीन|व्हाट्सऐप")
_DOCUMENT = re.compile(
    r"\b(pan|pan card|aadhaa?r|passport|voter ?id|voter card|ration card|driving licen[cs]e|licen[cs]e|"
    r"birth certificate|pension|scholarship|ayushman)\b"
    r"|पैन|आधार|पासपोर्ट|वोटर|राशन|लाइसेंस|ಪ್ಯಾನ್|ಆಧಾರ್|பான்|ஆதார்|పాన్|ఆధార్|पॅन|প্যান|આધાર|પાન|പാൻ|ਪੈਨ|ਆਧਾਰ")
_APPLY = re.compile(
    r"\b(apply|application|banwana|banvana|banwani|banvani|banwane|banvane|banana hai|register|registration|"
    r"renew|naya banana)\b|आवेदन|अप्लाई|बनवाना|बनवानी|ಅರ್ಜಿ|விண்ணப்ப|దరఖాస్తు|अर्ज|আবেদন|અરજી|അപേക്ഷ|ਅਰਜ਼ੀ")
_APPLICATION_PAPERWORK = re.compile(
    r"\b(?:apply (?:online )?for|ke liye (?:online )?apply|"
    r"(?:fill(?: in| out)?|submit|complete) (?:an? |the |my |this )?application)\b")
_PAN_CARD = re.compile(
    r"\b(?:pan card|pan (?:application|form|apply|online|ke liye|ka|banwana)|new pan|for (?:a )?pan)\b|"
    r"पैन|ಪ್ಯಾನ್|பான்|పాన్|पॅन|প্যান|પાન|പാൻ|ਪੈਨ")
_PAN_OTHER = re.compile(
    r"\b(?:e[ -]?pan|instant|correction|correct|update|change|reprint|duplicate|replacement|lost|renew|resume|"
    r"status|track|download|documents?|fees?|cost|eligibility|what|which|where|when|why)\b|"
    r"\blink(?:ing)? (?:a?adhaa?r|pan)\b|"
    r"क्या|कौन|कब|सुधार|बदल|दोबारा|स्थिति")
_PAN_FORM_ACTION = re.compile(r"\b(?:open|fill|filling|bhar\w*)\b|खोल|भर|ತುಂಬ|நிரப்ப|నింప|പൂരിപ്പ|পূরণ|ભર|ਭਰ")


_SCREEN_QUESTION = re.compile(
    r"(?:what am i looking at|what(?:'s| is) on (?:(?:my|the|this) )?screen|"
    r"(?:please )?explain (?:this|my) (?:screen|window|error|dialog|page)|"
    r"what does (?:this|that) (?:error|warning|message|dialog|button|icon|option)(?: on (?:my|this|the) screen)? "
    r"(?:mean|do)|what (?:is|are) (?:this|that|these)(?: (?:button|icon|option|dialog))?|what is it for|"
    r"what (?:do|should) i do next|how do i (?:fix|close|do) this|"
    r"help me with (?:this|what's on (?:my|the) screen)|where is (?:it|that)|and then|"
    r"(?:where is|where's|show me where) (?:the |this )?[^.!?\r\n]{1,75} (?:button|icon|menu|tab|option)(?: is)?|"
    r"(?:read|describe) (?:my|the|this) screen)")
_LESSON_START = re.compile(r"^(?:please )?(?:teach me|walk me through|show me how|guide me|how (?:do|can) i)\b")

# Short Romanized questions often have no language prediction from System 1. Keep these patterns by
# language so the same match that selects screen Q&A also selects the language of its reply.
_SCREEN_LOCAL = {lang: re.compile(pattern) for lang, pattern in {
    "hi": (r"(?:screen|स्क्रीन) (?:par|pe|पर|पे) kya (?:hai|dikh raha hai)|(?:स्क्रीन|परदे) पर क्या (?:है|दिख रहा है)|"
           r"(?:yeh?|woh?|is|us) (?:button|icon|error)(?: ka)? (?:kya (?:karta hai|hai|kaam hai|matlab hai))|"
           r"(?:यह|ये|वह|वो|इस) (?:बटन|आइकन|एरर)(?: का)? क्या (?:करता है|है|काम है|मतलब है)|"
           r"(?:yeh?|woh?) kya hai|(?:यह|ये|वह|वो) क्या है|"
           r"ab kya kar(?:un|oon|na hai)|अब क्या (?:करूँ|करना है)|"
           r"(?:kahan|kidhar) click kar(?:un|oon)|कहाँ क्लिक करूँ"),
    "kn": (r"(?:screen(?: alli|nalli)|screenalli|paradeya mele) (?:enu ide|enide|yenu ide|enu kanutte)|"
           r"(?:ಸ್ಕ್ರೀನ್‌ನಲ್ಲಿ|ಸ್ಕ್ರೀನ್ನಲ್ಲಿ|ಪರದೆಯ ಮೇಲೆ|ಸ್ಕ್ರೀನ್ ಮೇಲೆ) ಏನು (?:ಇದೆ|ಕಾಣುತ್ತಿದೆ)|"
           r"(?:ee|ii|aa) (?:button|icon|error) (?:enu|yenu) (?:madutte|maadutte|artha|helutte)|"
           r"(?:ಈ|ಆ) (?:ಬಟನ್|ಐಕಾನ್|ಎರರ್) ಏನು (?:ಮಾಡುತ್ತದೆ|ಅರ್ಥ)|"
           r"idu enu|ಇದು ಏನು|(?:iga|munde) enu madbeku|(?:ಈಗ|ಮುಂದೆ) ಏನು ಮಾಡಬೇಕು|"
           r"elli click madbeku|ಎಲ್ಲಿ ಕ್ಲಿಕ್ ಮಾಡಬೇಕು"),
    "ta": (r"(?:screen ?(?:la|le|il)|th?[ie]r(?:ai|aa|a|i)y[ia]l[ae]?) enna "
           r"(?:irukku(?:dhu)?(?: nu sollu(?:nga)?)?|irukkirathu|theriyudhu)|"
           r"(?:திரையில்|ஸ்கிரீனில்|ஸ்கிரீன்ல|ஸ்கிரீன் ல) என்ன (?:இருக்கு|இருக்கிறது|தெரிகிறது)|"
           r"(?:indha|intha|andha|antha) (?:button|icon|error) enna (?:seyyum|seyum|pannum|artham)|"
           r"(?:இந்த|அந்த) (?:பட்டன்|பொத்தான்|ஐகான்|எரர்) என்ன (?:செய்யும்|அர்த்தம்)|"
           r"idhu enna|ithu enna|இது என்ன|(?:ippo|aduthu|idhula) enna pannanum|"
           r"(?:இப்போ|அடுத்து|இதுல) என்ன பண்ணணும்|"
           r"enga click pannanum|எங்கே கிளிக் செய்ய வேண்டும்"),
    "te": (r"(?:screen (?:meeda|meedha|meedhu|lo)|screenlo) (?:emundi|emi undi|em kanipistondi)|"
           r"(?:స్క్రీన్ మీద|స్క్రీన్‌లో|స్క్రీన్లో|తెరపై) (?:ఏముంది|ఏమి ఉంది|ఏం కనిపిస్తోంది)|"
           r"(?:ee|aa) (?:button|icon|error) (?:em|emi) (?:chestundi|artham)|"
           r"(?:ఈ|ఆ) (?:బటన్|ఐకాన్|ఎర్రర్) (?:ఏం|ఏమి) (?:చేస్తుంది|అర్థం)|"
           r"idi enti|ఇది ఏంటి|ippudu em cheyali|ఇప్పుడు ఏం చేయాలి"),
    "mr": (r"screen var (?:kay|kaay) aahe|(?:स्क्रीनवर|स्क्रीन वर|पडद्यावर) काय (?:आहे|दिसतंय)|"
           r"(?:he|te) button (?:kay|kaay) karte|(?:हे|ते) बटण काय करतं|"
           r"he kay aahe|हे काय आहे|ata kay karu|आता काय करू|kuthe click karu|कुठे क्लिक करू"),
    "ml": (r"screen(?:il| il) (?:entha|enthu) (?:kaanunnathu|kanunnathu|undu)|"
           r"(?:സ്ക്രീനിൽ|സ്ക്രീനില്) എന്താണ് (?:കാണുന്നത്|ഉള്ളത്)|"
           r"(?:ee|aa) button enthu cheyyum|(?:ഈ|ആ) ബട്ടൺ എന്ത് ചെയ്യും|"
           r"ithu entha|ഇത് എന്താണ്|ini enthu cheyyanam|ഇനി എന്ത് ചെയ്യണം"),
    "bn": (r"screen(?: e|e) (?:ki|kee) (?:ache|achhe|dekhachhe)|স্ক্রিনে (?:কি|কী) (?:আছে|দেখাচ্ছে)|"
           r"(?:ei|oi) button ki kore|(?:এই|ওই) বাটন (?:কি|কী) করে|"
           r"eta ki|এটা কী|ekhon ki korbo|এখন কী করব|kothay click korbo|কোথায় ক্লিক করব"),
    "gu": (r"screen (?:par|ma) shu (?:che|chhe|dekhay chhe)|સ્ક્રીન પર શું (?:છે|દેખાય છે)|"
           r"(?:aa|e) button shu kare chhe|(?:આ|એ) બટન શું કરે છે|"
           r"aa shu chhe|આ શું છે|have shu karu|હવે શું કરું|kya click karu|ક્યાં ક્લિક કરું"),
    "pa": (r"screen (?:te|utte) ki (?:hai|aa|dikh reha hai)|ਸਕ੍ਰੀਨ (?:ਤੇ|ਉੱਤੇ) ਕੀ (?:ਹੈ|ਦਿਖ ਰਿਹਾ ਹੈ)|"
           r"(?:eh|oh) button ki karda (?:hai|aa)|(?:ਇਹ|ਉਹ) ਬਟਨ ਕੀ ਕਰਦਾ ਹੈ|"
           r"eh ki hai|ਇਹ ਕੀ ਹੈ|hun ki karaan|ਹੁਣ ਕੀ ਕਰਾਂ|kithe click karaan|ਕਿੱਥੇ ਕਲਿੱਕ ਕਰਾਂ"),
}.items()}

_TAKEOVER = {lang: re.compile(rf"(?:{prefix})[,;:—-]?\s+(.+)", re.I) for lang, prefix in {
    "en": r"(?:please )?(?:take over and|do this for me)",
    "hi": r"mere liye kar do|मेरे लिए कर दो",
    "kn": r"nanagaagi maadi|nanagagi madi|ನನಗಾಗಿ ಮಾಡಿ",
    "ta": r"enakkaaga seithu kodu|enakkaga senju kudu|எனக்காக செய்து கொடு",
    "te": r"naa kosam chesi pettu|నా కోసం చేసి పెట్టు",
    "mr": r"majhyasathi karun de|माझ्यासाठी करून दे",
    "ml": r"enikku vendi cheythu tharuu|എനിക്കു വേണ്ടി ചെയ്തു തരൂ|എനിക്ക് വേണ്ടി ചെയ്തു തരൂ",
    "bn": r"amar jonno kore dao|আমার জন্য করে দাও",
    "gu": r"mara mate kari aapo|મારા માટે કરી આપો",
    "pa": r"mere layi kar deo|ਮੇਰੇ ਲਈ ਕਰ ਦਿਓ",
}.items()}

_LANGUAGE_CUES = {lang: re.compile(rf"\b(?:{words})\b") for lang, words in {
    "hi": rf"{_HINDI_HOW}|dikha\w*|sikha\w*|shikha\w*|bharna|bharne",
    "kn": r"hege|hegge|th?orisu\w*|torsu|maadodu|maadbeku|th?umb\w*|tumbu\w*",
    "ta": r"eppadi|epadi|epdi|kaattu\w*|kaatu\w*|kathukodu|pannuvadhu|pannanum|nirapp\w*|niraipp\w*",
    "te": r"ela|elaa|ch[ou]opinchu|chupinchu|chupinchandi|nerpinchu|marchali|nimp\w*",
    "mr": r"kasa|kase|kashi|kasha|dakhav\w*|dakhva|shikav\w*|lavaycha|bharaycha",
    "ml": r"engane|enganeya|kaanikku\w*|kanikku|padippikku|pooripp\w*|puripp\w*",
    "bn": r"kibhabe|kivabe|kemon kore|shekhao|dekhao|dekhan|puro[nr]\w*|bhoro\w*",
    "gu": r"kevi rite|kevirite|batavo|shikhvado|shikhvo|bharvu",
    "pa": r"kiddan|kive|kiven|kivein|das deo|karna sikhao",
    "en": r"how (?:do|can|to)|show me|teach me|walk me through|guide me|step by step",
}.items()}


def _question_text(command: str) -> str:
    return " ".join(_norm(command).replace("’", "'").split()).rstrip("?.!।॥").rstrip()


def screen_language(command: str) -> str | None:
    text = _question_text(command)
    for language, pattern in _SCREEN_LOCAL.items():
        if pattern.fullmatch(text):
            return language
    return "en" if _SCREEN_QUESTION.fullmatch(text) else None


def language_hint(command: str) -> str | None:
    """Strong phrasing cues for requests routed before System 1 predicts their language."""
    if language := screen_language(command):
        return language
    for language, pattern in _TAKEOVER.items():
        if language != "en" and pattern.fullmatch(command.strip()):
            return language
    if language := script_language(command):
        return language
    text = _norm(command)
    matches = {lang for lang, cue in _LANGUAGE_CUES.items() if cue.search(text)}
    indic = matches - {"en"}
    # 'dekhao' may be Bengali or a Prisma spelling of Hindi. Let a second cue disambiguate it.
    if "bn" in indic and len(indic) > 1 and re.search(r"\bdekhao\b", text):
        indic.discard("bn")
    if len(indic) == 1:
        return next(iter(indic))
    return "en" if not indic and ("en" in matches or _TAKEOVER["en"].fullmatch(text.strip())) else None


def takeover_goal(command: str) -> str | None:
    text = command.strip()
    for pattern in _TAKEOVER.values():
        if match := pattern.fullmatch(text):
            return match[1].strip()
    return None


def is_screen_question(command: str) -> bool:
    return screen_language(command) is not None


def bind_reference(question: str, label: str) -> str:
    """Resolve common follow-ups to the last control that Bol OS actually highlighted."""
    key = question.casefold().strip().rstrip("?.!")
    if re.fullmatch(r"what does that (?:button|icon|option) do|what is it for", key):
        return f'What does the control labeled "{label}" do?'
    if key in {"where is it", "where is that"}:
        return f'Where is the control labeled "{label}"?'
    return question


def is_guide_request(command: str) -> bool:
    """Whether the user asks to be shown how to do something on this computer."""
    text = unicodedata.normalize("NFC", command).casefold().strip()
    if new_pan_request(text):
        return True
    if not text or _FORM.search(text):
        return False
    if (payload := _DICTATION.search(text)) and not _LESSON_START.match(text):
        before = text[:payload.start()]
        if not (_HOW.search(before) or _TEACH.search(before)):
            return False
    computer = bool(_COMPUTER.search(text) or _settings_topics(text) or _WALLPAPER.search(text))
    if _TEACH.search(text) or (_TEACH_WEAK.search(text) and computer):
        return True
    if _HOW.search(text) and (_SHOW.search(text) or computer or _DOCUMENT.search(text)):
        return True
    return bool(_DOCUMENT.search(text) and _APPLY.search(text))


def _settings_topics(text: str) -> tuple[str, ...]:
    # Hotspot takes precedence when someone says "Wi-Fi hotspot".
    return tuple(topic for topic, pattern in _SETTINGS_TOPICS.items() if pattern.search(text))


def settings_toggle_lesson(command: str) -> tuple[str, bool] | None:
    """Recognize requests to learn a single Windows toggle, without taking an action for the user."""
    text = _question_text(command)
    topics = _settings_topics(text)
    if not topics or (len(topics) != 1 and topics != ("hotspot", "wifi")):
        return None
    if not is_guide_request(text) or refuses_command(text):
        return None
    if _SETTINGS_OTHER_DEVICE.search(text) or _SETTINGS_OTHER_TASK.search(text) or _SWITCH_AMBIGUOUS.search(text):
        return None
    states = set(_ENGLISH_SWITCH.findall(text))
    if _SWITCH_ON.search(text):
        states.add("on")
    if _SWITCH_OFF.search(text):
        states.add("off")
    return (topics[0], "on" in states) if len(states) == 1 else None


def wallpaper_lesson(command: str) -> bool:
    """Recognize requests to learn how to change the Windows desktop background."""
    text = _question_text(command)
    return bool(_WALLPAPER.search(text) and _WALLPAPER_CHANGE.search(text) and is_guide_request(text)
                and not refuses_command(text) and not _SETTINGS_OTHER_DEVICE.search(text)
                and not _OTHER_BACKGROUND.search(text) and not _settings_topics(text))


def new_pan_request(command: str) -> bool:
    """A request to open or fill the regular new PAN application, rather than a lesson or another PAN service."""
    text = _question_text(command)
    if not _PAN_CARD.search(text) or any(pattern.search(text) for pattern in (_HOW, _TEACH, _DICTATION, _PAN_OTHER)):
        return False
    requested = _APPLY.search(text) or (_FORM.search(text) and _PAN_FORM_ACTION.search(text))
    return bool(requested) and not refuses_command(text)


def wants_form(goal: str) -> bool:
    """Whether the lesson's goal is to fill in an application ("PAN card ke liye apply karna hai"): when its
    form appears, the private form session takes over."""
    text = unicodedata.normalize("NFC", goal).casefold()
    return bool(_FORM.search(text) or _APPLICATION_PAPERWORK.search(text)
                or (_DOCUMENT.search(text) and (_APPLY.search(text)
                    or re.search(r"\b(bhar\w*|fill\w*|online)\b|भर", text))))


def _phrases(*groups: str) -> list[tuple[str, ...]]:
    return [tuple(_norm(w) for w in phrase.split()) for group in groups for phrase in group.split("|")]


# Each entry is a phrase of whole words; a reply matches when it contains one and is short.
_REPEAT = _phrases(
    "phir se|fir se|dobara|repeat|again|kya bola|kya kaha|samajh nahi|samjha nahi|samjhi nahi|say again",
    "फिर से|दोबारा|क्या बोला|क्या कहा|समझ नहीं",
    "innond sala|matte heli|matte helu|ಮತ್ತೆ ಹೇಳಿ|ಇನ್ನೊಂದ್ ಸಲ|thirumba sollu|tirumba sollunga|"
    "திரும்ப சொல்லு|திரும்ப சொல்லுங்க|malli cheppu|malli cheppandi|మళ్ళీ చెప్పు",
    "punha sang|पुन्हा सांग|aabar bolo|আবার বলো|fari bolo|ફરી બોલો|veendum parayu|വീണ്ടും പറയൂ|ਫਿਰ ਤੋਂ|ਦੁਬਾਰਾ")
_STOP = _phrases(
    "ruko|ruk jao|bas|band karo|band kar do|stop|rehne do|chhodo|chodo|cancel|exit|quit|nahi chahiye",
    "रुको|बस|बंद करो|रहने दो|छोड़ो",
    "beda|nillisu|ಬೇಡ|ನಿಲ್ಲಿಸು|nirutthu|niruthu|niruthunga|vendam|venaam|நிறுத்து|வேண்டாம்|aapu|vaddu|ఆపు|వద్దు",
    "thamba|थांबा|nako|नको|thamo|থামো|থাক|bas karo|બસ|રહેવા દો|nirthu|നിർത്തൂ|വേണ്ട|ਰੁਕੋ|ਬੱਸ|ਬੰਦ ਕਰੋ")
_NEXT = _phrases(
    "ho gaya|ho gya|hogaya|kar diya|kar liya|aage|aage badho|agla|next|done|finished|ok done",
    "हो गया|कर दिया|कर लिया|आगे|अगला",
    "aaytu|aytu|aythu|ಆಯ್ತು|ಮುಂದೆ|munde|mudinjathu|mudinjidhu|mudinjiduchu|aduthu|"
    "முடிஞ்சது|முடிந்தது|அடுத்து|ayipoyindi|ayindi|అయిపోయింది|"
    "అయింది|తర్వాత",
    "zhala|झालं|झाले|पुढे|hoye geche|হয়ে গেছে|পরেরটা|thai gayu|થઈ ગયું|આગળ|kazhinju|കഴിഞ്ഞു|അടുത്തത്|ਹੋ ਗਿਆ|ਅੱਗੇ")
_MAX_CONTROL_WORDS = 5


def _contains(words: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1))


def control(reply: str) -> str | None:
    """"stop", "next" or "repeat" for a short steering reply during a guide; None for anything else (a new
    command, which ends the guide and runs as usual)."""
    words = _words(reply)
    if not words or len(words) > _MAX_CONTROL_WORDS:
        return None
    # Repeat first: "samajh nahi aaya" contains a no-word but asks to hear the step again.
    for kind, phrases in (("repeat", _REPEAT), ("stop", _STOP), ("next", _NEXT)):
        if any(_contains(words, phrase) for phrase in phrases):
            return kind
    return None
