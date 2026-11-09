"""The guided form session and FormAssistant, with fake voice, page and profile (no UI Automation)."""

from dataclasses import replace

import pytest

from bolos.forms import FormAssistant, intent
from bolos.forms.fields import Field, Form
from bolos.forms.intent import forget_request, fraud_report
from bolos.forms.prompts import PROMPTS, prompt
from bolos.forms.session import FormSession, command_of
from bolos.forms.windows import FormChanged

VALID_AADHAAR = next(v for v in ("23456789012" + str(i) for i in range(10))
                     if __import__("bolos.forms.normalize", fromlist=["x"]).valid_aadhaar(v))


class Voice:
    def __init__(self, answers, consent="yes", followups=None):
        self.answers = iter(answers if consent is None else [consent, *answers])
        self.public = []
        self.hints = []  # the recognition hints passed with each private answer
        if followups is not None:
            self._followups = iter(followups)
            self.followup = lambda: (lambda v: (v, None) if v is not None else None)(next(self._followups, None))

    def speak(self, text, language):
        self.public.append(text)

    def listen(self):
        raise AssertionError("Personal field answers must never use public capture")

    def listen_private(self, hints=()):
        self.hints.append(list(hints))
        value = next(self.answers, None)
        return (value, None) if value is not None else None


class Highlight:
    def __init__(self):
        self.shown, self.hidden = [], 0

    def show(self, rect, label=""):
        self.shown.append((rect, label))

    def hide(self):
        self.hidden += 1


class Backend:
    def __init__(self, fields, buttons=("Submit",), url="https://onlinesbi.sbi/kyc", errors=(), verify=True,
                 changed=False, title="Re-KYC form"):
        self.form = Form(title, url, list(fields), list(buttons), [])
        self.writes, self.cleared, self.pressed, self.focused = [], [], [], []
        self.error_rounds = [list(errors), []]
        self.verify = verify
        self.changed = changed

    def scan(self):
        return self.form

    def url(self, form):
        return form.url

    def focus(self, form, field):
        if self.changed:
            raise FormChanged()
        self.focused.append(field.key)
        return field.rect

    def fill(self, form, field, value):
        if self.changed:
            raise FormChanged()
        self.writes.append((field.key, value))
        return self.verify

    def clear(self, form, field):
        self.cleared.append(field.key)
        self.writes = [w for w in self.writes if w[0] != field.key]
        return True

    def press(self, form, button):
        if self.changed:
            raise FormChanged()
        self.pressed.append(button)
        return True

    def errors(self, form):
        return self.error_rounds.pop(0) if self.error_rounds else []

    def rescan(self, form):
        written = dict(self.writes)
        return replace(form, fields=[replace(f, value=written.get(f.key, f.value)) for f in form.fields])


class Profile:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.updates = []

    def get(self, kind):
        return self.values.get(kind)

    def update(self, values):
        self.updates.append(dict(values))
        self.values.update(values)

    def forget(self, kind=None):
        self.values.clear()

    def kinds(self):
        return sorted(self.values)


NAME = Field("name", "Full name", required=True, rect=(0, 0, 100, 20))


def run(answers, fields=(NAME,), language="en", **kw):
    voice_kw = {k: kw.pop(k) for k in ("consent", "followups") if k in kw}
    profile = kw.pop("profile", None)
    voice = Voice(answers, **voice_kw)
    backend = Backend(fields, **kw)
    statuses = []
    outcome = FormAssistant(voice, lambda *e: statuses.append(e), backend, profile).fill(language)
    return voice, backend, statuses, outcome


def test_an_answer_fills_the_field_at_once_and_is_never_spoken():
    voice, backend, statuses, outcome = run(["asha example", "no"])
    assert backend.writes == [("name", "Asha Example")]
    assert not backend.pressed
    assert prompt("q_name", "en") in voice.public
    assert not any("Asha" in text for text in voice.public)
    assert not any("Asha" in str(event) for event in statuses)
    assert outcome.detail == {} and outcome.key == "form_handled"


def test_each_field_is_focused_and_ringed_while_it_is_asked():
    fields = [NAME, Field("m", "Mobile number", "mobile", rect=(10, 60, 200, 80))]
    voice = Voice(["Asha Example", "9876543210", "no"])
    backend = Backend(fields)
    highlight = Highlight()
    FormAssistant(voice, backend=backend, highlight=highlight).fill("en")
    assert backend.focused == ["name", "m"]
    assert highlight.shown == [((0, 0, 100, 20), "Full name"), ((10, 60, 200, 80), "Mobile number")]
    assert highlight.hidden >= 1  # the ring is gone before Submit is asked about


def test_name_and_place_answers_hint_recognition_with_common_indian_words():
    fields = [NAME, Field("c", "City / Town / Village"), Field("m", "Mobile number", "mobile")]
    voice, backend, _, _ = run(["raamesh kumaar", "dilli", "9876543210", "no"], fields=fields)
    assert backend.writes == [("name", "Ramesh Kumar"), ("c", "Delhi"), ("m", "9876543210")]
    name_hints, city_hints, mobile_hints = voice.hints[1:4]  # [0] is the consent answer
    assert "Sunita" in name_hints and "Kumar" in name_hints
    assert "Mysuru" in city_hints and "Karnataka" in city_hints
    assert mobile_hints == []


def test_a_state_dropdown_takes_the_state_in_any_script():
    from bolos.forms.indian import STATES

    hindi = tuple(native.split("|")[0] for _, native in STATES.values())
    state = Field("s", "राज्य", "choice", options=("राज्य चुनें", *hindi), control_type="ComboBox")
    voice, backend, _, _ = run(["karnataka", "nahi"], fields=[state], language="hi")
    assert backend.writes == [("s", "कर्नाटक")]
    assert voice.public[2] == prompt("q_state", "hi")  # 36 states are not read out: the state is simply said


def test_consent_is_asked_once_and_remembered(tmp_path):
    from bolos.forms.session import ConsentFile

    consent = ConsentFile(tmp_path / ".bolos-form-consent")
    voice = Voice(["Asha Example", "no"])
    FormAssistant(voice, backend=Backend([NAME]), consent=consent).fill("en")
    assert voice.public[:2] == [prompt("consent", "en"), prompt("how", "en")]
    voice = Voice(["Asha Example", "no"], consent=None)
    backend = Backend([NAME])
    FormAssistant(voice, backend=backend, consent=consent).fill("en")
    assert prompt("consent", "en") not in voice.public and prompt("how", "en") not in voice.public


def test_form_can_go_straight_to_first_field():
    voice = Voice(["Asha Example", "no"], consent=None)
    backend = Backend([NAME])
    FormAssistant(voice, backend=backend, ask_consent=False).fill("en")
    assert voice.public[0] == prompt("q_name", "en")
    assert ("name", "Asha Example") in backend.writes
    assert voice.public[0] == prompt("q_name", "en") and backend.writes == [("name", "Asha Example")]


def test_submit_needs_its_own_yes():
    voice, backend, _, _ = run(["Asha Example", "yes"])
    assert len(backend.writes) == 1 and backend.pressed == ["Submit"]
    assert any("press Submit now" in text for text in voice.public)
    assert voice.public[-1] == prompt("submitted", "en")


@pytest.mark.parametrize("reply", ["no", "maybe", "yes no", None])
def test_anything_but_a_clear_yes_does_not_submit(reply):
    _, backend, _, _ = run(["Asha Example", reply])
    assert backend.writes == [("name", "Asha Example")] and backend.pressed == []


@pytest.mark.parametrize("buttons", [("Pay now",), ("Submit", "Submit"), ("Next",), ()])
def test_never_presses_payment_ambiguous_or_missing_submit(buttons):
    _, backend, _, _ = run(["Asha Example", "yes"], buttons=buttons)
    assert backend.writes and not backend.pressed


def test_a_page_change_prevents_field_mutation_and_submission():
    voice, backend, statuses, outcome = run(["Asha Example", "yes"], changed=True)
    assert not outcome.ok and backend.writes == [] and backend.pressed == []
    assert any("page changed" in text for text in voice.public)
    assert statuses[-1] == ("state", "error")


def test_manual_fields_are_left_to_the_user_and_block_automatic_submit():
    fields = [Field("otp", "OTP", "secret", True, protected=True), NAME, Field("cap", "Enter captcha", "secret")]
    voice, backend, _, _ = run(["Asha Example", "done", "yes"], fields=fields)
    assert backend.writes == [("name", "Asha Example")]
    assert not any(text.startswith("OTP") for text in voice.public)
    assert prompt("secrets", "en") in voice.public
    assert prompt("type_yourself", "en", labels="OTP, Enter captcha") in voice.public
    assert not backend.pressed and prompt("submit", "en") not in voice.public


def test_not_saying_done_after_protected_fields_ends_without_submit():
    fields = [Field("otp", "OTP", "secret", True, protected=True), NAME]
    _, backend, _, _ = run(["Asha Example", "baad mein", "yes"], fields=fields)
    assert backend.writes and not backend.pressed


def test_skip_and_explain_are_internal_dialogue_controls():
    voice, backend, _, _ = run(["yeh kya hai", "chhodo"], fields=[Field("ifsc", "IFSC code", "ifsc")], language="hi")
    assert not backend.writes and not backend.pressed
    assert any("पासबुक" in text for text in voice.public)


def test_invalid_value_is_explained_plainly_and_never_spoken_or_written():
    voice, backend, _, _ = run(["98765", "skip"], fields=[Field("phone", "Mobile", "mobile")])
    assert not backend.writes
    assert any("10 digits" in text for text in voice.public)
    assert all("98765" not in text for text in voice.public)


def test_three_invalid_answers_move_on():
    voice, backend, _, _ = run(["1", "2", "3", "no"], fields=[Field("phone", "Mobile", "mobile")])
    assert not backend.writes and voice.public.count(prompt("q_mobile", "en")) == 3


def test_one_silence_asks_again_and_a_second_one_stops():
    voice, backend, _, _ = run([None, "Asha Example", "no"])
    assert voice.public.count(prompt("q_name", "en")) == 2 and backend.writes == [("name", "Asha Example")]
    voice, backend, _, _ = run([None, None, "Asha Example"])
    assert not backend.writes and voice.public[-1] == prompt("filled", "en")


@pytest.mark.parametrize("language", PROMPTS)
def test_every_language_has_every_prompt_and_fills(language):
    _, backend, _, outcome = run(["Asha Example", "no"], language=language)
    assert outcome.ok and backend.writes == [("name", "Asha Example")]
    assert set(PROMPTS[language]) == set(PROMPTS["en"])


def test_spoken_digits_are_written_as_digits():
    fields = [Field("m", "Mobile number", "mobile")]
    voice, backend, _, _ = run(["nau aath saat chhe paanch chaar teen do ek shunya", "na"], fields=fields,
                               language="hi")
    assert backend.writes == [("m", "9876543210")]
    assert voice.public[2] == prompt("q_mobile", "hi")


def test_a_birth_date_is_asked_for_as_one():
    fields = [Field("d", "Date of birth", "date", control_type="Date")]
    voice, backend, _, _ = run(["15 august 1985", "no"], fields=fields)
    assert backend.writes == [("d", "1985-08-15")]
    assert prompt("q_dob", "en") in voice.public


def test_back_clears_the_previous_answer_and_asks_it_again():
    fields = [NAME, Field("m", "Mobile number", "mobile")]
    answers = ["Asha Example", "galat bhara", "Asha Devi", "9876543210", "no"]
    voice, backend, _, _ = run(answers, fields=fields)
    assert backend.cleared == ["name"]
    assert backend.writes == [("name", "Asha Devi"), ("m", "9876543210")]
    assert prompt("cleared", "en", label="Full name") in voice.public
    assert backend.focused == ["name", "m", "name", "m"]  # the highlight goes back with the question


def test_back_with_nothing_before_says_so_and_asks_again():
    voice, backend, _, _ = run(["back", "Asha Example", "no"])
    assert prompt("back_none", "en") in voice.public and backend.writes == [("name", "Asha Example")]


def test_repeat_asks_again_without_using_up_tries():
    voice, _, _, _ = run(["repeat", "phir se", "dobara", "Asha Example", "no"])
    assert voice.public.count(prompt("q_name", "en")) == 4


def test_a_short_choice_list_is_read_out_and_entered_directly():
    marital = Field("s", "Marital status", "choice", options=("-- Select --", "Single", "Married"),
                    control_type="ComboBox")
    voice, backend, _, _ = run(["doosra", "no"], fields=[marital])
    assert backend.writes == [("s", "Married")]
    assert "Marital status? Single, Married" in voice.public


def test_gender_synonyms_choose_the_matching_option():
    gender = Field("g", "Gender", "choice", options=("Male", "Female", "Other"), control_type="RadioButton")
    _, backend, _, _ = run(["mahila", "no"], fields=[gender], language="hi")
    assert backend.writes == [("g", "Female")]


def test_fields_already_filled_are_skipped():
    fields = [replace(NAME, value="Asha Example"), Field("c", "City")]
    voice, backend, _, _ = run(["mysuru", "no"], fields=fields)
    assert backend.writes == [("c", "Mysuru")]
    assert prompt("q_name", "en") not in voice.public


def test_unverified_fill_is_reported_and_not_counted():
    voice, backend, _, _ = run(["Asha Example", "yes"], verify=False)
    assert prompt("not_entered", "en") in voice.public and not backend.pressed


def test_a_saved_answer_is_filled_in_and_kept_with_a_yes():
    profile = Profile({"name": "Ramesh Kumar"})
    voice, backend, _, _ = run(["haan", "no"], profile=profile)
    assert f"{prompt('q_name', 'en')} {prompt('saved_filled', 'en')}" in voice.public
    assert backend.writes == [("name", "Ramesh Kumar")]
    assert not any("Ramesh" in t for t in voice.public)
    assert profile.updates == []  # unchanged value: nothing to remember


def test_a_new_answer_replaces_the_saved_one():
    profile = Profile({"name": "Ramesh Kumar"})
    _, backend, _, _ = run(["suresh kumar", "no", "no"], profile=profile)
    assert backend.writes == [("name", "Ramesh Kumar"), ("name", "Suresh Kumar")]  # shown, then replaced


def test_new_answers_are_remembered_only_after_yes():
    profile = Profile()
    aadhaar = Field("a", "Aadhaar number", "aadhaar")
    run([VALID_AADHAAR, "no", "yes"], fields=[aadhaar], profile=profile)
    assert profile.updates == [{"aadhaar": VALID_AADHAAR}]
    profile = Profile()
    run([VALID_AADHAAR, "no", "no"], fields=[aadhaar], profile=profile)
    assert profile.updates == []


def test_page_errors_are_explained_and_the_field_asked_again():
    fields = [Field("m", "Mobile number", "mobile")]
    voice, backend, _, _ = run(["9876543210", "9876543211", "no"], fields=fields,
                               errors=["Mobile number: Please enter a valid mobile number"])
    assert prompt("page_errors", "en", count=1) in voice.public
    assert voice.public.count(prompt("q_mobile", "en")) == 2
    assert backend.writes[-1] == ("m", "9876543211")


def test_required_field_left_empty_blocks_submit():
    fields = [NAME, Field("c", "City", required=True)]
    voice, backend, _, _ = run(["Asha Example", "skip", "yes"], fields=fields)
    assert prompt("left_required", "en", labels="City") in voice.public
    assert not backend.pressed


def test_lookalike_site_warns_and_stops_unless_user_insists():
    voice, backend, _, _ = run(["no"], url="https://sbi-kyc-update.xyz/form")
    assert any("not an official website" in t for t in voice.public)
    assert prompt("dangerous_stop", "en") in voice.public and not backend.writes


def test_fraud_report_during_session_gives_1930_and_stops():
    voice, backend, _, _ = run(["maine otp bata diya"])
    assert any("1930" in t for t in voice.public) and not backend.writes


def test_site_check_never_certifies_an_unverified_page():
    voice = Voice([])
    backend = Backend([Field("otp", "OTP", "secret", protected=True)], url=None)
    FormAssistant(voice, backend=backend).check_site("en")
    assert any("cannot verify" in text for text in voice.public)
    assert any("never fill those" in text for text in voice.public)


@pytest.mark.parametrize("url,expected", [
    ("https://onlinesbi.sbi/retail/login.htm", "It belongs to SBI"),
    ("https://uidai-gov.in/update", "not an official website"),
    ("http://203.0.113.9/kyc", "only numbers"),
    ("https://example.org/form", "not on my list"),
])
def test_site_check_answers_explicitly(url, expected):
    voice = Voice([])
    FormAssistant(voice, backend=Backend([NAME], url=url, title="Feedback")).check_site("en")
    assert any(expected in text for text in voice.public)


def test_read_screen_guard_fails_closed_if_metadata_is_unavailable():
    class Broken:
        def scan(self):
            raise RuntimeError("Unavailable")
    assert FormAssistant(Voice([]), backend=Broken()).has_form()


def test_explain_gives_summary_then_fields_on_request():
    fields = [NAME, Field("ifsc", "IFSC code", "ifsc", required=True), Field("otp", "OTP", "secret")]
    voice = Voice([], followups=["teesra", "aur batao"])
    FormAssistant(voice, backend=Backend(fields)).explain("en")
    assert voice.public[0].startswith("Re-KYC form. This form asks for 3 things; 2 are required.")
    assert "bank passbook" in voice.public[0] and "You must type these yourself: OTP" in voice.public[0]
    assert voice.public.index(prompt("more", "en")) < len(voice.public) - 4
    assert voice.public[-3].startswith("Number 1, Full name. Required.")
    assert any(t.startswith("Number 3, OTP.") for t in voice.public)


def test_explain_without_followup_reads_every_field_in_the_language():
    voice = Voice([])
    FormAssistant(voice, backend=Backend([Field("ifsc", "IFSC code", "ifsc")])).explain("kn")
    assert "ಪಾಸ್‌ಬುಕ್" in voice.public[-1]


def test_forget_profile_and_fraud_help():
    profile = Profile({"name": "X"})
    voice = Voice([])
    assistant = FormAssistant(voice, backend=Backend([NAME]), profile=profile)
    assistant.forget_profile("hi")
    assert profile.values == {} and voice.public[-1] == prompt("forgotten", "hi")
    assistant.forget_profile("hi")
    assert voice.public[-1] == prompt("nothing_saved", "hi")
    assistant.fraud_help("ta")
    assert "1930" in voice.public[-1]


@pytest.mark.parametrize("command,action", [
    ("explain this form", "explain_form"), ("help me fill this form", "fill_form"),
    ("is this website real", "check_site"), ("upload Aadhaar photo", "upload_file"),
    ("ye form samjhao", "explain_form"), ("फॉर्म भरने में मदद करो", "fill_form"),
    ("is form mein kya bharna hai", "explain_form"), ("what should I fill in this form", "explain_form"),
    ("ಈ ಫಾರ್ಮ್ ವಿವರಿಸು", "explain_form"), ("ಈ ಫಾರ್ಮ್ ತುಂಬಿ", "fill_form"),
    ("இந்த படிவம் என்ன", "explain_form"), ("இந்த படிவத்தை நிரப்பு", "fill_form"),
    ("ఈ ఫారం ఏమి అడుగుతోంది", "explain_form"), ("ఈ ఫారం నింపు", "fill_form"),
    ("हा फॉर्म समजवा", "explain_form"), ("हा फॉर्म भरण्यास मदत करा", "fill_form"),
    ("ഈ ഫോം വിശദീകരിക്കൂ", "explain_form"), ("ഈ ഫോം പൂരിപ്പിക്കൂ", "fill_form"),
    ("এই ফর্ম বুঝিয়ে বলো", "explain_form"), ("এই ফর্ম পূরণ করো", "fill_form"),
    ("આ ફોર્મ સમજાવો", "explain_form"), ("આ ફોર્મ ભરો", "fill_form"),
    ("ਇਹ ਫਾਰਮ ਸਮਝਾਓ", "explain_form"), ("ਇਹ ਫਾਰਮ ਭਰੋ", "fill_form"),
    ("type explain this form", None), ("send Asha a message about this form", None),
    ("search for scholarship form", None), ("PAN card application documents needed", None),
])
def test_local_routing_avoids_legacy_model_guesses(command, action):
    assert intent(command) == action


@pytest.mark.parametrize("command", [
    "I need to fill the scholarship form",
    "EPFO ka form bharna h",
    "पैन का फ़ॉर्म भरना है",
    "ವಿದ್ಯಾರ್ಥಿವೇತನ ಅರ್ಜಿ ತುಂಬಬೇಕು",
    "SSP arji tumbabeku",
    "உதவித்தொகை விண்ணப்பம் நிரப்ப வேண்டும்",
    "scholarship vinnappam nirappa vendum",
    "స్కాలర్‌షిప్ దరఖాస్తు నింపాలి",
    "scholarship darakhastu nimpali",
    "शिष्यवृत्तीचा फॉर्म भरायचा आहे",
    "സ്കോളർഷിപ്പ് അപേക്ഷ പൂരിപ്പിക്കണം",
    "বৃত্তির আবেদনপত্র পূরণ করতে চাই",
    "શિષ્યવૃત્તિનું ફોર્મ ભરવું છે",
    "ਸਕਾਲਰਸ਼ਿਪ ਦਾ ਫ਼ਾਰਮ ਭਰਨਾ ਹੈ",
])
def test_named_forms_are_recognized_in_every_supported_language(command):
    assert intent(command) == "fill_form"


@pytest.mark.parametrize("text,fraud", [
    ("maine otp bata diya", True), ("I shared my OTP with a caller", True), ("paise kat gaye", True),
    ("mere saath fraud hua", True), ("मेरे साथ धोखा हुआ", True), ("ನನಗೆ ವಂಚನೆ ಆಯಿತು", True),
    ("fill this form", False), ("Asha Example", False), ("otp field kya hai", False),
])
def test_fraud_reports_are_recognised(text, fraud):
    assert fraud_report(text) is fraud


@pytest.mark.parametrize("text,forget", [
    ("meri jaankari mita do", True), ("forget my details", True), ("delete my saved data", True),
    ("मेरी जानकारी मिटा दो", True), ("explain this form", False), ("my name is Asha", False),
])
def test_forget_requests_are_recognised(text, forget):
    assert forget_request(text) is forget


@pytest.mark.parametrize("text,command", [
    ("chhodo", "skip"), ("baad mein", "skip"), ("ಬಿಡಿ", "skip"), ("pichla", "back"), ("galat bhara", "back"),
    ("தப்பு", "back"), ("phir se", "repeat"), ("ये क्या है", "explain"), ("idhu enna", "explain"),
    ("mera wala", "saved"), ("band karo", "stop"), ("ಸಾಕು", "stop"), ("Asha Example", None), ("560001", None),
])
def test_session_commands_in_many_languages(text, command):
    assert command_of(text) == command


def test_refusing_online_speech_stops_before_asking_for_values():
    voice, backend, _, _ = run(["Asha Example", "yes"], consent="no")
    assert not backend.writes and not backend.pressed and not backend.focused
    assert prompt("q_name", "en") not in voice.public
    assert voice.public == [prompt("consent", "en")]


@pytest.mark.parametrize("failed_prompt,expected_listens,expected_writes", [
    ("consent", 0, []), ("q_name", 1, []), ("submit", 2, [("name", "Asha Example")]),
])
def test_failed_mandatory_prompt_stops_before_unannounced_capture_or_submission(
        failed_prompt, expected_listens, expected_writes):
    class FailedPromptVoice(Voice):
        listens = 0

        def speak(self, text, language):
            super().speak(text, language)
            return False if text == prompt(failed_prompt, language) else None

        def listen_private(self, hints=()):
            self.listens += 1
            return super().listen_private(hints)

    voice = FailedPromptVoice(["Asha Example", "yes"])
    backend = Backend([NAME])
    statuses = []
    result = FormAssistant(voice, lambda *event: statuses.append(event), backend).fill("en")
    assert not result.ok and statuses[-1] == ("state", "error")
    assert voice.listens == expected_listens
    assert backend.writes == expected_writes and backend.pressed == []


def test_session_result_keeps_labels_only():
    voice = Voice(["Asha Example", "no"])
    backend = Backend([NAME])
    result = FormSession(backend.form, voice, backend, "en").run()
    assert result.filled == ["Full name"] and not result.submitted
    assert "Asha" not in repr(result)
