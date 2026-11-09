"""The buddy beside the cursor: its motion, drawings and state machine (no window needed)."""

import math
import time

import numpy as np
import pytest

from bolos import buddy as b
from bolos.config import load_settings
from bolos.controls import Controls
from bolos.user_settings import SettingError, UserSettings, env_defaults


def test_states_pick_the_drawing():
    assert b.visual_for("listening") == b.visual_for("hearing") == "bars"
    assert {b.visual_for(s) for s in ("transcribing", "understanding", "thinking", "executing")} == {"spinner"}
    assert b.visual_for("ready") == b.visual_for("done") == b.visual_for("contact_not_found") == "arrow"
    assert b.busy("thinking") and b.busy("confirming") and b.busy("guiding") and not b.busy("done")


def test_spring_trails_and_settles_on_the_target():
    spring = b.Spring(0, 0)
    x, _ = spring.step(100, 50, 1 / 60)
    assert 0 < x < 100  # it trails, it does not jump
    for _ in range(90):
        spring.step(100, 50, 1 / 60)
    assert spring.settled(100, 50)


def test_flight_is_clickys_arc():
    short = b.Flight((0, 0), (50, 0))
    long = b.Flight((0, 0), (5000, 0))
    assert short.duration == b.FLIGHT_MIN_S and long.duration == b.FLIGHT_MAX_S
    mid = b.Flight((0, 0), (800, 0))
    assert mid.duration == pytest.approx(1.0)
    start, apex, end = mid.pose(0), mid.pose(mid.duration / 2), mid.pose(mid.duration)
    assert (start.x, start.y) == (0, 0) and (round(end.x), round(end.y)) == (800, 0)
    assert apex.y < 0  # it arcs upward
    assert apex.swell == pytest.approx(1 + b.SWELL)
    assert start.swell == end.swell == pytest.approx(1.0)
    assert apex.degrees == pytest.approx(90)  # flying right, the tip faces right
    # The arc's height is capped (80 points), and scales with the display.
    assert -mid.pose(mid.duration / 2).y <= 80
    assert b.Flight((0, 0), (1600, 0), scale=2).duration == pytest.approx(1.0)


def test_landing_is_beside_the_target_and_on_its_monitor():
    assert b.landing((100, 100, 140, 120), None, 1.0) == (128, 122)
    x, y = b.landing((1900, 1060, 1920, 1080), (0, 0, 1920, 1080), 1.5)
    assert x == 1920 - 30 and y == 1080 - 30


def test_bubble_pops_in_from_half_size_with_a_little_overshoot():
    assert b.pop(0) == pytest.approx(0.5)
    assert max(b.pop(t / 100) for t in range(100)) > 1.0
    assert b.pop(2.0) == pytest.approx(1.0, abs=1e-3)


def test_typing_never_splits_a_letter_from_its_vowel_sign():
    letters = b.clusters("भेजें फ़ाइल")
    assert "".join(letters) == "भेजें फ़ाइल"
    assert letters[:3] == ["भे", "जें", " "]


def test_the_arrow_points_where_it_is_turned():
    tip = b.arrow_points(50, 50, 16, b.REST_DEGREES)[0]
    assert tip[0] < 50 and tip[1] < 50  # up and to the left at rest, like a cursor
    tip = b.arrow_points(50, 50, 16, 90)[0]
    assert tip[0] > 50 and abs(tip[1] - 50) < 1e-6
    image = b.arrow((60, 60), 30, 30, 16, b.REST_DEGREES, 8)
    assert image[30, 30, 3] == pytest.approx(1.0)  # solid in the middle
    assert image[30, 30, 0] == pytest.approx(b.ORANGE[0] / 255, abs=0.02)
    assert image[2, 2, 3] < 0.05  # clear in the corner


def test_the_bars_move_with_the_voice():
    quiet, loud = b.bar_heights(0.0, 0.0, 1.0), b.bar_heights(0.4, 0.0, 1.0)
    assert all(lo < hi for lo, hi in zip(quiet, loud, strict=True))
    assert max(loud) == loud[1]  # the tallest bar of the Bol OS mark


def test_the_spinner_turns():
    def brightest_column(seconds):
        image = b.spinner((40, 40), 20, 20, seconds, 1.0)
        return np.unravel_index(np.argmax(image[..., 3] * (image[..., 0] > 0.5)), image.shape[:2])

    assert brightest_column(0.0) != brightest_column(0.2)


class _Owner:
    def __init__(self, visible=True):
        self.visible = visible
        self._position = None
        self._scale = 1.0


@pytest.fixture
def window(monkeypatch):
    def make(visible=True):
        w = b._Window(_Owner(visible))
        monkeypatch.setattr(w, "scale_at", lambda point: 1.0)
        monkeypatch.setattr(w, "cursor_shown", lambda: True)
        return w

    return make


def run(w, start, seconds, cursor=(500, 500), dt=1 / 60):
    now = start
    while now < start + seconds:
        now += dt
        w.update(now, dt, cursor)
    return now


def test_widget_stays_put_clicks_and_remembers_a_drag(monkeypatch, tmp_path):
    area = (0, 0, 800, 600)
    monkeypatch.setattr(b, "_monitor_at", lambda *args, **kwargs: area)
    monkeypatch.setattr(b._Window, "scale_at", lambda *args: 1.0)
    clicks, moves = [], []
    owner = b.Buddy(on_click=lambda *args: clicks.append(args), on_move=lambda *args: moves.append(args),
                    position_path=tmp_path / "position.json")
    widget = b._Window(owner)
    now = run(widget, 0.0, 0.5, cursor=(50, 50))
    initial = widget.position
    assert (widget.spring.x, widget.spring.y) == initial
    widget.handle("point", ((20, 10, 40, 30), "Menu", area), now, (50, 50))
    run(widget, now, 0.2, cursor=(300, 240))
    assert (widget.spring.x, widget.spring.y) == initial

    widget.press_at = widget.press_center = initial
    widget._finish_press((initial[0] + 2, initial[1]))
    assert clicks[0][:2] == initial and not moves

    widget.press_at = widget.press_center = initial
    widget._finish_press((350, 250))
    assert moves[0][:2] == (350, 250) and len(clicks) == 1
    assert b._Window(owner).position == (350, 250)


def test_points_says_the_label_holds_and_flies_back(window):
    w = window()
    now = run(w, 0.0, 0.5)
    w.handle("point", ((100, 100, 140, 120), "Attach", None), now, (500, 500))
    assert w.mode == "flying"
    now = run(w, now, w.flight.duration + 0.05)
    assert w.mode == "pointing" and (round(w.spring.x), round(w.spring.y)) == (128, 122)
    now = run(w, now, 0.5)
    assert w._bubble_text(now)[0] == "Attach"  # typed out, 30-60 ms a letter
    now = run(w, now, b.HOLD_S + b.BUBBLE_FADE_S + b.RETURN_PAUSE_S)
    assert w.mode == "returning"
    run(w, now, b.FLIGHT_MAX_S + 0.1)
    assert w.mode == "follow"


def test_moving_the_mouse_cancels_the_flight_back(window):
    w = window()
    now = run(w, 0.0, 0.2)
    w.handle("point", ((100, 100, 140, 120), "", None), now, (500, 500))
    deadline = now + 6
    while w.mode != "returning" and now < deadline:
        now = run(w, now, 1 / 60)
    assert w.mode == "returning"
    w.update(now + 0.02, 0.02, (900, 500))
    assert w.mode == "follow"


def test_a_new_press_brings_it_back_but_a_guide_listening_does_not(window):
    w = window()
    w.handle("point", ((100, 100, 140, 120), "Next", None), 0.0, (500, 500))
    w.handle("state", "listening", 0.1, (500, 500))
    assert w.mode == "returning"

    w = window()
    w.handle("state", "guiding", 0.0, (500, 500))
    w.handle("point", ((100, 100, 140, 120), "Next", None), 0.0, (500, 500))
    w.handle("state", "listening", 0.1, (500, 500))
    assert w.mode == "flying"


def test_switched_off_it_only_appears_for_a_command(window):
    w = window(visible=False)
    now = run(w, 0.0, 1.0)
    assert w.opacity == 0.0
    w.handle("state", "listening", now, (500, 500))
    now = run(w, now, 0.5)
    assert w.opacity == pytest.approx(1.0)
    assert w.mix["bars"] == pytest.approx(1.0)
    w.handle("state", "done", now, (500, 500))
    now = run(w, now, b.TRANSIENT_PAUSE_S * 0.8)
    assert w.opacity == pytest.approx(1.0)  # it lingers a moment
    run(w, now, b.TRANSIENT_PAUSE_S + b.VANISH_S)
    assert w.opacity == 0.0


def test_it_hides_with_the_system_cursor(window):
    w = window()
    w.cursor_shown = lambda: False
    run(w, 0.0, 1.0)
    assert w.opacity == 0.0


def test_the_hello_types_itself_then_the_hint_stays(window):
    w = window()
    w.handle("welcome", ("Hi! I'm Bol OS", "Press Ctrl+Space and talk to me"), 0.0, (500, 500))
    now = run(w, 0.0, 2.0)
    assert w._bubble_text(now)[0] == "Hi! I'm Bol OS"
    now = run(w, now, 4.0)
    assert w._bubble_text(now)[0] == "Press Ctrl+Space and talk to me"
    w.handle("state", "listening", now, (500, 500))
    assert w._bubble_text(now)[0] == ""  # talking dismisses it


def test_compose_puts_the_bubble_on_the_monitor(window):
    w = window()
    w.text = type("Text", (), {"render": lambda self, text, px, bg, ink: np.zeros((14, 8 * len(text), 3), np.uint8)})()
    w.handle("point", ((1880, 100, 1900, 120), "Settings", None), 0.0, (500, 500))
    now = run(w, 0.0, 2.0)
    left, top, pixels = w.compose(now, (0, 0, 1920, 1080))
    assert pixels.dtype == np.uint8 and pixels.shape[2] == 4
    solid = np.nonzero((pixels[..., 3] > 200).any(axis=0))[0]
    assert left + solid.max() < 1920  # the bubble went to the buddy's left instead of running off screen
    assert left + solid.min() < w.spring.x - 60


def test_the_ring_waits_for_the_buddy_and_hiding_brings_it_back():
    from bolos.guide.pointer import Pointer
    from bolos.guide.screen import Rect

    class FakeBuddy:
        def __init__(self):
            self.points, self.released = [], 0

        def point(self, target, label="", bounds=None):
            self.points.append((target, label, bounds))
            return 0.3

        def release(self):
            self.released += 1

    fake = FakeBuddy()
    with Pointer(fake) as pointer:
        pointer.show(Rect(10, 20, 50, 40), Rect(0, 0, 1920, 1080), label="Attach")
        deadline = time.monotonic() + 3
        while not fake.points and time.monotonic() < deadline:
            time.sleep(0.02)
        pointer.hide()
    assert fake.points == [((10, 20, 50, 40), "Attach", (0, 0, 1920, 1080))]
    assert fake.released == 1


class _FakeBuddy:
    def __init__(self):
        self.calls = []

    def show(self, visible):
        self.calls.append(("show", visible))

    def welcome(self, greeting, hint):
        self.calls.append(("welcome", greeting, hint))


def test_the_dashboard_switches_the_buddy_and_replays_its_hello(tmp_path):
    defaults = env_defaults(load_settings())
    store = UserSettings.load(tmp_path / "settings.json", {**defaults, "user_language": "en", "hotkey": "ctrl+space"})
    assert store.options["buddy_visible"].default is True and store.options["buddy_visible"].live
    controls = Controls(store)
    with pytest.raises(SettingError):
        controls.test("welcome")
    controls.buddy = _FakeBuddy()
    controls.update({"buddy_visible": False})
    assert controls.buddy.calls == [("show", False)]
    assert controls.test("welcome") == "welcome"
    from bolos.responses import say

    language = controls.settings().user_language
    assert controls.buddy.calls[-1] == ("welcome", say("buddy_hello", language),
                                        say("buddy_hint", language, hotkey="Ctrl+Space"))


def test_every_language_has_the_hello():
    from bolos.responses import LANGUAGE_NAMES, say

    for code in LANGUAGE_NAMES:
        assert say("buddy_hello", code) and "Alt+F9" in say("buddy_hint", code, hotkey="Alt+F9")


def test_buddy_flight_matches_the_documented_numbers():
    # Clicky's constants, kept as the source has them (OverlayWindow.swift).
    assert (b.OFFSET, b.SPRING_RESPONSE, b.SPRING_DAMPING) == ((35, 25), 0.2, 0.6)
    assert (b.FLIGHT_SPEED, b.FLIGHT_MIN_S, b.FLIGHT_MAX_S, b.ARC, b.ARC_MAX) == (800.0, 0.6, 1.4, 0.2, 80.0)
    assert (b.HOLD_S, b.CANCEL_RETURN, b.LANDING, b.REST_DEGREES) == (3.0, 100.0, (8, 12), -35.0)
    assert math.isclose(b.SWELL, 0.3)
