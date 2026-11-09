"""The command log (bolos.history) and the tools that turn real use into test sets (eval/history_to_set.py,
eval/record_set.py)."""

import importlib.util
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from bolos import actions as a
from bolos.history import REDACTED, History, history_path, read_entries, span_slots, training_row

ROOT = Path(__file__).resolve().parent.parent


def load_script(relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history_to_set = load_script("eval/history_to_set.py")
record_set = load_script("eval/record_set.py")


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# Logging


def test_every_command_is_one_flushed_line(tmp_path):
    h = History(tmp_path / "history.jsonl", log_messages=False)
    first = h.record("Chrome kholo", language="hi", source="system1", action=a.OpenApp(app="Chrome"),
                     outcome="opened", timings={"stt": 412.345, "understand": 1.27}, trigger="wake")
    assert len(lines(tmp_path / "history.jsonl")) == 1  # on disk at once, not at exit
    second = h.record("kuch bhi", language="hi", outcome="not_understood")
    [e1, e2] = h.entries()
    assert (e1["id"], e2["id"]) == (first, second) and first != second
    assert e1["action"] == "open_app" and e1["steps"] == [{"action": "open_app", "app": "Chrome"}]
    assert e1["timings"] == {"stt": 412.3, "understand": 1.3} and e1["trigger"] == "wake"
    assert e1["source"] == "system1" and e1["outcome"] == "opened" and e1["corrected"] is False
    assert e2["action"] is None and e2["steps"] == []
    assert "T" in e1["time"]  # ISO time with the zone


def test_several_steps_are_one_entry(tmp_path):
    h = History(tmp_path / "h.jsonl")
    h.record("Chrome kholo aur Bangalore ka weather search karo", source="system1",
             steps=[a.OpenApp(app="Chrome"), a.WebSearch(query="Bangalore ka weather")], outcome="searched")
    [e] = h.entries()
    assert e["action"] == "multi" and [s["action"] for s in e["steps"]] == ["open_app", "web_search"]


def test_corrections_and_confirmations_fold_into_the_entry(tmp_path):
    h = History(tmp_path / "h.jsonl")
    wrong = h.record("Arijit ke gaane chalao", source="system1", action=a.PlayMedia(query="Arijit", app="YouTube"))
    right = h.record("Notepad kholo", source="system1", action=a.OpenApp(app="Notepad"))
    h.mark_corrected(wrong, a.PlayMedia(query="Arijit", app="Spotify"))
    h.mark_confirmed(right)
    assert len(lines(tmp_path / "h.jsonl")) == 4  # appended, never rewritten
    e1, e2 = h.entries()
    assert e1["corrected"] and e1["corrected_to"] == [{"action": "play_media", "query": "Arijit", "app": "Spotify"}]
    assert e2["confirmed"] and not e2["corrected"]


@pytest.mark.parametrize("transcript,text,kept", [
    ("Mummy ko message bhejo ki main ghar aa raha hoon", "main ghar aa raha hoon", "Mummy ko message bhejo ki"),
    ("ಅಮ್ಮ ಗೆ ಮೆಸೇಜ್ ಕಳಿಸು ನಾನು ಬರ್ತಿದ್ದೀನಿ ಅಂತ", "ನಾನು ಬರ್ತಿದ್ದೀನಿ", "ಅಮ್ಮ ಗೆ ಮೆಸೇಜ್ ಕಳಿಸು"),
    ("amma ku message anuppu naan late aaven nu", "NAAN LATE AAVEN", "amma ku message anuppu"),
    ("Raju ko bolo PIN 4321 hai", "PIN 4321 hai", "Raju ko bolo"),
])
def test_message_text_is_redacted_in_every_language(tmp_path, transcript, text, kept):
    h = History(tmp_path / "h.jsonl", log_messages=False)
    h.record(transcript, language="kn", source="system2", action=a.SendMessage(contact="Raju", text=text),
             outcome="sent", reply=f"Raju ko bhej diya: {text}")
    raw = (tmp_path / "h.jsonl").read_text(encoding="utf-8")
    assert text.casefold() not in raw.casefold()
    [e] = h.entries()
    assert e["redacted"] and REDACTED in e["transcript"] and e["transcript"].startswith(kept)
    assert e["steps"][0]["text"] == REDACTED and e["steps"][0]["contact"] == "Raju"
    assert e["reply"] == f"Raju ko bhej diya: {REDACTED}"


def test_dictation_not_found_word_for_word_redacts_the_whole_transcript(tmp_path):
    h = History(tmp_path / "h.jsonl", log_messages=False)
    h.record("likho ki meeting kal subah das baje hai", action=a.TypeText(text="Meeting kal subah 10 baje hai."))
    [e] = h.entries()
    assert e["transcript"] == REDACTED and e["steps"] == [{"action": "type_text", "text": REDACTED}]
    assert "meeting" not in (tmp_path / "h.jsonl").read_text(encoding="utf-8")


def test_short_dictation_never_damages_the_ids_or_names(tmp_path):
    h = History(tmp_path / "h.jsonl", log_messages=False)
    entry_id = h.record("type a", action=a.TypeText(text="a"), source="system1", outcome="typed")
    [e] = h.entries()
    assert e["id"] == entry_id and e["action"] == "type_text" and e["source"] == "system1"


def test_messages_are_kept_when_the_user_opts_in(tmp_path, monkeypatch):
    monkeypatch.setenv("BOLOS_LOG_MESSAGES", "1")
    h = History(tmp_path / "h.jsonl")
    h.record("Raju ko bolo main late hoon", action=a.SendMessage(contact="Raju", text="main late hoon"))
    [e] = h.entries()
    assert e["transcript"] == "Raju ko bolo main late hoon" and "redacted" not in e
    monkeypatch.setenv("BOLOS_LOG_MESSAGES", "0")
    assert History(tmp_path / "h.jsonl").log_messages is False


def test_a_command_corrected_to_a_message_is_scrubbed_after_the_fact(tmp_path):
    h = History(tmp_path / "h.jsonl", log_messages=False)
    other = h.record("Notepad kholo", action=a.OpenApp(app="Notepad"))
    wrong = h.record("Raju ko bolo main late hoon", action=a.WebSearch(query="main late hoon"))
    h.mark_corrected(wrong, a.SendMessage(contact="Raju", text="main late hoon"))
    raw = (tmp_path / "h.jsonl").read_text(encoding="utf-8")
    assert "late" not in raw
    e_other, e = h.entries()
    assert e_other["id"] == other and e_other["transcript"] == "Notepad kholo"
    assert e["transcript"] == f"Raju ko bolo {REDACTED}" and e["steps"][0]["query"] == REDACTED
    assert e["corrected_to"] == [{"action": "send_message", "app": "whatsapp", "contact": "Raju", "text": REDACTED}]


def test_a_line_cut_short_by_a_crash_loses_only_itself(tmp_path):
    path = tmp_path / "h.jsonl"
    h = History(path)
    h.record("Notepad kholo", action=a.OpenApp(app="Notepad"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"id": "x", "transcript": "Calcu')  # the app died mid-write
    after = History(path)  # restarted
    after.record("Spotify kholo", action=a.OpenApp(app="Spotify"))
    assert [e["transcript"] for e in after.entries()] == ["Notepad kholo", "Spotify kholo"]


def test_a_stage_that_did_not_run_keeps_the_line(tmp_path):
    h = History(tmp_path / "h.jsonl")
    h.record("Notepad kholo", action=a.OpenApp(app="Notepad"), timings={"stt": None, "understand": "x", "run": 5})
    [e] = h.entries()
    assert e["timings"] == {"run": 5.0}


def test_logging_never_breaks_a_command(tmp_path):
    (tmp_path / "dir.jsonl").mkdir()
    h = History(tmp_path / "dir.jsonl")
    assert h.record("Notepad kholo", action=a.OpenApp(app="Notepad"))
    h.mark_corrected("nope", a.OpenApp(app="Notepad"))
    h.mark_confirmed("nope")
    disabled = History(None)
    assert disabled.record("Notepad kholo") and disabled.entries() == []


def test_threads_never_interleave_lines(tmp_path):
    h = History(tmp_path / "h.jsonl")

    def log_many(n):
        for i in range(50):
            h.record(f"command {n}-{i} " + "x" * 2000, action=a.OpenApp(app="Notepad"))

    threads = [threading.Thread(target=log_many, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(lines(tmp_path / "h.jsonl")) == 200


def test_ids_never_collide_even_within_one_second():
    h = History(None)
    ids = [h.record("Notepad kholo") for _ in range(20000)]
    assert len(set(ids)) == len(ids)


def test_history_lives_next_to_preferences(tmp_path):
    assert history_path(tmp_path / "preferences.json") == tmp_path / "history.jsonl"
    assert history_path(None) is None


# Training rows


@pytest.mark.parametrize("text,action,slots", [
    ("SPOTIFY pe arijit chalao", {"action": "play_media", "query": "Arijit", "app": "Spotify"},
     {"app": "SPOTIFY", "query": "arijit"}),
    ("ಸ್ಪಾಟಿಫೈ ತೆರೆ", {"action": "open_app", "app": "ಸ್ಪಾಟಿಫೈ"}, {"app": "ಸ್ಪಾಟಿಫೈ"}),
    ("youtube youtube pe", {"action": "play_media", "query": "youtube", "app": "YouTube"}, {"query": "youtube"}),
    ("Notepad kholo", {"action": "open_app", "app": " Notepad "}, {"app": "Notepad"}),
    ("google kholo", {"action": "open_website", "site": "google.com", "browser": "Chrome"}, {}),
])
def test_slots_are_only_what_the_transcript_says(text, action, slots):
    assert span_slots(text, action) == slots


def test_training_rows_never_hold_messages_or_typing():
    assert training_row("Raju ko bolo hi", a.SendMessage(contact="Raju", text="hi")) is None
    assert training_row("likho hi", a.TypeText(text="hi")) is None
    assert training_row("", a.OpenApp(app="Notepad")) is None
    assert training_row("pause", a.MediaControl(control="pause"), "hi-IN") == {
        "text": "pause", "action": "media_control", "slots": {}, "control": "pause", "lang": "hi"}
    unknown_lang = training_row("Chrome kholo", a.OpenApp(app="Chrome"), "xx-IN")
    assert unknown_lang == {"text": "Chrome kholo", "action": "open_app", "slots": {"app": "Chrome"}}


# eval/history_to_set.py


def sample_history(path: Path) -> History:
    h = History(path, log_messages=False)
    ok = h.record("Notepad ತೆರೆ", language="kn", source="system1", action=a.OpenApp(app="Notepad"))
    h.mark_confirmed(ok)
    wrong = h.record("arijit ke gaane chalao", language="hi-IN", source="system1",
                     action=a.PlayMedia(query="arijit", app="YouTube"))
    h.mark_corrected(wrong, a.PlayMedia(query="arijit", app="Spotify"))
    editor = h.record("mera editor kholo", language="hi", source="system2", action=a.OpenApp(app="Notepad"))
    h.mark_corrected(editor, a.OpenApp(app="Visual Studio Code"))
    h.record("Chrome kholo", language="hi", source="system1", action=a.OpenApp(app="Chrome"))  # never labelled
    msg = h.record("Raju ko bolo main late hoon", language="hi",
                   action=a.SendMessage(contact="Raju", text="main late hoon"))
    h.mark_confirmed(msg)  # confirmed, but its words are redacted
    two = h.record("YouTube kholo aur Arijit chalao", language="hi", source="system1",
                   steps=[a.OpenApp(app="YouTube"), a.PlayMedia(query="Arijit", app="YouTube")])
    h.mark_confirmed(two)
    pause = h.record("gaana rok do", language="hi", action=a.MediaControl(control="pause"))
    h.mark_confirmed(pause)
    return h


def test_history_becomes_an_evaluation_set(tmp_path):
    sample_history(tmp_path / "h.jsonl")
    rows = history_to_set.build(read_entries(tmp_path / "h.jsonl"), "eval")
    by_text = {r["text"]: r for r in rows}
    assert set(by_text) == {"Notepad ತೆರೆ", "arijit ke gaane chalao", "mera editor kholo",
                            "YouTube kholo aur Arijit chalao", "gaana rok do"}
    kn = by_text["Notepad ತೆರೆ"]
    assert (kn["lang"], kn["action"], kn["slots"], kn["label"], kn["decided_by"]) == (
        "kn", "open_app", {"app": "Notepad"}, "confirmed", "system1")
    # The service the user didn't name was a preference, not something to hear.
    assert by_text["arijit ke gaane chalao"]["slots"] == {"query": "arijit"}
    assert by_text["arijit ke gaane chalao"]["label"] == "corrected"
    assert by_text["mera editor kholo"]["slots"] == {"app": "Visual Studio Code"}  # required: always
    assert by_text["gaana rok do"]["control"] == "pause"
    assert by_text["YouTube kholo aur Arijit chalao"]["action"] == "multi"
    raw = json.dumps(rows, ensure_ascii=False)
    assert "late" not in raw and REDACTED not in raw


def test_history_becomes_training_rows(tmp_path):
    sample_history(tmp_path / "h.jsonl")
    rows = history_to_set.build(read_entries(tmp_path / "h.jsonl"), "train")
    problem = load_script("train/check_rows.py").problem
    for row in rows:
        if row["action"] != "multi":
            assert problem(row) is None, row
    by_text = {r["text"]: r for r in rows}
    assert by_text["arijit ke gaane chalao"]["slots"] == {"query": "arijit"}
    assert by_text["mera editor kholo"]["incomplete"] is True
    assert by_text["YouTube kholo aur Arijit chalao"]["steps"] == [
        {"action": "open_app", "slots": {"app": "YouTube"}},
        {"action": "play_media", "slots": {"query": "Arijit", "app": "YouTube"}}]


def test_the_latest_label_wins(tmp_path):
    h = History(tmp_path / "h.jsonl")
    first = h.record("gaana bajao", action=a.PlayMedia(app="YouTube"))
    h.mark_confirmed(first)
    second = h.record("Gaana bajao.", action=a.PlayMedia(app="YouTube"))
    h.mark_corrected(second, a.PlayMedia(app="Spotify"))
    [row] = history_to_set.build(h.entries(), "eval")
    assert row["label"] == "corrected" and row["text"] == "Gaana bajao."


def test_review_labels_commands(tmp_path):
    h = History(tmp_path / "h.jsonl")
    h.record("Chrome kholo", action=a.OpenApp(app="Chrome"))
    h.record("mera editor kholo", action=a.OpenApp(app="Notepad"))
    h.record("Radiohead kaun hai", action=a.PlayMedia(query="Radiohead"))
    # newest first: Radiohead, then the editor, then Chrome
    answers = iter(["n", "fly_plane", "web_search query=Radiohead kaun hai", "n", "open_app app=VS Code", "y"])
    shown = []
    assert history_to_set.review(h, ask=lambda _: next(answers), show=shown.append) == 3
    e1, e2, e3 = h.entries()
    assert e1["confirmed"]
    assert e2["corrected_to"] == [{"action": "open_app", "app": "VS Code"}]
    assert e3["corrected_to"] == [{"action": "web_search", "query": "Radiohead kaun hai"}]
    assert any("not a valid action" in s for s in shown)
    assert history_to_set.review(h, ask=lambda _: "q", show=shown.append) == 0


def test_review_answers_piped_with_a_bom(tmp_path):
    h = History(tmp_path / "h.jsonl")
    h.record("Chrome kholo", action=a.OpenApp(app="Chrome"))
    assert history_to_set.review(h, ask=lambda _: "﻿y\n", show=lambda *_: None) == 1
    assert h.entries()[0]["confirmed"]


def test_review_correcting_to_what_it_did_is_a_confirmation(tmp_path):
    h = History(tmp_path / "h.jsonl")
    h.record("YouTube tira", action=a.OpenApp(app="YouTube"))
    answers = iter(["n", "open_app app=YouTube"])
    assert history_to_set.review(h, ask=lambda _: next(answers), show=lambda *_: None) == 1
    [e] = h.entries()
    assert e["confirmed"] and not e["corrected"]


@pytest.mark.parametrize("line,expected", [
    ("open_app app=Visual Studio Code", a.OpenApp(app="Visual Studio Code")),
    ("web_search query=Amazon pe shoes site=Amazon", a.WebSearch(query="Amazon pe shoes", site="Amazon")),
    ("media_control control=volume_up", a.MediaControl(control="volume_up")),
    ('{"action": "open_website", "site": "irctc.co.in"}', a.OpenWebsite(site="irctc.co.in")),
])
def test_typed_corrections(line, expected):
    assert history_to_set.parse_spoken_action(line) == expected


# eval/record_set.py


def tone(seconds: float, db: float, rate: int = 16000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    amp = 32768 * 10 ** (db / 20) * np.sqrt(2)
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def noise(seconds: float, rng) -> np.ndarray:
    return rng.normal(0, 30, int(seconds * 16000)).astype(np.int16)


def frames_of(x: np.ndarray) -> list[np.ndarray]:
    return [x[i:i + record_set.FRAME] for i in range(0, len(x) - record_set.FRAME + 1, record_set.FRAME)]


def feed_all(ep, audio: np.ndarray) -> str:
    for f in frames_of(audio):
        if ep.feed(f) in ("done", "no_speech"):
            break
    return ep.state


def test_a_reading_ends_after_the_silence():
    rng = np.random.default_rng(1)
    background = frames_of(noise(1.0, rng))
    ep = record_set.Endpointer(record_set.noise_floor_db(background))
    # speech with a short pause inside (a breath), then quiet
    audio = np.concatenate([noise(0.5, rng), tone(0.8, -25), noise(0.4, rng), tone(0.6, -30), noise(2.0, rng)])
    assert feed_all(ep, audio) == "done"
    start_s, end_s = ep.start * record_set.FRAME_S, ep.end * record_set.FRAME_S
    assert 0.45 <= start_s <= 0.6 and 2.2 <= end_s <= 2.4  # both bursts are one reading
    assert ep.n * record_set.FRAME_S <= end_s + 0.85  # stopped about 0.8 s after the last word


def test_a_key_click_is_not_a_reading():
    rng = np.random.default_rng(2)
    ep = record_set.Endpointer(-60.0)
    audio = np.concatenate([noise(0.3, rng), tone(0.12, -15), noise(1.2, rng), tone(0.7, -25), noise(1.5, rng)])
    assert feed_all(ep, audio) == "done"
    assert ep.start * record_set.FRAME_S >= 1.5


def test_silence_gives_up_and_long_speech_is_cut():
    rng = np.random.default_rng(3)
    ep = record_set.Endpointer(-60.0, max_wait_s=2.0)
    assert feed_all(ep, noise(3.0, rng)) == "no_speech"
    ep = record_set.Endpointer(-60.0, max_s=3.0)
    assert feed_all(ep, np.concatenate([noise(0.3, rng), tone(6.0, -20)])) == "done"
    assert (ep.end - ep.start) * record_set.FRAME_S <= 3.05


def test_a_background_measured_too_low_cannot_keep_a_reading_open():
    """A dead-quiet start (digital silence, or a very quiet room) puts the floor at -75 dB; ordinary room noise
    at -61 dB after the reading must still end it, not run on to max_s."""
    rng = np.random.default_rng(6)
    ep = record_set.Endpointer(record_set.noise_floor_db([np.zeros(480, np.int16)] * 20))
    assert ep.threshold == -65.0
    assert feed_all(ep, np.concatenate([tone(1.0, -18), noise(3.0, rng)])) == "done"
    assert ep.end * record_set.FRAME_S <= 1.05 and ep.n * record_set.FRAME_S <= 1.9
    assert ep.peak_db > -20


def test_soft_words_inside_a_reading_still_count():
    """Only frames far below the reading's loudest part are quiet: a softer second phrase is kept."""
    rng = np.random.default_rng(7)
    ep = record_set.Endpointer(-60.0)
    audio = np.concatenate([tone(0.6, -15), noise(0.3, rng), tone(0.6, -40), noise(2.0, rng)])
    assert feed_all(ep, audio) == "done"
    assert 1.45 <= ep.end * record_set.FRAME_S <= 1.55


def test_the_noise_floor_ignores_speech_and_digital_silence():
    rng = np.random.default_rng(4)
    assert record_set.noise_floor_db([np.zeros(480, np.int16)] * 10) == -75.0
    quiet = frames_of(noise(1.0, rng))
    assert -65 < record_set.noise_floor_db(quiet) < -55
    assert record_set.noise_floor_db(frames_of(tone(1.0, -10))) == -40.0  # someone talking: clamped


def test_commands_load_from_jsonl_and_csv(tmp_path):
    (tmp_path / "s.jsonl").write_text(
        '{"id": 7, "lang": "ta", "text": "Notepad திற", "action": "open_app", "slots": {"app": "Notepad"}}\n\n',
        encoding="utf-8")
    (tmp_path / "s.csv").write_text("command,lang,action,control\nawaaz badhao,hi,media_control,volume_up\n"
                                    "Spotify kholo,hi,open_app,\n,hi,open_app,\n", encoding="utf-8-sig")
    [j] = record_set.load_commands(tmp_path / "s.jsonl")
    assert j["text"] == "Notepad திற" and record_set.row_key(j, 0) == "7"
    c1, c2 = record_set.load_commands(tmp_path / "s.csv")
    assert c1 == {"text": "awaaz badhao", "lang": "hi", "action": "media_control", "control": "volume_up",
                  "slots": {}, "disputed": []}
    assert c2["disputed"] == ["app"] and "control" not in c2
    assert record_set.row_key(c2, 1).startswith("0002-")


@pytest.mark.parametrize("choice,lang,expected", [
    ("app", "bn", "hi-IN,en-IN"), ("row", "bn", "bn-IN,en-IN"), ("row", "hi-en", "hi-IN,en-IN"),
    ("row", "en", "hi-IN,en-IN"), ("row", "none", "hi-IN,en-IN"), ("row", None, "hi-IN,en-IN"),
    ("ml-IN,en-IN", "ta", "ml-IN,en-IN"),
])
def test_prisma_languages_per_row(choice, lang, expected):
    assert record_set.stt_languages(choice, lang, "hi-IN,en-IN") == expected


class Busy(Exception):
    pass


class FakeTurn:
    """A cross-process lock that is taken for the first `busy_for` short waits."""

    def __init__(self, busy_for: int = 0):
        self.busy_for, self.held, self.waits = busy_for, False, []

    def __call__(self, timeout: float):
        from contextlib import contextmanager

        @contextmanager
        def turn():
            self.waits.append(timeout)
            if timeout < 1 and self.busy_for > 0:
                self.busy_for -= 1
                raise Busy("Timbre is busy")
            self.held = True
            try:
                yield
            finally:
                self.held = False

        return turn()


def test_transcription_holds_the_shared_gnani_lock():
    lock, shown = FakeTurn(busy_for=1), []

    def call():
        assert lock.held  # Prisma only runs while no Timbre request can
        return "Notepad kholo"

    assert record_set.gnani_turn(call, turn=lock, busy=Busy, show=shown.append) == "Notepad kholo"
    assert lock.waits == [0.5, 600.0] and shown and "waiting for Gnani" in shown[0]
    assert not lock.held


def test_a_429_is_retried_and_other_errors_are_not():
    lock, answers = FakeTurn(), iter([RuntimeError("Prisma 429: RATE_LIMITED"), "ok"])

    def call():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert record_set.gnani_turn(call, turn=lock, busy=Busy, show=print, pause_s=0) == "ok"
    calls = []

    def failing(message):
        def call():
            calls.append(message)
            raise RuntimeError(message)
        return call

    with pytest.raises(RuntimeError, match="500"):
        record_set.gnani_turn(failing("Prisma 500"), turn=lock, busy=Busy, pause_s=0)
    assert calls == ["Prisma 500"]  # not retried
    with pytest.raises(RuntimeError, match="429"):
        record_set.gnani_turn(failing("Prisma 429"), turn=lock, busy=Busy, pause_s=0, attempts=3)
    assert calls.count("Prisma 429") == 3 and not lock.held


@pytest.fixture
def recording_env(tmp_path, monkeypatch):
    monkeypatch.setattr(record_set, "EVAL", tmp_path)
    rows = [{"id": i, "lang": lang, "text": text, "action": "open_app", "slots": {"app": app}}
            for i, (lang, text, app) in enumerate([("hi", "Notepad kholo", "Notepad"),
                                                   ("kn", "ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "ಕ್ಯಾಲ್ಕುಲೇಟರ್"),
                                                   ("bn", "Chrome khulo", "Chrome")])]
    rng = np.random.default_rng(5)
    clips = {r["text"]: record_set.to_wav(np.concatenate([tone(0.9, -24), noise(0.1, rng)])) for r in rows}
    heard = []

    def transcribe(wav: bytes, row: dict) -> str:
        samples = record_set.read_wav(wav)
        heard.append((row["text"], len(samples) / 16000))
        return row["text"].lower()

    return tmp_path, rows, clips, transcribe, heard


def test_recording_a_set_end_to_end_and_resuming(recording_env):
    tmp_path, rows, clips, transcribe, heard = recording_env
    out = tmp_path / "set_speech_ram.jsonl"

    def session(rows, keys, auto=0.0, **kw):
        return record_set.run(rows, set_name="set", speaker="ram", out=out, source=record_set.SimulatedMic(
            clips, fast=True), keys=record_set.ScriptedKeys(keys), transcribe=transcribe, show=lambda *_, **__: None,
            auto=auto, **kw)

    session(rows[:1], ["enter"], auto=None)  # the reader presses Enter and reads the first
    assert [r["typed"] for r in lines(out)] == ["Notepad kholo"]
    first = lines(out)[0]
    assert first["text"] == "notepad kholo" and first["speaker"] == "ram" and first["slots"] == {"app": "Notepad"}
    assert first["audio"] == "recordings/set/ram/0.wav" and (tmp_path / first["audio"]).exists()
    assert 1.0 <= heard[0][1] <= 1.9  # the reading plus a short margin, not the whole stream

    session(rows, ["esc"])  # resumes at the second: skips it, records the third by itself
    assert [r["typed"] for r in lines(out)] == ["Notepad kholo", "Chrome khulo"]
    assert len(heard) == 2

    session(rows, [])  # nothing left to ask
    assert len(heard) == 2

    session(rows, [], retry_skipped=True)
    assert [r["typed"] for r in lines(out)] == ["Notepad kholo", "ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "Chrome khulo"]  # in order
    assert [r["lang"] for r in lines(out)] == ["hi", "kn", "bn"]


def test_a_recording_without_a_transcript_is_finished_next_time(recording_env):
    tmp_path, rows, clips, transcribe, heard = recording_env
    out = tmp_path / "o.jsonl"

    def offline(wav, row):
        raise ConnectionError("no network")

    record_set.run(rows[:1], set_name="s", speaker="a", out=out, source=record_set.SimulatedMic(clips, fast=True),
                   keys=record_set.ScriptedKeys([]), transcribe=offline, show=lambda *_, **__: None, auto=0.0)
    assert not out.exists() or lines(out) == []
    assert (tmp_path / "recordings" / "s" / "a" / "0.wav").exists()
    record_set.run(rows[:1], set_name="s", speaker="a", out=out, source=record_set.SimulatedMic({}, fast=True),
                   keys=record_set.ScriptedKeys([]), transcribe=transcribe, show=lambda *_, **__: None)
    assert [r["text"] for r in lines(out)] == ["notepad kholo"]  # transcribed without recording again
    assert len(heard) == 1


def test_redo_and_retranscribe(recording_env):
    tmp_path, rows, clips, transcribe, heard = recording_env
    out = tmp_path / "o.jsonl"
    kw = dict(set_name="s", speaker="a", out=out, transcribe=transcribe, show=lambda *_, **__: None)
    record_set.run(rows[:2], source=record_set.SimulatedMic(clips, fast=True), keys=record_set.ScriptedKeys([]),
                   auto=0.0, **kw)
    assert len(lines(out)) == 2 and len(heard) == 2
    # At the third command the reader presses r: the second is recorded again, then the third.
    record_set.run(rows, source=record_set.SimulatedMic(clips, fast=True), keys=record_set.ScriptedKeys(["r"]),
                   auto=0.0, **kw)
    assert [t for t, _ in heard] == ["Notepad kholo", "ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "Chrome khulo"]
    assert len(lines(out)) == 3
    record_set.run(rows, source=None, keys=None, retranscribe=True, **kw)
    assert len(heard) == 7 and len(lines(out)) == 3
