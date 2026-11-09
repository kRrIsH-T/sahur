"""Guide mode: planning, finding targets again, the wait-for-the-user loop, and how the assistant runs it.

The screen side is faked: an observation is a list of marks, the screen's "fingerprint" is a named state that
a click can change, and the user's clicks and words come from a script the fake voice plays back.
"""

import dataclasses
import json
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from bolos import actions as a
from bolos.config import load_settings
from bolos.decide import Parse
from bolos.executor import Outcome
from bolos.guide import planner as plans
from bolos.guide import screen
from bolos.guide.pointer import Flight, Sprite
from bolos.guide.screen import BUTTON, EDIT, LISTITEM, TEXT, Mark, Observation, Rect, number, select_marks
from bolos.guide.session import REMIND_S, Click, Guide, find, ground
from bolos.llm import CodexLLM, OpenAICompatibleLLM
from bolos.overlay import OverlayState, describe_step, header
from bolos.pipeline import Assistant
from bolos.responses import say

MONITOR = Rect(0, 0, 2560, 1440)
START = Mark(1, "Start", BUTTON, Rect(443, 1368, 511, 1440))
SEARCH = Mark(2, "Search", EDIT, Rect(520, 1376, 840, 1432))
SETTINGS = Mark(3, "Settings", LISTITEM, Rect(1000, 500, 1100, 560))
BLUETOOTH = Mark(4, "Bluetooth & devices", LISTITEM, Rect(40, 300, 400, 340))


def observation(*marks, title="Desktop"):
    return Observation(MONITOR, 0.5, title, list(marks))


def step_answer(say_text="Neeche Start dabaiye.", mark=1, target="Start", nxt=(), complete=False, done_say="",
                action="click", x=0, y=0):
    return {"status": "step", "say": say_text, "action": action, "target": target, "mark": mark, "x": x, "y": y,
            "next": [{"say": s, "target": t, "action": "click"} for s, t in nxt], "complete": complete,
            "done_say": done_say}


# --- Planner ------------------------------------------------------------------------------------------------

def test_plan_grounds_the_first_step_on_a_mark_and_keeps_the_rest_by_label():
    plan = plans.parse(step_answer(nxt=[("Settings dabaiye.", "Settings")], complete=True, done_say="Ho gaya!"),
                       observation(START, SEARCH))
    assert plan.status == "step" and plan.complete and plan.done_say == "Ho gaya!"
    first, second = plan.steps
    assert (first.mark, first.point, first.target, first.action) == (1, None, "Start", "click")
    assert (second.mark, second.target) == (None, "Settings") and not second.grounded


def test_unknown_mark_falls_back_to_a_point_only_when_a_screenshot_was_sent(tmp_path):
    seen = observation(START)
    assert plans.parse(step_answer(mark=99, x=640, y=360), seen).steps[0].point is None
    seen.image = tmp_path / "screen.png"
    plan = plans.parse(step_answer(mark=99, x=640, y=360), seen)
    assert plan.steps[0].mark is None and plan.steps[0].point == (640, 360)
    plan = plans.parse(step_answer(mark=99, x=5000, y=360), seen)  # outside the 1280x720 image
    assert not plan.steps[0].grounded


@pytest.mark.parametrize("answer", [
    {"status": "maybe", "say": "x"},
    {"status": "step", "say": "   "},
    {},
])
def test_unusable_answers_give_no_plan(answer):
    assert plans.parse(answer, observation(START)) is None


def test_done_and_cannot_carry_only_what_to_say():
    assert plans.parse({"status": "done", "say": "Bluetooth on hai."}, observation()) == \
        plans.Plan("done", "Bluetooth on hai.")
    assert plans.parse({"status": "cannot", "say": "OTP kisi ko na batayein."}, observation()).status == "cannot"


def test_prediction_is_capped_and_stops_at_a_broken_step():
    many = [(f"Step {i}", f"T{i}") for i in range(9)]
    assert len(plans.parse(step_answer(nxt=many), observation(START)).steps) == 1 + plans.MAX_AHEAD
    answer = step_answer(nxt=[("ok", "A")])
    answer["next"] += [{"say": ""}, {"say": "after the gap", "target": "B", "action": "click"}]
    assert [s.target for s in plans.parse(answer, observation(START)).steps] == ["Start", "A"]


def test_the_prompt_asks_for_the_users_language_and_lists_the_marks():
    system = plans.system_prompt("ta-IN")
    assert "Tamil in its own native script" in system and "feminine" not in system
    assert "feminine first-person" in plans.system_prompt("hi")
    user = plans.user_prompt("wallpaper badlo", observation(START), ["Start dabaya"], "screen changed")
    assert "Goal: wallpaper badlo" in user and '[1] button "Start" at x=222 y=684 w=34 h=36' in user
    assert "1. Start dabaya" in user and "Note: screen changed" in user


def test_the_schema_suits_codex_structured_output():
    def strict(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                strict(child)
        if schema.get("type") == "array":
            strict(schema["items"])

    strict(plans.SCHEMA)


def test_codex_attaches_the_screenshot_before_its_other_options(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, input, **kwargs):
        seen["cmd"] = cmd
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
            f.write('{"status": "done", "say": "ok"}')
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("bolos.llm.subprocess.run", fake_run)
    llm = CodexLLM(dataclasses.replace(load_settings(), codex_path="codex.exe"))
    shot = tmp_path / "screen.png"
    assert llm.ask_json("system", "user", plans.SCHEMA, (shot,)) == {"status": "done", "say": "ok"}
    cmd = seen["cmd"]
    assert cmd[:4] == ["codex.exe", "exec", "-i", str(shot)] and cmd[-1] == "-"
    assert "computer_use" in cmd and "shell_tool" in cmd  # still no tools, whatever the screen says


def test_an_api_backend_sends_the_screenshot_as_an_image(monkeypatch, tmp_path):
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"\x89PNG fake")
    llm = OpenAICompatibleLLM(dataclasses.replace(load_settings(), llm_api_key="k", llm_base_url="http://x"))
    sent = {}

    def chat(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"status": "done"}'))])

    llm._chat = chat
    llm.ask_json("system", "user", plans.SCHEMA, (shot,))
    content = sent["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "user"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- Finding targets ------------------------------------------------------------------------------------------

def test_find_matches_labels_the_way_people_and_models_write_them():
    seen = observation(START, SETTINGS, BLUETOOTH, Mark(5, "Settings", BUTTON, Rect(0, 0, 9, 9), enabled=False))
    assert find("Bluetooth and devices", seen).rect == BLUETOOTH.rect
    assert find("settings", seen).rect == SETTINGS.rect  # the enabled one
    assert find("Setings", seen).rect == SETTINGS.rect  # a near miss
    assert find("Sign out", seen) is None and find("", seen) is None


TASKBAR_SEARCH = Mark(10, "Search", EDIT, Rect(440, 1380, 770, 1428), source="taskbar")
SETTINGS_SEARCH = Mark(11, "Search box, Find a setting", EDIT, Rect(113, 147, 797, 196))
WINDOW_SEARCH_BUTTON = Mark(12, "Search", BUTTON, Rect(308, 93, 362, 141))


def test_a_label_on_both_the_taskbar_and_the_window_means_the_window():
    # Seen live: "type Bluetooth in Search" pointed at the taskbar's Windows Search, not the Settings box.
    seen = observation(WINDOW_SEARCH_BUTTON, TASKBAR_SEARCH)
    assert find("Search", seen).rect == WINDOW_SEARCH_BUTTON.rect


def test_a_typing_step_points_at_a_text_box_never_a_button_of_that_name():
    seen = observation(WINDOW_SEARCH_BUTTON, SETTINGS_SEARCH, TASKBAR_SEARCH)
    assert find("Search", seen, "type").rect == TASKBAR_SEARCH.rect  # the only box named exactly so
    seen = observation(WINDOW_SEARCH_BUTTON, SETTINGS_SEARCH)
    assert find("Search", seen, "type").rect == SETTINGS_SEARCH.rect  # a box whose name starts with it
    # Clicks never take a partial name: "Delete" must not point at "Delete all".
    assert find("Search box", seen) is None
    assert find("Delete", observation(Mark(1, "Delete all", BUTTON, Rect(0, 0, 50, 20)))) is None


def test_a_typing_step_with_no_box_of_that_name_uses_the_focused_box():
    seen = observation(WINDOW_SEARCH_BUTTON, TASKBAR_SEARCH)
    seen.focused = Mark(0, "", EDIT, SETTINGS_SEARCH.rect)
    step = plans.Step('"Bluetooth" likhiye.', "type", "Settings search")
    assert ground(step, seen).rect == SETTINGS_SEARCH.rect
    assert ground(plans.Step("x", "click", "Settings search"), seen) is None


def test_the_nearest_of_two_same_named_things_follows_the_previous_ring():
    left = Mark(1, "Open", BUTTON, Rect(10, 10, 60, 40))
    right = Mark(2, "Open", BUTTON, Rect(2000, 10, 2050, 40))
    near_right = Mark(0, "", BUTTON, Rect(1900, 10, 1950, 40))
    from bolos.guide.session import Target

    assert ground(plans.Step("x", "click", "Open"), observation(left, right), Target(near_right.rect)).rect == \
        right.rect


def test_ground_uses_the_mark_then_the_point_then_the_label():
    seen = observation(START, SETTINGS)
    assert ground(plans.Step("x", "click", "Settings", mark=1), seen).rect == START.rect
    pointed = ground(plans.Step("x", "click", "", point=(640, 360)), seen).rect
    assert pointed.center == (1280, 720) and pointed.width == 88  # 22 screenshot px each side, at scale 0.5
    assert ground(plans.Step("x", "click", "Settings"), seen).rect == SETTINGS.rect


def test_marks_put_clickable_things_first_merge_duplicates_and_read_in_order():
    elements = [
        ("Settings", LISTITEM, Rect(1000, 500, 1100, 560), True),
        ("Settings", BUTTON, Rect(1001, 501, 1100, 560), True),  # the same thing, nested
        ("Hello", TEXT, Rect(10, 10, 200, 40), True),
        ("", TEXT, Rect(10, 50, 200, 80), True),  # unnamed text says nothing
        ("Off screen", BUTTON, Rect(3000, 10, 3100, 50), True),
        ("Tiny", BUTTON, Rect(10, 10, 13, 13), True),
        ("Bluetooth & devices", LISTITEM, Rect(160, 925, 1116, 934), True),  # a card scrolled nearly out of view
        ("Start", BUTTON, Rect(443, 1368, 511, 1460), True),  # clipped to the monitor
    ]
    marks = number(select_marks(elements, MONITOR))
    assert [(m.id, m.name) for m in marks] == [(1, "Hello"), (2, "Settings"), (3, "Start")]
    assert marks[2].rect.bottom == 1440
    assert [m.name for m in select_marks(elements, MONITOR, limit=2)] == ["Settings", "Start"]


# --- The wait-for-the-user loop -----------------------------------------------------------------------------

class FakePlanLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def ask_json(self, system, user, schema, images=()):
        self.calls.append(user)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeScreen:
    """The screen as a named state; each state has its marks."""

    def __init__(self, states, state="desktop"):
        self.states = states
        self.state = state
        self.looks = 0

    def look(self, with_image):
        self.looks += 1
        return observation(*self.states[self.state])

    def fingerprint(self, area):
        return self.state

    @staticmethod
    def changed(before, after):
        return 0.0 if before == after else 1.0


class FakePointer:
    def __init__(self):
        self.shown, self.events = [], []

    def show(self, rect, bounds=None, label=""):
        self.shown.append(rect)
        self.events.append("show")

    def hide(self):
        self.events.append("hide")

    def hide_after(self, seconds):
        self.events.append(("expire", seconds))

    def close(self):
        self.events.append("close")


class FakeInputs:
    def __init__(self):
        self.clicks, self.keys, self.typed_at = deque(), deque(), 0.0
        self.stopped = False

    def drain(self):
        clicks, keys = list(self.clicks), list(self.keys)
        self.clicks.clear()
        self.keys.clear()
        return clicks, keys

    def stop(self):
        self.stopped = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedVoice:
    """Speaks into a list; each poll plays the next scripted event: a reply, a click (optionally moving the
    screen to a new state), or nothing."""

    def __init__(self, script, screen, inputs, clock):
        self.script = deque(script)
        self.screen, self.inputs, self.clock = screen, inputs, clock
        self.spoken = []  # what was said after the opening "I'll show you" (kept in self.opened)
        self.opened = []

    def speak(self, text, language):
        if not self.spoken and not self.opened and text == say("guide_start", language):
            self.opened.append(text)
        else:
            self.spoken.append(text)
        return True

    def poll(self, timeout):
        self.clock.now += timeout
        if not self.script:
            return None
        event = self.script.popleft()
        if event is None:
            return None
        if event[0] == "say":
            return event[1], None
        _, (x, y), new_state = event
        self.inputs.clicks.append(Click(x, y, "left", self.clock.now))
        if new_state:
            self.screen.state = new_state
        return None


STATES = {"desktop": [START, SEARCH], "start": [START, SEARCH, SETTINGS], "settings": [BLUETOOTH],
          "elsewhere": [SEARCH]}


def make_guide(llm, script, state="desktop"):
    screen = FakeScreen(STATES, state)
    inputs, clock, pointer = FakeInputs(), Clock(), FakePointer()
    voice = ScriptedVoice(script, screen, inputs, clock)
    statuses = []
    guide = Guide(llm, voice, lambda *e: statuses.append(e), look=screen.look, fingerprint=screen.fingerprint,
                  changed=screen.changed, pointer=pointer, inputs=lambda: inputs, clock=clock, sleep=clock.sleep)
    return guide, voice, pointer, inputs, statuses


def test_a_screen_answer_points_and_remembers_only_the_same_window(tmp_path):
    reply = {"say": "It is here.", "target": "Start", "mark": START.id, "x": 0, "y": 0}
    llm = FakePlanLLM(reply, reply, reply)
    guide, voice, pointer, _, _ = make_guide(llm, [])
    seen = observation(START)
    seen.cursor = START.rect.center
    guide.answer_look = lambda: seen
    shot = tmp_path / "capture.png"
    shot.write_bytes(b"image")
    seen.image = shot
    assert guide.answer("Where is the Start button?", "en").ok
    assert not shot.exists() and pointer.shown == [START.rect] and pointer.events[-1] == ("expire", 5.0)
    assert voice.spoken[-1] == "It is here." and "over mark [1]" in llm.calls[0]
    seen.image = None
    assert guide.answer("What does that button do?", "en").ok
    assert "Where is the Start button?" in llm.calls[1]
    assert 'What does the control labeled "Start" do?' in llm.calls[1] and "over mark" not in llm.calls[1]
    seen.window_title = "Another app"
    assert guide.answer("Where is it?", "en").ok
    assert "Earlier dialogue" not in llm.calls[2]


@pytest.mark.parametrize("reply,focus_after,key", [(False, 1, "guide_stopped"), (True, 2, "focus_changed")])
def test_takeover_only_clicks_when_the_original_control_is_confirmed(monkeypatch, reply, focus_after, key):
    guide, _, _, _, _ = make_guide(FakePlanLLM(step_answer()), [])
    active = [1]
    seen = observation(START)
    seen.hwnd = 1
    guide.answer_look = lambda: seen
    monkeypatch.setattr(screen, "user_window", lambda: active[0])
    monkeypatch.setattr(screen, "window_title", lambda _: seen.window_title if active[0] == 1 else "Other")
    monkeypatch.setattr("bolos.executor.uia.click", lambda *_args, **_kwargs: pytest.fail("unverified click"))

    def confirm(label):
        assert label == START.name
        active[0] = focus_after
        return reply

    assert guide.drive("Open the menu", "en", confirm).key == key


def test_takeover_reobserves_after_click_even_when_the_plan_claims_it_is_complete(monkeypatch):
    llm = FakePlanLLM(step_answer(complete=True), {"status": "done", "say": "Menu opened."})
    guide, voice, _, _, _ = make_guide(llm, [])
    seen = observation(START)
    seen.hwnd = 1
    guide.answer_look = lambda: seen
    calls = []
    monkeypatch.setattr(screen, "user_window", lambda: seen.hwnd)
    monkeypatch.setattr(screen, "window_title", lambda _: seen.window_title)
    monkeypatch.setattr("bolos.executor.uia.click", lambda label, binding: calls.append((label, binding)))
    assert guide.drive("Open the menu", "en", lambda label: label == START.name).ok
    assert calls == [(START.name, (1, START.rect))] and "Clicked Start." in llm.calls[1]
    assert voice.spoken[-1] == "Menu opened."


def test_cursor_flight_starts_and_ends_at_the_correct_screen_points():
    flight = Flight((-500, 200), (-100, 200), 1, Rect(-1000, 0, 0, 800))
    offset = flight.size[0] // 2
    assert flight.origin(0) == (-500 - offset, 200 - offset)
    assert flight.origin(flight.duration) == (-100 - offset, 200 - offset)
    assert flight.origin(flight.duration / 2)[1] < 200 - offset


def test_a_whole_guide_from_one_plan_follows_the_users_clicks():
    llm = FakePlanLLM(step_answer(nxt=[("Settings dabaiye.", "Settings")], complete=True, done_say="Ho gaya!"))
    guide, voice, pointer, inputs, statuses = make_guide(llm, [
        None, ("click", START.rect.center, "start"),
        None, ("click", SETTINGS.rect.center, "settings"),
    ])
    outcome = guide.run("Settings kaise kholte hain dikhao", "hi")
    assert outcome == Outcome(True, "guide_done")
    assert voice.spoken == ["Neeche Start dabaiye.", "Settings dabaiye.", "Ho gaya!"]
    assert pointer.shown == [START.rect, SETTINGS.rect] and pointer.events[-1] == "close" and inputs.stopped
    assert len(llm.calls) == 1  # the second step was found locally, without asking System 2 again
    assert ("guide", {"step": 2}) in statuses and ("state", "guiding") in statuses


def test_without_a_complete_plan_it_looks_again_and_hears_that_it_is_done():
    llm = FakePlanLLM(step_answer(), {"status": "done", "say": "Start khul gaya."})
    guide, voice, _, _, _ = make_guide(llm, [("click", START.rect.center, "start")])
    assert guide.run("Start kholna sikhao", "hi") == Outcome(True, "guide_done")
    assert voice.spoken[-1] == "Start khul gaya." and "Neeche Start dabaiye." in llm.calls[1]


def test_a_click_elsewhere_that_changes_the_screen_plans_again_from_there():
    llm = FakePlanLLM(step_answer(), {"status": "done", "say": "Theek hai."})
    guide, _, _, _, _ = make_guide(llm, [("click", (5, 5), "elsewhere")])
    guide.run("Start kholna sikhao", "hi")
    assert "clicked somewhere else" in llm.calls[1]


def test_a_click_on_empty_space_is_ignored():
    llm = FakePlanLLM(step_answer(complete=True, done_say="Done."))
    guide, voice, _, _, _ = make_guide(llm, [("click", (5, 5), None), None, ("click", START.rect.center, "start")])
    assert guide.run("goal", "en") == Outcome(True, "guide_done") and voice.spoken[-1] == "Done."


def test_doing_the_next_step_directly_counts_for_both():
    llm = FakePlanLLM(step_answer(nxt=[("Settings dabaiye.", "Settings")], complete=True, done_say="Done."))
    # The user already has Start open and clicks Settings straight away.
    guide, voice, pointer, _, _ = make_guide(llm, [("click", SETTINGS.rect.center, "settings")], state="start")
    assert guide.run("goal", "en") == Outcome(True, "guide_done")
    assert voice.spoken == ["Neeche Start dabaiye.", "Done."] and pointer.shown == [START.rect]


def test_clicking_into_the_box_does_not_finish_a_typing_step_but_typing_does():
    llm = FakePlanLLM(step_answer('"Bluetooth" likhiye.', mark=2, target="Search", action="type", complete=True,
                                  done_say="Shabash."))
    guide, voice, _, inputs, _ = make_guide(llm, [("click", SEARCH.rect.center, None), None, None])
    original = guide.voice.poll

    def poll(timeout):  # after the click into the box, the user types and presses Enter
        result = original(timeout)
        if not voice.script and not inputs.keys and voice.spoken == ['"Bluetooth" likhiye.']:
            inputs.typed_at = guide.clock() + 0.01
            inputs.keys.append(0x0D)
        return result

    guide.voice.poll = poll
    assert guide.run("goal", "hi") == Outcome(True, "guide_done")
    assert voice.spoken == ['"Bluetooth" likhiye.', "Shabash."]


@pytest.mark.parametrize("reply,key,ok", [("ruko", "guide_stopped", True), ("band karo", "guide_stopped", True)])
def test_saying_stop_ends_the_guide(reply, key, ok):
    guide, voice, pointer, _, _ = make_guide(FakePlanLLM(step_answer()), [("say", reply)])
    assert guide.run("goal", "hi") == Outcome(ok, key)
    assert voice.spoken[-1] == say(key, "hi") and pointer.events[-1] == "close"


@pytest.mark.parametrize("action", ["right_click", "double_click"])
def test_a_single_left_click_does_not_complete_a_different_mouse_action(action):
    llm = FakePlanLLM(step_answer(action=action, complete=True))
    guide, _, _, _, _ = make_guide(llm, [("click", START.rect.center, None), ("say", "stop")])
    assert guide.run("goal", "en") == Outcome(True, "guide_stopped")


def test_saying_done_moves_on_and_saying_again_repeats():
    llm = FakePlanLLM(step_answer(complete=True, done_say="Shabash."))
    guide, voice, _, _, _ = make_guide(llm, [("say", "phir se bolo"), ("say", "ho gaya")])
    assert guide.run("goal", "hi") == Outcome(True, "guide_done")
    assert voice.spoken == ["Neeche Start dabaiye.", "Neeche Start dabaiye.", "Shabash."]


def test_localized_screen_question_keeps_the_lesson_running(monkeypatch):
    guide, _, _, _, statuses = make_guide(FakePlanLLM(step_answer(complete=True)), [
        ("say", "ee button enu madutte"), ("say", "mudinjiduchu"),
    ])
    answers = []

    def answer(question, language, text):
        answers.append((question, language, text))
        return Outcome(True, "screen")

    monkeypatch.setattr(guide, "answer", answer)
    assert guide.run("Start eppadi open pannuvadhu", "ta") == Outcome(True, "guide_done")
    assert answers[0][:2] == ("ee button enu madutte", "kn")
    assert "Highlighted control: Start" in answers[0][2]
    assert [value for key, value in statuses if key == "language"] == ["kn", "ta"]
    assert guide.pending is None


def test_a_new_command_ends_the_guide_and_is_kept_for_the_assistant():
    guide, voice, _, _, _ = make_guide(FakePlanLLM(step_answer()), [("say", "Spotify pe gaana chalao")])
    assert guide.run("goal", "hi") == Outcome(True, "guide_interrupted")
    assert guide.pending == "Spotify pe gaana chalao" and voice.spoken == ["Neeche Start dabaiye."]


def test_no_progress_gets_one_reminder_then_a_kind_stop():
    guide, voice, _, _, _ = make_guide(FakePlanLLM(step_answer()), [])
    assert guide.run("goal", "hi") == Outcome(False, "guide_timeout")
    assert voice.spoken == ["Neeche Start dabaiye.", "Neeche Start dabaiye.", say("guide_timeout", "hi")]
    assert REMIND_S < 120


def test_a_predicted_step_missing_from_the_screen_asks_again():
    llm = FakePlanLLM(step_answer(nxt=[("Printers dabaiye.", "Printers")]), {"status": "cannot", "say": "Nahi."})
    guide, voice, _, _, _ = make_guide(llm, [("click", START.rect.center, "start")])
    assert guide.run("goal", "hi") == Outcome(False, "guide_cannot")
    assert 'could not find "Printers"' in llm.calls[1] and voice.spoken[-1] == "Nahi."


def test_the_screenshot_is_deleted_after_each_plan(tmp_path):
    shots = []

    class ShootingScreen(FakeScreen):
        def look(self, with_image):
            seen = super().look(with_image)
            if with_image:
                seen.image = tmp_path / f"shot{len(shots)}.png"
                seen.image.write_bytes(b"png")
                shots.append(seen.image)
            return seen

    screen_ = ShootingScreen(STATES)
    inputs, clock, pointer = FakeInputs(), Clock(), FakePointer()
    voice = ScriptedVoice([("say", "ruko")], screen_, inputs, clock)
    llm = FakePlanLLM(step_answer())
    guide = Guide(llm, voice, look=screen_.look, fingerprint=screen_.fingerprint, changed=screen_.changed,
                  pointer=pointer, inputs=lambda: inputs, clock=clock, sleep=clock.sleep)
    guide.run("goal", "hi")
    assert shots and not any(path.exists() for path in shots)
    guide = Guide(FakePlanLLM(RuntimeError("down")), voice, look=screen_.look, fingerprint=screen_.fingerprint,
                  changed=screen_.changed, pointer=pointer, inputs=lambda: inputs, clock=clock, sleep=clock.sleep)
    guide.run("goal", "hi")
    assert len(shots) == 2 and not shots[1].exists()  # also when System 2 fails


def open_answer(url):
    return {"status": "step", "say": "PAN ki official website khol rahi hoon.", "action": "open", "target": "",
            "url": url, "mark": 0, "x": 0, "y": 0, "next": [], "complete": False, "done_say": ""}


def test_only_official_sites_are_opened():
    assert plans.parse(open_answer("https://tinpan.proteantech.in/"), observation()).steps[0].url == \
        "https://tinpan.proteantech.in/"
    for url in ("https://pan-card-apply.in/", "http://uidai.gov.in/", "https://uidai.gov.in.evil.com/", ""):
        assert plans.parse(open_answer(url), observation()) is None


def test_a_pan_lesson_opens_the_site_walks_the_pages_and_hands_over_at_the_form():
    llm = FakePlanLLM(open_answer("https://tinpan.proteantech.in/"),
                      step_answer("Apply Now dabaiye.", mark=3, target="Settings"))
    guide, voice, pointer, _, _ = make_guide(llm, [("click", SETTINGS.rect.center, "settings")], state="start")
    opened = []
    guide.open_url = opened.append
    guide.form_ready = lambda: guide.look.__self__.state == "settings"  # the form page is the "settings" state
    assert guide.run("PAN card ke liye apply karna hai", "hi") == Outcome(True, "guide_form_ready")
    assert opened == ["https://tinpan.proteantech.in/"]
    assert voice.opened == [say("guide_start", "hi")]
    assert voice.spoken == ["PAN ki official website khol rahi hoon.", "Apply Now dabaiye.",
                            say("guide_to_form", "hi")]


def test_named_form_search_waits_for_a_fillable_form_after_opening_the_site():
    request = "HDFC ka account opening form bharna hai"
    llm = FakePlanLLM({"url": "https://www.hdfcbank.com/", "query": "", "say": "Opening HDFC."},
                      {"status": "form", "say": "Form is here."},
                      step_answer("Click the application link.", mark=3, target="Settings", complete=True),
                      {"status": "form", "say": "Form is here."})
    guide, voice, _, _, _ = make_guide(llm, [("click", SETTINGS.rect.center, "settings")], state="start")
    opened = []
    guide.open_url = opened.append
    guide.form_visible = lambda: guide.look.__self__.state == "settings"
    assert guide.run(request, "en", find_form=True) == Outcome(True, "guide_form_ready")
    assert opened == ["https://www.hdfcbank.com/"]
    assert llm.calls[0] == request and "No fillable form" in llm.calls[2]
    assert voice.spoken == ["Opening HDFC.", "Click the application link."]


def test_form_start_uses_search_when_the_suggested_site_is_untrusted():
    llm = FakePlanLLM({"url": "https://passportindia.gov.in.example.com/", "query": "passport renewal form",
                       "say": "Finding the form."})
    step = plans.form_start(llm, "passport ka renewal form bharna h", "hi").steps[0]
    assert step.url == "https://www.google.com/search?q=passport+renewal+form"
    assert plans.form_start_url(step.url)
    assert not plans.form_start_url("https://www.google.com/url?q=https://example.com")


def test_form_search_can_wait_for_manual_sign_in_without_sending_that_screen():
    llm = FakePlanLLM({"url": "https://epfindia.gov.in/", "query": "", "say": "Opening the portal."},
                      {"status": "form", "say": "Form is here."})
    guide, voice, _, _, _ = make_guide(llm, [("click", (30, 30), "settings")], state="start")
    guide.open_url = lambda _: None
    guide.private = lambda: guide.look.__self__.state == "start"
    guide.form_visible = lambda: guide.look.__self__.state == "settings"
    assert guide.run("EPFO ka form bharna h", "en", find_form=True).key == "guide_form_ready"
    assert len(llm.calls) == 2 and "Settings" not in llm.calls[1]
    assert voice.spoken[-1] == say("guide_private_step", "en")


def test_details_typed_during_a_lesson_stop_the_screenshots():
    # A non-form lesson stops when the user opens a populated form. It does not start form entry.
    llm = FakePlanLLM(step_answer(), step_answer())
    guide, voice, _, _, _ = make_guide(llm, [("click", (5, 5), "elsewhere")])
    guide.private = lambda: guide.look.__self__.state == "elsewhere"
    assert guide.run("goal", "hi") == Outcome(False, "guide_form") and len(llm.calls) == 1


def test_the_opening_line_is_said_while_the_first_plan_is_made():
    guide, voice, _, _, statuses = make_guide(FakePlanLLM(step_answer()), [("say", "ruko")])
    guide.run("goal", "hi")
    assert voice.opened == [say("guide_start", "hi")] and ("reply", say("guide_start", "hi")) in statuses


def test_system_2_failing_or_missing_stops_without_pointing():
    guide, voice, pointer, _, _ = make_guide(FakePlanLLM(RuntimeError("quota")), [])
    assert guide.run("goal", "hi") == Outcome(False, "guide_lost") and pointer.shown == []
    guide, voice, _, _, _ = make_guide(None, [])
    assert guide.run("goal", "hi") == Outcome(False, "guide_unavailable")
    assert voice.spoken == [say("guide_unavailable", "hi")]


# --- The pointer ---------------------------------------------------------------------------------------------

def test_the_ring_stays_on_the_monitor_for_a_target_at_its_edge():
    sprite = Sprite(START.rect, 1.5, MONITOR)
    frame = sprite.frame(0.0)
    assert frame.shape == (sprite.size[1], sprite.size[0], 4) and frame.dtype == np.uint8
    alpha = frame[..., 3]
    rows = np.nonzero(alpha.max(axis=1) > 200)[0] + sprite.window.top  # where the solid ring is drawn
    assert rows.max() < MONITOR.bottom and rows.min() < START.rect.top
    assert alpha[0, 0] == 0  # corners stay see-through
    assert (frame[..., :3] <= alpha[..., None]).all()  # premultiplied, as UpdateLayeredWindow needs


def test_a_screen_sized_target_gets_a_static_ring():
    sprite = Sprite(Rect(0, 0, 2560, 1300), 1.5)
    assert not sprite.animated and (sprite.frame(0.1) == sprite.frame(0.9)).all()
    small = Sprite(SETTINGS.rect, 1.0)
    assert small.animated and not (small.frame(0.1) == small.frame(0.9)).all()


def test_a_form_field_ring_hugs_the_box_and_never_crosses_its_label():
    box = Rect(300, 600, 900, 660)  # an input; its label sits about 10 px above it at 150 %
    label_bottom = box.top - 10 * 1.5
    for seconds in (0.0, 0.4, 0.8, 1.2):
        sprite = Sprite(box, 1.5, snug=True)
        alpha = sprite.frame(seconds)[..., 3]
        rows = np.nonzero(alpha.max(axis=1) > 25)[0] + sprite.window.top
        assert rows.min() > label_bottom and rows.max() < box.bottom + 10 * 1.5
    wide = Sprite(box, 1.5).frame(0.4)[..., 3]
    assert wide.shape[0] > sprite.frame(0.4).shape[0]  # a guide target still gets the wide ring


# --- The overlay -----------------------------------------------------------------------------------------------

def test_the_overlay_shows_the_guide_step():
    state = OverlayState()
    assert state.apply("state", "guiding", 0.0) and state.apply("guide", {"step": 2}, 0.0)
    label, colour, detail = header(state, 1.0)
    assert label == "Showing you" and detail.startswith("step 2")
    assert not state.apply("guide", {"step": True}, 0.0) or state.guide_step is None
    state.apply("state", "done", 2.0)
    assert state.guide_step is None
    assert header(OverlayState(state="guide_lost"), 0.0)[0] == "Lost the next step"
    assert header(OverlayState(state="form_not_found"), 0.0)[0] == "No form on screen"


def test_the_decision_row_says_what_happens_without_repeating_the_heard_goal():
    from bolos.overlay import Step

    label, value, context = describe_step(Step("show_how", {"goal": "wallpaper badlo"}))
    assert label == "Show how" and "wallpaper" not in value + context and value


# --- The assistant ---------------------------------------------------------------------------------------------

GUIDE_DONE = Outcome(True, "guide_done")


class FakeGuide:
    def __init__(self, outcome=GUIDE_DONE, pending=None):
        self.outcome, self.pending_next, self.pending = outcome, pending, None
        self.runs = []

    def run(self, goal, language):
        self.runs.append((goal, language))
        self.pending = self.pending_next
        return self.outcome


class Voice:
    def __init__(self):
        self.spoken = []

    def speak(self, text, language):
        self.spoken.append(text)

    def listen(self):
        return None


class Runner:
    def __init__(self):
        self.executed = []

    def execute(self, action):
        self.executed.append(action)
        return Outcome(True, "opened", {"app": "Spotify"})


class Parser:
    def parse(self, command):
        return Parse("open_app", True, {"app": "Spotify"})


class Forms:
    """The form assistant as the guide sees it: `personal` = details typed into a form on screen."""

    def __init__(self, personal, ready=False):
        self.personal, self.ready = personal, ready
        self.filled = []

    def has_form(self):
        raise AssertionError("any text box is 'a form': never the guide's gate")

    def shows_personal_details(self):
        return self.personal

    def ready_to_fill(self):
        return self.ready

    def fill(self, language):
        self.filled.append(language)
        return Outcome(True, "form_handled")


def assistant(guide, forms=None):
    voice, runner = Voice(), Runner()
    return Assistant(Parser(), None, runner, voice, forms=forms, guide=guide), voice, runner


def test_a_guide_request_runs_the_guide_and_nothing_else():
    guide = FakeGuide()
    bot, voice, runner = assistant(guide)
    outcome = bot.handle("WhatsApp pe photo kaise bhejte hain, dikhao", "hi-IN")
    assert outcome == Outcome(True, "guide_done") and runner.executed == []
    assert guide.runs == [("WhatsApp pe photo kaise bhejte hain, dikhao", "hi-IN")]
    assert voice.spoken == []  # the guide spoke for itself
    assert bot._last is None  # "galat tha" never redoes a lesson


@pytest.mark.parametrize("request_text,language", [
    ("calculator scientific mode ku eppadi maathuradhu", "ta"),
    ("calculator na scientific mode ge hege badalayisodu", "kn"),
    ("calculator lo scientific mode ki ela marchali", "te"),
    ("PDF eppadi upload pannuvadhu", "ta"),
])
def test_voice_lesson_uses_detected_language_before_the_general_parser(request_text, language):
    guide = FakeGuide()
    bot, _, runner = assistant(guide)
    assert bot.handle(request_text, None).key == "guide_done"
    assert guide.runs == [(request_text, language)] and not runner.executed


def test_a_command_said_during_a_guide_runs_after_it():
    guide = FakeGuide(Outcome(True, "guide_interrupted"), pending="Spotify kholo")
    bot, voice, runner = assistant(guide)
    outcome = bot.handle("Bluetooth on karna sikhao", "hi-IN")
    assert outcome.key == "opened" and runner.executed == [a.OpenApp(app="Spotify")] and guide.pending is None


def test_details_typed_into_a_form_go_to_the_form_assistant_instead():
    guide = FakeGuide()
    bot, voice, _ = assistant(guide, forms=Forms(True))
    assert bot.handle("is page pe kaise aage badhein, dikhao", "hi-IN").key == "guide_form"
    assert guide.runs == [] and voice.spoken == [say("guide_form", "hi-IN")]


def test_an_empty_text_box_on_screen_does_not_stop_a_lesson():
    # Seen live (8 Nov): a chat app's empty message box counted as "a form" and every lesson was refused.
    guide = FakeGuide()
    bot, _, _ = assistant(guide, forms=Forms(False))
    assert bot.handle("folder kaise banate hain, dikhao?", "hi-IN").key == "guide_done" and guide.runs


def test_a_lesson_that_reaches_its_form_hands_over_to_the_form_session():
    forms = Forms(False)
    guide = FakeGuide(Outcome(True, "guide_form_ready"))
    bot, _, _ = assistant(guide, forms=forms)
    assert bot.handle("PAN card ke liye apply karna hai", "hi-IN") == Outcome(True, "form_handled")
    assert forms.filled == ["hi-IN"]


def test_without_a_guide_it_says_so():
    bot, voice, _ = assistant(None)
    assert bot.handle("folder banana sikhao", "en-IN").key == "guide_unavailable"
    assert voice.spoken == [say("guide_unavailable", "en")]


def test_show_how_is_never_a_trained_action():
    assert "show_how" not in a.ACTION_DESCRIPTIONS and "show_how" in a.DIALOGUE_ACTIONS
    assert a.parse_action({"action": "show_how", "goal": "x"}) == a.ShowHow(goal="x")
    assert json.loads(a.ShowHow(goal="x").model_dump_json()) == {"action": "show_how", "goal": "x"}


# --- Rehearsal findings (8 Nov) ---------------------------------------------------------------------------------

def test_the_status_card_moves_away_from_the_ring():
    from bolos.overlay import place

    area, w, h, m = (0, 0, 2560, 1380), 600, 300, 24
    assert place(w, h, area, m, None) == (2560 - 600 - 24, 1380 - 300 - 24)  # bottom right
    banner_button = (2100, 1200, 2300, 1260)  # seen live: a cookie banner's buttons under the card
    x, y = place(w, h, area, m, banner_button)
    assert y + h <= banner_button[1] or x + w <= banner_button[0]
    state = OverlayState()
    assert state.apply("avoid", (1, 2, 3, 4), 0.0) and state.avoid == (1, 2, 3, 4)
    assert state.apply("avoid", None, 0.0) and state.avoid is None


def test_a_whole_page_is_never_a_target():
    page = Mark(1, "Protean PAN Services", 50030, Rect(0, 182, 2560, 1369))
    tab = Mark(2, "Protean PAN Services", 50019, Rect(1770, 0, 2043, 62))
    seen = observation(page, tab)
    assert ground(plans.Step("wait", "key", "Protean PAN Services", mark=1), seen) is None
    assert find("Protean PAN Services", observation(page)) is None
