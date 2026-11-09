"""What the form assistant tells the rest of Bol OS about the screen (privacy gate, a form worth filling), and
that answers are never read aloud."""

from dataclasses import replace

from bolos.forms import FormAssistant
from bolos.forms.fields import Field
from tests.test_forms_session import Backend, Voice

NAME = Field("name", "First Name", required=True)
LAST = Field("last", "Last Name / Surname", required=True)
DOB = Field("dob", "Date of Birth (DD/MM/YYYY)", required=True, kind="date")
MOBILE = Field("mobile", "Mobile Number", required=True, kind="mobile")
SEARCH = Field("q", "Search", kind="text")
TYPE = Field("type", "Select PAN Application Type", control_type="ComboBox", options=("New PAN - Form No. 93",))


class PeekBackend(Backend):
    """A page whose scan must not open dropdowns: peek() is what the quiet checks use."""

    def __init__(self, fields):
        super().__init__(fields)
        self.scans = 0

    def scan(self):
        self.scans += 1
        raise AssertionError("the quiet checks must not open dropdowns (scan with options)")

    def peek(self):
        return self.form


def assistant(fields):
    return FormAssistant(Voice([]), backend=PeekBackend(fields))


def test_an_empty_message_box_is_not_personal_and_not_a_form():
    # Seen live (8 Nov): a chat app's empty message box made every lesson and every "screen par kya hai"
    # into "a form".
    chat = assistant([Field("composer", "Work with Codex")])
    assert not chat.shows_personal_details() and not chat.ready_to_fill()


def test_typed_details_are_personal_but_a_search_box_is_not():
    assert assistant([replace(NAME, value="Asha"), LAST]).shows_personal_details()
    assert not assistant([replace(SEARCH, value="pan card")]).shows_personal_details()
    assert not assistant([replace(TYPE, value="New PAN - Form No. 93")]).shows_personal_details()


def test_the_pan_form_is_ready_to_fill_but_a_page_of_dropdowns_is_not():
    assert assistant([TYPE, NAME, LAST, DOB, MOBILE]).ready_to_fill()
    assert not assistant([TYPE, replace(TYPE, key="t2"), replace(TYPE, key="t3"), SEARCH]).ready_to_fill()


def test_short_form_detection_ignores_a_search_or_chat_box():
    assert assistant([NAME]).has_fillable_form()
    assert not assistant([replace(SEARCH, required=True)]).has_fillable_form()
    chat = assistant([Field("composer", "Message")])
    chat.backend.form.buttons = ["Send"]
    assert not chat.has_fillable_form()


def test_a_secret_box_blocks_screenshots_even_when_its_value_is_unavailable():
    otp = Field("otp", "One-time code", "secret", protected=True)
    assert assistant([otp]).shows_personal_details()


def test_an_unreadable_window_is_treated_as_personal():
    class Broken(PeekBackend):
        def peek(self):
            raise RuntimeError("no UI tree")

    broken = FormAssistant(Voice([]), backend=Broken([]))
    assert broken.shows_personal_details() and not broken.ready_to_fill()


def test_answers_are_never_spoken_back_even_on_a_pc_with_a_local_voice():
    class LocalVoice(Voice):
        def speak_private(self, text, language):
            raise AssertionError("form answers are shown in the box, never read aloud")

    voice = LocalVoice(["Asha Example", "no"])
    backend = Backend([NAME])
    FormAssistant(voice, backend=backend).fill("hi-IN")
    assert backend.writes == [("name", "Asha Example")]
    assert not any("Asha" in text for text in voice.public)
