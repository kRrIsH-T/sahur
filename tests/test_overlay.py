"""The overlay's event handling, wording, text fitting and layout, without a window (fake text metrics)."""

import struct
import zlib

import pytest

from bolos import overlay as ov
from bolos.overlay import OverlayState, Step


class FakeMetrics:
    """Every character is 7 px wide (combining marks 0), so widths are predictable."""

    icons = True

    def measure(self, font: str, text: str) -> int:
        return sum(0 if ov.unicodedata.category(ch).startswith("M") else 7 for ch in text)

    def ascent(self, font: str) -> int:
        return 14

    def linespace(self, font: str) -> int:
        return 20


def measure(text: str) -> int:
    return FakeMetrics().measure("value", text)


def decision(source="system1", ms=1.2, *steps):
    steps = steps or (("open_app", {"app": "Chrome"}),)
    return {"source": source, "ms": ms, "steps": [{"action": a, "fields": f} for a, f in steps]}


def fresh(**kw) -> OverlayState:
    return OverlayState(**kw)


# ---------------------------------------------------------------- event handling


def test_a_full_turn_fills_every_part():
    st = fresh()
    assert st.apply("trigger", "wake", 0)
    assert st.apply("state", "listening", 0)
    assert st.apply("level", 0.5, 0.05)
    assert st.levels[-1] == 0.5
    assert st.apply("state", "hearing", 0.1)
    assert st.apply("state", "transcribing", 1)
    assert st.apply("heard", "  Chrome kholo  ", 1.2)
    assert st.heard == "Chrome kholo"
    assert st.apply("language", "HI", 1.2)
    assert st.language == "hi"
    assert st.apply("decision", decision(), 1.3)
    assert st.decision.steps == (Step("open_app", {"app": "Chrome"}),)
    assert st.apply("reply", "Chrome खोल दिया।", 1.4)
    assert st.apply("state", "done", 1.5)
    assert st.trigger is None  # each listen reports its own


def test_a_new_listen_after_a_finished_command_clears_it():
    st = fresh()
    for kind, value in [("heard", "Chrome kholo"), ("language", "hi"), ("decision", decision()),
                        ("reply", "ho gaya"), ("state", "done")]:
        st.apply(kind, value, 0)
    st.apply("state", "listening", 1)
    assert (st.heard, st.language, st.decision, st.reply) == ("", None, None, "")


def test_the_answer_to_a_question_keeps_the_question_on_screen():
    st = fresh()
    for kind, value in [("heard", "Raju ko message bhejo ki aa raha hoon"), ("decision", decision()),
                        ("reply", "Raju ko bhejun? haan ya na"), ("state", "confirming")]:
        st.apply(kind, value, 0)
    st.apply("trigger", "followup", 1)
    st.apply("state", "listening", 1)
    assert st.heard and st.decision and st.reply


def test_heard_clears_the_previous_decision_and_reply():
    st = fresh()
    st.apply("state", "understanding", 0)
    st.apply("decision", decision(), 0)
    st.apply("reply", "old", 0)
    st.apply("language", "ta", 0)
    st.apply("heard", "new words", 1)
    assert st.decision is None and st.reply == ""
    assert st.language == "ta"  # mid-turn the language stays


def test_a_typed_command_after_a_finished_one_drops_the_old_language():
    st = fresh()
    st.apply("language", "ta", 0)
    st.apply("state", "done", 0)
    st.apply("heard", "Notepad kholo", 1)
    assert st.language is None


def test_meter_restarts_flat_and_clamps_levels():
    st = fresh()
    st.apply("level", 0.9, 0)
    st.apply("state", "listening", 0)
    assert max(st.levels) == 0
    st.apply("level", 3.0, 0)
    st.apply("level", -1, 0)
    assert list(st.levels)[-2:] == [1.0, 0.0]
    assert len(st.levels) == ov.LEVEL_BARS


@pytest.mark.parametrize("value", [None, "loud", float("nan"), float("inf"), True])
def test_bad_levels_are_ignored(value):
    st = fresh()
    st.apply("state", "listening", 0)
    assert not st.apply("level", value, 0)


def test_levels_only_redraw_while_the_microphone_is_open():
    st = fresh()
    assert not st.apply("level", 0.3, 0)
    st.apply("state", "hearing", 0)
    assert st.apply("level", 0.3, 0)


def test_repeated_state_and_trigger_are_not_changes():
    st = fresh()
    st.apply("state", "hearing", 0)
    assert not st.apply("state", "hearing", 1)
    assert st.state_since == 0
    assert st.apply("trigger", "tap", 1)
    assert not st.apply("trigger", "tap", 2)


@pytest.mark.parametrize("kind,value", [("bogus", 1), ("state", ""), ("state", 3), ("heard", None),
                                         ("decision", "open chrome"), ("decision", {"steps": []}),
                                         ("decision", {"steps": [{"fields": {}}]}), ("thinking", "slow"),
                                         ("reply", 5), ("trigger", None), ("action", "{not json")])
def test_unknown_and_malformed_events_change_nothing(kind, value):
    st = fresh()
    assert not st.apply(kind, value, 0)
    assert st == fresh()


def test_decision_shapes():
    d = ov.normalize_decision({"source": "system2", "ms": 8900, "steps": [
        {"action": "open_app", "fields": {"app": "Chrome"}}, "junk", {"action": ""},
        {"action": "web_search", "query": "weather"}]})
    assert d.source == "system2" and d.ms == 8900
    assert d.steps == (Step("open_app", {"app": "Chrome"}), Step("web_search", {"query": "weather"}))
    bare = ov.normalize_decision({"action": "media_control", "control": "pause"})
    assert bare.steps == (Step("media_control", {"control": "pause"}),) and bare.source is None
    assert ov.normalize_decision({"steps": [{"action": "cancel"}], "ms": "fast"}).ms is None


def test_round_two_action_event_is_still_understood():
    st = fresh()
    assert st.apply("action", '{"action": "play_media", "query": "Radiohead", "app": "Spotify"}', 0)
    assert st.decision.steps == (Step("play_media", {"query": "Radiohead", "app": "Spotify"}),)


def test_thinking_clock_follows_reports_and_falls_back_to_the_local_clock():
    st = fresh()
    st.apply("state", "thinking", 10.0)
    assert st.thinking_seconds(12.5) == pytest.approx(2.5)  # no report yet: time in the state
    assert st.apply("thinking", 3.0, 13.0)
    assert st.thinking_seconds(13.1) == pytest.approx(3.1)  # carried forward between reports
    assert st.thinking_seconds(20.0) == pytest.approx(3.3)  # a stalled reporter: a stalled clock
    st.apply("state", "executing", 21)
    st.apply("state", "thinking", 30)
    assert st.thinking is None  # a new thinking spell starts from zero


# ---------------------------------------------------------------- wording


@pytest.mark.parametrize("source,ms,text", [
    ("system1", 1.234, "System 1 · 1.2 ms"),
    ("system1", 42.0, "System 1 · 42 ms"),
    ("system2", 8900, "Codex · 8.9 s"),
    ("system2_fields", 7400, "Codex fields · 7.4 s"),
    ("system2_fields", None, "Codex fields"),
    ("habit", 0.3, "Habit"),
    ("user", 0.5, "You taught me"),
    ("system1", None, "System 1"),
    ("something_new", 3, "Something new"),
    (None, 3, None),
])
def test_badge(source, ms, text):
    assert ov.badge_text(source, ms) == text


@pytest.mark.parametrize("step,row", [
    (Step("open_app", {"app": "Notepad"}), ("Open app", "Notepad", "")),
    (Step("open_website", {"site": "Amazon", "browser": "Chrome"}), ("Open site", "Amazon", "in Chrome")),
    (Step("web_search", {"query": "weather", "site": "Flipkart", "browser": "Edge"}),
     ("Search", "weather", "on Flipkart in Edge")),
    (Step("play_media", {"query": "Anirudh", "app": "Spotify"}), ("Play", "Anirudh", "on Spotify")),
    (Step("play_media", {}), ("Play", "music", "")),
    (Step("media_control", {"control": "volume_up"}), ("Media", "Volume up", "")),
    (Step("send_message", {"contact": "Aai", "text": "yetoy"}), ("Message", "“yetoy”", "to Aai")),
    (Step("send_message", {"contact": "Raju", "text": ""}), ("Message", "Raju", "")),
    (Step("type_text", {"text": "kal meeting"}), ("Type", "“kal meeting”", "")),
    (Step("click", {"target": "Submit"}), ("Click", "Submit", "")),
    (Step("read_screen", {}), ("Read", "the screen", "")),
    (Step("new_thing", {"a": "x", "b": None, "c": "y"}), ("New thing", "x, y", "")),
])
def test_describe_step(step, row):
    assert ov.describe_step(step) == row


def test_header_wording():
    st = fresh(hotkey="ctrl+space")
    st.apply("trigger", "tap", 0)
    st.apply("state", "listening", 0)
    assert ov.header(st, 0) == ("Listening", ov.STATE_COLOURS["listening"], "tapped Ctrl+Space")
    st.apply("state", "thinking", 0)
    st.apply("thinking", 4.21, 0)
    assert ov.header(st, 0)[2] == "4.2 s · asking Codex"
    st.apply("state", "contact_not_found", 1)
    assert ov.header(st, 1)[:2] == ("Contact not found", ov.STATE_COLOURS["error"])
    st.apply("state", "disk_on_fire", 2)
    assert ov.header(st, 2)[:2] == ("Disk on fire", ov.STATE_COLOURS["error"])


def test_language_chip():
    assert ov.language_chip("ta") == "தமிழ் · Tamil"
    assert ov.language_chip("en") == "English"
    assert ov.language_chip("none") is None and ov.language_chip(None) is None
    assert ov.language_chip("xx") == "XX"
    assert set(ov.LANGUAGES) == {"hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa"}


def test_format_hotkey():
    assert ov.format_hotkey("ctrl+shift+space") == "Ctrl+Shift+Space"


# ---------------------------------------------------------------- text fitting


def test_clusters_keep_conjuncts_and_vowel_signs_whole():
    assert ov.clusters("क्षत्रिय") == ["क्ष", "त्रि", "य"]
    assert ov.clusters("தமிழ்") == ["த", "மி", "ழ்"]
    assert "".join(ov.clusters("ಕನ್ನಡ")) == "ಕನ್ನಡ"
    assert ov.clusters("ab") == ["a", "b"]


def test_wrap_breaks_between_words():
    lines = ov.wrap("aaa bbb ccc ddd", 7 * 7, measure, 3)
    assert lines == ["aaa bbb", "ccc ddd"]
    assert all(measure(line) <= 49 for line in lines)


def test_wrap_ellipsizes_what_does_not_fit():
    lines = ov.wrap("one two three four five six seven eight", 9 * 7, measure, 2)
    assert len(lines) == 2 and lines[-1].endswith(ov.ELLIPSIS)
    assert all(measure(line) <= 63 for line in lines)


def test_wrap_breaks_a_word_too_long_for_a_line_between_clusters():
    word = "क्षत्रिय" * 6
    lines = ov.wrap(word, 4 * 7, measure, 10)
    assert "".join(lines) == word
    assert all(measure(line) <= 28 for line in lines)
    assert all(not ov.unicodedata.category(line[0]).startswith("M") for line in lines)


def test_wrap_of_a_long_word_stops_at_the_line_limit():
    lines = ov.wrap("x" * 100 + " tail", 10 * 7, measure, 2)
    assert len(lines) == 2 and lines[-1].endswith(ov.ELLIPSIS) and measure(lines[-1]) <= 70


def test_ellipsize():
    assert ov.ellipsize("hello world", 6 * 7, measure) == "hello…"


# ---------------------------------------------------------------- layout


def scene_for(events, now=1.0, scale=1.0):
    st = fresh()
    for kind, value in events:
        st.apply(kind, value, 0)
    return st, ov.compose(st, now, FakeMetrics(), scale)


def texts(scene):
    return [op.text for op in scene.ops if isinstance(op, ov.TextOp)]


def inside(scene):
    for op in scene.ops:
        right = op.x + (FakeMetrics().measure(op.font, op.text) if isinstance(op, ov.TextOp) else op.w)
        bottom = op.y + (20 if isinstance(op, ov.TextOp) else op.h)
        if op.x < 0 or op.y < 0 or right > scene.width or bottom > scene.height:
            return False
    return True


def test_ready_shows_the_hint():
    _, scene = scene_for([])
    assert "Ready" in texts(scene)
    assert any("Hey Bol OS" in t for t in texts(scene))
    assert not scene.animating and inside(scene)


def test_listening_draws_a_meter_and_animates():
    _, scene = scene_for([("trigger", "wake"), ("state", "listening"), ("level", 0.8)])
    bars = [op for op in scene.ops if isinstance(op, ov.ShapeOp) and op.w == 3]
    assert len(bars) == ov.LEVEL_BARS
    assert bars[-1].h > bars[0].h
    assert scene.animating and inside(scene)
    assert "heard “Hey Bol OS”" in texts(scene)


def test_meter_stays_while_transcribing_then_gives_way_to_the_words():
    st, listening = scene_for([("state", "listening"), ("level", 0.8)])
    st.apply("state", "transcribing", 1)
    transcribing = ov.compose(st, 1, FakeMetrics())
    assert transcribing.height == listening.height
    st.apply("heard", "Chrome kholo", 1)
    heard = ov.compose(st, 1, FakeMetrics())
    assert not [op for op in heard.ops if isinstance(op, ov.ShapeOp) and op.w == 3]


def test_a_multi_step_decision_is_numbered_and_laid_out_in_order():
    _, scene = scene_for([
        ("heard", "Chrome kholo aur weather search karo"), ("language", "hi"),
        ("decision", decision("system1", 1.4, ("open_app", {"app": "Chrome"}),
                              ("web_search", {"query": "Bangalore ka weather", "browser": "Chrome"}))),
        ("reply", "Chrome खोल दिया और weather सर्च कर दिया।"), ("state", "done")])
    t = texts(scene)
    for expected in ["Done", "हिन्दी · Hindi", "System 1 · 1.4 ms", "1", "2", "Open app", "Chrome",
                     "Search", "Bangalore ka weather", "in Chrome"]:
        assert expected in t
    assert t.index("1") < t.index("Open app") < t.index("2") < t.index("Search")
    assert inside(scene)
    ys = {op.text: op.y for op in scene.ops if isinstance(op, ov.TextOp)}
    assert ys["Open app"] < ys["Search"]


def test_a_single_step_is_not_numbered():
    _, scene = scene_for([("decision", decision())])
    assert "1" not in texts(scene)


def test_long_content_stays_inside_the_window_at_any_scale():
    long = "likho ki " + " ".join(["aaj ki meeting mein bahut saari baatein hui"] * 8)
    for scale in (1.0, 1.25, 1.5, 2.0):
        _, scene = scene_for([
            ("heard", long), ("language", "ml"),
            ("decision", decision("system2", 8900, ("type_text", {"text": long}),
                                  ("send_message", {"contact": "Priya", "text": long}),
                                  ("open_website", {"site": "x" * 300, "browser": "Chrome"}))),
            ("reply", long), ("state", "thinking")], scale=scale)
        assert scene.width == round(ov.WIDTH * scale)
        assert inside(scene)
        heard_lines = [op for op in scene.ops if isinstance(op, ov.TextOp) and op.font == "heard"]
        assert len(heard_lines) == 3 and heard_lines[-1].text.endswith(ov.ELLIPSIS)


def test_a_quote_cut_short_keeps_its_closing_mark():
    _, scene = scene_for([("decision", decision("system1", 2, ("type_text", {"text": "word " * 200})))])
    values = [op for op in scene.ops if isinstance(op, ov.TextOp) and op.font == "value"]
    assert len(values) == 3 and values[0].text.startswith("“") and values[-1].text.endswith(ov.ELLIPSIS + "”")
    assert inside(scene)


def test_a_long_state_label_never_runs_under_the_language_chip():
    _, scene = scene_for([("language", "ml"), ("state", "a_very_long_outcome_key_nobody_has_wording_for_yet")])
    m = FakeMetrics()
    label = next(op for op in scene.ops if isinstance(op, ov.TextOp) and op.font == "state")
    chip = next(op for op in scene.ops if isinstance(op, ov.TextOp) and op.font == "chip")
    assert label.text.endswith(ov.ELLIPSIS)
    assert label.x + m.measure("state", label.text) < chip.x
    assert inside(scene)


def test_the_status_dot_pulses_only_while_working():
    st, a = scene_for([("state", "thinking")], now=0.0)
    b = ov.compose(st, 0.7, FakeMetrics())
    assert a.ops[0].colour != b.ops[0].colour
    st.apply("state", "done", 1)
    c, d = ov.compose(st, 1.0, FakeMetrics()), ov.compose(st, 1.7, FakeMetrics())
    assert c.ops[0].colour == d.ops[0].colour and not c.animating


def test_every_state_has_wording_and_colour():
    for state in ["ready", "listening", "hearing", "transcribing", "understanding", "thinking", "confirming",
                  "executing", "done", "error"]:
        label, colour, _ = ov.header(fresh(state=state), 0)
        assert label and colour.startswith("#") and len(colour) == 7


# ---------------------------------------------------------------- pixels


def test_shape_pixels_are_antialiased_rounded_rectangles():
    rows = ov.shape_pixels(10, 10, 5, "#ffffff", bg="#000000")
    assert len(rows) == 10 and all(len(r) == 10 for r in rows)
    assert rows[5][5] == "#ffffff" and rows[0][0] == "#000000"
    assert rows[0][3] not in ("#ffffff", "#000000")  # a partly covered edge pixel


def test_changed_ops_redraws_only_what_moved():
    st, before = scene_for([("state", "listening"), ("level", 0.2)])
    st.apply("level", 0.9, 0.05)
    after = ov.compose(st, 1.0, FakeMetrics())
    changed = ov.changed_ops(before.ops, after.ops)
    assert changed and len(changed) < len(after.ops) // 2  # the meter scrolled; the header did not move
    assert all(isinstance(after.ops[i], ov.ShapeOp) for i in changed)
    assert ov.changed_ops(after.ops, after.ops) == []
    st.apply("heard", "Chrome kholo", 0.1)  # the meter gives way to words: a different scene
    assert ov.changed_ops(after.ops, ov.compose(st, 1.0, FakeMetrics()).ops) is None
    assert ov.changed_ops([], after.ops) is None


def test_the_pulse_uses_a_few_tones():
    st, _ = scene_for([("state", "thinking")])
    tones = {ov.compose(st, t / 100, FakeMetrics()).ops[0].colour for t in range(300)}
    assert 2 < len(tones) <= ov.PULSE_STEPS + 1


def test_blend():
    assert ov.blend("#000000", "#ffffff", 0.5) == "#808080"
    assert ov.blend("#123456", "#abcdef", 0) == "#123456"


def test_framed_rgba_cuts_corners_and_draws_the_border():
    w, h = 40, 30
    bgra = bytes([10, 20, 30, 0]) * (w * h)
    rgba = ov.framed_rgba(w, h, bgra, 8, "#ff0000")

    def px(x, y):
        return tuple(rgba[(y * w + x) * 4:(y * w + x) * 4 + 4])

    assert px(20, 15) == (30, 20, 10, 255)  # inside: BGRA -> RGBA, untouched
    assert px(0, 0)[3] == 0  # the corner is cut away
    assert px(20, 0)[:3] == (255, 0, 0) and px(20, 0)[3] == 255  # the hairline border
    assert px(0, 15)[:3] == (255, 0, 0)
    assert 0 < px(2, 2)[3] < 255  # the curve is antialiased
    assert ov.framed_rgba(w, h, bgra, 8)[(15 * w + 0) * 4:(15 * w) * 4 + 3] == bytes([30, 20, 10])


def test_save_png_writes_a_valid_png(tmp_path):
    w, h = 3, 2
    rgba = bytes(range(w * h * 4))
    path = tmp_path / "x" / "shot.png"
    ov.save_png(path, (w, h, rgba))
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (w, h)
    idat = data.index(b"IDAT")
    (length,) = struct.unpack(">I", data[idat - 4:idat])
    raw = zlib.decompress(data[idat + 4:idat + 4 + length])
    assert raw == b"\x00" + rgba[:12] + b"\x00" + rgba[12:]


def test_demo_covers_every_language():
    assert sorted(scene[0] for scene in ov.LANGUAGE_SCENES) == sorted(ov.LANGUAGES)
    for _, _, heard, dec, final, reply in ov.LANGUAGE_SCENES:
        assert heard and reply and ov.normalize_decision(dec) and final in ("done", "confirming")


def test_a_finished_command_settles_back_to_the_idle_card():
    st = OverlayState()
    st.apply("heard", "[form request]", 0.0)
    st.apply("state", "done", 1.0)
    assert not st.settle(1.0 + ov.SETTLE_S - 0.1)
    assert st.state == "done"
    assert st.settle(1.0 + ov.SETTLE_S)
    assert (st.state, st.heard, st.reply, st.decision) == ("ready", "", "", None)
    assert not st.settle(100.0)


def test_working_and_confirming_states_never_settle():
    st = OverlayState()
    for state in ("listening", "thinking", "confirming", "executing", "guiding"):
        st.apply("state", state, 0.0)
        assert not st.settle(1000.0)
        assert st.state == state
