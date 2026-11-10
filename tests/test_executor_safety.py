"""Regression tests for destination and private-field guards; no desktop actions are performed."""

from types import SimpleNamespace

import pytest

from bolos import actions as a
from bolos.contacts import Contact, ContactBook
from bolos.decide import Parse
from bolos.executor import Executor, Outcome, uia
from bolos.pipeline import Assistant
from bolos.responses import LANGUAGES, say


def _forbid_clipboard(monkeypatch):
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("A protected or unverified field must not access the clipboard or keyboard")

    for name in ("_get_clipboard", "_set_clipboard", "send_keys"):
        monkeypatch.setattr(uia, name, forbidden)
    return touched


@pytest.mark.parametrize("kind", ["secret", "file"])
def test_protected_typing_stops_before_clipboard_or_keyboard(monkeypatch, kind):
    touched = _forbid_clipboard(monkeypatch)
    monkeypatch.setattr(uia, "_focused_field_kind", lambda: kind)
    with pytest.raises(uia.ProtectedControl):
        uia.type_text("FAKE_PRIVATE_VALUE")
    assert touched == []


def test_unavailable_focus_metadata_stops_before_clipboard_or_keyboard(monkeypatch):
    touched = _forbid_clipboard(monkeypatch)

    def unavailable():
        raise RuntimeError("No UIA metadata")

    monkeypatch.setattr(uia, "_focused_field_kind", unavailable)
    with pytest.raises(uia.ProtectedControl):
        uia.type_text("FAKE_PRIVATE_VALUE")
    assert touched == []


def _focus_metadata(monkeypatch, label, automation_id, password=False, control_type="Edit"):
    from pywinauto import uia_defines, uia_element_info

    info = SimpleNamespace(name=label, automation_id=automation_id, control_type=control_type,
                           element=SimpleNamespace(CurrentIsPassword=password))
    monkeypatch.setattr(uia_defines, "IUIA", lambda: SimpleNamespace(get_focused_element=lambda: object()))
    monkeypatch.setattr(uia_element_info, "UIAElementInfo", lambda _: info)
    return info


@pytest.mark.parametrize("label,automation_id,password", [
    ("OTP", "field", False), ("Customer credential", "field", True),
    ("Credential", "api_key", False), ("Credential", "private-key", False),
    ("Credential", "access_token", False), ("ओटीपी", "field", False),
])
def test_focused_metadata_protects_native_secrets_and_underscored_ids(
        monkeypatch, label, automation_id, password):
    touched = _forbid_clipboard(monkeypatch)
    _focus_metadata(monkeypatch, label, automation_id, password)
    with pytest.raises(uia.ProtectedControl):
        uia.type_text("FAKE_PRIVATE_VALUE")
    assert touched == []


def test_unlabelled_edit_focus_is_unverified_and_not_typed(monkeypatch):
    touched = _forbid_clipboard(monkeypatch)
    _focus_metadata(monkeypatch, "", "")
    with pytest.raises(uia.ProtectedControl):
        uia.type_text("FAKE_PRIVATE_VALUE")
    assert touched == []


class PrivateControl:
    def __init__(self, label, automation_id="field", password=False):
        self.element_info = SimpleNamespace(name=label, automation_id=automation_id, control_type="Edit",
                                            element=SimpleNamespace(CurrentIsPassword=password))
        self.reads = 0

    def is_visible(self):
        return True

    @property
    def iface_value(self):
        self.reads += 1
        raise AssertionError("Secret ValuePattern must never be fetched")

    def window_text(self):
        self.reads += 1
        return "FAKE_PRIVATE_VALUE"


@pytest.mark.parametrize("label,automation_id,password", [
    ("Password", "password", False), ("OTP", "otp", False), ("Bank PIN", "pin", False),
    ("Credential", "api_key", False), ("Credential", "private-key", False),
    ("Customer code", "field", True), ("", "", False),
])
def test_secret_screen_values_are_never_fetched(monkeypatch, label, automation_id, password):
    from bolos.guide import screen

    ctrl = PrivateControl(label, automation_id, password)
    window = SimpleNamespace(window_text=lambda: "Example document", descendants=lambda: [ctrl])
    monkeypatch.setattr(screen, "user_window", lambda: 1)
    monkeypatch.setattr(uia, "_window", lambda hwnd: window)
    assert uia.screen_text() == "Example document"
    assert ctrl.reads == 0


def _executor(monkeypatch):
    import bolos.executor as executor_module
    from bolos.executor import play, whatsapp

    monkeypatch.setattr(executor_module, "foreground_window", lambda: 42)
    contacts = ContactBook([Contact("Test Person", "919000000000", ())])
    runner = Executor(contacts, catalog=object())
    calls = []
    monkeypatch.setattr(runner, "open_app", lambda app: Outcome(True, "opened", {"app": app}))
    monkeypatch.setattr(runner, "open_site", lambda site, browser=None: Outcome(True, "opened_site", {"site": site}))
    monkeypatch.setattr(runner, "open_url", lambda *args: calls.append("navigate"))
    monkeypatch.setattr(play, "play", lambda *args: Outcome(True, "playing", {"query": "demo"}))
    monkeypatch.setattr(whatsapp, "open_draft", lambda *args: calls.append("message"))
    monkeypatch.setattr(uia, "type_text", lambda text: calls.append("typed"))
    monkeypatch.setattr(uia, "click", lambda target: calls.append("clicked") or target)
    runner.begin()
    return runner, calls


@pytest.mark.parametrize("opener", [
    a.OpenApp(app="Notepad"), a.OpenWebsite(site="example.org"), a.WebSearch(query="example"),
    a.PlayMedia(query="demo"), a.SendMessage(contact="Test Person", text="test"),
])
@pytest.mark.parametrize("later", [a.TypeText(text="private words"), a.Click(target="Submit")])
def test_opening_or_navigating_then_typing_or_clicking_fails_even_if_hwnd_is_unchanged(monkeypatch, opener, later):
    runner, calls = _executor(monkeypatch)
    assert runner.execute(opener).ok
    result = runner.execute(later)
    assert not result.ok and result.key == "focus_changed"
    assert "typed" not in calls and "clicked" not in calls


def test_a_new_command_can_target_the_newly_reviewed_foreground(monkeypatch):
    runner, calls = _executor(monkeypatch)
    assert runner.execute(a.OpenApp(app="Notepad")).ok
    assert runner.execute(a.TypeText(text="first command")).key == "focus_changed"
    runner.begin()
    assert runner.execute(a.TypeText(text="new command")).ok
    assert calls == ["typed"]


def test_a_click_then_type_cannot_assume_the_same_window_means_the_same_field(monkeypatch):
    runner, calls = _executor(monkeypatch)
    assert runner.execute(a.Click(target="Next")).ok
    assert runner.execute(a.TypeText(text="private words")).key == "focus_changed"
    assert calls == ["clicked"]


@pytest.mark.parametrize("target,labels,at_point,clicks", [
    ("Activate", ["Deactivate"], 42, []),
    ("खोलो", ["खालो"], 42, []),
    ("Next", ["Next", "Next"], 42, []),
    ("Next", ["Next"], 43, []),
    ("Next", ["Next >"], 42, ["Next >"]),
])
def test_click_requires_a_single_exact_label_at_an_unobstructed_point(monkeypatch, target, labels, at_point, clicks):
    pressed = []
    controls = [SimpleNamespace(element_info=SimpleNamespace(control_type="Button", name=label),
                                is_visible=lambda: True, is_enabled=lambda: True,
                                rectangle=lambda: SimpleNamespace(left=10, top=10, right=30, bottom=30),
                                click_input=lambda label=label: pressed.append(label)) for label in labels]
    monkeypatch.setattr(uia, "_window", lambda hwnd: SimpleNamespace(descendants=lambda: controls))
    monkeypatch.setattr(uia.win32gui, "GetForegroundWindow", lambda: 42)
    monkeypatch.setattr(uia.win32gui, "GetAncestor", lambda hwnd, _: hwnd)
    monkeypatch.setattr(uia.win32gui, "WindowFromPoint", lambda _: at_point)

    if clicks:
        assert uia.click(target) == clicks[0]
    else:
        with pytest.raises(uia.ControlNotFound):
            uia.click(target)
    assert pressed == clicks


def test_whatsapp_requests_only_a_draft_and_reports_the_users_next_step(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from bolos.executor import whatsapp

    opened = []
    monkeypatch.setattr(whatsapp.os, "startfile", opened.append)
    runner = Executor(ContactBook([Contact("Test Person", "919000000000", ())]), catalog=object())
    message = "Can we meet at 5? + tea & snacks / आज"
    outcome = runner.execute(a.SendMessage(contact="Test Person", text=message))

    assert outcome == Outcome(True, "message_draft", {"contact": "Test Person"})
    url, = opened
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc) == ("whatsapp", "send")
    assert parse_qs(parsed.query) == {"phone": ["919000000000"], "text": [message]}
    assert "press Send" in say(outcome.key, "en", **outcome.detail)
    assert all("Test Person" in say(outcome.key, language, **outcome.detail) for language in LANGUAGES)


class Voice:
    def __init__(self, reply):
        self.reply = reply
        self.spoken = []
        self.followups = 0

    def speak(self, text, language):
        self.spoken.append(text)

    def followup(self):
        self.followups += 1
        return (self.reply, "en") if self.reply is not None else None

    def listen(self):
        raise AssertionError("Confirmation must use followup capture")


class Runner:
    def __init__(self):
        self.executed = []

    def execute(self, action):
        self.executed.append(action)
        return (Outcome(True, "clicked", {"target": action.target}) if isinstance(action, a.Click)
                else Outcome(True, "opened", {"app": action.app}))


class Planner:
    def parse(self, _):
        return Parse("multi", True)


class LLM:
    def __init__(self, steps):
        self.steps = steps

    def resolve_steps(self, _):
        return self.steps


@pytest.mark.parametrize("target", ["Send", "Submit"])
@pytest.mark.parametrize("reply,allowed", [("yes", True), ("no", False), ("yes no", False), (None, False)])
def test_generic_send_or_submit_click_needs_yes_and_refusal_stops_later_steps(target, reply, allowed):
    runner, voice = Runner(), Voice(reply)
    click, later = a.Click(target=target), a.OpenApp(app="Calculator")
    assistant = Assistant(Planner(), LLM([click, later]), runner, voice)
    result = assistant.handle("do these tasks", "en")
    assert voice.followups == 1
    assert say("confirm_click", "en", target=target) in voice.spoken
    assert runner.executed == ([click, later] if allowed else [])
    assert result.ok is allowed
    if not allowed:
        assert any(say("steps_stopped", "en") in text for text in voice.spoken)


@pytest.mark.parametrize("language", LANGUAGES)
def test_click_confirmation_template_exists_for_every_language(language):
    text = say("confirm_click", language, target="Submit")
    assert "Submit" in text and "{" not in text and "}" not in text


@pytest.mark.parametrize("risky", [a.Click(target="Submit"), a.SendMessage(contact="Test Person", text="test")])
def test_failed_confirmation_readback_never_listens_or_executes_even_when_yes_is_available(risky):
    class FailedSpeechVoice(Voice):
        def speak(self, text, language):
            super().speak(text, language)
            return False

    runner, voice = Runner(), FailedSpeechVoice("yes")
    later = a.OpenApp(app="Calculator")
    assistant = Assistant(Planner(), LLM([risky, later]), runner, voice)
    result = assistant.handle("do these tasks", "en")
    assert not result.ok
    assert voice.followups == 0 and runner.executed == []
