"""Spoken form filling that feels like dictation. Answers never enter the command parser, System 2, learning or
history.

FormAssistant is what the pipeline calls (explain, fill, check_site, upload, forget_profile, fraud_help).
FormSession is the filling dialogue: field by field in screen order it puts the cursor in the field, rings it on
screen, asks in the user's language ("अपना पूरा नाम बताइए।"), and fills the box the moment the answer is heard,
written the way forms expect (bolos.forms.normalize and bolos.forms.indian). "Galat" goes back to the previous
field. At the end it explains page errors and submits only on a separate clear yes. OTPs, PINs, CVVs, passwords,
card numbers and CAPTCHAs are never asked for or filled.

Privacy: answers are heard only through voice.listen_private (never logged) and are never spoken back: the
value is visible in the box on screen. Values never appear in status events, outcomes or exceptions.
"""

import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from bolos.executor import Outcome
from bolos.forms import explain as explainer
from bolos.forms import indian, kinds, safety
from bolos.forms.intent import forget_request, fraud_report
from bolos.forms.normalize import _ORDINAL, InvalidValue, normalize, real_options
from bolos.forms.prompts import prompt
from bolos.forms.validate import reason_text
from bolos.forms.windows import FormChanged, UnsupportedField
from bolos.responses import is_no, is_yes


def _phrases(*groups: str) -> frozenset[str]:
    return frozenset(p.strip() for g in groups for p in g.split("|") if p.strip())


# A search or find box holds a query, not personal details, and is never part of the form to fill.
_SEARCH_LABEL = re.compile(r"\b(search|find|khoj|dhundh)\w*|खोज|ढूंढ", re.IGNORECASE)

_STOP = _phrases(
    "cancel|stop|stop it|band karo|bandh karo|bas|bas karo|ruko|rehne do|chhodo sab|nillisu|saaku|niruthu|podhum|"
    "aapu|aapandi|thamba|thambva|nirthu|mathi|thamo|bondho koro|roko|bas kar|ruk jao",
    "कैंसल|रुको|रहने दो|बंद करो|बस|ಬೇಡ|ನಿಲ್ಲಿಸು|ಸಾಕು|வேண்டாம்|நிறுத்து|போதும்|ఆపు|ఆపండి|थांबा|बस्स|നിർത്തുക|മതി|"
    "থামো|বন্ধ করো|રોકો|બંધ કરો|ਰੁਕੋ|ਬੰਦ ਕਰੋ|ਬਸ")
_SKIP = _phrases(
    "skip|skip it|leave it|next|chhodo|chhod do|chod do|baad mein|baad me|aage|aage badho|bidi|bittu bidi|"
    "mundhe|vidu|vittu|adutha|vadiley|vadileyi|tarvata|soda|sodun dya|nantar|vidu|vittekku|adutthathu|baad dao|"
    "chhere dao|pore|chhodi do|pachhi|chhaddo|baad vich|agla",
    "छोड़ो|छोड़ दो|बाद में|आगे|स्किप|ಬಿಡಿ|ಮುಂದೆ|விடு|அடுத்து|వదిలేయ్|తర్వాత|सोडा|नंतर|വിടുക|അടുത്തത്|"
    "বাদ দাও|পরে|છોડો|પછી|ਛੱਡੋ|ਬਾਅਦ ਵਿੱਚ")
_BACK = _phrases(
    "back|go back|previous|previous one|wrong|i made a mistake|galat bhara|galat bhar diya|galat|pichla|"
    "pichhla|peeche|pichhe|pichla wala|hinde|hindina|tappu|munthaiya|munnadi|pinnadi|tappu|venakki|"
    "mundhu|tappu ayindi|maage|mage|chuk|chukla|chuklo|pinnil|thettu|thetti|aager ta|bhul|pachhu|pachhal|khotu|"
    "pichhe|galti|galti ho gayi",
    "पिछला|पीछे|गलत|गलत भरा|गलती|ಹಿಂದೆ|ಹಿಂದಿನದು|ತಪ್ಪು|முந்தைய|பின்னால்|தப்பு|వెనక్కి|ముందుది|తప్పు|मागे|मागील|चूक|"
    "പിന്നിലേക്ക്|മുമ്പത്തെ|തെറ്റ്|আগেরটা|পিছনে|ভুল|પાછળ|પાછલું|ખોટું|ਪਿੱਛੇ|ਪਿਛਲਾ|ਗਲਤ")
_REPEAT = _phrases(
    "repeat|again|say again|phir se|phir se bolo|dobara|dubara|kya|kya bola|mathe|mathe heli|thirumba|"
    "thirumba sollu|malli|malli cheppu|punha|punha sanga|veendum|veendum parayu|abar|abar bolo|fari|fari bolo|"
    "pher|pher dasso",
    "फिर से|दोबारा|ಮತ್ತೆ|ಮತ್ತೆ ಹೇಳಿ|மீண்டும்|திரும்ப சொல்லு|మళ్ళీ|మళ్ళీ చెప్పు|पुन्हा|पुन्हा सांगा|വീണ്ടും|"
    "আবার|আবার বলো|ફરી|ફરી બોલો|ਦੁਬਾਰਾ|ਫੇਰ")
_EXPLAIN = _phrases(
    "explain|what is this|what does this mean|help|meaning|samjhao|samjhaiye|yeh kya hai|ye kya hai|matlab|"
    "iska matlab|vivarisu|enu idu|idu enu|vilakku|vilakkam|idhu enna|enna idhu|vivarinchu|idi enti|enti idi|"
    "samjav|samjava|he kay aahe|vishadeekarikku|ithu enthaanu|enthaanu|bujhiye bolo|eta ki|samjavo|aa shu che|"
    "shu che|samjhao ji|eh ki hai|ki matlab",
    "समझाओ|ये क्या है|यह क्या है|मतलब|ವಿವರಿಸು|ವಿವರಿಸಿ|ಇದು ಏನು|விளக்கம்|விளக்கு|இது என்ன|వివరించు|ఇది ఏమిటి|"
    "समजवा|हे काय आहे|വിശദീകരണം|ഇത് എന്താണ്|বুঝিয়ে বলো|এটা কী|સમજાવો|આ શું છે|ਸਮਝਾਓ|ਇਹ ਕੀ ਹੈ")
_SAVED = _phrases(
    "use saved|saved|saved one|use my saved|from profile|mera wala|mera wala bhar do|mera vala|save wala|"
    "save vala|jo save hai|profile se|saved wala|nanna du|nannadu|ulisiddu|ennoda|ennodathu|semithathu|naadi|"
    "save chesindi|maza|majha|jatan kelela|ente|sookshichathu|amar ta|amarta|save kora|maru|maaru|saachvelu|"
    "mera|saved vala|ho jo save hai",
    "सेव वाला|मेरा वाला|मेरा|ಉಳಿಸಿದ್ದು|ನನ್ನದು|சேமித்தது|என்னுடையது|సేవ్ చేసింది|నాది|जतन केलेले|माझे|സൂക്ഷിച്ചത്|"
    "എന്റേത്|সেভ করাটা|আমারটা|સાચવેલું|મારું|ਸੇਵ ਵਾਲਾ|ਮੇਰਾ")
_DONE = _phrases(
    "done|finished|i have done it|ho gaya|ho gya|kar diya|bhar diya|aaytu|aayitu|mugitu|mudinjathu|mudinchidu|"
    "mudinjadhu|aipoyindi|ayipoyindi|zhala|jhala|kazhinju|hoye geche|hoyeche|thai gayu|thayu|ho gaya ji|"
    "ho gya ji|bhar ditta|ho gaya hai",
    "हो गया|भर दिया|ಆಯ್ತು|ಮುಗಿಯಿತು|முடிந்தது|ముగిసింది|అయిపోయింది|झाले|झालं|കഴിഞ്ഞു|হয়ে গেছে|થઈ ગયું|ਹੋ ਗਿਆ")
_NO_MIDDLE_NAME = _phrases(
    "none|no|no middle name|i have no middle name|i don't have one|i dont have one|"
    "nahi|nahin|nahi hai|middle name nahi hai|"
    "नहीं|नहीं है|मेरा बीच का नाम नहीं है|ಇಲ್ಲ|இல்லை|లేదు|नाही|ഇല്ല|নেই|નથી|ਨਹੀਂ")
_MORE = _phrases(
    "more|tell more|details|all|aur|aur batao|sab|sab batao|innu|innu heli|innum|innum sollu|inka|inka cheppu|"
    "aankhi|anakhi|aankhi sanga|innum parayu|kooduthal|aro|aro bolo|vadhu|vadhu kaho|hor|hor dasso|sabh",
    "और|और बताओ|सब|ಇನ್ನೂ|ಇನ್ನೂ ಹೇಳು|மேலும்|இன்னும்|ఇంకా|ఇంకా చెప్పు|आणखी|आणखी सांगा|കൂടുതൽ|আরও|আরও বলো|વધુ|"
    "વધુ કહો|ਹੋਰ|ਹੋਰ ਦੱਸੋ")
_SUBMIT_BUTTON = re.compile(r"^(submit|submit form|submit application|save and submit|send application|apply|"
                            r"जमा करें|सबमिट|सबमिट करें|ಸಲ್ಲಿಸಿ|சமர்ப்பி|సమర్పించు|जमा करा|സമർപ്പിക്കുക|জমা দিন|"
                            r"સબમિટ|ਜਮ੍ਹਾਂ ਕਰੋ)$", re.IGNORECASE)
_MONEY_BUTTON = re.compile(r"pay|payment|transfer|send money|buy|purchase|order|भुगतान|पे |ಪಾವತಿ|செலுத்து", re.I)
_MAX_TRIES = 3


def _key(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.!?।,]+", " ", (text or "").casefold())).strip()


def command_of(text: str | None) -> str | None:
    """A session control word said instead of an answer, or None for an answer."""
    key = _key(text)
    if not key:
        return None
    if fraud_report(key):
        return "fraud"
    if forget_request(key):
        return "forget"
    for name, phrases in (("stop", _STOP), ("skip", _SKIP), ("back", _BACK), ("repeat", _REPEAT),
                          ("explain", _EXPLAIN), ("saved", _SAVED)):
        if key in phrases:
            return name
    return None


class FieldsBackend:
    """bolos.forms.fields (UI Automation) with a page-change check before every change.

    Each change rescans the form's own window (not the foreground one, so typed mode works) and uses the
    freshly found control: if the page navigated or the field is gone, FormChanged stops the session."""

    def scan(self):
        from bolos.forms import fields

        return fields.scan()

    def scan_for_filling(self):
        from bolos.forms import fields

        return fields.scan(options=False)

    def peek(self):
        """Inspect the window used for screen context without opening any dropdown."""
        from bolos.forms import fields
        from bolos.guide.screen import user_window

        return fields.scan(user_window(), options=False)

    def rescan(self, form, *, option_key: str | None = None):
        from bolos.forms import fields

        fresh = (fields.scan(form.hwnd, options=False) if option_key is None
                 else fields.scan(form.hwnd, option_key=option_key))
        if (fresh.hwnd, fresh.url, fresh.title) != (form.hwnd, form.url, form.title):
            raise FormChanged("The page changed")
        if not {f.key for f in form.fields}.intersection(f.key for f in fresh.fields):
            raise FormChanged("The form is no longer on the page")
        return fresh

    def _current(self, form, target):
        fresh = self.rescan(form)
        match = next((f for f in fresh.fields if f.key == target.key and f.label == target.label
                      and f.control_type == target.control_type), None)
        if match is None:
            raise FormChanged("The field is no longer on the page")
        return fresh, replace(match, options=target.options or match.options)

    def refresh_target(self, form, target):
        """Find the field by identity before asking. Earlier choices can change its label and options."""
        if target.control_type == "ComboBox" and not target.protected:
            fresh = self.rescan(form, option_key=target.key)
        else:
            fresh = self.rescan(form)
        return next((f for f in fresh.fields if f.key == target.key and f.control_type == target.control_type), None)

    def focus(self, form, target):
        """Put the cursor in the field (scrolling it into view) and return where it is on screen now."""
        from bolos.forms import fields

        _, current = self._current(form, target)
        return fields.focus(current)

    def fill(self, form, target, value: str) -> bool:
        from bolos.forms import fields

        _, current = self._current(form, target)
        if current.control_type == "CheckBox":
            return fields.check_box(current, value == "true")
        if current.control_type in {"ComboBox", "RadioButton"}:
            # A browser's dropdown list sometimes opens late or closes early (seen about once in 15 live runs):
            # one more try on the freshly found control before telling the user to pick it themselves.
            return fields.choose(current, value) or fields.choose(self._current(form, target)[1], value)
        return fields.fill(current, value)

    def press(self, form, button: str) -> bool:
        from bolos.forms import fields

        fresh = self.rescan(form)
        if fresh.errors or fresh.fields != form.fields or any(
                before.value != after.value for before, after in zip(form.fields, fresh.fields, strict=True)):
            raise FormChanged("The form changed after review")
        return fields.press(fresh, button)

    def errors(self, form) -> list[str]:
        return self.rescan(form).errors

    def url(self, form) -> str | None:
        from bolos.forms import fields

        return getattr(form, "url", None) or fields.browser_url(getattr(form, "hwnd", None))


@dataclass
class SessionResult:
    """What happened, by field LABEL only. Values are never kept here."""

    ok: bool = True
    filled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    user_must: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    submitted: bool = False
    stopped: bool = False


class _Stop(Exception):
    def __init__(self, ok=True, key="filled"):
        super().__init__(key)
        self.ok, self.key = ok, key


def _form_url_identity(url: str | None) -> tuple | None:
    try:
        parts = urlsplit(url or "")
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        return parts.scheme, parts.hostname, port, parts.path
    except ValueError:
        return None


def _matches_application(form, expected_url: str) -> bool:
    from bolos.guide.planner import is_pan_application, is_pan_application_form

    if is_pan_application(expected_url):
        return is_pan_application(form.url) and is_pan_application_form(form)
    expected = _form_url_identity(expected_url)
    return expected is not None and _form_url_identity(form.url) == expected


class ConsentFile:
    """Remembers, on this PC, that the user agreed to online speech recognition for form answers, so the
    question (and the how-to that follows it) is asked once rather than at every form."""

    def __init__(self, path):
        from pathlib import Path

        self.path = Path(path)

    def given(self) -> bool:
        return self.path.exists()

    def remember(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError:
            pass  # asked again next time; never a reason to stop filling


class FieldHighlight:
    """The orange ring around the field being asked (bolos.guide.pointer), and the buddy flying to it with the
    field's name when the buddy runs. Windows only; a session without one simply does not highlight."""

    def __init__(self, buddy=None):
        self.buddy = buddy
        self._pointer = None

    def show(self, rect: tuple[int, int, int, int], label: str = "") -> None:
        from bolos.guide.pointer import Pointer
        from bolos.guide.screen import Rect

        if self._pointer is None:
            self._pointer = Pointer(self.buddy)
        self._pointer.show(Rect(*rect), label=label, snug=True)

    def hide(self) -> None:
        pointer, self._pointer = self._pointer, None
        if pointer is not None:
            pointer.hide()
            pointer.close()


# The question for a field, by concept (prompts.py q_*). Place fields ask for the city or the state by label.
_QUESTION = {"name": "q_name", "gender": "q_gender", "mobile": "q_mobile", "email": "q_email", "address": "q_address",
             "postal": "q_postal", "aadhaar": "q_aadhaar", "pan": "q_pan", "ifsc": "q_ifsc", "account": "q_account",
             "occupation": "q_occupation", "income": "q_income"}
_BIRTH = re.compile(r"birth|dob|जन्म|ಜನ್ಮ|ಹುಟ್ಟಿದ|பிறந்த|పుట్టిన|ജനന|জন্ম|જન્મ|ਜਨਮ", re.IGNORECASE)
_NAME_PART = re.compile(r"\b(first|given|middle|last)\s+name\b|\bsurname\b", re.IGNORECASE)
_PAN_TYPE = re.compile(r"\bpan\s+application\s+type\b", re.IGNORECASE)
_SPOKEN_OPTIONS = 5  # a choice with more options than this (36 states) is answered without a list read out


def _name_part(label: str) -> str | None:
    match = _NAME_PART.search(label or "")
    if match is None:
        return None
    part = (match[1] or "last").lower()
    return "first" if part == "given" else part


def question(target, concept: str, language: str | None) -> str:
    """What to ask for one field: "अपना पूरा नाम बताइए।" for a name, else the page's label as a question."""
    if _is_pan_type(target):
        return prompt("q_pan_application_type", language)
    key = _QUESTION.get(concept)
    part = _name_part(target.label) if concept == "name" else None
    if part:
        key = f"q_{part}_name"
    elif concept == "date" and _BIRTH.search(target.label or ""):
        key = "q_dob"
    elif concept == "place":
        key = "q_state" if indian.is_state_field(target.label) else "q_city"
    text = prompt(key, language) if key else prompt("ask", language, label=target.label)
    if part == "middle" and not target.required:
        text = f"{text} {prompt('skip_if_none', language)}"
    options = real_options(target.options)
    if options and len(options) <= _SPOKEN_OPTIONS and concept != "gender":
        text = f"{text} {', '.join(options)}"
    return text


def _is_pan_type(target) -> bool:
    return target.control_type == "ComboBox" and _PAN_TYPE.search(target.label or "") is not None


def _takes_hints(listen) -> bool:
    import inspect

    try:
        parameters = inspect.signature(listen).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "hints" or p.kind is p.VAR_KEYWORD for p in parameters)


class FormSession:
    """Fills a form the way dictation feels: each field is highlighted and focused, asked in plain words, and
    filled the moment the answer is heard. The value appears in the box on screen; it is never spoken."""

    def __init__(self, form, voice, backend, language: str | None, profile=None, status=lambda *_: None,
                 highlight=None, consent=None, *, ask_consent=True):
        from bolos.guide.planner import is_pan_application

        self.form = form
        self.voice = voice
        self.backend = backend
        self.language = language
        self.profile = profile
        self.status = status
        self.highlight = highlight  # FieldHighlight or None
        self.consent = consent  # ConsentFile or None (asked every time)
        self.ask_consent = ask_consent
        self.result = SessionResult()
        # field key -> (field, concept, value) for this session only; dropped when run() returns
        self._entered: dict[str, tuple] = {}
        self._order: list[str] = []
        self._manual: list = []  # fields the user fills in themselves, in screen order
        self._unanswered_fields = 0
        self._structure_changed = False
        self._pan_registration = is_pan_application(form.url)

    # --- speech -------------------------------------------------------------------------------------------
    def _say(self, key: str, **values) -> None:
        text = prompt(key, self.language, **values)
        self.status("reply", text)
        self._say_text(text)

    def _say_text(self, text: str) -> None:
        if self.voice.speak(text, self.language) is False:
            raise _Stop(False, "")

    def _listen(self, hints=()) -> str | None:
        """A private answer (never logged, never a command). `hints` are words recognition should favour."""
        self.status("state", "listening")
        listen = self.voice.listen_private
        heard = listen(hints=list(hints)) if hints and _takes_hints(listen) else listen()
        return heard[0].strip() if heard and heard[0] and heard[0].strip() else None

    # --- on screen ----------------------------------------------------------------------------------------
    def _show(self, target) -> None:
        """Put the cursor in the field and ring it, so the user sees which box the question is about."""
        self.status("state", "filling")
        rect = getattr(target, "rect", None)
        focus = getattr(self.backend, "focus", None)
        if callable(focus):
            try:
                rect = focus(self.form, target) or rect
            except FormChanged:
                raise
            except Exception:
                pass  # a field that cannot take focus is still asked
        if self.highlight is not None and rect:
            try:
                self.highlight.show(rect, target.label)
            except Exception:
                pass  # the ring is a help, never a reason to stop

    def _hide(self) -> None:
        if self.highlight is not None:
            try:
                self.highlight.hide()
            except Exception:
                pass

    # --- the dialogue -------------------------------------------------------------------------------------
    def run(self) -> SessionResult:
        try:
            self._run()
        except _Stop as stop:
            self.result.ok = stop.ok or self.result.submitted
            self.result.stopped = True
            self._hide()
            if stop.key:
                try:
                    self._say(stop.key)
                except _Stop:
                    self.result.ok = self.result.submitted
        except FormChanged:
            self.result.ok = False
            self._hide()
            try:
                self._say("changed")
            except _Stop:
                pass
        except UnsupportedField:
            self.result.ok = False
            self._hide()
            try:
                self._say("incomplete")
            except _Stop:
                pass
        finally:
            self._hide()
            self._entered.clear()
        return self.result

    def _run(self) -> None:
        self.status("state", "filling")
        if self.ask_consent and (self.consent is None or not self.consent.given()):
            self._say("consent")
            if not is_yes(self._listen() or ""):
                raise _Stop(True, "")
            if self.consent is not None:
                self.consent.remember()
            self._say("how")
        self._safety_first()
        fields = list(self.form.fields)
        self._fill_fields(fields, skip_filled=True)
        self._hide()
        self._let_user_type()
        self._fix_page_errors(fields)
        self._finish()

    def _safety_first(self) -> None:
        try:
            url = self.backend.url(self.form)
        except Exception:
            url = None
        warnings = safety.assess(url, self.form)
        spoken = [w for w in warnings if w.key in safety.DANGER | {"site_money_fields"}]
        for text in safety.messages(spoken, self.language):
            self._say_text(text)
        if safety.dangerous(warnings):
            self._say("continue_anyway")
            if not is_yes(self._listen() or ""):
                raise _Stop(True, "dangerous_stop")

    def _filled_now(self) -> dict[str, str]:
        """Field key -> value on the page now, from a scan that opens no dropdown. The first scan opens every
        dropdown to read its options, and a value read meanwhile can be the highlighted item, not the chosen
        one: a field must not be skipped as answered because of that."""
        rescan = getattr(self.backend, "rescan", None)
        if rescan is None:
            return {f.key: f.value for f in self.form.fields if getattr(f, "value", None)}
        try:
            fresh = rescan(self.form)
        except FormChanged:
            raise
        except Exception:
            return {}
        first = {f.key: f for f in self.form.fields}
        # Unopened, a dropdown reports its placeholder ("-- Select --", "चुनें") as its value: it is answered
        # only when the value is one of its real options.
        return {f.key: f.value for f in fresh.fields if getattr(f, "value", None) and (
            f.control_type != "ComboBox" or f.value in real_options(first.get(f.key, f).options or f.options))}

    def _fill_fields(self, fields: list, skip_filled: bool) -> None:
        refresh = getattr(self.backend, "refresh_target", None)
        filled = self._filled_now() if skip_filled and refresh is None else {}
        positions = {target.key: index for index, target in enumerate(fields)}
        revising = None
        i = 0
        while i < len(fields):
            target = fields[i]
            if refresh is not None:
                current = refresh(self.form, target)
                if current is None:
                    self._structure_changed = True
                    i += 1
                    continue  # earlier selections can hide an inapplicable field
                target = current
                answered = target.value if target.control_type != "ComboBox" else target.value in real_options(
                    target.options)
            else:
                answered = filled.get(target.key)
            concept = kinds.classify(target)
            sens = kinds.sensitivity(concept)
            if self._pan_registration and target.control_type == "CheckBox":
                sens = kinds.MANUAL  # privacy and CAPTCHA remain manual even with incomplete browser labels
            if target.protected or sens in {kinds.NEVER_FILL, kinds.MANUAL} or target.kind in {"secret", "file",
                                                                                                "manual"}:
                if target.label not in self.result.user_must:
                    self.result.user_must.append(target.label)
                    self._manual.append((target, sens))
                i += 1
                continue
            if skip_filled and target.key != revising and answered and not target.error:
                i += 1
                continue
            revising = self._ask(target, concept, positions, offer_saved=skip_filled and target.key != revising)
            if revising is not None:
                i = positions[revising]
                continue
            i += 1

    def _ask(self, target, concept: str, positions: dict[str, int], *, offer_saved: bool) -> str | None:
        """Highlight one field, ask, and fill it with the answer. Returns a field key to go back to."""
        saved = self.profile.get(concept) if self.profile is not None and concept in kinds.PROFILE_CONCEPTS \
            else None
        asked = question(target, concept, self.language)
        pan_options = ", ".join(real_options(target.options)) if _is_pan_type(target) else ""
        optional_middle_name = concept == "name" and not target.required and _name_part(target.label) == "middle"
        hints = indian.keyterms(concept, target.label)
        self._show(target)
        prefilled = False
        if offer_saved and saved and not getattr(target, "value", None):
            try:
                prefilled = self._enter(target, concept, normalize(target, saved, concept))
            except InvalidValue:
                saved = None  # a saved answer that does not fit this field is not offered
        tries = silences = 0
        while tries < _MAX_TRIES:
            self.status("state", "filling")
            text = f"{asked} {prompt('saved_filled', self.language)}" if prefilled else asked
            self.status("reply", f"{text} {pan_options}" if pan_options else text)
            self._say_text(text)
            answer = self._listen(hints)
            if answer is None:
                silences += 1
                if silences < 2:
                    continue  # one silence: ask again
                self._unanswered_fields += 1
                if self._unanswered_fields >= 2:
                    raise _Stop(True, "filled")
                if not prefilled:
                    self._skip(target)
                    self._say("no_answer_skipped")
                return None
            silences = 0
            self._unanswered_fields = 0
            command = command_of(answer)
            if command == "stop":
                raise _Stop(True, "filled")
            if command == "fraud":
                raise _Stop(True, "fraud")
            if command == "forget":
                self._forget()
                continue
            if command == "skip" or (optional_middle_name and _key(answer) in _NO_MIDDLE_NAME):
                if not prefilled:
                    self._skip(target)
                return None
            if command == "back":
                previous = self._go_back(target, positions)
                if previous is not None:
                    return previous
                continue
            if command == "repeat":
                continue
            if command == "explain":
                where = kinds.where_to_find(concept, self.language)
                self._say_text(" ".join(t for t in (kinds.explain(concept, self.language), where, pan_options) if t))
                continue
            if command is None and prefilled and target.kind != "checkbox":
                if is_yes(answer):
                    return None  # keep the saved answer that is already in the box
                if is_no(answer):
                    continue  # 'no' to the saved answer: they will say a new one
            if command == "saved":
                if not saved:
                    self._say("nothing_saved")
                    continue
                answer = saved
            try:
                value = normalize(target, answer, concept)
            except InvalidValue as error:
                self._say_text(reason_text(error.reason, self.language))
                tries += 1
                continue
            if self._enter(target, concept, value):
                return None
            self._say("not_entered")
            self._skip(target)
            return None
        self._skip(target)
        return None

    def _skip(self, target) -> None:
        if target.label not in self.result.skipped:
            self.result.skipped.append(target.label)

    def _enter(self, target, concept: str, value: str) -> bool:
        """Put `value` into the field now. Records it for 'go back', the end review and the profile."""
        self.status("state", "executing")
        if not self.backend.fill(self.form, target, value):
            self.status("state", "filling")
            return False
        self.status("state", "filling")
        self._entered[target.key] = (target, concept, value)
        if target.key in self._order:
            self._order.remove(target.key)
        self._order.append(target.key)
        if target.label in self.result.skipped:
            self.result.skipped.remove(target.label)
        if target.label not in self.result.filled:
            self.result.filled.append(target.label)
        return True

    def _go_back(self, current, positions: dict[str, int]) -> str | None:
        """Ask for a correction without erasing the value already on the page."""
        previous = next((k for k in reversed(self._order) if k in positions and k != current.key), None)
        if previous is None:
            self._say("back_none")
            return None
        target = self._entered.pop(previous)[0]
        self._order.remove(previous)
        if target.label in self.result.filled:
            self.result.filled.remove(target.label)
        if target.label not in self.result.skipped:
            self.result.skipped.append(target.label)
        self._say("correct", label=target.label)
        return previous

    def _forget(self) -> None:
        if self.profile is not None:
            self.profile.forget()
        self._say("forgotten" if self.profile is not None else "nothing_saved")

    def _let_user_type(self) -> None:
        """OTP, CAPTCHA, uploads, declarations: the user does them; wait until they say done (or stop)."""
        if self._pan_registration:
            self._say("review_consent_captcha")
            return
        if not self._manual:
            return
        if (any(kinds.is_consent(target) for target, _ in self._manual)
                and any(kinds.classify(target) == "captcha" for target, _ in self._manual)):
            self._say("review_consent_captcha")
            return  # these controls already prevent submission; let the user finish at their own pace
        if any(sens == kinds.NEVER_FILL for _, sens in self._manual):
            self._say("secrets")
        self._show(self._manual[0][0])
        self._say("type_yourself", labels=", ".join(self.result.user_must))
        reply = self._listen()
        self._hide()
        if reply is None or command_of(reply) == "stop" or not (is_yes(reply) or _key(reply) in _DONE):
            raise _Stop(True, "filled")

    def _fix_page_errors(self, fields: list) -> None:
        messages = self._errors()
        if not messages:
            return
        for text in explainer.page_errors(messages, self.form, self.language):
            self._say_text(text)
        wrong = [f for f in fields if any(m.casefold().startswith(f.label.casefold()) for m in messages)
                 and not f.protected and kinds.sensitivity(kinds.classify(f)) not in {kinds.NEVER_FILL,
                                                                                       kinds.MANUAL}]
        if wrong:
            self._fill_fields(wrong, skip_filled=False)
        self.result.errors = explainer.page_errors(self._errors(), self.form, self.language)

    def _errors(self) -> list[str]:
        try:
            return list(self.backend.errors(self.form))
        except FormChanged:
            raise
        except Exception:
            return []

    def _review(self):
        rescan = getattr(self.backend, "rescan", None)
        if rescan is None:
            return self.form, [label for label in self.result.skipped
                               if any(f.label == label and f.required for f in self.form.fields)]
        try:
            fresh = rescan(self.form)
        except FormChanged:
            raise
        except Exception:
            raise UnsupportedField("Could not check the form") from None
        left = [f.label for f in fresh.fields if f.required and not f.protected and not f.value
                and kinds.sensitivity(kinds.classify(f)) not in {kinds.NEVER_FILL, kinds.MANUAL}]
        return fresh, left

    def _submit_button(self, form) -> str | None:
        buttons = [b for b in getattr(form, "buttons", ()) or ()
                   if _SUBMIT_BUTTON.match(b.strip()) and not _MONEY_BUTTON.search(b)]
        return buttons[0] if len(buttons) == 1 else None  # two Submit buttons: never guess

    def _finish(self) -> None:
        reviewed, left = self._review()
        if left:
            self._say("left_required", labels=", ".join(left))
        button = self._submit_button(reviewed)
        manual = any(kinds.sensitivity(kinds.classify(f)) in {kinds.NEVER_FILL, kinds.MANUAL}
                     for f in reviewed.fields)
        errors = self.result.errors or reviewed.errors
        if (left or errors or self.result.skipped or self.result.user_must or manual or self._structure_changed
                or self._pan_registration or button is None):
            if errors or left:
                self._say("incomplete")
            elif self.result.skipped:
                self._say("skipped")
            self._remember()
            self._say("filled")
            return
        self.status("state", "confirming")
        self._say("submit")
        reply = self._listen()
        if command_of(reply) == "stop":
            raise _Stop(True)
        # Only a clear, short yes submits: "haan", "yes", "ಹೌದು". "Haan nahi", silence or anything else does not.
        if reply and is_yes(reply) and command_of(reply) is None:
            self.status("state", "executing")
            if self.backend.press(reviewed, button):
                self.result.submitted = True
                self._say("submitted")
                self._remember()
                return
        self._remember()
        self._say("filled")

    def _remember(self) -> None:
        if self.profile is None:
            return
        new = {}
        for _target, concept, value in self._entered.values():
            if concept in kinds.PROFILE_CONCEPTS and self.profile.get(concept) != value:
                new[concept] = value
        if not new:
            return
        self._say("remember")
        if is_yes(self._listen() or ""):
            self.profile.update(new)
            self._say("remembered")


class FormAssistant:
    """What the pipeline calls for form actions. Every method speaks its own result and returns an Outcome
    with no personal data: key "form_handled" when it went through, "form_not_found" when no form was on
    screen, "form_not_finished" when the session ended without completing (the overlay shows each key)."""

    def __init__(self, voice, status=lambda *_: None, backend=None, profile=None, highlight=None, consent=None,
                 *, ask_consent=True):
        self.voice = voice
        self.status = status
        self.backend = backend or FieldsBackend()
        self.profile = profile
        self.highlight = highlight  # FieldHighlight: rings the field being asked
        self.consent = consent  # ConsentFile: the online-speech question is asked once, not at every form
        self.ask_consent = ask_consent

    def has_form(self) -> bool:
        """Fail closed when UI metadata cannot be inspected: never send an uncertain page to a model."""
        try:
            return bool(self.backend.scan().fields)
        except Exception:
            return True

    def _peek(self):
        peek = getattr(self.backend, "peek", None)
        return peek() if callable(peek) else self.backend.scan()

    def shows_personal_details(self) -> bool:
        """Whether the window in front has a form with something already typed into it (a name, a number), so
        a screenshot of it must not go to System 2. An empty box (a chat app's message box, a search field)
        is not a reason. Secret fields are blocked because their values are deliberately unavailable here."""
        try:
            form = self._peek()
        except Exception:
            return True
        return any(f.kind == "secret" or (getattr(f, "value", None) or "").strip() for f in form.fields
                   if getattr(f, "control_type", "Edit") not in {"CheckBox", "RadioButton", "ComboBox"}
                   and not _SEARCH_LABEL.search(f.label or ""))

    def ready_to_fill(self, minimum: int = 3) -> bool:
        """Whether the window in front shows an application form for the form session: at least `minimum`
        text boxes the session can fill (name, date of birth, mobile ...). A search box, a one-field login or a
        settings page made of dropdowns and switches is not one."""
        try:
            form = self._peek()
        except Exception:
            return False
        return len(self._fillable_fields(form, {"Edit", "Date"})) >= minimum

    @staticmethod
    def _fillable_fields(form, control_types):
        return [f for f in form.fields if f.control_type in control_types and not f.protected
                and f.kind not in {"secret", "file", "manual"} and not _SEARCH_LABEL.search(f.label or "")]

    def has_fillable_form(self) -> bool:
        """Recognize small forms as well as applications without mistaking a search or chat box for a form."""
        try:
            form = self._peek()
        except Exception:
            return False
        fields = self._fillable_fields(form, {"Edit", "Date", "ComboBox", "RadioButton", "CheckBox"})
        boxes = [f for f in fields if f.control_type in {"Edit", "Date"}]
        return len(boxes) >= 3 or bool(fields and (any(f.required for f in fields)
                                                   or any(_SUBMIT_BUTTON.fullmatch(b.strip()) for b in form.buttons)))

    def _say(self, key, language, **values):
        return self.voice.speak(prompt(key, language, **values), language)

    def _done(self, ok=True, failure="form_not_finished") -> Outcome:
        self.status("state", "done" if ok else "error")
        return Outcome(ok, "form_handled" if ok else failure)

    def _form(self, language, *, for_filling=False, expected_url=None):
        try:
            scan = getattr(self.backend, "scan_for_filling", self.backend.scan) if for_filling else self.backend.scan
            form = scan()
            if form.fields and (expected_url is None or _matches_application(form, expected_url)):
                return form
        except Exception:
            pass
        key = "return_to_application" if expected_url is not None else "no_fields"
        self.status("reply", prompt(key, language))
        self._say(key, language)
        return None

    def _url(self, form) -> str | None:
        try:
            return self.backend.url(form)
        except Exception:
            return None

    def upload(self, language=None) -> Outcome:
        self.voice.speak(kinds.explain("file", language), language)
        return self._done()

    def explain(self, language=None) -> Outcome:
        """Short summary first; each field on 'more' or a field number."""
        form = self._form(language)
        if form is None:
            return self._done(False, "form_not_found")
        explanation = explainer.explain(form, language)
        self.voice.speak(explanation.summary, language)
        warnings = [w for w in safety.assess(self._url(form), form)
                    if w.key in safety.DANGER | {"site_money_fields"}]
        for text in safety.messages(warnings, language):
            self.voice.speak(text, language)
        followup = getattr(self.voice, "followup", None)
        if not callable(followup):
            for line in explanation.fields:
                self.voice.speak(line, language)
            return self._done()
        self._say("more", language)
        for _ in range(len(explanation.fields) + 2):
            self.status("state", "listening")
            heard = followup()
            key = _key(heard[0] if heard else "")
            if not key or command_of(key) == "stop":
                break
            index = _field_number(key, len(explanation.fields))
            if index is not None:
                self.voice.speak(explanation.fields[index], language)
            elif key in _MORE or is_yes(key):
                for line in explanation.fields:
                    self.voice.speak(line, language)
                break
            else:
                break
        return self._done()

    def check_site(self, language=None) -> Outcome:
        """Answer 'is this site real?' explicitly, without ever certifying a page as safe."""
        try:
            form = self.backend.scan()
        except Exception:
            form = None
        warnings = safety.assess(self._url(form) if form is not None else None, form)
        for text in safety.messages(warnings, language):
            self.voice.speak(text, language)
        return self._done()

    def fill(self, language=None, *, expected_url: str | None = None) -> Outcome:
        form = self._form(language, for_filling=True, expected_url=expected_url)
        if form is None:
            return self._done(False, "form_not_found")
        result = FormSession(form, self.voice, self.backend, language, self.profile, self.status,
                             highlight=self.highlight, consent=self.consent, ask_consent=self.ask_consent).run()
        return self._done(result.ok)

    def forget_profile(self, language=None) -> Outcome:
        if self.profile is None or not self.profile.kinds():
            if self.profile is not None:
                self.profile.forget()
            self._say("nothing_saved", language)
        else:
            self.profile.forget()
            self._say("forgotten", language)
        return self._done()

    def fraud_help(self, language=None) -> Outcome:
        self._say("fraud", language)
        return self._done()


def _field_number(text: str, count: int) -> int | None:
    """'3', 'teesra wala', 'number 3', 'the second one' -> a 0-based field index."""
    for word in text.split():
        if word.isdigit() and 1 <= int(word) <= count:
            return int(word) - 1
        if word in _ORDINAL and _ORDINAL[word] < count:
            return _ORDINAL[word]
    return None


__all__ = ["ConsentFile", "FieldHighlight", "FieldsBackend", "FormAssistant", "FormSession", "SessionResult",
           "command_of", "question"]
