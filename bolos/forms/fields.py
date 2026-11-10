"""What a form looks like: fields, buttons and page errors, plus the public scan/fill API.

Two producers fill these objects. `bolos.forms.windows.WindowsFormBackend.snapshot` (used by FormAssistant)
records metadata only and never reads a value. `scan()` below reads the whole form of a window through UI
Automation, including current values, so a guided session can skip fields that are already filled and verify
what it entered. Values of passwords, OTPs, PINs, CVVs and CAPTCHAs are never read (`value` stays None), and
`value`/`element` are excluded from repr so a logged Field never carries personal data.

The UI Automation work lives in bolos/forms/windows.py; the functions here are the stable entry points.
"""

import re
from dataclasses import dataclass
from dataclasses import field as _field


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"
    required: bool = False
    options: tuple[str, ...] = ()
    protected: bool = False
    help: str = ""
    # Filled by scan(); the metadata-only snapshot leaves the defaults.
    control_type: str = "Edit"  # Edit, Date, ComboBox, RadioButton (a whole group), CheckBox, File
    value: str | None = _field(default=None, compare=False, repr=False)  # None: unknown or never read
    error: str | None = None
    is_password: bool = False
    rect: tuple[int, int, int, int] | None = _field(default=None, compare=False)  # left, top, right, bottom
    element: object = _field(default=None, compare=False, repr=False)  # the UIA element (group for radios)
    parts: tuple = _field(default=(), compare=False, repr=False)  # radios: (name, element); date: spinners
    hwnd: int | None = _field(default=None, compare=False)

    @property
    def id(self) -> str:
        return self.key

    @property
    def kind_hint(self) -> str:
        return self.kind


@dataclass
class Form:
    """One window's form in screen (reading) order. `url` is None for desktop windows."""

    title: str
    url: str | None
    fields: list[Field]
    buttons: list[str]
    errors: list[str]
    hwnd: int | None = None
    headings: list[str] = _field(default_factory=list)
    button_elements: dict = _field(default_factory=dict, compare=False, repr=False)

    def field(self, label: str) -> Field | None:
        """The field whose label matches exactly (case-insensitive), else None."""
        key = label.casefold().strip()
        return next((f for f in self.fields if f.label.casefold() == key), None)


@dataclass(frozen=True)
class Snapshot:
    identity: tuple
    fields: tuple[Field, ...]
    submit: str | None = None
    host: str | None = None
    errors: int = 0


def field_kind(label: str, control_type: str = "Edit", password: bool = False) -> str:
    """Conservative, local glossary. Unknown fields remain literal text."""
    text = label.casefold()
    if password or re.search(r"\b(otp|cvv|cvc|password|passcode|(?:re)?captcha|not a robot|i am human|"
                             r"mpin|upi pin|atm pin|card pin|"
                             r"api key|token|secret|private key|recovery code|security answer|"
                             r"security code|verification code|one.time.code|characters shown)\b", text):
        return "secret"
    secret_words = (
        "पासवर्ड", "ओटीपी", "ओटीपी", "सीवीवी", "कैप्चा", "कॅप्चा", "गुप्तशब्द",
        "ಪಾಸ್", "ಒಟಿಪಿ", "ಓಟಿಪಿ", "ಸಿವಿವಿ", "ಕ್ಯಾಪ್ಚಾ", "ரகசிய", "கடவுச்சொல்", "ஓடிபி", "ஒடிபி", "கேப்ட்சா",
        "పాస్", "ఓటీపీ", "ఓటిపి", "సీవీవీ", "క్యాప్చా", "പാസ്", "ഒടിപി", "ഓടിപി", "സിവിവി", "ക്യാപ്ച",
        "পাসওয়ার্ড", "ওটিপি", "সিভিভি", "ক্যাপচা", "પાસવર્ડ", "ઓટીપી", "સીવીવી", "કેપ્ચા",
        "ਪਾਸਵਰਡ", "ਓਟੀਪੀ", "ਸੀਵੀਵੀ", "ਕੈਪਚਾ",
    )
    if any(w in text for w in secret_words):
        return "secret"
    # Card data and banking logins: never entered for anyone (RBI: no bank asks for them on a form).
    if re.search(r"\b(debit|credit|atm)\s*card\b|\b(expiry|expiration|valid (thru|through|till|upto))\b|"
                 r"\b(user ?id|login ?id|username|net ?banking (user|login|id|password))\b", text) or (
            re.search(r"\bcard (number|no)\b", text)
            and not re.search(r"aadhaa?r|\bpan\b|ration|voter|election|आधार|पैन", text)) or any(
            w in text for w in ("डेबिट कार्ड", "क्रेडिट कार्ड", "एटीएम कार्ड")):
        return "secret"
    native_pin = ("पिन", "ಪಿನ್", "பின்", "పిన్", "പിൻ", "পিন", "પિન", "ਪਿੰਨ")
    postal_words = ("postal", "pin code", "pincode", "zip", "डाक", "पिन कोड", "ಪಿನ್ ಕೋಡ್", "ಅಂಚೆ",
                    "அஞ்சல்", "பின் கோடு", "తపాలా", "పిన్ కోడ్", "തപാൽ", "പിൻ കോഡ്", "পোস্টাল",
                    "পিন কোড", "ટપાલ", "પિન કોડ", "ਡਾਕ", "ਪਿੰਨ ਕੋਡ")
    if any(w in text for w in native_pin):
        return "postal" if any(w in text for w in postal_words) else "secret"
    if "pin" in text and not re.search(r"\b(pin\s*code|postal|pincode|zip)\b", text):
        return "secret"
    if re.search(r"\b(upload|browse|choose file|select file|attach)\b", text):
        return "file"
    # A card's name label describes text, not the card number ('Full name as on Aadhaar').
    if re.search(r"\b(name|nominee)\b", text) or any(w in text for w in ("नाम", "ಹೆಸರು", "பெயர்", "పేరు")):
        return "text"
    if control_type == "CheckBox":
        return "checkbox"
    if control_type in {"ComboBox", "RadioButton"}:
        return "choice"
    for kind, words in {
        "aadhaar": ("aadhaar", "aadhar", "आधार"),
        "ifsc": ("ifsc", "आईएफएससी"),
        "pan": ("pan number", "pan card", "पैन"),
        "postal": ("pin code", "pincode", "postal", "zip code", "पिन कोड"),
        "mobile": ("mobile", "phone", "मोबाइल", "फ़ोन", "फोन"),
        "email": ("email", "e-mail", "ईमेल"),
        "date": ("date of birth", "birth date", "dob", "जन्म"),
        "account": ("account number", "खाता संख्या"),
    }.items():
        if any(w in text for w in words):
            return kind
    return "pan" if re.search(r"\bpan\b", text) else "text"


EXPLANATIONS = {
    "en": {
        "aadhaar": "The 12 digit number on your Aadhaar card. Check the website before sharing it.",
        "pan": "The 10 character code on your PAN card.",
        "ifsc": "The 11 character branch code on the first page of your bank passbook or cheque book.",
        "postal": "The 6 digit postal PIN code for your address; this is not a banking PIN.",
        "mobile": "Your 10 digit Indian mobile number.",
        "email": "Your email address, including the at sign and domain.",
        "date": "Your date of birth. Say or type day, month and four digit year, or year-month-day.",
        "account": "Your account number as printed in your passbook; keep every digit, including zeros.",
        "checkbox": "A choice or declaration. Read it before agreeing; you can say no.",
        "choice": "Choose exactly one of the options shown.",
        "secret": "Enter this yourself. I will not ask for or fill a password, OTP, banking PIN or CAPTCHA.",
        "file": "Choose the document yourself. Automatic uploads are not supported yet.",
        "manual": "Complete this control yourself. Its group, state or visibility is not clear enough "
                  "for automatic entry.",
        "text": "Give the information named by this label. If unclear, skip and check with the organisation.",
    },
    "hi": {
        "aadhaar": "आधार कार्ड पर लिखा 12 अंकों का नंबर। देने से पहले वेबसाइट की जाँच करें।",
        "pan": "पैन कार्ड पर लिखा 10 अक्षर और अंकों वाला कोड।",
        "ifsc": "पासबुक के पहले पन्ने या चेकबुक पर लिखा बैंक शाखा का 11 अक्षरों वाला कोड।",
        "postal": "आपके पते का 6 अंकों का डाक पिन कोड। यह बैंक का पिन नहीं है।",
        "mobile": "आपका 10 अंकों का भारतीय मोबाइल नंबर।",
        "email": "आपका ईमेल पता, जिसमें ऐट और डोमेन शामिल हों।",
        "date": "अपनी जन्म तिथि दिन, महीना और चार अंकों का साल बताइए।",
        "account": "पासबुक में लिखा खाता नंबर। शुरुआत के शून्य भी रहने दें।",
        "checkbox": "यह विकल्प या घोषणा है। सहमत होने से पहले पढ़ें। आप ना कह सकते हैं।",
        "choice": "स्क्रीन पर दिए विकल्पों में से एक चुनें।",
        "secret": "इसे खुद भरें। मैं पासवर्ड, ओटीपी, बैंक पिन या कैप्चा नहीं पूछूँगी और नहीं भरूँगी।",
        "file": "दस्तावेज़ खुद चुनें। अपने आप अपलोड करना अभी उपलब्ध नहीं है।",
        "manual": "इसे खुद भरें। इसके समूह, स्थिति या स्क्रीन पर जगह की जानकारी अपने आप भरने के लिए पर्याप्त नहीं है।",
        "text": "इस नाम के खाने में माँगी गई जानकारी दें। समझ न आए तो छोड़कर संस्था से पूछें।",
    },
}


def explanation(field: Field, language: str) -> str:
    # The two-language glossary is explicit; other languages retain the visible label, rather than an
    # unreviewed translation of a legal or financial term.
    return EXPLANATIONS.get(language, EXPLANATIONS["en"])[field.kind]


# Public UI Automation API. Thin wrappers so callers (and fakes in tests) depend on bolos.forms.fields only.

def scan(hwnd: int | None = None, options: bool = True, *, option_key: str | None = None) -> Form:
    """The form in window `hwnd` (default: the foreground window). Raises RuntimeError if it has no UI tree.

    With `options` (default), dropdowns whose choices are not in the tree (Chrome/Edge <select>) are opened for a
    moment to list them, then closed with their selection unchanged; options=False skips that (faster rescans,
    and the returned dropdown fields then have no options). With `option_key`, only that dropdown is opened."""
    from bolos.forms import windows

    return windows.scan(hwnd, options, option_key=option_key) if option_key is not None else windows.scan(hwnd, options)


def focus(field: Field) -> tuple[int, int, int, int] | None:
    """Put the keyboard focus in the field, scrolled into view. Returns its screen rectangle afterwards."""
    from bolos.forms import windows

    return windows.focus(field)


def fill(field: Field, value: str) -> bool:
    """Put `value` into a text or date field; True only when the control is verified to hold it."""
    from bolos.forms import windows

    return windows.fill(field, value)


def choose(field: Field, option: str) -> bool:
    """Select `option` in a dropdown or radio group; True when verified selected."""
    from bolos.forms import windows

    return windows.choose(field, option)


def check_box(field: Field, on: bool) -> bool:
    """Tick (on=True) or untick a checkbox; True when verified."""
    from bolos.forms import windows

    return windows.check_box(field, on)


def press(form: Form, button_name: str) -> bool:
    """Invoke the form's button whose label matches exactly. Callers confirm with the user first."""
    from bolos.forms import windows

    return windows.press(form, button_name)


def errors(hwnd: int | None = None) -> list[str]:
    """The page's current error messages (alerts, then "label: message" per invalid field)."""
    from bolos.forms import windows

    return windows.errors(hwnd)


def browser_url(hwnd: int | None = None) -> str | None:
    """The full URL of the page in a Chrome/Edge window, or None for other windows."""
    from bolos.forms import windows

    return windows.browser_url(hwnd)
