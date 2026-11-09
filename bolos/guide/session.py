"""One guide, start to finish: look -> plan -> point and say -> wait for the user to do it -> next.

    "WhatsApp pe photo kaise bhejte hain, dikhao"
      look:  screenshot + numbered UI Automation marks (bolos.guide.screen)
      plan:  System 2 picks the next step and predicts the ones after it (bolos.guide.planner)
      show:  a ring around the target (bolos.guide.pointer), the instruction on the overlay and spoken
      wait:  the user's own click inside the ring finishes the step; their voice can say ho gaya / phir se /
             ruko; a click elsewhere that changes the screen means they went another way: look and plan again
      next:  the next predicted step is found again by its label with UI Automation (no System 2 call); only
             when it is not on screen is System 2 asked again

Bol OS never clicks or types here. The screenshot goes to System 2 (Codex or the configured API), like the
screen text of read_screen; a page with a form is left to the private form assistant instead.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import Callable

from bolos.executor import Outcome, uia
from bolos.executor.uia import match_label
from bolos.guide import intent, screen
from bolos.guide import planner as plans
from bolos.guide.intent import control
from bolos.responses import say

log = logging.getLogger(__name__)

MAX_STEPS = 20  # steps shown in one guide
MAX_PLANS = 6  # System 2 calls in one guide
REMIND_S = 25.0  # say the step again after this long without progress
GIVE_UP_S = 120.0  # then stop
POLL_S = 0.15  # how often the wait loop checks voice, clicks and the screen
SETTLE_MIN_S, SETTLE_MAX_S = 0.35, 2.5  # after a click, wait for the screen to stop changing
SETTLED = 0.004  # fingerprint change between two looks that counts as still
CHANGED = 0.02  # a click elsewhere that changed this much of the screen took the user somewhere new
SWITCHED = 0.3  # without any click: the window changed under the pointer (alt-tab, a shortcut)
TYPING_IDLE_S = 1.2  # after typing stops this long, check whether the next step's target appeared
APPEAR_S, APPEAR_POLL_S = 4.0, 0.6  # how long a predicted step's target may take to appear
CLICK_MARGIN = 6  # physical pixels around a target that still count as hitting it
OPEN_WAIT_S, PAGE_SETTLE_S = 6.0, 5.0  # an opened site: time to appear, then to finish drawing
PAGE_LOAD_S, PAGE_MARKS = 12.0, 8  # and to list its links and buttons (at least this many)
POINT_RADIUS_IMAGE = 22  # a target given as a point: this many screenshot pixels around it
CLICK_BUTTONS = {"click": "left", "right_click": "right"}
DRIVE_CONTROLS = {screen.BUTTON, screen.HYPERLINK, screen.LISTITEM, screen.MENUITEM, screen.TREEITEM,
                  screen.TABITEM, screen.CHECKBOX, screen.RADIOBUTTON}
CONTEXT_TTL_S = 180

VK_LBUTTON, VK_RBUTTON, VK_RETURN, VK_TAB, VK_ESCAPE = 0x01, 0x02, 0x0D, 0x09, 0x1B
_TYPING_KEYS = [0x08, 0x20] + list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0xBA, 0xC1))
_MOUSE_EVENTS = {0x0202: "left", 0x0205: "right", 0x020A: "scroll", 0x020E: "scroll"}
_MOUSE_PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


@dataclass(frozen=True)
class Click:
    x: int
    y: int
    button: str  # "left", "right" or "scroll"
    at: float


class InputWatch:
    """Observe button releases and wheel events even when a click is shorter than one polling interval."""

    PERIOD_S = 0.015

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.clicks: deque[Click] = deque(maxlen=128)
        self.keys: deque[int] = deque(maxlen=32)
        self.typed_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> InputWatch:
        self._thread = threading.Thread(target=self._run, daemon=True, name="bolos-guide-input")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)

    def drain(self) -> tuple[list[Click], list[int]]:
        clicks, keys = [], []
        while self.clicks:
            clicks.append(self.clicks.popleft())
        while self.keys:
            keys.append(self.keys.popleft())
        return clicks, keys

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _MOUSE_PROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]

        @_MOUSE_PROC
        def on_mouse(code, message, data):
            if code >= 0 and message in _MOUSE_EVENTS:
                point = wintypes.POINT.from_address(data)  # first member of MSLLHOOKSTRUCT
                self.clicks.append(Click(point.x, point.y, _MOUSE_EVENTS[message], self.clock()))
            return user32.CallNextHookEx(None, code, message, data)

        timer = user32.SetTimer(None, 0, round(self.PERIOD_S * 1000), None)
        hook = user32.SetWindowsHookExW(14, on_mouse, None, 0) if timer else None  # WH_MOUSE_LL, read-only
        state = user32.GetAsyncKeyState
        watched = [VK_RETURN, VK_TAB, VK_ESCAPE, *_TYPING_KEYS]
        if not hook:
            watched += [VK_LBUTTON, VK_RBUTTON]
        down = {vk: bool(state(vk) & 0x8000) for vk in watched}
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                if timer:
                    if user32.GetMessageW(ctypes.byref(message), None, 0, 0) <= 0:
                        break
                    if message.message != 0x0113:  # WM_TIMER; GetMessage services the mouse hook continuously
                        continue
                elif self._stop.wait(self.PERIOD_S):
                    break
                for vk in watched:
                    pressed = bool(state(vk) & 0x8000)
                    if pressed != down[vk]:
                        if vk in (VK_LBUTTON, VK_RBUTTON) and not pressed:
                            x, y = screen.cursor_position()
                            self.clicks.append(Click(x, y, "left" if vk == VK_LBUTTON else "right", self.clock()))
                        elif pressed and vk in (VK_RETURN, VK_TAB, VK_ESCAPE):
                            self.keys.append(vk)
                        elif pressed:
                            self.typed_at = self.clock()
                    down[vk] = pressed
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)
            if timer:
                user32.KillTimer(None, timer)


@dataclass
class Target:
    """Where a step's ring is, in physical pixels."""

    rect: screen.Rect
    name: str = ""

    def hit(self, click: Click) -> bool:
        return self.rect.contains(click.x, click.y, CLICK_MARGIN)


def ground(step: plans.Step, observation: screen.Observation, previous: Target | None = None) -> Target | None:
    """Where `step` is on this observation: its mark or point when the plan was made from it, else its label
    found among the marks (exactly, or nearly: "Bluetooth & devices" for "Bluetooth and devices"). A typing
    step with no box of that name points at the box the keyboard is in: the one the user just clicked."""
    if step.mark is not None and (mark := observation.mark(step.mark)):
        if not mark.enabled or (step.action == "type" and not mark.typing):
            return find(step.target, observation, step.action)
        if step.action != "type" and _covers_screen(mark.rect, observation.monitor):
            return None  # a whole page or window is not something to click (seen live: "wait for the page")
        return Target(mark.rect, mark.name)
    if step.point is not None:
        x, y = observation.to_screen(*step.point)
        r = round(POINT_RADIUS_IMAGE / observation.scale)
        return Target(screen.Rect(x - r, y - r, x + r, y + r), step.target)
    near = previous.rect if previous else None
    if step.action == "type":
        if found := find(step.target, observation, "type", near):
            return found
        if observation.focused is not None and observation.focused.enabled:
            return Target(observation.focused.rect, observation.focused.name)
        return None
    return find(step.target, observation, step.action, near)


def _covers_screen(rect: screen.Rect, monitor: screen.Rect) -> bool:
    return rect.width * rect.height > 0.4 * monitor.width * monitor.height


def _label(text: str) -> str:
    from bolos.executor.uia import _label as normalise

    return normalise(text.replace("&", " and "))


def find(label: str, observation: screen.Observation, action: str | None = None,
         near: screen.Rect | None = None) -> Target | None:
    """The mark with this label. Several can share it (the taskbar's "Search" and a window's search box): a
    text box for a typing step, then the user's window over the taskbar, then the one nearest the previous
    ring, then reading order. A typing step also accepts a box whose name starts with the label ("Search box,
    Find a setting" for "Search"); clicks never take a partial name ("Delete" is not "Delete all")."""
    if not label.strip():
        return None
    key = _label(label)
    marks = [m for m in observation.marks if m.name and m.enabled
             and (action == "type" or not _covers_screen(m.rect, observation.monitor))]
    if action == "type":
        boxes = [m for m in marks if m.typing]
        by_label = _by_label(boxes)
        hit = match_label(key, list(by_label))
        candidates = by_label[hit] if hit else [m for m in boxes if _label(m.name).startswith(key + " ")]
        if not candidates:
            return None
    else:
        by_label = _by_label(marks)
        hit = match_label(key, list(by_label))
        # Some apps append their window title to visible names, e.g. "Scientific Calculator" in Calculator.
        app_name = _label(observation.window_title)
        if hit is None and app_name and f"{key} {app_name}" in by_label:
            hit = f"{key} {app_name}"
        if hit is None:
            return None
        candidates = by_label[hit]

    def rank(mark: screen.Mark):
        distance = 0.0
        if near is not None:
            (x, y), (nx, ny) = mark.rect.center, near.center
            distance = ((x - nx) ** 2 + (y - ny) ** 2) ** 0.5
        return mark.source == "taskbar", distance, mark.id

    best = min(candidates, key=rank)
    return Target(best.rect, best.name)


def _by_label(marks: list[screen.Mark]) -> dict[str, list[screen.Mark]]:
    groups: dict[str, list[screen.Mark]] = {}
    for mark in marks:
        groups.setdefault(_label(mark.name), []).append(mark)
    return groups


@dataclass
class Waited:
    """How a step's wait ended."""

    kind: str  # "done", "skip" (the step after it is done too), "changed", "stop", "timeout", "command"
    text: str = ""  # for "command": what the user said


class Guide:
    """Runs guides. `look`, `fingerprint`, `pointer` and `inputs` are the screen side, replaceable in tests."""

    def __init__(self, llm, voice, status: Callable[[str, object], None] = lambda *_: None, *,
                 look: Callable[[bool], screen.Observation] | None = None,
                 fingerprint: Callable[[screen.Rect], object] | None = None,
                 changed: Callable[[object, object], float] = screen.changed,
                 pointer=None, inputs: Callable[[], InputWatch] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 open_url: Callable[[str], None] | None = None, private: Callable[[], bool] | None = None,
                 form_ready: Callable[[], bool] | None = None,
                 form_visible: Callable[[], bool] | None = None,
                 answer_look: Callable[[], screen.Observation] | None = None, buddy=None):
        self.llm = llm
        self.buddy = buddy  # bolos.buddy.Buddy: flies to each target and names it
        self.open_url = open_url  # opens an official site in the browser (the executor's own way)
        self.private = private  # True when the screen shows a form with details typed in: no screenshots
        self.form_ready = form_ready  # True when the form to fill is on screen: the form session takes over
        self.form_visible = form_visible  # also detects short forms once the planner identifies the requested page
        self.voice = voice
        self.status = status
        self.look = look or (lambda with_image: screen.observe(with_image=with_image))
        self.answer_look = answer_look or (lambda: screen.observe(foreground_only=True))
        self.fingerprint = fingerprint or screen.fingerprint
        self.changed = changed
        self.pointer_factory = (lambda: pointer) if pointer is not None else None
        self.inputs_factory = inputs or (lambda: InputWatch(clock).start())
        self.clock = clock
        self.sleep = sleep
        self.pending: str | None = None  # a new command the user gave mid-guide, for the assistant to run
        self._answer_pointer = None
        self._context: deque[tuple[str, str]] = deque(maxlen=4)
        self._context_window = None
        self._context_at = 0.0
        self._last_target = ""

    def _new_pointer(self):
        if self.pointer_factory is not None:
            return self.pointer_factory()
        from bolos.guide.pointer import Pointer

        return Pointer(self.buddy)

    def hide_answer(self) -> None:
        if self._answer_pointer is not None:
            self._answer_pointer.hide()

    def close(self) -> None:
        if self._answer_pointer is not None:
            self._answer_pointer.close()
            self._answer_pointer = None
        self._context.clear()
        self._last_target = ""

    def answer(self, question: str, language: str | None, text: str = "", *,
               expected_window: int | None = None) -> Outcome | None:
        """Answer about the current window and optionally point. None requests the local readback fallback."""
        self.hide_answer()
        if self.llm is None or not hasattr(self.llm, "ask_json"):
            return None
        before = screen.user_window() if expected_window is None else expected_window
        if self.private is not None and self.private():
            self._context.clear()
            self._last_target = ""
            return self._end("guide_form", language, ok=False)
        observation = None
        started = self.clock()
        self.status("state", "thinking")
        try:
            observation = self.answer_look()
            if observation.hwnd is not None and observation.hwnd != before:
                return self._end("focus_changed", language, ok=False)
            window = (observation.hwnd, observation.window_title)
            if self._context_window != window or self.clock() - self._context_at > CONTEXT_TTL_S:
                self._context.clear()
                self._last_target = ""
            if not observation.image and not observation.marks and not text:
                return None
            resolved = intent.bind_reference(question, self._last_target) if self._last_target else question
            answer_view = replace(observation, cursor=None) if resolved != question else observation
            reply = plans.answer(self.llm, resolved, language, answer_view, text, list(self._context))
        except Exception:
            log.warning("Could not answer the screen question")
            return None
        finally:
            self.status("thinking", self.clock() - started)
            if observation is not None and observation.image is not None:
                observation.image.unlink(missing_ok=True)
        if reply is None:
            return None
        if observation.hwnd is not None:
            if (screen.user_window() != observation.hwnd
                    or (observation.hwnd and screen.window_title(observation.hwnd) != observation.window_title)):
                return self._end("focus_changed", language, ok=False)
        target = (find(self._last_target, observation) if resolved != question else ground(reply, observation))
        if target is not None:
            if self._answer_pointer is None:
                self._answer_pointer = self._new_pointer()
            self._answer_pointer.show(target.rect, observation.monitor, label=target.name)
        self._context.append((question[:300], reply.say))
        if target is not None:
            self._last_target = target.name
        self._context_window = window
        self._context_at = self.clock()
        self.status("reply", reply.say)
        self.voice.speak(reply.say, language)
        if target is not None:
            self._answer_pointer.hide_after(5.0)
        return Outcome(True, "screen")

    # The whole guide

    def run(self, goal: str, language: str | None, *, find_form: bool = False) -> Outcome:
        self.pending = None
        self.hide_answer()
        if self.llm is None or not hasattr(self.llm, "ask_json"):
            return self._end("guide_unavailable", language, ok=False)
        pointer = self._new_pointer()
        inputs = self.inputs_factory()
        try:
            if find_form:
                if failure := self._open_form_start(goal, language):
                    return failure
            return self._guide(goal, language, pointer, inputs, find_form=find_form)
        finally:
            inputs.stop()
            pointer.close()
            self.status("avoid", None)

    def drive(self, goal: str, language: str | None, confirm: Callable[[str], bool]) -> Outcome:
        """Propose one verified foreground control at a time, then look again after the confirmed click."""
        self.pending = None
        self.hide_answer()
        if self.llm is None or not hasattr(self.llm, "ask_json"):
            return self._end("guide_unavailable", language, ok=False)
        pointer = self._new_pointer()
        done: list[str] = []
        try:
            for _ in range(MAX_PLANS):
                before = screen.user_window()
                if self.private is not None and self.private():
                    return self._end("guide_form", language, ok=False)
                seen, plan = self._plan(goal, language, done, None, pointer, drive=True, expected_window=before)
                if not seen.hwnd or seen.hwnd != before or screen.user_window() != seen.hwnd:
                    return self._end("focus_changed", language, ok=False)
                if plan is None:
                    return self._end("guide_lost", language, ok=False)
                if plan.status == "done":
                    return self._end("guide_done", language, ok=True, spoken=plan.say)
                if plan.status != "step":
                    return self._end("guide_cannot", language, ok=False, spoken=plan.say)
                step = plan.steps[0]
                mark = seen.mark(step.mark) if step.mark else None
                if (mark is None or not mark.enabled or not mark.name or mark.source != "window"
                        or mark.control_type not in DRIVE_CONTROLS or ground(step, seen) is None):
                    return self._end("guide_cannot", language, ok=False)
                pointer.show(mark.rect, seen.monitor, label=mark.name)
                self._avoid(mark.rect)
                self.status("reply", step.say)
                if not confirm(mark.name):
                    return self._end("guide_stopped", language, ok=True)
                pointer.hide()
                if self.private is not None and self.private():
                    return self._end("guide_form", language, ok=False)
                if screen.user_window() != seen.hwnd or screen.window_title(seen.hwnd) != seen.window_title:
                    return self._end("focus_changed", language, ok=False)
                self.status("state", "executing")
                try:
                    uia.click(mark.name, binding=(seen.hwnd, mark.rect))
                except uia.ControlNotFound:
                    return self._end("focus_changed", language, ok=False)
                done.append(f"Clicked {mark.name}.")
                self._settle(seen.monitor)
            return self._end("guide_lost", language, ok=False)
        finally:
            pointer.close()
            self.status("avoid", None)

    def _avoid(self, rect: screen.Rect) -> None:
        """Tell the status card where the ring is, so it moves to another corner instead of covering it."""
        self.status("avoid", (rect.left, rect.top, rect.right, rect.bottom))

    def _open_form_start(self, goal: str, language: str | None) -> Outcome | None:
        self.status("state", "thinking")
        try:
            start = plans.form_start(self.llm, goal, language)
        except Exception:
            log.warning("Could not locate the form's starting site")
            start = None
        if start is None:
            return self._end("guide_lost", language, ok=False)
        if start.status == "cannot":
            return self._end("guide_cannot", language, ok=False, spoken=start.say)
        if not self._open(start.steps[0], self.look(False), language, number=1, form_start=True):
            return self._end("guide_lost", language, ok=False)
        return None

    def _guide(self, goal: str, language: str | None, pointer, inputs: InputWatch, *, find_form=False) -> Outcome:
        done: list[str] = []
        note = None
        wants_form = find_form or intent.wants_form(goal)
        for attempt in range(MAX_PLANS):
            before = screen.user_window() if self.private is not None else None
            if not find_form and self._at_form(wants_form):
                return self._to_form(language)
            if self.private is not None and self.private():
                if not find_form:
                    return self._end("guide_form", language, ok=False)
                if self._at_form(wants_form):
                    return self._to_form(language, announce=False)  # sizeable form with protected controls
                observation = self.look(False)
                plan = plans.Plan("step", "", (plans.Step(say("guide_private_step", language), "manual"),))
            elif attempt == 0 and not find_form:
                observation, plan = self._first_plan(goal, language, pointer, before)
            else:
                observation, plan = self._plan(goal, language, done, note, pointer, expected_window=before,
                                               find_form=find_form)
            if observation.hwnd is not None and (screen.user_window() != observation.hwnd
                                                 or (before is not None and observation.hwnd != before)):
                note = "The active window changed while planning. Continue from the current window."
                continue
            if find_form and observation.hwnd and screen.window_title(observation.hwnd) != observation.window_title:
                note = "The page changed while planning. Continue from the current page."
                continue
            if plan is None:
                return self._end("guide_lost", language, ok=False)
            if plan.status == "form":
                if find_form and (self.form_visible is None or not self.form_visible()):
                    note = "No fillable form controls are present. Find the actual online form before handing off."
                    continue
                if find_form:
                    return self._to_form(language, announce=False)
                return self._to_form(language) if wants_form else self._end("guide_form", language, ok=False)
            if plan.status == "done":
                if find_form:
                    note = "The requested fillable form is still needed. Continue until its controls are visible."
                    continue
                return self._end("guide_done", language, ok=True, spoken=plan.say)
            if plan.status == "cannot":
                return self._end("guide_cannot", language, ok=False, spoken=plan.say)
            note = None
            steps = list(plan.steps)
            index = 0
            previous: Target | None = None
            while index < len(steps):
                if len(done) >= MAX_STEPS:
                    return self._end("guide_lost", language, ok=False)
                step = steps[index]
                if step.action == "open":
                    if not self._open(step, observation, language, number=len(done) + 1):
                        return self._end("guide_lost", language, ok=False)
                    done.append(step.say)
                    index += 1
                    continue
                if index > 0 and not find_form and self._at_form(wants_form):
                    return self._to_form(language)  # the page the last click opened is the form
                target = None
                if index > 0:  # a predicted step: find it on the screen as it is now
                    # An app opened by the last click can take a few seconds to show its window: keep
                    # looking for a moment before asking System 2 again.
                    waited_since = self.clock()
                    while True:
                        observation = self.look(False)
                        target = ground(step, observation, previous)
                        if target is not None or self.clock() - waited_since >= APPEAR_S:
                            break
                        self.sleep(APPEAR_POLL_S)
                else:
                    target = ground(step, observation, previous)
                if target is None and (step.action not in ("type", "key", "scroll", "manual")
                                       or (index > 0 and step.action == "type")):
                    note = f"I could not find \"{step.target}\" on the screen for the step: {step.say}"
                    break
                following = steps[index + 1] if index + 1 < len(steps) else None
                waited = self._show_and_wait(step, target, following, observation, language, pointer, inputs,
                                             number=len(done) + 1)
                pointer.hide()
                if waited.kind in ("done", "skip"):
                    done.append(step.say)
                    previous = target or previous
                    index += 1
                    if waited.kind == "skip" and following is not None:
                        done.append(following.say)
                        index += 1
                    continue
                if waited.kind == "changed":
                    note = "The person clicked somewhere else and the screen changed; continue from what it shows now."
                    break
                if waited.kind == "stop":
                    return self._end("guide_stopped", language, ok=True)
                if waited.kind == "timeout":
                    return self._end("guide_timeout", language, ok=False)
                self.pending = waited.text  # a new command: the assistant runs it after this guide ends
                pointer.hide()
                self.status("state", "done")
                return Outcome(True, "guide_interrupted")
            else:
                if plan.complete and not find_form:
                    return self._end("guide_done", language, ok=True, spoken=plan.done_say)
                note = None  # every predicted step is done: look again to confirm or continue
        return self._end("guide_lost", language, ok=False)

    def _first_plan(self, goal, language, pointer, expected_window) -> tuple[screen.Observation, plans.Plan | None]:
        """The first plan, worked out while Bol OS already says it will show the way: the reply starts at
        once instead of after the ~6 s System 2 takes."""
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self._plan(goal, language, [], None, pointer,
                                                                          expected_window=expected_window)),
                                  daemon=True, name="bolos-guide-plan")
        worker.start()
        self.status("reply", say("guide_start", language))
        self.voice.speak(say("guide_start", language), language)
        self.status("state", "thinking")
        worker.join()
        return result[0] if result else (screen.Observation(screen.Rect(0, 0, 1, 1), 1.0, ""), None)

    def _at_form(self, wants_form: bool) -> bool:
        """Whether an application lesson has reached its form."""
        try:
            return wants_form and self.form_ready is not None and self.form_ready()
        except Exception:
            log.warning("Could not check the page for a form", exc_info=True)
            return False

    def _to_form(self, language: str | None, *, announce=True) -> Outcome:
        if announce:
            text = say("guide_to_form", language)
            self.status("state", "guiding")
            self.status("reply", text)
            self.voice.speak(text, language)
        return Outcome(True, "guide_form_ready")

    def _open(self, step: plans.Step, observation: screen.Observation, language: str | None, number: int,
              *, form_start=False) -> bool:
        """Open an official site (planner.official was checked when the plan was read; checked again here) and
        wait for the browser to show it."""
        allowed = plans.form_start_url if form_start else plans.official
        if self.open_url is None or not allowed(step.url):
            return False
        self.status("state", "guiding")
        self.status("guide", {"step": number})
        self.status("reply", step.say)
        area = observation.monitor
        before = self.fingerprint(area)
        self.voice.speak(step.say, language)
        try:
            self.open_url(step.url)
        except Exception:
            log.warning("Could not open %s", step.url, exc_info=True)
            return False
        started = self.clock()
        while self.clock() - started < OPEN_WAIT_S:  # the browser comes to the front and starts loading
            self.sleep(0.25)
            if self.changed(before, self.fingerprint(area)) >= SWITCHED:
                break
        # Then the page itself: a blank, still-loading page also looks "settled" (seen live: the next look came
        # too early and System 2 could only say "wait for the page"). Loaded means the browser lists the page's
        # links and buttons and that list has stopped growing.
        counts: list[int] = []
        while self.clock() - started < PAGE_LOAD_S:
            try:
                counts.append(sum(m.source == "window" for m in self.look(False).marks))
            except Exception:
                counts.append(0)
            if len(counts) >= 2 and counts[-1] >= PAGE_MARKS and counts[-1] == counts[-2]:
                break
            self.sleep(0.7)
        self._settle(area, longest=PAGE_SETTLE_S)
        return True

    def _plan(self, goal, language, done, note, pointer, *, drive=False,
              expected_window=None, find_form=False) -> tuple[screen.Observation, plans.Plan | None]:
        pointer.hide()  # never in its own screenshot
        self.status("state", "thinking")
        started = self.clock()
        observation = None
        try:
            observation = self.answer_look() if drive else self.look(True)
            if observation.hwnd is not None and expected_window is not None and observation.hwnd != expected_window:
                return observation, None
            plan = plans.plan(self.llm, goal, language, observation, done, note, drive=drive, find_form=find_form)
        except Exception:  # quota, network, timeout, a window closing: stop rather than point at a guess
            log.warning("Could not plan the guide's next step", exc_info=True)
            return screen.Observation(screen.Rect(0, 0, 1, 1), 1.0, ""), None
        finally:
            self.status("thinking", self.clock() - started)
            # The screenshot can show anything on the user's screen: it exists only for this one call.
            if observation is not None and observation.image is not None:
                observation.image.unlink(missing_ok=True)
                observation.image = None
        log.info("Guide plan: %s", plan.status if plan else "empty")
        return observation, plan

    # One step

    def _show_and_wait(self, step: plans.Step, target: Target | None, following: plans.Step | None,
                       observation: screen.Observation, language: str | None, pointer, inputs: InputWatch,
                       number: int) -> Waited:
        if target is not None:
            pointer.show(target.rect, observation.monitor, label=target.name)
            self._avoid(target.rect)
        self.status("state", "guiding")
        self.status("guide", {"step": number})
        self.status("reply", step.say)
        inputs.drain()  # only what the user does from now on counts (including while the step is spoken)
        typed_before = inputs.typed_at
        area = observation.monitor
        base = self.fingerprint(area)  # before speaking: a change made while it is spoken still counts
        self.voice.speak(step.say, language)
        started = last_check = self.clock()
        reminded = False
        # Where the step after this one is, found before any click: clicking it usually makes it disappear
        # (a Start menu entry closes the menu), so it cannot be looked up after the screen has settled.
        typing = step.action in ("type", "key")

        def find_following(seen: screen.Observation) -> Target | None:
            if following is None:
                return None
            return find(following.target, seen, following.action, target.rect if target else None)

        next_target = find_following(observation)
        previous_click = None
        double_click_seconds = ctypes.windll.user32.GetDoubleClickTime() / 1000 if step.action == "double_click" else 0
        while True:
            heard = self._poll()
            if heard is not None:
                if question_language := intent.screen_language(heard):
                    pointer.hide()
                    context = f"Current lesson instruction: {step.say}"
                    if target is not None and target.name:
                        context += f"\nHighlighted control: {target.name}"
                    self.status("language", question_language)
                    result = self.answer(heard, question_language, context)
                    self.hide_answer()
                    self.status("language", language)
                    if result is not None and not result.ok:
                        return Waited("changed")
                    if self.changed(base, self.fingerprint(area)) >= CHANGED:
                        return Waited("changed")
                    if target is not None:
                        pointer.show(target.rect, area, label=target.name)
                    self.status("reply", step.say)
                    self.status("state", "guiding")
                    inputs.drain()
                    started = last_check = self.clock()
                    continue
                kind = control(heard)
                if kind == "stop":
                    return Waited("stop")
                if kind == "next":
                    return Waited("done")
                if kind == "repeat":
                    self._repeat(step, language)
                    started = self.clock()
                    continue
                return Waited("command", heard)
            clicks, keys = inputs.drain()
            for click in clicks:
                if click.button == "scroll":
                    if self.changed(base, self._settle(area)) >= SETTLED:
                        return Waited("done" if step.action == "scroll" else "changed")
                    continue
                if target is not None and target.hit(click):
                    if typing:
                        continue  # clicking into the box is how typing starts, not the end of the step
                    if step.action == "double_click" and click.button == "left":
                        if previous_click is not None and click.at - previous_click.at <= double_click_seconds:
                            self._settle(area)
                            return Waited("done")
                        previous_click = click
                        continue
                    if CLICK_BUTTONS.get(step.action) == click.button:
                        self._settle(area)
                        return Waited("done")
                if following is not None and next_target is None:
                    next_target = find_following(self.look(False))  # at once, before it can vanish
                if (next_target is not None and next_target.hit(click)
                        and CLICK_BUTTONS.get(following.action) == click.button):
                    self._settle(area)
                    return Waited("skip")
                if self.changed(base, self._settle(area)) >= CHANGED:
                    return Waited("changed")
            if (step.action == "type" and VK_RETURN in keys) or (step.action == "key" and keys):
                self._settle(area)
                return Waited("done")
            now = self.clock()
            if now - last_check >= 1.0:
                last_check = now
                if observation.hwnd is not None and screen.user_window() != observation.hwnd:
                    return Waited("changed")
                current = self.fingerprint(area)
                if (typing and inputs.typed_at > typed_before and now - inputs.typed_at >= TYPING_IDLE_S
                        and following is not None and (next_target := find_following(self.look(False)))):
                    return Waited("done")  # they typed, and what comes next is now on screen
                threshold = CHANGED if step.action == "scroll" else SWITCHED
                if self.changed(base, current) >= threshold:
                    after = self._settle(area)
                    if self.changed(base, after) >= threshold:
                        if step.action == "scroll" or (typing and inputs.typed_at > typed_before):
                            return Waited("done")
                        moved = find(step.target, self.look(False), step.action) if step.target else None
                        if moved is None:
                            return Waited("changed")
                        if target is None or not moved.rect.near(target.rect):
                            target = moved  # the same thing moved (a scroll, a window drag): follow it
                            pointer.show(target.rect, area, label=target.name)
                            self._avoid(target.rect)
                        base = after
            if now - started >= GIVE_UP_S:
                return Waited("timeout")
            if not reminded and now - started >= REMIND_S:
                reminded = True
                self._repeat(step, language)

    def _repeat(self, step: plans.Step, language: str | None) -> None:
        self.status("state", "guiding")
        self.status("reply", step.say)
        self.voice.speak(step.say, language)

    def _poll(self) -> str | None:
        """What the user said since the last check, without waiting for them to speak."""
        poll = getattr(self.voice, "poll", None)
        if poll is None:
            self.sleep(POLL_S)
            return None
        heard = poll(POLL_S)
        if heard:
            self.status("state", "guiding")
        return heard[0] if heard and heard[0] else None

    def _settle(self, area: screen.Rect, longest: float = None):
        """Wait until the screen stops changing after a click, and return its fingerprint then."""
        started = self.clock()
        previous = self.fingerprint(area)
        still = 0
        while self.clock() - started < (longest or SETTLE_MAX_S):
            self.sleep(0.12)
            current = self.fingerprint(area)
            still = still + 1 if self.changed(previous, current) < SETTLED else 0
            previous = current
            if still >= 2 and self.clock() - started >= SETTLE_MIN_S:
                break
        return previous

    def _end(self, key: str, language: str | None, ok: bool, spoken: str = "") -> Outcome:
        text = spoken or say(key, language)
        self.status("state", "done" if ok else key)
        self.status("reply", text)
        self.voice.speak(text, language)
        return Outcome(ok, key)
