"""Preferences: learning from corrections (F7). The older preference behaviour is covered through the pipeline
in tests/test_pipeline.py."""

import importlib.util
import json
from pathlib import Path

import pytest

from bolos import actions as a
from bolos.preferences import MAX_HABITS, Preferences


def load_script(relative: str):
    path = Path(__file__).resolve().parent.parent / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(prefs: Preferences) -> list[dict]:
    path = prefs.corrections_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_correction_becomes_the_users_habit_and_a_training_row(tmp_path):
    prefs = Preferences(tmp_path / "preferences.json")
    prefs.begin_command()
    prefs.learn_correction("arijit ke gaane chalao", a.PlayMedia(query="Arijit", app="Spotify"), "hi")
    assert prefs.recall("Arijit ke gaane chalao.") == a.PlayMedia(query="Arijit", app="Spotify")
    assert prefs.habit_source("arijit ke gaane chalao") == "user"
    [row] = rows(prefs)
    assert prefs.corrections_path == tmp_path / "corrections.jsonl"
    # Only what the transcript says is a slot, copied as it was heard ("arijit", not "Arijit").
    assert row["text"] == "arijit ke gaane chalao" and row["action"] == "play_media"
    assert row["slots"] == {"query": "arijit"} and row["lang"] == "hi" and row["source"] == "correction"
    assert "time" in row and "incomplete" not in row
    # Survives a restart.
    again = Preferences(tmp_path / "preferences.json")
    assert again.recall("arijit ke gaane chalao") == a.PlayMedia(query="Arijit", app="Spotify")


def test_galat_tha_after_a_correction_takes_it_all_back(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_habit("mera editor kholo", a.OpenApp(app="Notepad"))  # System 2's guess, earlier
    prefs.learn_correction("ok google", a.OpenApp(app="Chrome"))  # an older correction stays
    prefs.begin_command()
    prefs.learn_correction("mera editor kholo", a.OpenApp(app="Visual Studio Code"), "hi-IN")
    assert [r["text"] for r in rows(prefs)] == ["ok google", "mera editor kholo"]
    assert prefs.forget_last()
    assert prefs.recall("mera editor kholo") == a.OpenApp(app="Notepad")  # System 2's habit is back
    assert prefs.habit_source("mera editor kholo") == "system2"
    assert [r["text"] for r in rows(prefs)] == ["ok google"]
    assert Preferences(tmp_path / "p.json").habits["mera editor kholo"]["app"] == "Notepad"  # saved


def test_a_missing_required_field_marks_the_row_incomplete(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_correction("mera editor kholo", a.OpenApp(app="Visual Studio Code"), "hi")
    [row] = rows(prefs)
    assert row["slots"] == {} and row["incomplete"] is True and row["action"] == "open_app"


def test_corrections_beat_system2_habits_for_the_same_command(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_habit("gaana bajao", a.PlayMedia(app="YouTube"))
    prefs.learn_correction("gaana bajao", a.PlayMedia(app="Spotify"))
    prefs.learn_habit("gaana bajao", a.PlayMedia(app="YouTube"))  # a later System 2 answer
    assert prefs.recall("gaana bajao") == a.PlayMedia(app="Spotify")
    prefs.learn_correction("gaana bajao", a.PlayMedia(app="JioSaavn"))  # the user changes their mind
    assert prefs.recall("gaana bajao") == a.PlayMedia(app="JioSaavn")


def test_eviction_keeps_corrections_longest(tmp_path):
    prefs = Preferences(None)
    prefs.learn_correction("mera editor kholo", a.OpenApp(app="Visual Studio Code"))  # oldest
    for i in range(MAX_HABITS):
        prefs.learn_habit(f"app number {i} kholo", a.OpenApp(app=f"App {i}"))
    assert len(prefs.habits) == MAX_HABITS
    assert prefs.habit_source("mera editor kholo") == "user"
    assert prefs.recall("app number 0 kholo") is None  # the oldest System 2 habit went instead


@pytest.mark.parametrize("action", [a.SendMessage(contact="Raju", text="main late hoon"),
                                    a.TypeText(text="mera pin 4321 hai"), a.Unknown(), a.Cancel()])
def test_messages_typing_unknown_and_cancel_teach_nothing(tmp_path, action):
    prefs = Preferences(tmp_path / "p.json")
    prefs.begin_command()
    prefs.learn_correction("Raju ko bolo main late hoon", action)
    assert prefs.habits == {} and rows(prefs) == [] and not prefs.has_undo()


@pytest.mark.parametrize("command", ["", "  ", "?!"])
def test_an_empty_transcript_teaches_nothing(tmp_path, command):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_correction(command, a.OpenApp(app="Notepad"))
    assert prefs.habits == {} and rows(prefs) == []


def test_media_control_rows_carry_the_control(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_correction("thoda dheere karo", a.MediaControl(control="volume_down"), "hi")
    [row] = rows(prefs)
    assert row["action"] == "media_control" and row["control"] == "volume_down" and row["slots"] == {}


@pytest.mark.parametrize("command,action,lang,slots", [
    # Kannada in native script and in Latin, Tamil, Telugu, Bengali, Gujarati, Punjabi, Malayalam, Marathi
    ("ನೋಟ್‌ಪ್ಯಾಡ್ ತೆರೆ", a.OpenApp(app="ನೋಟ್‌ಪ್ಯಾಡ್"), "kn", {"app": "ನೋಟ್‌ಪ್ಯಾಡ್"}),
    ("spotify alli arijit haaku", a.PlayMedia(query="Arijit", app="Spotify"), "kn",
     {"app": "spotify", "query": "arijit"}),
    ("யூடியூப் திற", a.OpenApp(app="YouTube"), "ta", {}),
    ("youtube lo arijit paatalu pettu", a.PlayMedia(query="arijit paatalu", app="YouTube"), "te",
     {"app": "youtube", "query": "arijit paatalu"}),
    ("Chrome khulo", a.OpenApp(app="Google Chrome"), "bn", {}),
    ("amazon par shoes shodho", a.WebSearch(query="shoes", site="Amazon"), "gu", {"site": "amazon", "query": "shoes"}),
    ("ਨੋਟਪੈਡ ਖੋਲ੍ਹੋ", a.OpenApp(app="ਨੋਟਪੈਡ"), "pa", {"app": "ਨੋਟਪੈਡ"}),
    ("whatsapp thurakku", a.OpenApp(app="WhatsApp"), "ml", {"app": "whatsapp"}),
    ("नोटपॅड उघड", a.OpenApp(app="Notepad"), "mr", {}),
    ("submit button dabao", a.Click(target="Submit"), "hi", {"target": "submit"}),
])
def test_corrections_in_every_language(tmp_path, command, action, lang, slots):
    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_correction(command, action, lang)
    assert prefs.recall(command) == action
    [row] = rows(prefs)
    assert row["slots"] == slots and row["lang"] == lang and row["text"] == command
    for value in row["slots"].values():
        assert value in row["text"]  # exact substrings, as train/check_rows.py requires


def test_rows_pass_the_training_data_checker(tmp_path):
    problem = load_script("train/check_rows.py").problem

    prefs = Preferences(tmp_path / "p.json")
    prefs.learn_correction("Spotify pe Spotify Wrapped chalao", a.PlayMedia(query="Spotify Wrapped", app="Spotify"))
    prefs.learn_correction("ek gaana chalao", a.PlayMedia(app="Spotify"))
    prefs.learn_correction("bangalore ka mausam", a.WebSearch(query="Bangalore ka mausam"))
    prefs.learn_correction("awaaz kam", a.MediaControl(control="volume_down"))
    for row in rows(prefs):
        assert problem(row) is None, row
    first = rows(prefs)[0]
    assert first["slots"] == {"app": "Spotify", "query": "Spotify Wrapped"}  # not overlapping


def test_a_row_cut_short_by_a_crash_does_not_swallow_the_next(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.corrections_path.write_text('{"text": "Calcu', encoding="utf-8")  # the app died mid-write
    prefs.learn_correction("gaana bajao", a.PlayMedia(app="Spotify"))
    raw = prefs.corrections_path.read_text(encoding="utf-8").splitlines()
    assert raw[0] == '{"text": "Calcu' and json.loads(raw[1])["text"] == "gaana bajao"


def test_an_unwritable_corrections_file_still_learns_the_habit(tmp_path):
    prefs = Preferences(tmp_path / "p.json")
    prefs.corrections_path.mkdir()  # can't be opened for appending
    prefs.begin_command()
    prefs.learn_correction("gaana bajao", a.PlayMedia(app="Spotify"))
    assert prefs.recall("gaana bajao") == a.PlayMedia(app="Spotify")
    assert prefs.forget_last() and prefs.habit_source("gaana bajao") is None


def test_preferences_without_a_file_still_learn(tmp_path):
    prefs = Preferences(None)
    prefs.learn_correction("gaana bajao", a.PlayMedia(app="Spotify"))
    assert prefs.recall("gaana bajao") == a.PlayMedia(app="Spotify")
    assert prefs.corrections_path is None


def test_old_habit_files_without_a_source_still_load(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"habits": {"zoom wala kholo": {"action": "open_app", "app": "Zoom"}}}),
                    encoding="utf-8")
    prefs = Preferences(path)
    assert prefs.recall("zoom wala kholo") == a.OpenApp(app="Zoom")
    assert prefs.habit_source("zoom wala kholo") == "system2"
    prefs.learn_correction("zoom wala kholo", a.OpenApp(app="Zoom Workplace"))
    assert prefs.recall("zoom wala kholo") == a.OpenApp(app="Zoom Workplace")


def test_malformed_saved_preferences_keep_valid_older_entries(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"media": {"music": {"Spotify": 2, "bad": "3"}, "video": []},
                                "media_names": None, "app_names": {"bad": []},
                                "habits": {"zoom": {"action": "open_app", "app": "Zoom"}, "broken": []}}),
                    encoding="utf-8")
    prefs = Preferences(path)
    assert prefs.media_app("play music", "") == "Spotify"
    assert prefs.media_app("play a video", "") == "YouTube"
    assert prefs.app_names == {} and prefs.recall("broken") is None
    assert prefs.recall("zoom") == a.OpenApp(app="Zoom")
