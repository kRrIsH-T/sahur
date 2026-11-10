import pytest

from bolos import actions as a
from bolos.decide import Parse
from bolos.executor import Outcome
from bolos.pipeline import Assistant
from bolos.preferences import Preferences
from bolos.responses import say


class FakeParser:
    def __init__(self, parse):
        self.result = parse
        self.calls = 0

    def parse(self, command):
        self.calls += 1
        return self.result


class FakeLLM:
    def __init__(self, action=None):
        self.action = action
        self.calls = []

    def fill(self, name, command, fields):
        self.calls.append(("fill", name, fields))
        return self.action

    def resolve(self, command):
        self.calls.append(("resolve", command))
        return self.action

    def describe_screen(self, text, language, image=None, *, question=""):
        self.calls.append(("describe_screen", image))
        self.screen_question = question
        return f"summary of {text}"


class DownLLM(FakeLLM):
    def fill(self, name, command, fields):
        raise RuntimeError("429 quota exceeded")

    def resolve(self, command):
        raise RuntimeError("429 quota exceeded")


class FakeRunner:
    def __init__(self, outcome=None):
        self.outcome = outcome or Outcome(True, "opened", {"app": "Google Chrome"})
        self.executed = []

    def execute(self, action):
        self.executed.append(action)
        return self.outcome


class FakeVoice:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.spoken = []

    def speak(self, text, language):
        self.spoken.append(text)

    def listen(self):
        return self.replies.pop(0) if self.replies else None


SEND = a.SendMessage(contact="Raju", text="5 minute lagenge")


def sure(action, **slots):
    return Parse(action, True, slots)


def make(parse, llm=None, runner=None, voice=None, preferences=None):
    runner = runner or FakeRunner(Outcome(True, "sent", {"contact": "Raju"}))
    voice = voice or FakeVoice()
    return Assistant(FakeParser(parse), llm, runner, voice, preferences=preferences), runner, voice


def test_sure_parse_runs_without_the_llm():
    llm = FakeLLM()
    assistant, runner, voice = make(sure("open_app", app="chrome"), llm=llm,
                                    runner=FakeRunner(Outcome(True, "opened", {"app": "Google Chrome"})))
    assistant.handle("chrome kholo", "hi-IN")
    assert runner.executed == [a.OpenApp(app="chrome")]
    assert llm.calls == []
    assert voice.spoken == ["Google Chrome खोल दिया।"]


def test_information_request_searches_never_plays():
    # The bug: "X ke baare mein batao" used to open a YouTube video.
    assistant, runner, _ = make(sure("web_search", query="Lata Mangeshkar"),
                                runner=FakeRunner(Outcome(True, "searched", {"query": "Lata Mangeshkar"})))
    assistant.handle("mujhe Lata Mangeshkar ke baare mein batao", "hi-IN")
    assert runner.executed == [a.WebSearch(query="Lata Mangeshkar")]


def test_unsure_fields_go_to_llm_fill():
    llm = FakeLLM(a.OpenApp(app="VLC media player"))
    assistant, runner, _ = make(Parse("open_app", True, {"app": "VLC"}, frozenset({"app"})), llm=llm)
    assistant.handle("VLC kholo", "hi-IN")
    assert llm.calls == [("fill", "open_app", ["app"])]
    assert runner.executed == [a.OpenApp(app="VLC media player")]


def test_unsure_action_goes_to_llm_resolve():
    llm = FakeLLM(a.PlayMedia(query="Radiohead", app="Spotify"))
    assistant, runner, _ = make(Parse("open_app", False), llm=llm,
                                runner=FakeRunner(Outcome(True, "playing_on", {"query": "Creep", "app": "Spotify"})))
    assistant.handle("Spotify pe radio head chalavo", "hi-IN")
    assert llm.calls == [("resolve", "Spotify pe radio head chalavo")]
    assert runner.executed == [a.PlayMedia(query="Radiohead", app="Spotify")]


def test_send_requires_spoken_yes():
    voice = FakeVoice(replies=[("haan bhejo", "hi-IN")])
    assistant, runner, voice = make(sure("send_message", contact="Raju", text="5 minute lagenge"), voice=voice)
    assistant.handle("Raju ko WhatsApp karo ki 5 minute lagenge", "hi-IN")
    assert runner.executed == [SEND]
    assert "Raju के लिए यह मैसेज व्हाट्सऐप में खोलूँ: 5 minute lagenge?" in voice.spoken[0]


@pytest.mark.parametrize("reply", [("nahi", "hi-IN"), ("haan nahi ruko", "hi-IN"), ("hmm", None), None])
def test_send_not_executed_without_clear_yes(reply):
    voice = FakeVoice(replies=[reply])
    assistant, runner, voice = make(sure("send_message", contact="Raju", text="aa raha hoon"), voice=voice)
    outcome = assistant.handle("Raju ko bolo aa raha hoon", "hi-IN")
    assert runner.executed == []
    assert outcome.key == "not_sent"


def test_sure_unknown_never_executes_or_asks_llm():
    llm = FakeLLM(a.OpenApp(app="x"))
    assistant, runner, voice = make(Parse("unknown", True), llm=llm)
    assert assistant.handle("arre yaar", "en-IN").key == "not_understood"
    assert runner.executed == [] and llm.calls == []
    assert voice.spoken == ["I didn't catch that. Try one short request, like open Calculator."]


def test_unsure_without_llm_is_not_understood():
    assistant, runner, _ = make(Parse("open_app", False, {"app": "chrome"}))
    assert assistant.handle("hmm chrome", "hi-IN").key == "not_understood"
    assert runner.executed == []


def test_llm_unknown_is_not_understood():
    assistant, runner, _ = make(Parse("click", False), llm=FakeLLM(a.Unknown()))
    assert assistant.handle("arre", "hi-IN").key == "not_understood"
    assert runner.executed == []


def test_read_back_names_the_resolved_contact():
    from bolos.contacts import Contact, ContactBook

    book = ContactBook([Contact("Raju", "919800000001", ("राजू", "Raju bhai"))])
    voice = FakeVoice(replies=[("haan", "hi-IN")])
    assistant, runner, voice = make(sure("send_message", contact="राजू", text="aa raha hoon"), voice=voice)
    assistant.contacts = book
    assistant.handle("राजू को बोलो aa raha hoon", "hi-IN")
    assert voice.spoken[0].startswith("Raju के लिए यह मैसेज व्हाट्सऐप में खोलूँ")
    assert runner.executed == [a.SendMessage(contact="Raju", text="aa raha hoon")]


def test_unknown_contact_fails_before_asking():
    from bolos.contacts import ContactBook

    voice = FakeVoice(replies=[("haan", "hi-IN")])
    assistant, runner, voice = make(sure("send_message", contact="Raju", text="hi"), voice=voice)
    assistant.contacts = ContactBook([])
    assert assistant.handle("Raju ko message karo hi", "hi-IN").key == "contact_not_found"
    assert runner.executed == []
    assert voice.replies, "never asked for confirmation"


def test_unique_alias_for_a_duplicate_display_name_never_opens_the_other_chat():
    from bolos.contacts import Contact, ContactBook

    voice = FakeVoice(replies=[("yes", "en")])
    assistant, runner, _ = make(sure("send_message", contact="Raju from work", text="hello"), voice=voice)
    assistant.contacts = ContactBook([Contact("Raju", "1111", ("Raju from work",)),
                                     Contact("Raju", "2222", ("Raju from school",))])
    events = []
    assistant.status = lambda *event: events.append(event)
    assert assistant.handle("message Raju from work saying hello", "en").key == "contact_not_found"
    assert runner.executed == [] and voice.replies
    assert ("reply", "I couldn't match Raju from work to one person. Try saying their full name.") in events


def test_completed_action_still_reports_success_when_preferences_file_cannot_be_replaced(tmp_path):
    path = tmp_path / "preferences.json"
    prefs = Preferences(path)
    path.mkdir()
    runner = FakeRunner(Outcome(True, "playing_on", {"query": "Arijit", "app": "Spotify"}))
    assistant, runner, voice = make(sure("play_media", query="Arijit", app="Spotify"), runner=runner,
                                    preferences=prefs)
    assert assistant.handle("play Arijit on Spotify", "en").ok
    assert len(runner.executed) == 1
    assert voice.spoken == [say("playing_on", "en", query="Arijit", app="Spotify")]


def test_missing_field_asks_again():
    from bolos.llm import SlotError

    class EmptyMessage(FakeLLM):
        def fill(self, name, command, fields):
            raise SlotError("text is empty")

    assistant, runner, _ = make(Parse("send_message", True, {"contact": "Raju"}, frozenset({"text"})),
                                llm=EmptyMessage())
    assert assistant.handle("Raju ko message karo", "hi-IN").key == "not_understood"
    assert runner.executed == []


def test_executor_crash_is_reported_not_raised():
    class Boom:
        def execute(self, action):
            raise RuntimeError("window vanished")

    assistant, _, voice = make(sure("open_app", app="chrome"), runner=Boom())
    assert assistant.handle("chrome kholo", "en-IN").key == "error"
    assert voice.spoken == ["Something went wrong, please try again."]


def test_llm_outage_on_unsure_action_is_not_understood():
    assistant, runner, _ = make(Parse("send_message", False), llm=DownLLM())
    assert assistant.handle("Raju ko message karo", "hi-IN").key == "not_understood"
    assert runner.executed == []


def test_llm_outage_uses_system1_query_or_the_spoken_command():
    runner = FakeRunner(Outcome(True, "playing_on", {"query": "q", "app": "YouTube"}))
    assistant, runner, _ = make(Parse("play_media", True, {"query": "Arijit"}, frozenset({"query"})),
                                llm=DownLLM(), runner=runner)
    assistant.handle("YouTube pe Arijit ke gaane chalao", "hi-IN")
    assert runner.executed == [a.PlayMedia(query="Arijit")]
    assistant.parser = FakeParser(Parse("web_search", True, {}, frozenset({"query"})))
    assistant.handle("Bangalore ka weather batao", "hi-IN")
    assert runner.executed[-1] == a.WebSearch(query="Bangalore ka weather batao")


@pytest.mark.parametrize("parse", [
    Parse("send_message", True, {"contact": "Raju", "text": "aa raha"}, frozenset({"text"})),
    Parse("type_text", True, {"text": "hello"}, frozenset({"text"})),
    Parse("click", True, {"target": "OK"}, frozenset({"target"})),
])
def test_unsure_messages_typing_and_clicks_are_never_guessed(parse):
    assistant, runner, _ = make(parse, llm=DownLLM(), voice=FakeVoice(replies=[("haan", "hi-IN")]))
    assert assistant.handle("x", "hi-IN").key == "not_understood"
    assert runner.executed == []


def test_media_control_needs_no_fields():
    runner = FakeRunner(Outcome(True, "control_pause"))
    assistant, runner, voice = make(Parse("media_control", True, control="pause"), runner=runner)
    assistant.handle("gaana rok do", "hi-IN")
    assert runner.executed == [a.MediaControl(control="pause")]
    assert voice.spoken == ["रोक दिया।"]


def test_read_screen_is_summarised():
    runner = FakeRunner(Outcome(True, "screen", {"text": "Inbox 3 unread"}))
    assistant, _, voice = make(sure("read_screen"), llm=FakeLLM(), runner=runner)
    assistant.handle("screen padho", "hi-IN")
    assert voice.spoken == [say("one_moment", "hi-IN"), "summary of Inbox 3 unread"]


def test_read_screen_looks_at_a_screenshot_and_deletes_it(tmp_path):
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"png")
    runner = FakeRunner(Outcome(True, "screen", {"text": "Inbox 3 unread"}))
    llm = FakeLLM()
    assistant = Assistant(FakeParser(sure("read_screen")), llm, runner, FakeVoice(), screenshot=lambda: shot)
    assistant.handle("screen par kya hai", "hi-IN")
    assert ("describe_screen", shot) in llm.calls and not shot.exists()


def test_contextual_question_uses_screen_and_shows_the_answer():
    question = "What does this error on my screen mean?"
    runner = FakeRunner(Outcome(True, "screen", {"text": "Import failed"}))
    llm = FakeLLM()
    voice = FakeVoice()
    status = []
    assistant = Assistant(FakeParser(sure("web_search", query="error")), llm, runner, voice,
                          status=lambda *event: status.append(event))
    assert assistant.handle(question, "en-IN").ok
    assert runner.executed == [a.ReadScreen()]
    assert llm.screen_question == question
    assert ("reply", "summary of Import failed") in status


def test_romanized_kannada_screen_question_does_not_click():
    runner = FakeRunner(Outcome(True, "screen", {"text": "Calculator"}))
    llm = FakeLLM()
    bot, _, _ = make(sure("click", target="Calculator"), llm=llm, runner=runner)
    assert bot.handle("ee button enu madutte", None).ok
    assert bot.language == "kn"
    assert runner.executed == [a.ReadScreen()]


def test_read_screen_reports_unavailable_when_it_has_no_context():
    runner = FakeRunner(Outcome(True, "screen", {"text": ""}))
    llm = FakeLLM()
    assistant, _, voice = make(sure("read_screen"), llm=llm, runner=runner)
    assert assistant.handle("What am I looking at?", "en-IN").key == "screen_unavailable"
    assert not llm.calls
    assert voice.spoken[-1] == say("screen_unavailable", "en-IN")


# Preferences


def test_unnamed_service_goes_to_the_usual_one(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    runner = FakeRunner(Outcome(True, "playing_on", {"query": "q", "app": "x"}))
    assistant, runner, _ = make(sure("play_media", query="Radiohead"), runner=runner, preferences=prefs)
    assistant.handle("Radiohead chalao", "hi-IN")
    assert runner.executed[-1].app == "Spotify"  # default for music
    for _ in range(2):
        assistant.parser = FakeParser(sure("play_media", query="Arijit", app="JioSaavn"))
        assistant.handle("JioSaavn pe Arijit chalao", "hi-IN")
    assistant.parser = FakeParser(sure("play_media", query="Radiohead"))
    assistant.handle("Radiohead chalao", "hi-IN")
    assert runner.executed[-1].app == "JioSaavn"
    assert Preferences(tmp_path / "p.json").media_app("Radiohead chalao", "Radiohead") == "JioSaavn"  # saved


def test_videos_and_music_are_learned_separately(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.note_media_app("Spotify pe gaane chalao", "", "Spotify")
    assert prefs.media_app("Bahubali trailer chalao", "Bahubali trailer") == "YouTube"
    assert prefs.media_app("Coldplay chalao", "Coldplay") == "Spotify"


def test_system2_answers_become_habits_and_can_be_forgotten(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    llm = FakeLLM(a.OpenApp(app="Visual Studio Code"))
    assistant, runner, voice = make(Parse("open_app", False), llm=llm, preferences=prefs,
                                    runner=FakeRunner(Outcome(True, "opened", {"app": "Visual Studio Code"})))
    assistant.handle("mera code editor kholo", "hi-IN")
    assistant.handle("Mera code editor kholo.", "hi-IN")  # same words: no System 2 this time
    assert [c[0] for c in llm.calls] == ["resolve"]
    assert assistant.parser.calls == 1
    assert runner.executed == [a.OpenApp(app="Visual Studio Code")] * 2
    assert assistant.handle("galat tha", "hi-IN").key == "forgot"
    assistant.handle("mera code editor kholo", "hi-IN")
    assert [c[0] for c in llm.calls] == ["resolve", "resolve"]


def test_failed_or_dry_run_commands_teach_nothing(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    llm = FakeLLM(a.OpenApp(app="Zoom"))
    assistant, _, _ = make(Parse("open_app", False), llm=llm, preferences=prefs,
                           runner=FakeRunner(Outcome(False, "app_not_found", {"app": "Zoom"})))
    assistant.handle("zoom kholo", "hi-IN")
    assert prefs.habits == {}
    assistant.runner = FakeRunner(Outcome(True, "dry_run"))
    assistant.learning = False
    assistant.handle("zoom kholo", "hi-IN")
    assert prefs.habits == {}


def test_messages_are_never_habits(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_habit("Raju ko bolo aa raha hoon", SEND)
    assert prefs.recall("Raju ko bolo aa raha hoon") is None


@pytest.mark.parametrize("reply,expected", [("galat tha", True), ("Wrong.", True), ("ye galat hai", True),
                                            ("tappu", True), ("galat number pe message karo", False),
                                            ("Raju ko bolo galat hai", False), ("", False)])
def test_is_correction(reply, expected):
    from bolos.responses import is_correction

    assert is_correction(reply) is expected


def test_a_habit_from_an_earlier_session_can_be_forgotten(tmp_path):
    first = Preferences(tmp_path / "p.json")
    first.learn_habit("mera editor kholo", a.OpenApp(app="Notepad"))  # wrong, learned yesterday
    prefs = Preferences(tmp_path / "p.json")  # restart
    assistant, runner, _ = make(Parse("open_app", False), llm=FakeLLM(a.OpenApp(app="Visual Studio Code")),
                                preferences=prefs)
    assistant.handle("mera editor kholo", "hi-IN")
    assert runner.executed == [a.OpenApp(app="Notepad")]
    assert assistant.handle("galat tha", "hi-IN").key == "forgot"
    assistant.handle("mera editor kholo", "hi-IN")
    assert runner.executed[-1] == a.OpenApp(app="Visual Studio Code")


def test_galat_tha_only_undoes_the_command_just_before(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    llm = FakeLLM(a.OpenApp(app="Zoom Workplace"))
    assistant, _, _ = make(Parse("open_app", False), llm=llm, preferences=prefs)
    assistant.handle("zoom wala kholo", "hi-IN")  # learned as a habit
    assistant.parser = FakeParser(sure("cancel"))
    assistant.handle("rehne do", "hi-IN")  # teaches nothing
    assert assistant.handle("galat tha", "hi-IN").key == "nothing_to_forget"
    assert "zoom wala kholo" in prefs.habits


def test_undo_covers_everything_one_command_taught(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.begin_command()
    prefs.learn_app_name("visual studio", "Visual Studio Installer")  # the executor's loose match
    prefs.learn_habit("visual studio kholo", a.OpenApp(app="visual studio"))
    prefs.note_media_app("YouTube pe gaane chalao", "", "YouTube")
    assert prefs.forget_last()
    assert prefs.app_names == {} and prefs.habits == {} and not prefs.media.get("music")
    assert Preferences(tmp_path / "p.json").app_names == {}  # saved


def test_undo_restores_a_name_it_overwrote(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_app_name("editor", "Visual Studio Code")
    prefs.begin_command()
    prefs.learn_app_name("editor", "Notepad")
    assert prefs.forget_last() and prefs.app_names == {"editor": "Visual Studio Code"}


def test_native_script_commands_keep_their_vowel_signs():
    from bolos.executor.apps import key
    from bolos.preferences import habit_key

    assert habit_key("नोटपैड खोलो") != habit_key("नोटपैड खेलो")  # open vs play
    assert habit_key("नोटपैड खोलो।") == habit_key("नोटपैड खोलो")
    assert key("राम") != key("रोमा")


def test_fields_filled_by_system2_never_freeze_system1s_action(tmp_path):
    # System 1 sure (and wrong) of play_media, unsure of the query: System 2 only fills the query.
    prefs = Preferences(tmp_path / "p.json")
    llm = FakeLLM(a.PlayMedia(query="Radiohead"))
    assistant, runner, _ = make(Parse("play_media", True, {}, frozenset({"query"})), llm=llm, preferences=prefs,
                                runner=FakeRunner(Outcome(True, "playing_on", {"query": "q", "app": "YouTube"})))
    assistant.handle("Radiohead kaun hai", "hi-IN")
    assert runner.executed and prefs.habits == {}


def test_dictated_text_is_never_stored(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    assistant, _, _ = make(Parse("type_text", False), llm=FakeLLM(a.TypeText(text="mera pin 4321 hai")),
                           preferences=prefs, runner=FakeRunner(Outcome(True, "typed")))
    assistant.handle("likho ki mera pin 4321 hai", "hi-IN")
    assert prefs.habits == {}


@pytest.mark.parametrize("reply,expected", [("galat nahi tha", False), ("ye galat nahi hai", False),
                                            ("nahi, galat tha", True), ("tappu illa", False)])
def test_a_negated_correction_is_not_one(reply, expected):
    from bolos.responses import is_correction

    assert is_correction(reply) is expected


def test_service_votes_merge_spellings(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    for app in ["Spotify", "spotify", "Spotify", "spotify"]:
        prefs.note_media_app("x chalao", "x", app)
    for _ in range(3):
        prefs.note_media_app("y chalao", "y", "YouTube")
    assert prefs.media_app("Radiohead chalao", "Radiohead") == "Spotify"


def test_a_dry_run_correction_changes_nothing(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_habit("zoom wala kholo", a.OpenApp(app="Zoom"))
    assistant, _, _ = make(Parse("open_app", False), preferences=prefs, runner=FakeRunner(Outcome(True, "dry_run")))
    assistant.learning = False
    assistant.handle("zoom wala kholo", "hi-IN")
    assert assistant.handle("galat tha", "hi-IN").key == "forgot"
    assert "zoom wala kholo" in Preferences(tmp_path / "p.json").habits


def test_brief_cuts_long_screen_text_at_a_word_boundary():
    from bolos.pipeline import READBACK_MAX_CHARS, _brief

    assert _brief("short  text\nhere") == "short text here"
    long = "word " * 500
    cut = _brief(long)
    assert len(cut) <= READBACK_MAX_CHARS + 3 and cut.endswith("...") and not cut.endswith("wor...")
