"""End-to-end check of guided form filling in a real browser, in seven languages.

For each language this serves docs/demo on 127.0.0.1, opens the bank re-KYC form (the Hindi page for hi, the
English page otherwise) in a private Chrome or Edge it starts itself (temporary profile, closed afterwards: the
user's own browser is never touched), and runs bolos.forms.session's guided filling against it. A scripted user
answers every question the way Prisma (format=transliterate) writes speech: Latin letters, code-mixed, numbers
as spoken words. The first answer is the natural one; when the session rejects it, or the box on screen shows
something else afterwards (the user says "galat"), the user repeats it in a plain canonical form, and after that
says skip. At the end the page is scanned again through UI Automation and every field's value is compared with
what the user meant.

    uv run python eval/forms_e2e.py                    # Chrome, hi ta kn te mr bn en
    uv run python eval/forms_e2e.py --browser edge --langs hi,en --json out.json

The session is bolos.forms.session.FormSession (owned by the forms-logic work) with its own FieldsBackend, pinned
to the private window by a thin adapter (run_session). Answers are invented test data; nothing is spoken aloud
and no network call is made.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bolos.forms import fields as F  # noqa: E402
from bolos.forms.prompts import PROMPTS  # noqa: E402
from docs.demo.browser import bring_to_front, find_browser, private_browser  # noqa: E402
from docs.demo.serve import start  # noqa: E402

LANGS = ("hi", "ta", "kn", "te", "mr", "bn", "en")

# ---- the user's (invented) details ------------------------------------------------------------------------------


def _verhoeff_check(number: str) -> str:
    d = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
         (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
         (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
         (9, 8, 7, 6, 5, 4, 3, 2, 1, 0))
    p = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
         (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
         (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8))
    inv = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)
    c = 0
    for i, n in enumerate(reversed(number)):
        c = d[c][p[(i + 1) % 8][int(n)]]
    return str(inv[c])


AADHAAR = "23412341234" + _verhoeff_check("23412341234")  # a checksum-valid, invented number
MOBILE, PIN, ACCOUNT = "9876543210", "570001", "001234567890"

# What the user means, per concept. Option values differ between the English and the Hindi page.
EXPECTED = {
    "name": "Asha Kumari", "father": "Ramesh Kumar", "dob": "1985-08-15", "mobile": MOBILE,
    "email": "asha.kumari@gmail.com", "aadhaar": AADHAAR, "pan": "ABCPE1234F", "ckyc": "",
    "address": "12 MG Road, Gandhi Nagar", "city": "Mysuru", "pin": PIN, "same_address": "true",
    "account": ACCOUNT, "ifsc": "SBIN0001234", "nominee": "Ravi Kumar", "declare": "true",
    "gender": "Female", "marital": "Married", "state": "Karnataka", "occupation": "Farmer",
    "income": "1 to 5 lakh", "pep": "No",
}
EXPECTED_HI = {**EXPECTED, "gender": "महिला", "marital": "विवाहित", "state": "कर्नाटक", "occupation": "किसान",
               "income": "1 से 5 लाख", "pep": "नहीं"}

# Labels as the pages show them (UIA names without the "*" marker) -> concept.
CONCEPTS = {
    "Full name (as on Aadhaar)": "name", "Father's / Spouse's name": "father", "Date of birth": "dob",
    "Gender": "gender", "Marital status": "marital", "Mobile number": "mobile", "Email address": "email",
    "Aadhaar number": "aadhaar", "PAN": "pan", "CKYC number (optional)": "ckyc", "Permanent address": "address",
    "City / Town / Village": "city", "State": "state", "PIN code": "pin",
    "Correspondence address is the same as permanent address": "same_address",
    "Savings account number": "account", "IFSC code": "ifsc", "Occupation": "occupation",
    "Annual income": "income", "Nominee name (optional)": "nominee",
    "Are you a politically exposed person (PEP)?": "pep",
    "I declare that the information given above is true": "declare",
    "पूरा नाम (आधार के अनुसार)": "name", "पिता / पति या पत्नी का नाम": "father", "जन्म तिथि": "dob", "लिंग": "gender",
    "वैवाहिक स्थिति": "marital", "मोबाइल नंबर": "mobile", "ईमेल पता": "email", "आधार नंबर": "aadhaar",
    "पैन नंबर": "pan", "सीकेवाईसी नंबर (अगर हो)": "ckyc", "स्थायी पता": "address", "शहर / कस्बा / गाँव": "city",
    "राज्य": "state", "पिन कोड": "pin", "पत्राचार का पता स्थायी पते जैसा ही है": "same_address",
    "बचत खाता संख्या": "account", "आईएफएससी कोड": "ifsc", "व्यवसाय": "occupation", "सालाना आय": "income",
    "नॉमिनी का नाम (अगर चाहें)": "nominee", "क्या आप राजनीतिक रूप से जुड़े व्यक्ति (पीईपी) हैं?": "pep",
    "मैं घोषणा करता/करती हूँ कि ऊपर दी गई जानकारी सही है": "declare",
}

# Spoken digits 0-9 as Prisma transliterates them.
DIGITS = {
    "hi": "shunya ek do teen chaar paanch chhe saat aath nau",
    "ta": "poojyam onnu rendu moonu naalu anju aaru ezhu ettu ombodhu",
    "kn": "sonne ondu eradu mooru naalku aidu aaru elu entu ombattu",
    "te": "sunna okati rendu moodu naalugu aidu aaru edu enimidi tommidi",
    "mr": "shunya ek don teen chaar paach saha saat aath nau",
    "bn": "shunno ek dui tin char panch chhoy saat aat noy",
    "en": "zero one two three four five six seven eight nine",
}
YES = {"hi": "haan", "ta": "aamaa", "kn": "howdu", "te": "avunu", "mr": "ho", "bn": "hyan", "en": "yes"}
NO = {"hi": "nahi", "ta": "illai", "kn": "illa", "te": "ledu", "mr": "nahi", "bn": "na", "en": "no"}
BACK = {"hi": "galat", "ta": "tappu", "kn": "tappu", "te": "tappu", "mr": "chuk", "bn": "bhul", "en": "go back"}
SKIP = {"hi": "chhodo", "ta": "vittudu", "kn": "bidi", "te": "vadilesey", "mr": "soda", "bn": "baad dao",
        "en": "skip it"}


def spoken(number: str, lang: str) -> str:
    words = DIGITS[lang].split()
    return " ".join(words[int(c)] if c.isdigit() else c.lower() for c in number)


# Natural answers per language (first try). Missing concepts use the shared/default form.
NATURAL = {
    "hi": {"dob": "pandrah august unnis sau pachasi", "gender": "mahila", "marital": "shaadishuda",
           "state": "karnataka", "occupation": "kisaan", "income": "ek se paanch lakh", "pep": "nahi",
           "email": "asha dot kumari at the rate gmail dot com", "pan": "a b c p e ek do teen chaar f",
           "ifsc": "s b i n shunya shunya shunya ek do teen chaar"},
    "ta": {"dob": "padhinanju august aayirathu thollaayirathu enbathu anju", "gender": "pen",
           "marital": "kalyanam aagiduchu", "state": "karnataka", "occupation": "vivasayi",
           "income": "oru lakh la irundhu anju lakh", "pep": "illai",
           "email": "asha dot kumari at gmail dot com", "pan": "a b c p e onnu rendu moonu naalu f",
           "ifsc": "s b i n poojyam poojyam poojyam onnu rendu moonu naalu"},
    "kn": {"dob": "hadinaidu august saavirada ombainooru embattaidu", "gender": "hennu",
           "marital": "maduve aagide", "state": "karnataka", "occupation": "raitha",
           "income": "ondu inda aidu lakh", "pep": "illa", "email": "asha dot kumari at gmail dot com",
           "pan": "a b c p e ondu eradu mooru naalku f",
           "ifsc": "s b i n sonne sonne sonne ondu eradu mooru naalku"},
    "te": {"dob": "padihenu august pandommidi vandala enabhai aidu", "gender": "stree",
           "marital": "pelli ayyindi", "state": "karnataka", "occupation": "raithu",
           "income": "okati nunchi aidu lakshalu", "pep": "ledu", "email": "asha dot kumari at gmail dot com",
           "pan": "a b c p e okati rendu moodu naalugu f",
           "ifsc": "s b i n sunna sunna sunna okati rendu moodu naalugu"},
    "mr": {"dob": "pandhra august ekonishe pachyaaenshi", "gender": "stree", "marital": "vivahit",
           "state": "karnataka", "occupation": "shetkari", "income": "ek te paach lakh", "pep": "nahi",
           "email": "asha dot kumari at the rate gmail dot com", "pan": "a b c p e ek don teen chaar f",
           "ifsc": "s b i n shunya shunya shunya ek don teen chaar"},
    "bn": {"dob": "ponero august unish sho pochashi", "gender": "mohila", "marital": "bibahito",
           "state": "karnataka", "occupation": "krishok", "income": "ek theke panch lakh", "pep": "na",
           "email": "asha dot kumari at the rate gmail dot com", "pan": "a b c p e ek dui tin char f",
           "ifsc": "s b i n shunno shunno shunno ek dui tin char"},
    "en": {"dob": "fifteenth august nineteen eighty five", "gender": "female", "marital": "married",
           "state": "karnataka", "occupation": "farmer", "income": "one to five lakh", "pep": "no",
           "email": "asha dot kumari at gmail dot com", "pan": "a b c p e one two three four f",
           "ifsc": "s b i n zero zero zero one two three four"},
}


def answers(concept: str, lang: str, expected: dict) -> list[str]:
    """What the user says for a concept: [natural, canonical fallback]. The fallback is what a careful user
    says after being asked again; for choices it is the option exactly as shown (they read it off the screen)."""
    nat = NATURAL[lang]
    canonical = {
        "name": "Asha Kumari", "father": "Ramesh Kumar", "nominee": "Ravi Kumar", "city": "Mysuru",
        "address": "12 MG Road, Gandhi Nagar", "dob": "15/08/1985", "mobile": MOBILE, "aadhaar": AADHAAR,
        "pin": PIN, "account": ACCOUNT, "email": "asha.kumari@gmail.com", "pan": expected["pan"],
        "ifsc": "SBIN0001234", "same_address": YES[lang], "declare": YES[lang], "ckyc": "skip",
    }
    natural = {
        "mobile": spoken(MOBILE, lang), "aadhaar": spoken(AADHAAR, lang), "pin": spoken(PIN, lang),
        "account": spoken(ACCOUNT, lang), "ckyc": SKIP[lang], **nat,
    }
    if concept in ("gender", "marital", "state", "occupation", "income", "pep"):
        canonical[concept] = expected[concept]
    first = natural.get(concept, canonical[concept])
    return list(dict.fromkeys([first, canonical[concept]]))


# ---- recognising what the session said ---------------------------------------------------------------------------

def _template_regex(template: str) -> re.Pattern:
    parts = re.split(r"(\{\w+\})", template)
    body = "".join(f"(?P<{p[1:-1]}>.+?)" if re.fullmatch(r"\{\w+\}", p) else re.escape(p) for p in parts)
    return re.compile(body, re.S)


def prompt_key(text: str, lang: str) -> tuple[str | None, dict]:
    """The prompts.py key whose template best explains `text` (longest fixed part), with its placeholders."""
    best, best_len, groups = None, 0, {}
    for key, template in PROMPTS.get(lang, PROMPTS["en"]).items():
        fixed = len(re.sub(r"\{\w+\}", "", template))
        if fixed <= best_len or fixed < 4:
            continue
        match = _template_regex(template).search(text)
        if match:
            best, best_len, groups = key, fixed, match.groupdict()
    return best, groups


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFC", text or "").casefold())


@dataclass
class FieldLog:
    concept: str
    label: str
    said: list = field(default_factory=list)
    corrected: int = 0  # times the user saw a wrong value in the box and said "galat"
    invalid: int = 0
    fill_failed: int = 0


# What the session says that is not a question about the highlighted field.
_NOT_QUESTIONS = {"consent", "how", "remember", "continue_anyway", "type_yourself", "submit", "secrets", "correct",
                  "back_none", "not_entered", "page_errors", "page_error_raw", "left_required", "incomplete",
                  "skipped", "filled", "submitted", "changed"}


class ScriptedUser:
    """The person in front of the form: hears the question, sees the ringed box, answers the way Prisma writes
    speech, and looks at the box afterwards. A wrong value in the box gets "galat" (go back) and the answer
    again in a plain canonical form; after that, skip."""

    def __init__(self, lang: str, expected: dict, hwnd: int):
        self.lang, self.expected, self.hwnd = lang, expected, hwnd
        self.spoken: list[tuple[str, str | None, dict]] = []  # (text, prompt key, placeholders)
        self.logs: dict[str, FieldLog] = {}
        self.current: FieldLog | None = None  # the field the session has ringed
        self.answered: FieldLog | None = None  # the last field the user answered, checked on screen next
        self.unknown: list[str] = []
        self.replies = 0
        self.before_submit = None  # the page as scanned when the session asked to submit
        self.warned = False
        self.dialogue: list[str] = []

    # the session's highlight: which field is being asked
    def show(self, rect, label=""):
        label = re.sub(r"\s+", " ", label or "").strip()
        self.current = self.logs.setdefault(label, FieldLog(CONCEPTS.get(label, "?"), label))
        self.dialogue.append(f"  [ring: {label}]")

    def hide(self):
        pass

    # speech out
    def speak(self, text, language=None, *_, **__):
        key, groups = prompt_key(text, self.lang)
        self.spoken.append((text, key, groups))
        self.dialogue.append(f"  bolos [{key}]: {text}")
        if key in ("invalid", "format") and self.current:
            self.current.invalid += 1
        elif key == "not_entered" and self.current:
            self.current.fill_failed += 1
        return True

    # speech in
    def listen(self, *_, **__) -> str | None:
        self.replies += 1
        if self.replies > 400:
            return None  # a looping session: stop it
        text, key, _ = self.spoken[-1] if self.spoken else ("", None, {})
        if key == "remember":
            return NO[self.lang]  # never store test data in a profile
        if key == "continue_anyway":
            self.warned = True
            return YES[self.lang]  # the local demo bank page: the warning is expected, carry on
        if key == "type_yourself":
            return YES[self.lang]  # "done": the user handles CAPTCHA, OTP and upload themselves
        if key == "submit":
            self.before_submit = F.scan(self.hwnd)  # verify what was entered before the page goes away
            return YES[self.lang]
        if key in ("consent", "how"):
            return YES[self.lang]
        if key in _NOT_QUESTIONS or self.current is None:
            self.unknown.append(text)
            return YES[self.lang] if text.rstrip().endswith("?") else None
        # A question about the ringed field. First, a glance at the box answered last.
        if self.answered is not None and self.answered is not self.current and self._wrong(self.answered):
            self.answered.corrected += 1
            self.answered = None
            return BACK[self.lang]
        log = self.current
        if log.concept == "?":
            return "skip"
        options = answers(log.concept, self.lang, self.expected)
        reply = options[len(log.said)] if len(log.said) < len(options) else "skip"
        log.said.append(reply)
        self.answered = log if reply != "skip" else None
        return reply

    def listen_private(self, *_, **__):
        heard = self.listen()
        self.dialogue.append(f"  user: {heard}")
        return (heard, self.lang) if heard is not None else None

    def _wrong(self, log: FieldLog) -> bool:
        """Whether the box for `log` shows something other than what the user meant (and they may still fix it)."""
        if log.corrected or len(log.said) >= len(answers(log.concept, self.lang, self.expected)):
            return False
        want = self.expected.get(log.concept, "")
        got = next((f.value for f in F.scan(self.hwnd, options=False).fields if f.label == log.label), None)
        return bool(want) and not _same(log.concept, got, want)

    # the fields API bound to our own browser window (for a session that asks its io for it)
    def scan(self, hwnd=None):
        return F.scan(self.hwnd)

    def errors(self, hwnd=None):
        return F.errors(self.hwnd)

    def browser_url(self, hwnd=None):
        return F.browser_url(self.hwnd)

    fill, choose, check_box, press = staticmethod(F.fill), staticmethod(F.choose), staticmethod(F.check_box), \
        staticmethod(F.press)


def run_session(hwnd: int, user: ScriptedUser, lang: str) -> dict:
    """Run bolos.forms.session's guided filling on our own window. The session's FieldsBackend scans the
    foreground window when it starts; this thin adapter pins that scan to the private browser window instead
    (everything else, rescans and changes, already goes to form.hwnd)."""
    from bolos.forms import session

    class PinnedBackend(session.FieldsBackend):
        def scan(self):
            return F.scan(hwnd)

    backend = PinnedBackend()
    form = backend.scan()
    if not any(f.label in CONCEPTS for f in form.fields):
        raise RuntimeError("No expected form fields were found in the browser")
    result = session.FormSession(form, user, backend, lang, highlight=user).run()
    return {"api": "FormSession", "result": result}


# ---- one language ------------------------------------------------------------------------------------------------

@dataclass
class Row:
    lang: str
    browser: str
    page: str
    api: str = ""
    answerable: int = 0
    verified: int = 0
    first_try: int = 0
    fallback: int = 0
    skipped_by_user: int = 0
    mismatches: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    submitted: bool = False
    seconds: float = 0.0
    unknown_prompts: list = field(default_factory=list)
    session: dict = field(default_factory=dict)
    dialogue: list = field(default_factory=list)
    error: str = ""


def _same(concept: str, got: str | None, want: str) -> bool:
    if concept in ("mobile", "aadhaar", "pin", "account"):
        return re.sub(r"\D", "", got or "") == want
    if concept in ("pan", "ifsc"):
        return (got or "").replace(" ", "").upper() == want
    return (got or "").strip() == want


def run_language(lang: str, browser: str, base: str) -> Row:
    page = "kyc_hindi.html" if lang == "hi" else "kyc_bank.html"
    expected = EXPECTED_HI if lang == "hi" else EXPECTED
    row = Row(lang, browser, page)
    started = time.monotonic()
    with private_browser(base + page, browser) as (hwnd, _):
        bring_to_front(hwnd)  # date segments are typed through UIA focus + posted keys to this window
        user = ScriptedUser(lang, expected, hwnd)
        try:
            outcome = run_session(hwnd, user, lang)
            row.api = outcome["api"]
            result = outcome["result"]
            row.session = {"ok": result.ok, "filled": len(result.filled), "skipped": result.skipped,
                           "user_must": result.user_must, "errors": result.errors, "stopped": result.stopped,
                           "submitted": result.submitted, "safety_warning": user.warned}
        except Exception as exc:  # report, do not hide: the table shows which language broke
            row.error = f"{type(exc).__name__}: {exc}"
        after = F.scan(hwnd)
        row.submitted = (after.url or "").endswith("/submit")
        row.page_errors = after.errors
        checked = user.before_submit or after
        by_label = {f.label: f for f in checked.fields}
        detected = {CONCEPTS[label] for label in by_label if label in CONCEPTS}
        if missing := expected.keys() - detected:
            row.error = row.error or f"Missing expected fixture fields: {', '.join(sorted(missing))}"
        if row.submitted or row.session.get("submitted"):
            row.error = row.error or "The form submitted with manual fields pending"
        for label, concept in CONCEPTS.items():
            if label not in by_label:
                continue
            want = expected[concept]
            if concept == "ckyc":
                continue
            if concept == "declare":
                if by_label[label].value != "false" or label not in row.session.get("user_must", []):
                    row.error = row.error or "The declaration was not left for the user"
                continue
            row.answerable += 1
            log = user.logs.get(label)
            got = by_label[label].value
            if _same(concept, got, want):
                row.verified += 1
            else:
                row.mismatches.append({"label": label, "concept": concept, "got": got, "want": want,
                                       "said": log.said if log else None,
                                       "corrected": log.corrected if log else None,
                                       "invalid": log.invalid if log else None,
                                       "fill_failed": log.fill_failed if log else None})
            if log and log.said:
                if len(log.said) == 1 and _same(concept, got, want):
                    row.first_try += 1
                elif len(log.said) > 1 and log.said[-1] != "skip":
                    row.fallback += 1
                if log.said[-1] == "skip":
                    row.skipped_by_user += 1
        row.unknown_prompts = user.unknown[:10]
        row.dialogue = user.dialogue
    row.seconds = round(time.monotonic() - started, 1)
    return row


def table(rows: list[Row]) -> str:
    lines = ["| lang | browser | API | verified | first try | after re-ask | user skipped | submitted | s |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.lang} | {r.browser} | {r.api or '-'} | {r.verified}/{r.answerable} | {r.first_try} | "
                     f"{r.fallback} | {r.skipped_by_user} | {'yes' if r.submitted else 'no'} | {r.seconds} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--langs", default=",".join(LANGS))
    parser.add_argument("--json", help="write the full per-language results here")
    parser.add_argument("--dialogue", action="store_true", help="print what the session said and heard")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Hindi labels in the report on a cp1252 Windows console
    if find_browser(args.browser) is None:
        print(f"{args.browser} is not installed", file=sys.stderr)
        return 2
    server, base = start()
    rows = []
    try:
        for lang in args.langs.split(","):
            row = run_language(lang.strip(), args.browser, base)
            rows.append(row)
            print(f"{row.lang}: {row.verified}/{row.answerable} verified, first try {row.first_try}, "
                  f"re-asked {row.fallback}, skipped {row.skipped_by_user}, {row.seconds} s {row.error}", flush=True)
            for m in row.mismatches:
                print("   mismatch", json.dumps(m, ensure_ascii=False), flush=True)
            print("   session", json.dumps(row.session, ensure_ascii=False), flush=True)
            if args.dialogue:
                print("\n".join(row.dialogue), flush=True)
    finally:
        server.shutdown()
        server.server_close()
    print()
    print(table(rows))
    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    return 0 if all(r.answerable > 0 and r.verified == r.answerable and not r.error for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
