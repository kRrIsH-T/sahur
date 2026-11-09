"""Compound commands: the joiner lexicon, splitting (with a fake System 1 and a few real-model cases),
context carried between steps, and opening an address in a named browser."""

import time

import pytest

from bolos import actions as a
from bolos.compound import MAX_CUTS, action_of, browser_name, carry_over, chain, media_service, split
from bolos.decide import Parse
from bolos.fast.conjunctions import (
    AND_WORDS,
    CONJUNCTIONS,
    PARTICIPLES,
    and_word,
    find_joiners,
    first_word_length,
    has_joiner,
    tokens,
)

LANGUAGES = ("hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa")


# The lexicon.

def joiners(text: str) -> list[tuple[str, bool]]:
    return [(j.text, j.participle) for j in find_joiners(tokens(text)) if j.languages]


def test_every_language_has_conjunctions_and_an_and_word():
    for lang in LANGUAGES:
        assert CONJUNCTIONS[lang], lang
        assert and_word(lang) == AND_WORDS[lang]
    assert all(PARTICIPLES[lang] for lang in LANGUAGES if lang != "en")
    assert and_word(None) == "and" and and_word("xx") == "and"


@pytest.mark.parametrize("text, expected", [
    ("Chrome kholo aur weather batao", [("aur", False)]),
    ("Chrome kholo fir weather batao", [("fir", False)]),  # "phir" folded
    ("Chrome kholo uske baad weather batao", [("uske baad", False)]),  # the longer phrase wins
    ("YouTube khol ke Arijit ke gaane chalao", [("khol ke", True)]),
    ("Spotify open karke gaane chalao", [("karke", True)]),
    ("Chrome open madi amele weather huduku", [("madi", True), ("amele", False)]),  # "aamele" folded
    ("Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari", [("ಮತ್ತೆ", False)]),
    ("Chrome thirandhu score thedu", [("thirandhu", True)]),
    ("Calculator thira appuram Excel thira", [("appuram", False)]),
    ("YouTube open chesi paatalu pettu", [("chesi", True)]),
    ("Calculator ughad nantar Excel ughad", [("nantar", False)]),
    ("Calculator thurakku ennittu Excel thurakku", [("ennittu", False)]),
    ("Calculator kholo tarpor Excel kholo", [("tarpor", False)]),
    ("Notepad kholine lakho", [("kholine", True)]),
    ("ਕ੍ਰੋਮ ਖੋਲ੍ਹੋ ਅਤੇ ਮੌਸਮ ਲੱਭੋ", [("ਅਤੇ", False)]),
    ("क्रोम खोलो और मौसम बताओ", [("और", False)]),
    ("open chrome and then search news", [("and then", False)]),
])
def test_joiners_in_every_language(text, expected):
    assert joiners(text) == expected


def test_short_english_words_are_not_the_joiners_they_fold_into():
    # "the" folds to Punjabi "te", "any" to Marathi "ani", "ten" to "then": only the listed spelling counts
    assert joiners("play the next one") == []
    assert joiners("open any song at ten") == []
    assert joiners("Spotify te Diljit de gaane chalao") == [("te", False)]
    assert joiners("Excel ughad ani Word ughad") == [("ani", False)]
    assert joiners("gaan bondho koro aar Notepad kholo") == [("aar", False)]


def test_punctuation_is_a_joiner_but_a_dot_in_an_address_is_not():
    toks = tokens("Chrome kholo, amazon.in kholo.")
    assert [t.text for t in toks] == ["Chrome", "kholo", ",", "amazon.in", "kholo", "."]
    assert [j.text for j in find_joiners(toks)] == [",", "."]


def test_has_joiner_needs_words_on_both_sides():
    assert has_joiner("Chrome kholo aur weather batao")
    assert not has_joiner("aur batao")
    assert not has_joiner("YouTube kholo phir")
    assert not has_joiner("Chrome kholo")


def test_first_words():
    assert first_word_length(tokens("pehle Chrome kholo"), 0) == 1
    assert first_word_length(tokens("sabse pehle Chrome kholo"), 0) == 2
    assert first_word_length(tokens("Chrome kholo"), 0) == 0


# Splitting, with a fake System 1: a table of what it understands; anything else is unknown and unsure.

def sure(action, control=None, confidence=0.99, **slots):
    return Parse(action, True, slots, control=control, confidence=confidence)


class FakeParser:
    def __init__(self, table: dict[str, Parse]):
        self.table = table
        self.calls: list[str] = []

    def parse(self, command: str) -> Parse:
        self.calls.append(command)
        return self.table.get(command, Parse("unknown", False))


OPEN_CHROME = sure("open_app", app="Chrome")
WEATHER = sure("web_search", query="Bangalore weather")


def test_splits_at_a_conjunction():
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    assert split("Chrome kholo aur Bangalore weather batao", parser) == ["Chrome kholo", "Bangalore weather batao"]


def test_conjunctions_and_punctuation_at_the_cut_are_dropped():
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    assert split("Chrome kholo, aur phir Bangalore weather batao", parser) == [
        "Chrome kholo", "Bangalore weather batao"]
    assert split("Chrome kholo. Bangalore weather batao.", parser) == ["Chrome kholo", "Bangalore weather batao"]


def test_a_participle_stays_with_the_first_step():
    parser = FakeParser({"Chrome khol ke": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    assert split("Chrome khol ke Bangalore weather batao", parser) == ["Chrome khol ke", "Bangalore weather batao"]


def test_first_words_are_dropped_from_step_one():
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    assert split("pehle Chrome kholo phir Bangalore weather batao", parser) == [
        "Chrome kholo", "Bangalore weather batao"]


def test_a_leading_conjunction_belongs_to_no_step():
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    assert split("aur phir Chrome kholo aur Bangalore weather batao", parser) == [
        "Chrome kholo", "Bangalore weather batao"]
    assert split("and then first Chrome kholo aur Bangalore weather batao", parser) == [
        "Chrome kholo", "Bangalore weather batao"]
    assert split("aur Bangalore weather batao", parser) is None
    assert split("aur", parser) is None and split(", aur phir", parser) is None


def test_no_joiner_means_no_split_and_no_parse():
    parser = FakeParser({})
    assert split("Bangalore weather batao", parser) is None
    assert parser.calls == []


def test_the_whole_parse_given_is_not_made_again():
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER})
    command = "Chrome kholo aur Bangalore weather batao"
    split(command, parser, whole=Parse("multi", True))
    assert command not in parser.calls


def test_three_steps_beat_two():
    parser = FakeParser({
        "Chrome kholo": OPEN_CHROME, "Bangalore weather batao": WEATHER,
        "gaana rok do": sure("media_control", control="pause"),
        # System 1 misreading two steps as one, surely: the three-step split still wins
        "Bangalore weather batao aur gaana rok do": WEATHER,
    })
    assert split("Chrome kholo phir Bangalore weather batao aur gaana rok do", parser) == [
        "Chrome kholo", "Bangalore weather batao", "gaana rok do"]


def test_among_splits_with_as_many_parts_the_surest_weakest_part_wins():
    parser = FakeParser({
        "Notepad kholo": sure("open_app", app="Notepad", confidence=0.99),
        "Excel kholo phir Word kholo": sure("open_app", app="Excel", confidence=0.6),
        "Notepad kholo aur Excel kholo": sure("open_app", app="Notepad", confidence=0.9),
        "Word kholo": sure("open_app", app="Word", confidence=0.99),
    })  # "Excel kholo" alone is not understood, so no three-part split
    assert split("Notepad kholo aur Excel kholo phir Word kholo", parser) == [
        "Notepad kholo aur Excel kholo", "Word kholo"]


@pytest.mark.parametrize("part", [
    Parse("unknown", True), Parse("cancel", True), Parse("multi", True),
    Parse("web_search", False, {"query": "Bangalore weather"}),  # unsure of the action
    Parse("web_search", True, {"query": "Bangalore weather"}, unsure=frozenset({"query"})),  # of a field
    Parse("open_app", True, {}),  # sure, but no app: not a valid action
])
def test_every_part_must_be_a_sure_complete_step(part):
    parser = FakeParser({"Chrome kholo": OPEN_CHROME, "Bangalore weather batao": part})
    assert split("Chrome kholo aur Bangalore weather batao", parser) is None


def test_titles_are_not_split():
    # "Tom" is one word, never a step; "Jerry chalao" alone means nothing without it
    parser = FakeParser({"Tom": sure("web_search", query="Tom"), "Jerry chalao": sure("play_media", query="Jerry")})
    assert split("Tom and Jerry chalao", parser) is None


def test_a_part_that_is_only_a_name_is_not_an_open():
    parser = FakeParser({"Romeo aur Juliet movie dikhao": sure("play_media", query="Romeo aur Juliet movie"),
                         "Shah Rukh": sure("open_app", app="Shah Rukh"),
                         "Kajol ki movie chalao": sure("play_media", query="Kajol ki movie")})
    assert split("Shah Rukh aur Kajol ki movie chalao", parser) is None


def test_never_cut_inside_a_message():
    command = "Raju ko bolo ki main aa raha hoon aur khana kha lunga"
    message = Parse("send_message", True, {"contact": "Raju", "text": "main aa raha hoon aur khana kha lunga"},
                    unsure=frozenset({"text"}))  # sure of the action is enough, even unsure of the text's end
    parser = FakeParser({command: message,
                         "Raju ko bolo ki main aa raha hoon": sure("send_message", contact="Raju",
                                                                   text="main aa raha hoon"),
                         "khana kha lunga": sure("type_text", text="khana kha lunga")})
    assert split(command, parser) is None


def test_never_cut_inside_a_field_system1_is_sure_of():
    command = "search for salt and pepper chicken recipe"
    table = {"search for salt": sure("web_search", query="salt"),
             "pepper chicken recipe": sure("web_search", query="pepper chicken recipe")}
    whole = sure("web_search", query="salt and pepper chicken recipe")
    assert split(command, FakeParser({**table, command: whole})) is None
    # the same field, but System 1 unsure of the whole: its spans are no evidence, the parts decide
    unsure_whole = Parse("web_search", False, {"query": "salt and pepper chicken recipe"})
    assert split(command, FakeParser({**table, command: unsure_whole})) == ["search for salt",
                                                                             "pepper chicken recipe"]


def test_a_field_holding_the_joiner_word_from_one_side_does_not_block():
    command = "gaan bondho koro ar Notepad kholo"
    parser = FakeParser({command: sure("open_app", app="ar Notepad"),
                         "gaan bondho koro": sure("media_control", control="pause"),
                         "Notepad kholo": sure("open_app", app="Notepad")})
    assert split(command, parser) == ["gaan bondho koro", "Notepad kholo"]


def test_a_field_that_swallowed_the_first_step_does_not_block():
    # System 1 sure of one search for all of it, step 1's verb included: the tagger failed, the cut stays
    command = "ಕ್ರೋಮ್ ತೆರೆ ಮತ್ತು ಬೆಂಗಳೂರು ಹವಾಮಾನ ಹುಡುಕು"
    parser = FakeParser({command: sure("web_search", query="ಕ್ರೋಮ್ ತೆರೆ ಮತ್ತು ಬೆಂಗಳೂರು ಹವಾಮಾನ"),
                         "ಕ್ರೋಮ್ ತೆರೆ": sure("open_app", app="ಕ್ರೋಮ್"),
                         "ಬೆಂಗಳೂರು ಹವಾಮಾನ ಹುಡುಕು": sure("web_search", query="ಬೆಂಗಳೂರು ಹವಾಮಾನ")})
    assert split(command, parser) == ["ಕ್ರೋಮ್ ತೆರೆ", "ಬೆಂಗಳೂರು ಹವಾಮಾನ ಹುಡುಕು"]


def test_inside_a_swallowing_field_every_part_must_ask_for_something():
    command = "Pirates of the Caribbean aur Avengers ke baare mein batao"
    table = {command: sure("web_search", query="Pirates of the Caribbean aur Avengers"),
             "Pirates of the Caribbean": sure("web_search", query="Pirates of the Caribbean"),
             "Avengers ke baare mein batao": sure("web_search", query="Avengers")}
    assert split(command, FakeParser(table)) is None
    # System 1 unsure of the whole: its spans are no evidence, and a search that is all query is a step
    table[command] = Parse("web_search", False, {"query": "Pirates of the Caribbean aur Avengers"})
    assert split(command, FakeParser(table)) == ["Pirates of the Caribbean", "Avengers ke baare mein batao"]


def test_a_dictated_text_never_counts_as_swallowing():
    command = "Chrome kholo aur weather dekho likh do"
    parser = FakeParser({command: sure("type_text", text="Chrome kholo aur weather dekho"),
                         "Chrome kholo": OPEN_CHROME, "weather dekho likh do": sure("type_text", text="weather dekho")})
    assert split(command, parser) is None


def test_open_then_play_on_it_is_split_and_chained_into_one_step():
    command = "YouTube khol ke Arijit ke gaane chalao"
    parser = FakeParser({command: sure("play_media", query="Arijit ke gaane", app="YouTube"),
                         "YouTube khol ke": sure("open_app", app="YouTube"),
                         "Arijit ke gaane chalao": sure("play_media", query="Arijit ke gaane")})
    parts = split(command, parser)
    assert parts == ["YouTube khol ke", "Arijit ke gaane chalao"]
    assert chain([action_of(parser.parse(p)) for p in parts]) == [
        a.PlayMedia(query="Arijit ke gaane", app="YouTube")]


def test_two_opens_are_two_steps():
    command = "Calculator kholo aur Excel kholo"
    parser = FakeParser({command: sure("open_app", app="Calculator"),
                         "Calculator kholo": sure("open_app", app="Calculator"),
                         "Excel kholo": sure("open_app", app="Excel")})
    assert split(command, parser) == ["Calculator kholo", "Excel kholo"]


def test_too_many_cut_points_is_left_to_system2():
    command = " aur ".join(["Notepad kholo"] * (MAX_CUTS + 2))
    parser = FakeParser({"Notepad kholo": sure("open_app", app="Notepad")})
    assert split(command, parser) is None


def test_action_of():
    assert action_of(sure("media_control", control="pause")) == a.MediaControl(control="pause")
    assert action_of(sure("media_control", control="explode")) is None
    assert action_of(sure("web_search", query="x", app="ignored")) == a.WebSearch(query="x")
    assert action_of(sure("click")) is None


# Real System 1 (the installed model files). A few cases its numbers rest on; the full hand-written check in
# every language is in the round 3 report.

@pytest.fixture(scope="module")
def system1():
    from bolos.decide import FastParser
    from bolos.fast.model import MODELS_DIR

    model, slots = MODELS_DIR / "system1.npz", MODELS_DIR / "slots.npz"
    if not (model.exists() and slots.exists()):
        pytest.skip("System 1 model files are not installed")
    return FastParser(model, slots)


@pytest.mark.parametrize("command, steps", [
    ("Calculator kholo aur Excel kholo", ["open_app", "open_app"]),
    ("Chrome kholo aur Bangalore ka weather search karo", ["open_app", "web_search"]),
    ("open calculator and open notepad", ["open_app", "open_app"]),
    ("gaana rok do aur Notepad kholo", ["media_control", "open_app"]),
    ("Calculator open maadu, Notepad open maadu mattu Paint open maadu", ["open_app", "open_app", "open_app"]),
    ("ಕ್ರೋಮ್ ತೆರೆ ಮತ್ತು ಬೆಂಗಳೂರು ಹವಾಮಾನ ಹುಡುಕು", ["open_app", "web_search"]),
    ("volume koottu apram adutha paatu podu", ["media_control", "media_control"]),
    ("YouTube khol ke Arijit ke gaane chalao", ["play_media"]),  # the open folds into the play
])
def test_real_system1_splits_compounds(system1, command, steps):
    parts = split(command, system1)
    assert parts is not None
    assert [s.action for s in chain([action_of(system1.parse(p)) for p in parts])] == steps


@pytest.mark.parametrize("command", [
    "Tom and Jerry chalao",
    "Raju ko message bhejo ki main aa raha hoon aur khana kha lunga",
    "search for salt and pepper chicken recipe",
    "gaana phir se chalao",
    "Spotify te Diljit de gaane chalao",
    "and play the next one",
    "Ravi ge message kalisu naanu barthini mattu oota maadthini anta",
    "Mummy ko bolo ki Chrome kholo aur khana bana do",
])
def test_real_system1_keeps_single_commands_whole(system1, command):
    assert split(command, system1) is None


def test_real_system1_split_is_fast(system1):
    command = ("Chrome kholo aur phir Bangalore ka kal ka weather search karo uske baad YouTube pe Arijit Singh ke "
               "purane romantic gaane chalao aur awaaz thodi badhao")
    assert len(command.split()) >= 25
    whole = system1.parse(command)  # the pipeline has it already
    assert split(command, system1, whole) is not None  # and a warm-up
    best = min(_timed(lambda: split(command, system1, whole)) for _ in range(5))
    assert best < 0.030 * 2  # budget 30 ms; twice that so a busy machine doesn't fail the suite


def _timed(f) -> float:
    start = time.perf_counter()
    f()
    return time.perf_counter() - start


# Context carried from one step to the next.

@pytest.mark.parametrize("opened, browser", [
    (a.OpenApp(app="Chrome"), "Chrome"), (a.OpenApp(app="Google Chrome"), "Chrome"),
    (a.OpenApp(app="क्रोम"), "Chrome"), (a.OpenApp(app="microsoft edge"), "Edge"),
    (a.OpenApp(app="Firefox"), "Firefox"), (a.OpenApp(app="Brave browser"), "Brave"),
    (a.OpenApp(app="opera"), "Opera"), (a.WebSearch(query="news", browser="Edge"), "Edge"),
])
def test_a_browser_opened_first_is_where_the_next_search_and_site_open(opened, browser):
    assert carry_over(opened, a.WebSearch(query="weather")) == a.WebSearch(query="weather", browser=browser)
    assert carry_over(opened, a.OpenWebsite(site="Flipkart")) == a.OpenWebsite(site="Flipkart", browser=browser)


def test_what_the_user_said_in_the_next_step_wins():
    assert carry_over(a.OpenApp(app="Chrome"), a.WebSearch(query="x", browser="Edge")).browser == "Edge"
    assert carry_over(a.OpenApp(app="YouTube"), a.PlayMedia(query="x", app="Spotify")).app == "Spotify"
    assert carry_over(a.OpenApp(app="Amazon"), a.WebSearch(query="x", site="Flipkart")).site == "Flipkart"


@pytest.mark.parametrize("opened, service", [
    (a.OpenApp(app="YouTube"), "YouTube"), (a.OpenApp(app="यूट्यूब"), "YouTube"),
    (a.OpenApp(app="Spotify"), "Spotify"), (a.OpenWebsite(site="jio saavn"), "JioSaavn"),
    (a.PlayMedia(query="Radiohead", app="Spotify"), "Spotify"),
])
def test_a_service_opened_first_is_where_the_next_play_plays(opened, service):
    assert carry_over(opened, a.PlayMedia(query="Arijit")) == a.PlayMedia(query="Arijit", app=service)


def test_a_site_with_a_search_page_is_where_the_next_search_searches():
    assert carry_over(a.OpenWebsite(site="Amazon"), a.WebSearch(query="shoes")).site == "Amazon"
    assert carry_over(a.OpenApp(app="YouTube"), a.WebSearch(query="cricket highlights")).site == "YouTube"
    # Gmail has no search page for the web: "Gmail kholo aur weather batao" searches Google
    assert carry_over(a.OpenWebsite(site="Gmail"), a.WebSearch(query="weather")) == a.WebSearch(query="weather")


def test_nothing_carries_where_nothing_was_set_up():
    for previous in (a.OpenApp(app="Notepad"), a.MediaControl(control="pause"), a.TypeText(text="hi")):
        for following in (a.WebSearch(query="x"), a.PlayMedia(query="y"), a.OpenWebsite(site="z"),
                          a.OpenApp(app="Excel")):
            assert carry_over(previous, following) == following


@pytest.mark.parametrize("steps, chained", [
    # an open folded into the play or search on it
    ([a.OpenApp(app="YouTube"), a.PlayMedia(query="Arijit")], [a.PlayMedia(query="Arijit", app="YouTube")]),
    ([a.OpenApp(app="यूट्यूब"), a.PlayMedia(query="Arijit", app="YouTube")],
     [a.PlayMedia(query="Arijit", app="YouTube")]),
    ([a.OpenWebsite(site="Amazon"), a.WebSearch(query="shoes")], [a.WebSearch(query="shoes", site="Amazon")]),
    # ... but not into a play somewhere else
    ([a.OpenApp(app="YouTube"), a.PlayMedia(query="Arijit", app="Spotify")],
     [a.OpenApp(app="YouTube"), a.PlayMedia(query="Arijit", app="Spotify")]),
    # a browser stays a step, and what follows opens in it
    ([a.OpenApp(app="Chrome"), a.WebSearch(query="weather"), a.OpenWebsite(site="Flipkart")],
     [a.OpenApp(app="Chrome"), a.WebSearch(query="weather", browser="Chrome"),
      a.OpenWebsite(site="Flipkart", browser="Chrome")]),
    # context runs on from a folded step
    ([a.OpenApp(app="Spotify"), a.PlayMedia(query="Radiohead"), a.PlayMedia(query="Coldplay")],
     [a.PlayMedia(query="Radiohead", app="Spotify"), a.PlayMedia(query="Coldplay", app="Spotify")]),
    ([a.MediaControl(control="pause"), a.OpenApp(app="Notepad")],
     [a.MediaControl(control="pause"), a.OpenApp(app="Notepad")]),
    ([], []),
])
def test_chain(steps, chained):
    assert chain(steps) == chained


def test_names_of_browsers_and_services():
    assert browser_name("Google Chrome") == "Chrome" and browser_name("Notepad") is None
    assert browser_name(None) is None and media_service(None) is None
    assert media_service("you tube") == "YouTube" and media_service("Chrome") is None


def test_browser_is_never_a_slot_the_models_fill():
    assert all("browser" not in fields for fields in a.SLOT_FIELDS.values())
    assert a.parse_action({"action": "web_search", "query": "x"}).browser is None


# Opening an address in a named browser.

@pytest.fixture
def launched(monkeypatch):
    from bolos.executor import apps

    started: list[list[str]] = []
    monkeypatch.setattr(apps.subprocess, "Popen", lambda cmd, *args, **kwargs: started.append(cmd))
    return started


def test_browser_found_through_app_paths(monkeypatch, tmp_path, launched):
    from bolos.executor import apps

    exe = tmp_path / "chrome.exe"
    exe.write_text("")
    monkeypatch.setattr(apps, "_app_path", lambda name: str(exe) if name == "chrome.exe" else None)
    assert apps.browser_exe("Chrome") == str(exe)
    assert apps.open_in_browser("Chrome", "https://example.com/?q=1")
    assert launched == [[str(exe), "https://example.com/?q=1"]]


def test_browser_found_in_its_install_folder(monkeypatch, tmp_path):
    from bolos.executor import apps

    exe = tmp_path / "Mozilla Firefox" / "firefox.exe"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(apps, "_app_path", lambda name: None)
    for root in apps._INSTALL_ROOTS:
        monkeypatch.delenv(root, raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert apps.browser_exe("firefox") == str(exe)


def test_browser_found_in_the_start_menu_catalog(monkeypatch, tmp_path):
    from bolos.executor import apps

    exe = tmp_path / "brave.exe"
    exe.write_text("")
    monkeypatch.setattr(apps, "_app_path", lambda name: None)
    for root in apps._INSTALL_ROOTS:
        monkeypatch.delenv(root, raising=False)
    catalog = apps.Catalog(apps=[apps.App("Brave", str(exe))])
    assert apps.browser_exe("Brave", catalog) == str(exe)


def test_missing_browser_falls_back(monkeypatch, launched):
    from bolos.executor import apps

    monkeypatch.setattr(apps, "_app_path", lambda name: None)
    for root in apps._INSTALL_ROOTS:
        monkeypatch.delenv(root, raising=False)
    assert apps.browser_exe("Netscape") is None
    assert not apps.open_in_browser("Opera", "https://example.com")
    opened = []
    monkeypatch.setattr(apps.os, "startfile", opened.append)
    assert apps.open_in_browser("Edge", "https://example.com")  # Edge always answers its URL protocol
    assert opened == ["microsoft-edge:https://example.com"] and launched == []


def test_executor_opens_searches_and_sites_in_the_carried_browser(monkeypatch):
    from bolos.contacts import ContactBook
    from bolos.executor import Executor, apps, play

    in_browser, default = [], []
    monkeypatch.setattr(apps, "open_in_browser", lambda b, url, catalog=None: in_browser.append((b, url)) or True)
    monkeypatch.setattr(play, "open_url", default.append)
    executor = Executor(ContactBook([]), catalog=apps.Catalog(apps=[]))
    assert executor.execute(a.WebSearch(query="weather", browser="Chrome")).key == "searched"
    assert executor.execute(a.OpenWebsite(site="Flipkart", browser="Edge")).key == "opened_site"
    assert executor.execute(a.WebSearch(query="weather")).key == "searched"
    assert in_browser == [("Chrome", "https://www.google.com/search?q=weather"),
                          ("Edge", "https://www.flipkart.com")]
    assert default == ["https://www.google.com/search?q=weather"]


def test_executor_uses_the_default_browser_when_the_named_one_is_missing(monkeypatch):
    from bolos.contacts import ContactBook
    from bolos.executor import Executor, apps, play

    default = []
    monkeypatch.setattr(apps, "open_in_browser", lambda b, url, catalog=None: False)
    monkeypatch.setattr(play, "open_url", default.append)
    executor = Executor(ContactBook([]), catalog=apps.Catalog(apps=[]))
    assert executor.execute(a.OpenWebsite(site="github dot com", browser="Brave")).ok
    assert default == ["https://www.github.com"]
