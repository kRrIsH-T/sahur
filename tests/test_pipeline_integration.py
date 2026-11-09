"""Exercise the connected command policy without speech APIs or Windows actions."""

import json

import pytest

from bolos import actions as a
from bolos.decide import Parse
from bolos.executor import Outcome
from bolos.history import History
from bolos.pipeline import Assistant
from bolos.preferences import Preferences
from bolos.responses import say


class Voice:
    user_language = "hi-IN"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.spoken = []
        self.followups = 0
        self.listens = 0

    def speak(self, text, language):
        self.spoken.append((text, language))

    def followup(self):
        self.followups += 1
        return self.replies.pop(0) if self.replies else None

    def listen(self):
        self.listens += 1
        raise AssertionError("A confirmation should use hands free followup")


class Parser:
    def __init__(self, rows=None, default=None):
        self.rows = rows or {}
        self.default = default or Parse("unknown", True)
        self.calls = []

    def parse(self, text):
        self.calls.append(text)
        return self.rows.get(text, self.default)


class Runner:
    def __init__(self, fail_at=None):
        self.executed = []
        self.fail_at = fail_at
        self.begins = 0

    def begin(self):
        self.begins += 1

    def execute(self, action):
        self.executed.append(action)
        if len(self.executed) == self.fail_at:
            return Outcome(False, "error")
        if action.action == "open_app":
            return Outcome(True, "opened", {"app": action.app})
        if action.action == "web_search":
            return Outcome(True, "searched", {"query": action.query})
        if action.action == "play_media":
            return Outcome(True, "playing_on", {"query": action.query, "app": action.app or "Spotify"})
        if action.action == "send_message":
            return Outcome(True, "sent", {"contact": action.contact})
        return Outcome(True, "typed")


class StepsLLM:
    def __init__(self, steps, voice=None):
        self.steps = steps
        self.voice = voice
        self.calls = []

    def resolve_steps(self, command):
        if self.voice:
            assert self.voice.spoken, "Acknowledge the wait before calling System 2"
        self.calls.append(command)
        return self.steps


class Corrector:
    def __init__(self, fixed):
        self.fixed = fixed
        self.calls = []

    def correct(self, original, previous_action, correction):
        self.calls.append((original, previous_action, correction))
        return self.fixed


class Forms:
    def __init__(self):
        self.calls = []

    def has_form(self):
        return True

    def shows_personal_details(self):
        return False

    def ready_to_fill(self):
        return True  # a real form (3+ text boxes) is on screen

    def _outcome(self, method, language):
        self.calls.append((method, language))
        return Outcome(True, "done")

    def explain(self, language):
        return self._outcome("explain", language)

    def fill(self, language):
        return self._outcome("fill", language)

    def check_site(self, language):
        return self._outcome("check_site", language)

    def upload(self, language):
        return self._outcome("upload", language)


def compound_parser():
    text = "Chrome kholo aur weather search karo"
    parser = Parser({
        text: Parse("open_app", True, {"app": "Chrome"}),
        "Chrome kholo": Parse("open_app", True, {"app": "Chrome"}),
        "weather search karo": Parse("web_search", True, {"query": "weather"}),
    })
    return text, parser


def test_local_compound_runs_both_steps_in_the_requested_browser(tmp_path):
    text, parser = compound_parser()
    runner, voice = Runner(), Voice()
    prefs = Preferences(tmp_path / "preferences.json")
    history = History(tmp_path / "history.jsonl", log_messages=False)
    events = []
    assistant = Assistant(parser, None, runner, voice, preferences=prefs, history=history,
                          status=lambda *event: events.append(event))
    assert assistant.handle(text, "hi", trigger="tap").ok
    assert runner.executed == [a.OpenApp(app="Chrome"), a.WebSearch(query="weather", browser="Chrome")]
    assert runner.begins == 1
    assert prefs.habits == {}
    assert len(voice.spoken) == 1
    decision = next(value for key, value in events if key == "decision")
    assert decision["source"] == "system1" and len(decision["steps"]) == 2
    entry = history.entries()[0]
    assert entry["action"] == "multi" and entry["trigger"] == "tap"
    assert len(entry["steps"]) == 2 and entry["timings"]["total"] >= 0


def test_execution_failure_stops_remaining_steps():
    text, parser = compound_parser()
    runner, voice = Runner(fail_at=1), Voice()
    assistant = Assistant(parser, None, runner, voice)
    assert not assistant.handle(text, "en").ok
    assert runner.executed == [a.OpenApp(app="Chrome")]
    assert say("steps_stopped", "en") in voice.spoken[-1][0]


@pytest.mark.parametrize("remote_steps", [None, [a.OpenApp(app="Chrome"), a.Unknown()]])
def test_unknown_later_task_cannot_silently_execute_only_the_first(remote_steps):
    command = "Chrome kholo aur mera dusra kaam karo"
    first = Parse("open_app", True, {"app": "Chrome"})
    parser = Parser({command: first, "Chrome kholo": first})
    runner, voice = Runner(), Voice()
    llm = StepsLLM(remote_steps) if remote_steps is not None else None
    assistant = Assistant(parser, llm, runner, voice)
    assert assistant.handle(command, "hi").key == "not_understood"
    assert runner.executed == []


def test_unknown_later_task_runs_only_after_system_two_resolves_the_full_plan():
    command = "Chrome kholo aur mera dusra kaam karo"
    first = Parse("open_app", True, {"app": "Chrome"})
    parser = Parser({command: first, "Chrome kholo": first})
    runner, voice = Runner(), Voice()
    llm = StepsLLM([a.OpenApp(app="Chrome"), a.WebSearch(query="weather")], voice)
    assistant = Assistant(parser, llm, runner, voice)
    assert assistant.handle(command, "hi").ok
    assert llm.calls == [command]
    assert runner.executed == [a.OpenApp(app="Chrome"), a.WebSearch(query="weather", browser="Chrome")]


@pytest.mark.parametrize("steps", [
    [a.OpenApp(app="Chrome"), a.Unknown()],
    [a.OpenApp(app="Chrome"), a.Cancel()],
    [a.OpenApp(app="Chrome")] * 4,
    [],
])
def test_system_two_partial_or_unsupported_plan_executes_nothing(steps):
    runner, voice = Runner(), Voice()
    assistant = Assistant(Parser(default=Parse("multi", True)), StepsLLM(steps), runner, voice)
    assert assistant.handle("do these things", "en").key == "not_understood"
    assert runner.executed == []


def test_slow_plan_is_announced_once_before_the_remote_call():
    voice, runner = Voice(), Runner()
    llm = StepsLLM([a.OpenApp(app="Chrome"), a.WebSearch(query="weather")], voice)
    events = []
    assistant = Assistant(None, llm, runner, voice, status=lambda *event: events.append(event))
    assert assistant.handle("open my browser and look at the weather", "en").ok
    assert voice.spoken[0] == (say("one_moment", "en"), "en")
    assert sum(text == say("one_moment", "en") for text, _ in voice.spoken) == 1
    assert ("state", "thinking") in events
    assert any(key == "thinking" and value >= 0 for key, value in events)


def test_message_confirmation_uses_followup_and_logs_no_message_text(tmp_path):
    secret = "my appointment is at 4 pm"
    parser = Parser(default=Parse("send_message", True, {"contact": "Raju", "text": secret}))
    runner, voice = Runner(), Voice([("yes send it", "en")])
    history = History(tmp_path / "history.jsonl", log_messages=False)
    assistant = Assistant(parser, None, runner, voice, history=history)
    assert assistant.handle(f"send Raju {secret}", "en").ok
    assert voice.followups == 1 and voice.listens == 0
    assert runner.executed == [a.SendMessage(contact="Raju", text=secret)]
    assert secret not in history.path.read_text(encoding="utf-8")
    assert assistant._last is None


def test_successful_correction_teaches_original_command_and_marks_its_history(tmp_path):
    command = "Radiohead chalao"
    original = a.PlayMedia(query="Radiohead", app="YouTube")
    fixed = a.PlayMedia(query="Radiohead", app="Spotify")
    parser = Parser({command: Parse("play_media", True, {"query": "Radiohead", "app": "YouTube"})})
    prefs = Preferences(tmp_path / "preferences.json")
    history = History(tmp_path / "history.jsonl", log_messages=False)
    llm, voice, runner = Corrector(fixed), Voice(), Runner()
    assistant = Assistant(parser, llm, runner, voice, preferences=prefs, history=history)
    assert assistant.handle(command, "hi").ok
    assert assistant.handle("galat tha, Spotify pe chalao", "hi").ok
    assert llm.calls == [(command, original, "galat tha, Spotify pe chalao")]
    assert prefs.habit_source(command) == "user"
    assert prefs.recall(command) == fixed
    rows = [json.loads(line) for line in prefs.corrections_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["text"] == command
    assert rows[0]["slots"] == {"query": "Radiohead"}
    assert history.entries()[0]["corrected"]
    assert history.entries()[0]["corrected_to"] == [fixed.model_dump(exclude_none=True)]
    parser.calls.clear()
    assert assistant.handle(command, "hi").ok
    assert parser.calls == [] and len(llm.calls) == 1
    assert runner.executed[-1] == fixed


def test_correction_reclassified_as_private_scrubs_history_and_retained_context(tmp_path):
    secret = "the PIN is 8246"
    command = f"Raju ko bolo ki {secret}"
    parser = Parser({command: Parse("web_search", True, {"query": secret})})
    prefs = Preferences(tmp_path / "preferences.json")
    history = History(tmp_path / "history.jsonl", log_messages=False)
    llm = Corrector(a.SendMessage(contact="Raju", text=secret))
    voice, runner = Voice([("haan", "hi")]), Runner()
    assistant = Assistant(parser, llm, runner, voice, preferences=prefs, history=history)
    assert assistant.handle(command, "hi").ok
    assert secret in history.path.read_text(encoding="utf-8")
    assert assistant.handle("galat tha, Raju ko bhejo", "hi").ok
    assert secret not in history.path.read_text(encoding="utf-8")
    assert not prefs.habits and not prefs.corrections_path.exists()
    assert assistant._last is None, "Private reclassification must clear the previous transcript from correction memory"


def test_script_language_wins_over_conflicting_model_language():
    parser = Parser(default=Parse("open_app", True, {"app": "Notepad"}, language="en"))
    voice = Voice()
    assistant = Assistant(parser, None, Runner(), voice)
    assert assistant.handle("Notepad ತೆರೆ", "hi-IN").ok
    assert all(language == "kn" for _, language in voice.spoken)


def test_model_language_controls_romanised_replies_and_neutral_turn_keeps_it():
    parser = Parser({
        "Notepad tegi": Parse("open_app", True, {"app": "Notepad"}, language="te"),
        "Calculator": Parse("open_app", True, {"app": "Calculator"}),
    })
    voice = Voice()
    assistant = Assistant(parser, None, Runner(), voice)
    assert assistant.handle("Notepad tegi", None).ok
    assert assistant.handle("Calculator", None).ok
    assert [language for _, language in voice.spoken] == ["te", "te"]


@pytest.mark.parametrize("command,method", [
    ("explain this form", "explain"),
    ("fill this form with 111122223333", "fill"),
    ("is this website safe", "check_site"),
    ("upload my identity proof", "upload"),
])
def test_form_request_bypasses_parser_and_remote_model_and_is_not_logged(tmp_path, command, method):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"Form request reached {name}")

    forms, runner, voice = Forms(), Runner(), Voice()
    history = History(tmp_path / "history.jsonl", log_messages=False)
    prefs = Preferences(tmp_path / "preferences.json")
    events = []
    assistant = Assistant(Forbidden(), Forbidden(), runner, voice, forms=forms, history=history,
                          preferences=prefs, status=lambda *event: events.append(event))
    assert assistant.handle(command, "en").ok
    assert forms.calls == [(method, "en")] and runner.executed == []
    assert not prefs.habits and not prefs.corrections_path.exists()
    assert history.entries()[0]["transcript"] == "[form request]"
    assert ("heard", "[form request]") in events
    assert command not in history.path.read_text(encoding="utf-8")


def test_read_screen_of_a_form_uses_local_explanation():
    class ForbiddenLLM:
        def describe_screen(self, *_):
            raise AssertionError("Form content must not be sent to System 2")

    forms, runner = Forms(), Runner()
    assistant = Assistant(Parser(default=Parse("read_screen", True)), ForbiddenLLM(), runner, Voice(), forms=forms)
    assert assistant.handle("read the screen", "en").ok
    assert forms.calls == [("explain", "en")] and runner.executed == []


@pytest.mark.parametrize("already_open", [True, False])
def test_named_form_fills_open_form_or_finds_it_first(already_open):
    class ScreenForms(Forms):
        def has_fillable_form(self):
            return already_open

    class FormGuide:
        pending = None

        def __init__(self):
            self.requests = []

        def run(self, goal, language, *, find_form=False):
            self.requests.append((goal, language, find_form))
            return Outcome(True, "guide_form_ready")

    forms, guide, runner = ScreenForms(), FormGuide(), Runner()
    assistant = Assistant(None, None, runner, Voice(), forms=forms, guide=guide)
    assert assistant.handle("EPFO ka form bharna h with 123456789012", "hi").ok
    assert guide.requests == ([] if already_open else [("EPFO ka form bharna h", "hi", True)])
    assert forms.calls == [("fill", "hi")] and not runner.executed


def test_compound_cannot_chain_a_form_dialogue_with_another_action():
    llm = StepsLLM([a.FillForm(), a.Click(target="Submit")])
    forms, runner = Forms(), Runner()
    assistant = Assistant(None, llm, runner, Voice(), forms=forms)
    assert assistant.handle("do both tasks", "en").key == "not_understood"
    assert forms.calls == [] and runner.executed == []
