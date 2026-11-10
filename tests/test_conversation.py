"""Answers, follow-ups and the conversation memory, without speech APIs, Windows actions or a real LLM."""

from datetime import datetime

import pytest

from bolos import actions as a
from bolos.conversation import Conversation, Turn, asks_for_search, needs_live_facts, refers_back
from bolos.decide import Parse
from bolos.executor import Outcome
from bolos.llm import LLM, Heard, SlotError
from bolos.pipeline import Assistant
from bolos.preferences import Preferences
from bolos.responses import say

MODI = "नरेंद्र मोदी भारत के प्रधानमंत्री हैं।"


# --- The memory itself -----------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_turns_are_kept_oldest_first_and_only_the_last_few():
    talk = Conversation(max_turns=2)
    for n in range(3):
        talk.add(f"command {n}", [a.OpenApp(app=f"App {n}")], f"reply {n}")

    assert [turn.said for turn in talk.recent()] == ["command 1", "command 2"]
    assert talk.last().did == ({"action": "open_app", "app": "App 2"},)


def test_an_old_conversation_is_forgotten():
    clock = Clock()
    talk = Conversation(window_s=180, clock=clock)
    talk.add("Narendra Modi kaun hai?", [a.Answer(text=MODI)], MODI)
    clock.now += 179
    assert talk.recent()
    clock.now += 2
    assert talk.recent() == [] and talk.last() is None


def test_a_long_reply_is_remembered_in_part():
    talk = Conversation()
    talk.add("sau shabd bolo", [a.Answer(text="x")], "shabd " * 400)
    assert len(talk.last().reply) <= 600


@pytest.mark.parametrize("command", [
    "isko YouTube pe chalao", "unke baare mein search karo", "tell me more about him", "yeh Raju ko bhej do",
    "type that", "इसे लिख दो", "adhai pathi Google la thedu", "alli eshtu jana idare?", "ಅದನ್ನು ಹುಡುಕು",
])
def test_a_pronoun_points_back(command):
    assert refers_back(command)


@pytest.mark.parametrize("command", [
    "Calculator kholo", "Spotify par Pink Floyd ke gaane chalao", "gaana rok do", "Narendra Modi kaun hai?",
    "Chrome se search karo", "take a screenshot", "",
])
def test_a_complete_command_does_not_point_back(command):
    assert not refers_back(command)


@pytest.mark.parametrize("command", [
    "Amazon par running shoes search karo", "running shoes dhondho", "Google pe dhundho", "transformer khojo",
    "look up the Taj Mahal", "ताजमहल सर्च करो", "ಇದನ್ನು ಹುಡುಕು", "adhai thedu",
])
def test_a_request_to_search_is_recognised(command):
    assert asks_for_search(command)


@pytest.mark.parametrize("command", ["Narendra Modi kaun hai?", "photosynthesis kya hota hai", "tum kaun ho"])
def test_a_question_is_not_a_request_to_search(command):
    assert not asks_for_search(command) and not needs_live_facts(command)


@pytest.mark.parametrize("command", ["Bangalore ka weather batao", "aaj ki khabar sunao", "sone ka bhav kya hai",
                                     "दिल्ली का मौसम कैसा है", "India ka score kya hai"])
def test_live_facts_are_recognised(command):
    assert needs_live_facts(command)


# --- LLM.converse: the prompt and what is made of the answer ---------------------------------------------

class ScriptedLLM(LLM):
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def _structured(self, system, user, schema, images=()):
        self.prompts.append((system, user, schema))
        return self.answer


def test_converse_returns_a_spoken_answer_without_markup():
    llm = ScriptedLLM({"steps": [{"action": "answer", "reply": "**नरेंद्र मोदी**\nभारत के प्रधानमंत्री हैं।"}],
                       "language": "hi"})
    heard = llm.converse("Narendra Modi kaun hai?", "hi")

    assert heard == Heard([a.Answer(text="नरेंद्र मोदी भारत के प्रधानमंत्री हैं।")], "hi")


def test_converse_shows_the_conversation_the_date_and_the_reply_language():
    llm = ScriptedLLM({"steps": [{"action": "web_search", "query": "Narendra Modi"}], "language": "hi"})
    turns = [Turn("Narendra Modi kaun hai?", ({"action": "answer", "text": MODI},), MODI)]
    heard = llm.converse("unke baare mein search karo", "hi", turns, now=datetime(2026, 11, 11, 14, 5))

    system, user, schema = llm.prompts[0]
    assert heard.steps == [a.WebSearch(query="Narendra Modi")]
    assert "Now: Wednesday, 11 November 2026, 02:05 PM" in user
    assert "Person: Narendra Modi kaun hai?" in user and user.count(MODI) == 1  # the answer's words appear once
    assert "Reply language: Hindi\n" in user and user.endswith("Request: unke baare mein search karo\nJSON:")
    assert "answer" in system and schema["properties"]["steps"]["items"]["required"][-1] == "reply"


def test_converse_asks_for_the_language_only_when_it_is_not_known():
    llm = ScriptedLLM({"steps": [{"action": "answer", "reply": "ಬೆಂಗಳೂರು."}], "language": "kn"})
    heard = llm.converse("Karnataka da rajadhani yaavudu?", "hi-IN", language_sure=False)

    assert heard.language == "kn"
    assert "the language the person spoke" in llm.prompts[0][1]


def test_converse_lists_this_pcs_apps_only_when_given():
    llm = ScriptedLLM({"steps": [{"action": "open_app", "app": "Obsidian"}], "language": "hi"})
    llm.converse("Cidian kholu", "hi", apps=["Notepad", "Obsidian"])
    llm.converse("Narendra Modi kaun hai?", "hi")

    assert "Apps on this PC: Notepad, Obsidian" in llm.prompts[0][1]
    assert "Apps on this PC" not in llm.prompts[1][1]


@pytest.mark.parametrize("answer", [
    {"steps": []},
    {"steps": [{"action": "answer", "reply": "  "}]},
    {"steps": [{"action": "answer", "reply": "hello"}, {"action": "open_app", "app": "Chrome"}]},
    {"steps": [{"action": "open_app", "app": "Chrome"}, {"action": "unknown"}]},
    {"steps": [{"action": "open_app", "app": "A"}] * 4},
    {"steps": [{"action": "dance"}]},
])
def test_converse_rejects_answers_it_cannot_use(answer):
    with pytest.raises(SlotError):
        ScriptedLLM(answer).converse("kuch bhi", "hi")


def test_a_very_long_answer_is_cut_at_a_sentence_end():
    reply = "यह एक वाक्य है। " * 200
    heard = ScriptedLLM({"steps": [{"action": "answer", "reply": reply}], "language": "hi"}).converse("bolo", "hi")

    assert len(heard.steps[0].text) <= 900 and heard.steps[0].text.endswith("है।")


# --- The pipeline: when System 2 is asked, and what happens to its answer --------------------------------

class Voice:
    user_language = "hi-IN"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.spoken = []

    def speak(self, text, language):
        self.spoken.append(text)

    def followup(self):
        return self.replies.pop(0) if self.replies else None

    listen = followup


class Parser:
    def __init__(self, rows):
        self.rows = rows

    def parse(self, text):
        return self.rows.get(text, Parse("unknown", False))


class Runner:
    def __init__(self, installed=("Calculator", "Obsidian")):
        self.installed = installed
        self.executed = []

    def knows_app(self, spoken):
        return spoken in self.installed

    def app_names(self):
        return list(self.installed)

    def execute(self, action):
        self.executed.append(action)
        match action:
            case a.OpenApp(app=app):
                return Outcome(True, "opened", {"app": app})
            case a.WebSearch(query=query):
                return Outcome(True, "searched", {"query": query})
            case a.PlayMedia(query=query, app=app):
                return Outcome(True, "playing_on", {"query": query, "app": app or "Spotify"})
            case a.MediaControl(control=control):
                return Outcome(True, f"control_{control}")
            case a.SendMessage(contact=contact):
                return Outcome(True, "message_draft", {"contact": contact})
        return Outcome(True, "typed")


class Chat:
    """System 2: answers each command from a table and records what it was shown."""

    def __init__(self, answers, language=None):
        self.answers = answers
        self.language = language
        self.calls = []

    def converse(self, command, language=None, turns=(), now=None, *, language_sure=True, apps=()):
        self.calls.append({"command": command, "language": language, "turns": list(turns),
                           "language_sure": language_sure, "apps": list(apps)})
        steps = self.answers[command]
        if isinstance(steps, Exception):
            raise steps
        return Heard(list(steps), self.language)

    def resolve_steps(self, command):
        raise AssertionError("the conversation replaces resolve_steps")

    def fill(self, action, command, fields):
        raise AssertionError("System 2 is asked about a command once")

    def resolve(self, command):
        raise AssertionError("System 2 is asked about a command once")


QUESTION = "Narendra Modi kaun hai?"
WHO = Parse("web_search", True, {"query": "Narendra Modi kaun hai"})


def make(rows, answers, *, voice=None, preferences=None, language=None, runner=None):
    voice, runner, llm = voice or Voice(), runner or Runner(), Chat(answers, language)
    bot = Assistant(Parser(rows), llm, runner, voice, preferences=preferences)
    return bot, llm, runner, voice


def test_a_question_is_answered_aloud_instead_of_searched():
    statuses = []
    bot, llm, runner, voice = make({QUESTION: WHO}, {QUESTION: [a.Answer(text=MODI)]})
    bot.status = lambda *event: statuses.append(event)

    outcome = bot.handle(QUESTION, "hi")

    assert outcome == Outcome(True, "answered") and runner.executed == []
    assert voice.spoken == [say("one_moment", "hi"), MODI]
    assert ("reply", MODI) in statuses and ("state", "done") in statuses
    assert bot.conversation.last() == Turn(QUESTION, ({"action": "answer", "text": MODI},), MODI)


def test_a_follow_up_is_resolved_with_the_conversation():
    follow_up = "unke baare mein Google pe search karo"
    rows = {QUESTION: WHO, follow_up: Parse("web_search", True, {"query": "unke baare mein"})}
    bot, llm, runner, voice = make(rows, {QUESTION: [a.Answer(text=MODI)],
                                          follow_up: [a.WebSearch(query="Narendra Modi")]})
    bot.handle(QUESTION, "hi")
    bot.handle(follow_up, "hi")

    assert runner.executed == [a.WebSearch(query="Narendra Modi")]
    assert [turn.said for turn in llm.calls[1]["turns"]] == [QUESTION] and llm.calls[1]["turns"][0].reply == MODI
    assert voice.spoken[-1] == say("searched", "hi", query="Narendra Modi")


def test_the_previous_answer_can_be_sent_after_the_usual_confirmation():
    from bolos.contacts import Contact, ContactBook

    send = "yeh Raju ko bhej do"
    voice = Voice([("haan", None)])
    bot, llm, runner, _ = make({QUESTION: WHO}, {QUESTION: [a.Answer(text=MODI)],
                                                 send: [a.SendMessage(contact="Raju", text=MODI)]}, voice=voice)
    bot.contacts = ContactBook([Contact("Raju", "+910000000000", ())])
    bot.handle(QUESTION, "hi")
    outcome = bot.handle(send, "hi")

    assert outcome.key == "message_draft" and runner.executed == [a.SendMessage(contact="Raju", text=MODI)]
    assert say("confirm_send", "hi", contact="Raju", text=MODI) in voice.spoken
    # A message is the user's own business: it is neither remembered nor sent along with the next request.
    assert [turn.said for turn in bot.conversation.recent()] == [QUESTION]


def test_a_follow_up_never_becomes_a_habit(tmp_path):
    play, again = "Spotify par Pink Floyd chalao", "isko YouTube pe chalao"
    rows = {play: Parse("play_media", True, {"query": "Pink Floyd", "app": "Spotify"})}
    prefs = Preferences(tmp_path / "preferences.json")
    bot, llm, runner, _ = make(rows, {again: [a.PlayMedia(query="Pink Floyd", app="YouTube")]}, preferences=prefs)
    bot.handle(play, "hi")
    bot.handle(again, "hi")

    assert runner.executed[-1] == a.PlayMedia(query="Pink Floyd", app="YouTube")
    assert prefs.recall(again) is None


def test_an_answer_never_becomes_a_habit_but_a_first_command_from_system_two_does(tmp_path):
    prefs = Preferences(tmp_path / "preferences.json")
    odd = "woh likhne wala app kholo"
    bot, llm, runner, _ = make({QUESTION: WHO}, {QUESTION: [a.Answer(text=MODI)],
                                                 odd: [a.OpenApp(app="Notepad")]}, preferences=prefs)
    bot.handle(QUESTION, "hi")
    bot.conversation.clear()
    bot.handle(odd, "hi")

    assert prefs.recall(QUESTION) is None
    assert prefs.recall(odd) == a.OpenApp(app="Notepad")


@pytest.mark.parametrize("command, parse", [
    ("Amazon par running shoes search karo", Parse("web_search", True, {"query": "running shoes", "site": "Amazon"})),
    ("running shoes dhundho", Parse("web_search", True, {"query": "running shoes"})),
    ("Bangalore ka weather batao", Parse("web_search", True, {"query": "Bangalore weather"})),
    ("Calculator kholo", Parse("open_app", True, {"app": "Calculator"})),
    ("Spotify par Pink Floyd chalao", Parse("play_media", True, {"query": "Pink Floyd", "app": "Spotify"})),
])
def test_clear_commands_stay_on_this_computer(command, parse):
    bot, llm, runner, voice = make({command: parse}, {})

    assert bot.handle(command, "hi").ok
    assert llm.calls == [] and len(runner.executed) == 1 and say("one_moment", "hi") not in voice.spoken


def test_a_media_control_with_a_pronoun_needs_no_conversation():
    play, pause = "Spotify par Pink Floyd chalao", "ise rok do"
    rows = {play: Parse("play_media", True, {"query": "Pink Floyd", "app": "Spotify"}),
            pause: Parse("media_control", True, control="pause")}
    bot, llm, runner, _ = make(rows, {})
    bot.handle(play, "hi")

    assert bot.handle(pause, "hi") == Outcome(True, "control_pause") and llm.calls == []


def test_chatter_gets_a_reply_instead_of_not_understood():
    bot, llm, runner, voice = make({"tum kaun ho?": Parse("unknown", True)},
                                   {"tum kaun ho?": [a.Answer(text="मैं साहुर हूँ।")]})

    assert bot.handle("tum kaun ho?", "hi").ok and voice.spoken[-1] == "मैं साहुर हूँ।"


def test_silence_is_not_sent_to_system_two():
    bot, llm, runner, voice = make({"...": Parse("unknown", True), "2": Parse("unknown", True)}, {})

    assert bot.handle("...", "hi").key == "not_understood" and bot.handle("2", "hi").key == "not_understood"
    assert llm.calls == []


def test_a_misheard_app_name_is_matched_against_this_pcs_apps():
    bot, llm, runner, voice = make({"Cidian kholu": Parse("open_app", True, {"app": "Cidian"})},
                                   {"Cidian kholu": [a.OpenApp(app="Obsidian")]})

    assert bot.handle("Cidian kholu", "hi").ok
    assert runner.executed == [a.OpenApp(app="Obsidian")] and llm.calls[0]["apps"] == ["Calculator", "Obsidian"]


def test_app_names_are_not_sent_with_a_question():
    bot, llm, runner, _ = make({QUESTION: WHO}, {QUESTION: [a.Answer(text=MODI)]})
    bot.handle(QUESTION, "hi")

    assert llm.calls[0]["apps"] == []


def test_a_request_to_tell_a_joke_is_not_played_as_a_song():
    joke = "ek joke sunao"
    bot, llm, runner, voice = make({joke: Parse("play_media", True, {"query": "ek joke"}, frozenset({"query"}))},
                                   {joke: [a.Answer(text="एक चुटकुला।")]})

    assert bot.handle(joke, "hi").ok and runner.executed == [] and voice.spoken[-1] == "एक चुटकुला।"


@pytest.mark.parametrize("failure", [RuntimeError("429 quota exceeded"), SlotError("no steps"), [a.Unknown()]])
def test_when_system_two_cannot_help_system_one_still_searches(failure):
    bot, llm, runner, voice = make({QUESTION: WHO}, {QUESTION: failure})

    assert bot.handle(QUESTION, "hi") == Outcome(True, "searched", {"query": "Narendra Modi kaun hai"})
    assert len(llm.calls) == 1 and runner.executed == [a.WebSearch(query="Narendra Modi kaun hai")]


def test_when_system_two_cannot_help_chatter_is_asked_again():
    bot, llm, runner, voice = make({"haan toh": Parse("unknown", True)}, {"haan toh": [a.Unknown()]})

    assert bot.handle("haan toh", "hi").key == "not_understood" and runner.executed == []


def test_an_unsure_command_is_asked_once_with_the_conversation():
    bot, llm, runner, _ = make({"Calculator kholo": Parse("open_app", True, {"app": "Calculator"})},
                               {"aur Notepad bhi": [a.OpenApp(app="Notepad")]})
    bot.handle("Calculator kholo", "hi")
    bot.handle("aur Notepad bhi", "hi")

    assert runner.executed[-1] == a.OpenApp(app="Notepad") and len(llm.calls) == 1
    assert [turn.said for turn in llm.calls[0]["turns"]] == ["Calculator kholo"]


def test_an_answer_can_follow_what_could_be_done():
    command = "Camera kholo aur meri photo lo"
    bot, llm, runner, voice = make({}, {command: [a.OpenApp(app="Camera"), a.Answer(text="बटन आपको दबाना होगा।")]})

    assert bot.handle(command, "hi").ok and runner.executed == [a.OpenApp(app="Camera")]
    assert voice.spoken[-1] == "Camera खोल दिया और बटन आपको दबाना होगा।"
    assert bot.conversation.last().reply == "बटन आपको दबाना होगा।"


def test_system_two_names_the_language_the_detector_could_not():
    question = "Karnataka da rajadhani yaavudu?"
    bot, llm, runner, voice = make({}, {question: [a.Answer(text="ಬೆಂಗಳೂರು.")]}, language="kn")

    bot.handle(question, None)

    assert llm.calls[0]["language_sure"] is False and bot.language == "kn"
    bot.handle("Calculator kholo", None)  # the next reply stays in the conversation's language
    assert voice.spoken[-1] == say("not_understood", "kn")


def test_a_language_known_from_the_script_is_not_second_guessed():
    question = "नरेंद्र मोदी कौन हैं?"
    bot, llm, runner, voice = make({}, {question: [a.Answer(text=MODI)]}, language="en")

    bot.handle(question, None)

    assert llm.calls[0]["language_sure"] is True and bot.language == "hi"


def test_show_it_again_repeats_the_lesson_instead_of_resuming_music():
    class Guide:
        pending = None

        def __init__(self):
            self.goals = []

        def run(self, goal, language, **_):
            self.goals.append(goal)
            return Outcome(False, "guide_lost")

    lesson = "Microsoft Store se game kaise download karein, dikhao"
    again = "Phir se dikhao."
    bot, llm, runner, voice = make({again: Parse("media_control", True, control="resume")}, {})
    bot.guide = guide = Guide()
    bot.handle(lesson, "hi")
    bot.handle(again, "hi")

    assert guide.goals == [lesson, lesson] and runner.executed == [] and llm.calls == []


@pytest.mark.parametrize("command, action", [
    ("likho ki kal chhutti hai", a.TypeText(text="kal chhutti hai")),
    ("screen par kya hai", None),
])
def test_private_turns_are_not_remembered(command, action):
    rows = {command: Parse("type_text", True, {"text": "kal chhutti hai"})} if action else {}
    bot, llm, runner, _ = make(rows, {})
    bot.handle(command, "hi")

    assert bot.conversation.recent() == []


def test_the_status_card_does_not_print_an_answer_twice():
    from bolos.overlay import Step, describe_step

    assert describe_step(Step("answer", {"text": MODI})) == ("Answer", "in words", "")


OFFER = "ब्राइटनेस मैं खुद नहीं बदल सकती। आप कहें तो सेटिंग्स खोल दूँ।"


def test_yes_accepts_what_the_last_answer_offered():
    bot, llm, runner, voice = make({"brightness kam karo": Parse("unknown", True)},
                                   {"brightness kam karo": [a.Answer(text=OFFER)],
                                    "haan": [a.OpenApp(app="Settings")]})
    bot.handle("brightness kam karo", "hi")

    assert bot.handle("haan", "hi").ok and runner.executed == [a.OpenApp(app="Settings")]
    assert llm.calls[1]["turns"][0].reply == OFFER


def test_yes_without_an_offer_authorizes_nothing():
    bot, llm, runner, voice = make({"Calculator kholo": Parse("open_app", True, {"app": "Calculator"})}, {})
    bot.handle("Calculator kholo", "hi")

    assert bot.handle("haan", "hi").key == "not_understood" and bot.handle("yes", "hi").key == "not_understood"
    assert llm.calls == [] and len(runner.executed) == 1


@pytest.mark.parametrize("risky", [a.TypeText(text="hello"), a.Click(target="Submit"),
                                   a.SendMessage(contact="Raju", text="hello")])
def test_yes_to_an_offer_never_types_clicks_or_sends(risky):
    bot, llm, runner, voice = make({QUESTION: WHO}, {QUESTION: [a.Answer(text=MODI)], "haan": [risky]})
    bot.handle(QUESTION, "hi")

    assert bot.handle("haan", "hi").key == "not_understood" and runner.executed == []


@pytest.mark.parametrize("risky", [a.TypeText(text="Narendra Modi"), a.Click(target="Search"),
                                   a.SendMessage(contact="Raju", text="hello")])
@pytest.mark.parametrize("follow_up", [False, True])
def test_system_two_cannot_turn_a_question_into_typing_clicking_or_a_message(risky, follow_up):
    question = "What does that mean?" if follow_up else QUESTION
    search = a.WebSearch(query=question.rstrip("?"))
    rows = {QUESTION: WHO, question: Parse("web_search", True, {"query": search.query})}
    bot, llm, runner, voice = make(rows, {QUESTION: [a.Answer(text=MODI)], question: [risky]})
    if follow_up:
        bot.handle(QUESTION, "hi")

    assert bot.handle(question, "hi") == Outcome(True, "searched", {"query": search.query})
    assert runner.executed == [search]


def test_the_executor_knows_which_apps_this_pc_has(monkeypatch):
    from bolos.contacts import ContactBook
    from bolos.executor import Executor, sites

    class Catalog:
        def find(self, spoken, learned):
            return object() if spoken == "Obsidian" else None

        def names(self):
            return ["Visual Studio Code", "Obsidian", "Run"]

    monkeypatch.setattr(sites, "known", lambda spoken: spoken == "Netflix")
    executor = Executor(ContactBook([]), Catalog())

    assert executor.knows_app("Obsidian") and executor.knows_app("Netflix") and not executor.knows_app("Cidian")
    assert executor.app_names() == ["Run", "Obsidian", "Visual Studio Code"]


def test_what_sahur_says_after_an_action_is_not_shown_as_something_to_send():
    from bolos.llm import _conversation

    turns = [Turn(QUESTION, ({"action": "answer", "text": MODI},), MODI),
             Turn("search karo", ({"action": "web_search", "query": "Narendra Modi"},), "Narendra Modi सर्च कर दिया।")]
    shown = _conversation(turns)

    assert f"and said: {MODI}" in shown and "सर्च कर दिया" not in shown


def test_one_moment_stays_in_the_conversations_language():
    follow_up = "and Tamil Nadu?"
    bot, llm, runner, voice = make({}, {"what is the capital of Karnataka": [a.Answer(text="Bengaluru.")],
                                        follow_up: [a.Answer(text="Chennai.")]}, language="en")
    bot.language_detector = lambda command: "ta" if "Tamil" in command else "en"
    bot.handle("what is the capital of Karnataka", None)
    bot.handle(follow_up, None)

    assert voice.spoken[-2:] == [say("one_moment", "en"), "Chennai."] and bot.language == "en"
