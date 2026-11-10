"""A small always-on-top window that shows what Bol OS heard, what it decided and what it says back.

It renders the status events of the pipeline and listener (see `Overlay.set`). Three layers, so most of it
is testable without a window:

- `OverlayState.apply` turns status events into display state (pure, no Tk).
- `compose` lays that state out into a `Scene` of drawing ops, measuring text through a `Metrics` object
  (pure; tests pass fake metrics).
- `Overlay` owns the Tk window: it drains a queue filled by worker threads, draws the scene and keeps the
  window out of the user's way.

The window must never take keyboard focus, because Bol OS types and clicks into whatever window is in the
foreground. Tk activates a top-level window when it first creates it, so the first map happens under
`LockSetForegroundWindow`, and the window is a no-activate, click-through tool window (WS_EX_NOACTIVATE |
WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW): clicks fall through to the app underneath.

    uv run python -m bolos.overlay demo                     every state and language, for a demo video
    uv run python -m bolos.overlay demo --shots docs/overlay  the same, saving a PNG of each scene
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import struct
import sys
import threading
import time
import unicodedata
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bolos.hotkey import format_hotkey

# Palette: a calm dark card; colour only where it means something (state, source).
BG = "#17191e"
SURFACE = "#262931"
HAIRLINE = "#2b2f37"
FG = "#eef0f3"
SOFT = "#c3c8d0"  # reply text
MUTED = "#8f96a1"

STATE_COLOURS = {
    "ready": "#8f96a1",
    "listening": "#4cc2f5",
    "hearing": "#37d1a0",
    "transcribing": "#a48bfa",
    "understanding": "#a48bfa",
    "thinking": "#f2b64a",
    "confirming": "#ff9d5c",
    "guiding": "#ff8a1f",  # the colour of the guide's on-screen ring
    "filling": "#ff8a1f",  # the same ring, around the form field being asked
    "executing": "#5ea8ff",
    "done": "#4bd583",
    "error": "#f47272",
    "warning": "#f2b64a",
}
STATE_LABELS = {
    "ready": "Ready",
    "listening": "Listening",
    "hearing": "Hearing you",
    "transcribing": "Transcribing",
    "understanding": "Understanding",
    "thinking": "Thinking",
    "confirming": "Confirm",
    "guiding": "Showing you",
    "filling": "Filling the form",
    "executing": "Working on it",
    "done": "Done",
    "error": "Something went wrong",
}
# Failure outcomes the pipeline may report as a state instead of "error": (wording, colour key).
OUTCOME_STATES = {
    "not_understood": ("Didn't catch that", "warning"),
    "microphone_unavailable": ("Microphone unavailable", "error"),
    "shortcut_unavailable": ("Voice shortcut unavailable", "error"),
    "speech_unavailable": ("Can't recognise speech right now", "warning"),
    "not_sent": ("Not sent", "ready"),
    "contact_not_found": ("Contact not found", "error"),
    "app_not_found": ("App not found", "error"),
    "file_not_found": ("File not found", "warning"),
    "download_not_found": ("No file in Downloads", "warning"),
    "download_pending": ("Download in progress", "warning"),
    "file_ambiguous": ("More than one file matches", "warning"),
    "file_name_only": ("Say the file name", "warning"),
    "file_unavailable": ("Couldn't get to the file", "warning"),
    "control_not_found": ("Not found on screen", "error"),
    "screen_unavailable": ("Can't read the screen", "warning"),
    "focus_changed": ("Window changed, did nothing", "warning"),
    "nothing_playing": ("Nothing is playing", "warning"),
    "form_not_found": ("No form on screen", "warning"),
    "form_not_finished": ("Form not finished", "warning"),
    "guide_lost": ("Lost the next step", "warning"),
    "guide_pan_not_loaded": ("PAN form not visible yet", "warning"),
    "guide_cannot": ("Can't show that", "warning"),
    "guide_timeout": ("Stopped showing", "ready"),
    "guide_unavailable": ("Can't show right now", "error"),
    "guide_form": ("Use the form assistant", "warning"),
    "guide_switch_app": ("Switch to the app you want help with", "warning"),
    "guide_settings_open_failed": ("Open Windows Settings", "warning"),
}
ATTENTION_STATES = {"microphone_unavailable", "shortcut_unavailable", "speech_unavailable"}
ANIMATED_STATES = {"listening", "hearing", "transcribing", "understanding", "thinking", "executing", "guiding",
                   "filling"}
METER_STATES = {"listening", "hearing"}  # the microphone is open: a live level meter
# After these a new listen is a new command: the previous one is cleared. (A listen after "confirming" is
# the answer to the question on screen, so that one keeps it.)
TERMINAL_STATES = {"ready", "done", "error"} | set(OUTCOME_STATES)
SETTLE_S = 8.0  # a finished command stays on screen this long, then the card goes back to its idle hint

SOURCE_COLOURS = {
    "system1": "#37d1a0",
    "habit": "#a48bfa",
    "system2": "#f2b64a",
    "system2_fields": "#f2b64a",
    "user": "#f47fbf",
}

LANGUAGES = {  # code: (English name, autonym)
    "hi": ("Hindi", "हिन्दी"),
    "en": ("English", "English"),
    "kn": ("Kannada", "ಕನ್ನಡ"),
    "ta": ("Tamil", "தமிழ்"),
    "te": ("Telugu", "తెలుగు"),
    "mr": ("Marathi", "मराठी"),
    "ml": ("Malayalam", "മലയാളം"),
    "bn": ("Bengali", "বাংলা"),
    "gu": ("Gujarati", "ગુજરાતી"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ"),
}

CONTROL_WORDS = {
    "pause": "Pause",
    "resume": "Resume",
    "next": "Next track",
    "previous": "Previous track",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "mute": "Mute",
}

ELLIPSIS = "…"
SEP = " · "
LEVEL_BARS = 44
PULSE_STEPS = 12


# ---------------------------------------------------------------------------------------------------------
# Display state


@dataclass(frozen=True)
class Step:
    action: str
    fields: dict


@dataclass(frozen=True)
class Decision:
    source: str | None
    ms: float | None
    steps: tuple[Step, ...]


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def normalize_decision(value) -> Decision | None:
    """The ("decision", {...}) payload as a Decision, or None when it has no usable step. Lenient about
    shape (a bare {"action": ..., ...} is one step whose other keys are its fields) since it crosses a
    thread and module boundary and a bad payload must not break the window."""
    if not isinstance(value, dict):
        return None
    raw_steps = value.get("steps")
    if raw_steps is None and "action" in value:
        raw_steps = [value]
    steps = []
    for raw in raw_steps if isinstance(raw_steps, (list, tuple)) else ():
        if not isinstance(raw, dict) or not isinstance(raw.get("action"), str) or not raw["action"]:
            continue
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            fields = {k: v for k, v in raw.items() if k not in ("action", "fields")}
        steps.append(Step(raw["action"], dict(fields)))
    if not steps:
        return None
    source = value.get("source") if isinstance(value.get("source"), str) else None
    return Decision(source, _number(value.get("ms")), tuple(steps))


@dataclass
class OverlayState:
    """Everything the window shows, updated by status events. `apply` returns whether anything changed."""

    hotkey: str = "Ctrl+Space"
    state: str = "ready"
    state_since: float = 0.0
    trigger: str | None = None
    levels: deque = field(default_factory=lambda: deque([0.0] * LEVEL_BARS, maxlen=LEVEL_BARS))
    heard: str = ""
    language: str | None = None
    decision: Decision | None = None
    reply: str = ""
    thinking: float | None = None  # last System 2 time reported, seconds
    thinking_at: float = 0.0  # when it was reported
    guide_step: int | None = None  # while showing how: the step on screen now
    avoid: tuple[int, int, int, int] | None = None  # the guide's ring (screen pixels): keep the card off it
    corner: str = "bottom-right"  # where the card sits (CORNERS), chosen on the dashboard
    api_model: str | None = None

    def apply(self, kind: str, value, now: float) -> bool:
        if kind == "hotkey" and isinstance(value, str) and value:
            changed, self.hotkey = value != self.hotkey, value
            return changed
        if kind == "corner" and value in CORNERS:
            changed, self.corner = value != self.corner, value
            return changed
        if kind == "state" and isinstance(value, str) and value:
            if value == self.state:
                return False
            if value in METER_STATES and self.state in TERMINAL_STATES:
                self.heard, self.language, self.decision, self.reply = "", None, None, ""
            if value in METER_STATES and self.state not in METER_STATES:
                self.levels.extend([0.0] * LEVEL_BARS)
            if value == "thinking":
                self.thinking = None
            if value in TERMINAL_STATES:
                self.trigger = None  # each listen reports its own trigger
                self.guide_step = None
            self.state, self.state_since = value, now
            return True
        if kind == "level":
            level = _number(value)
            if level is None:
                return False
            self.levels.append(min(max(level, 0.0), 1.0))
            return self.state in METER_STATES
        if kind == "heard" and isinstance(value, str):
            if self.state in TERMINAL_STATES:
                self.language = None  # a typed command: no listening state started a new turn
            self.heard, self.decision, self.reply = value.strip(), None, ""
            return True
        if kind == "language":
            language = value.strip().lower() if isinstance(value, str) and value.strip() else None
            changed, self.language = language != self.language, language
            return changed
        if kind == "decision":
            decision = normalize_decision(value)
            if decision is None:
                return False
            self.decision = decision
            return True
        if kind == "action" and isinstance(value, str):  # the round-2 pipeline's event: an Action as JSON
            try:
                return self.apply("decision", {"steps": [json.loads(value)]}, now)
            except ValueError:
                return False
        if kind == "thinking":
            seconds = _number(value)
            if seconds is None:
                return False
            self.thinking, self.thinking_at = seconds, now
            return self.state == "thinking"
        if kind == "reply" and isinstance(value, str):
            self.reply = value.strip()
            return True
        if kind == "guide" and isinstance(value, dict):
            step = value.get("step")
            step = step if isinstance(step, int) and not isinstance(step, bool) and step > 0 else None
            changed, self.guide_step = step != self.guide_step, step
            return changed
        if kind == "avoid":
            box = None
            if isinstance(value, (list, tuple)) and len(value) == 4 and all(isinstance(v, int) for v in value):
                box = tuple(value)
            changed, self.avoid = box != self.avoid, box
            return changed
        if kind == "trigger" and isinstance(value, str):
            changed, self.trigger = value != self.trigger, value
            return changed
        return False  # unknown kinds are ignored, as the contract says

    def settle(self, now: float) -> bool:
        """Back to the idle card once a finished command has been on screen for SETTLE_S, so the last result
        (a failure or "form handled") never sits on the user's screen until the next command."""
        if (self.state == "ready" or self.state in ATTENTION_STATES or self.state not in TERMINAL_STATES
                or now - self.state_since < SETTLE_S):
            return False
        self.heard, self.language, self.decision, self.reply = "", None, None, ""
        self.trigger = self.guide_step = None
        self.state, self.state_since = "ready", now
        return True

    def thinking_seconds(self, now: float) -> float:
        """System 2's running time: the last reported value, carried forward smoothly between the ~10 per
        second reports (at most 0.3 s, so a stalled reporter shows a stalled clock), else the local clock."""
        if self.thinking is not None:
            return self.thinking + min(max(now - self.thinking_at, 0.0), 0.3)
        return max(now - self.state_since, 0.0)


# ---------------------------------------------------------------------------------------------------------
# Wording


def format_duration(ms: float) -> str:
    if ms < 10:
        return f"{ms:.1f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.1f} s"


def badge_text(source: str | None, ms: float | None, api_model: str | None = None) -> str | None:
    """The source badge: "System 1 · 1.2 ms", "Habit", "Codex · 8.9 s", "Codex fields · 7.4 s" (System 1
    chose the action, Codex filled its fields), "You taught me". Habits and taught commands are lookups, so
    their time says nothing and is left out."""
    if not source:
        return None
    timed = f"{SEP}{format_duration(ms)}" if ms is not None and ms >= 0 else ""
    if source == "system1":
        return "System 1" + timed
    if source in ("system2", "system2_fields"):
        model = "Gemini" if api_model and api_model.startswith("gemini-") else api_model or "Codex"
        return model + (" fields" if source == "system2_fields" else "") + timed
    return {"habit": "Habit", "user": "You taught me"}.get(source, source.replace("_", " ").capitalize())


def language_chip(code: str | None) -> str | None:
    if not code or code == "none":
        return None
    english, autonym = LANGUAGES.get(code, (code.upper(), code.upper()))
    return english if english == autonym else f"{autonym}{SEP}{english}"


def _text(fields: dict, name: str) -> str:
    value = fields.get(name)
    return str(value).strip() if value not in (None, "") else ""


def describe_step(step: Step) -> tuple[str, str, str]:
    """(label, value, context) for one step: ("Play", "Arijit Singh", "on YouTube")."""
    f, action = step.fields, step.action
    browser = _text(f, "browser")
    in_browser = f"in {browser}" if browser else ""
    if action == "open_app":
        return "Open app", _text(f, "app"), ""
    if action == "find_file":
        return ("Open file" if f.get("open") else "Find file"), _text(f, "query"), ""
    if action == "open_recent_download":
        return "Open file", "Newest file in Downloads", ""
    if action == "open_website":
        return "Open site", _text(f, "site"), in_browser
    if action == "web_search":
        site = _text(f, "site")
        return "Search", _text(f, "query"), " ".join(p for p in (f"on {site}" if site else "", in_browser) if p)
    if action == "play_media":
        app = _text(f, "app")
        return "Play", _text(f, "query") or "music", f"on {app}" if app else ""
    if action == "media_control":
        control = _text(f, "control")
        return "Media", CONTROL_WORDS.get(control, control.replace("_", " ").capitalize()), ""
    if action == "send_message":
        contact, text = _text(f, "contact"), _text(f, "text")
        if not text:  # "Raju ko WhatsApp karo": who, but not what yet
            return "Message", contact, ""
        return "Message", f"“{text}”", f"to {contact}" if contact else ""
    if action == "type_text":
        text = _text(f, "text")
        return "Type", f"“{text}”" if text else "", ""
    if action == "read_screen":
        return "Read", "the screen", ""
    if action == "drive_screen":
        return "Control", "the current app", "confirmed clicks"
    if action == "click":
        return "Click", _text(f, "target"), ""
    if action == "show_how":  # the goal is the heard sentence right above: say what happens instead
        return "Show how", "on your screen", "step by step"
    if action == "cancel":
        return "Cancel", "nothing to do", ""
    if action == "unknown":
        return "Unclear", "", ""
    values = ", ".join(_text(f, k) for k in f if _text(f, k))
    return action.replace("_", " ").capitalize(), values, ""


def header(st: OverlayState, now: float) -> tuple[str, str, str]:
    """(state wording, colour, detail) for the top row."""
    if st.state in STATE_LABELS:
        label, colour = STATE_LABELS[st.state], STATE_COLOURS[st.state]
    elif st.state in OUTCOME_STATES:
        label, key = OUTCOME_STATES[st.state]
        colour = STATE_COLOURS[key]
    else:  # an outcome key nobody told us about: it is a failure (successes are reported as "done")
        label, colour = st.state.replace("_", " ").capitalize(), STATE_COLOURS["error"]
    detail = ""
    if st.state == "thinking":  # System 2 is working out the command
        detail = f"{st.thinking_seconds(now):.1f} s{SEP}{st.api_model or 'asking System 2'}"
    elif st.state == "confirming":
        detail = "say yes or no"
    elif st.state == "guiding":
        detail = f"step {st.guide_step}{SEP}say “stop” to end" if st.guide_step else "say “stop” to end"
    elif st.state == "filling":
        detail = "say “skip” or “stop”"
    elif st.state in METER_STATES:
        detail = {
            "hotkey": f"holding {format_hotkey(st.hotkey)}",
            "tap": f"tapped {format_hotkey(st.hotkey)}",
            "wake": "heard “Hey Bol OS”",
            "followup": "for your answer",
        }.get(st.trigger or "", "")
    return label, colour, detail


# ---------------------------------------------------------------------------------------------------------
# Text fitting


def clusters(word: str) -> list[str]:
    """Split a word where a line may break inside it: never before a combining mark or a joiner, nor after
    a virama (so Indic conjuncts and vowel signs stay whole)."""
    out: list[str] = []
    for ch in word:
        joins = out and (unicodedata.category(ch).startswith("M") or ch in "‌‍"
                         or out[-1][-1] in "‌‍" or "VIRAMA" in unicodedata.name(out[-1][-1], ""))
        if joins:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _fit_prefix(word: str, width: int, measure) -> tuple[str, str]:
    """The longest prefix of `word` (in whole clusters, at least one) that fits, and the rest."""
    parts = clusters(word)
    head = parts[0]
    for i in range(1, len(parts)):
        candidate = head + parts[i]
        if measure(candidate) > width:
            return head, "".join(parts[i:])
        head = candidate
    return head, ""


def ellipsize(text: str, width: int, measure) -> str:
    """`text` with "…" appended, shortened by whole clusters until it fits."""
    parts = clusters(text.rstrip())
    while parts and measure("".join(parts).rstrip() + ELLIPSIS) > width:
        parts.pop()
    return "".join(parts).rstrip() + ELLIPSIS


def wrap(text: str, width: int, measure, max_lines: int) -> list[str]:
    """Greedy word wrap into at most `max_lines` lines no wider than `width`; a word too long for a line is
    broken between clusters, and text that doesn't fit ends in "…"."""
    lines: list[str] = []
    line = ""
    words = text.split()
    for i, word in enumerate(words):
        candidate = f"{line} {word}" if line else word
        if measure(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        while measure(word) > width:
            head, word = _fit_prefix(word, width, measure)
            lines.append(head)
            if len(lines) >= max_lines:
                break
        line = word
        if len(lines) >= max_lines:
            rest = " ".join(([word] if word else []) + words[i + 1:])
            if rest:
                lines[max_lines - 1:] = [ellipsize(lines[max_lines - 1], width, measure)]
            return lines[:max_lines]
    if line:
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------------------------------------
# Layout


class Metrics(Protocol):
    """Text measurement in device pixels, for each font role in FONTS."""

    icons: bool  # whether the icon font exists

    def measure(self, font: str, text: str) -> int: ...

    def ascent(self, font: str) -> int: ...

    def linespace(self, font: str) -> int: ...


# Font roles: (family, points). Content (what was heard, the decision's values, the reply, the language)
# is in Nirmala UI, which has every Indic script Bol OS speaks plus Segoe UI's Latin glyphs, so code-mixed
# text keeps one baseline and one line height. Interface wording is in Segoe UI.
FONTS = {
    "state": ("Segoe UI Semibold", 11),
    "detail": ("Segoe UI", 10),
    "chip": ("Nirmala UI", 9),
    "hint": ("Segoe UI", 10),
    "heard": ("Nirmala UI", 13),
    "badge": ("Segoe UI Semibold", 9),
    "label": ("Segoe UI", 9),
    "num": ("Segoe UI Semibold", 8),
    "value": ("Nirmala UI", 11),
    "reply": ("Nirmala UI", 11),
    "icon": ("Segoe Fluent Icons", 11),
}
ICON_SPEAKER = chr(0xE767)  # "Volume" in Segoe Fluent Icons and Segoe MDL2 Assets
WIDTH = 408  # logical pixels (96 dpi); everything is multiplied by the display scale


@dataclass(frozen=True)
class TextOp:
    x: int
    y: int  # top of the line box
    text: str
    font: str
    colour: str


@dataclass(frozen=True)
class ShapeOp:
    """A filled rounded rectangle (a circle when w == h == 2r), antialiased against BG."""

    x: int
    y: int
    w: int
    h: int
    r: int
    colour: str


@dataclass
class Scene:
    width: int
    height: int
    ops: list = field(default_factory=list)
    animating: bool = False


def blend(colour: str, other: str, t: float) -> str:
    """`colour` moved a fraction t towards `other`."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def _fn(m: Metrics, font: str):
    return lambda text: m.measure(font, text)


def compose(st: OverlayState, now: float, m: Metrics, scale: float = 1.0) -> Scene:
    """Lay the state out top to bottom: header, meter, heard words, decision, reply."""

    def px(v: float) -> int:
        return int(round(v * scale))

    width, pad = px(WIDTH), px(18)
    inner = width - 2 * pad
    ops: list = []
    scene = Scene(width, 0, ops, animating=st.state in ANIMATED_STATES)

    def text(x: int, top: int, s: str, font: str, colour: str) -> None:
        ops.append(TextOp(x, top, s, font, colour))

    def baseline_top(font: str, baseline: int) -> int:
        return baseline - m.ascent(font)

    # Header: status dot, state, detail ........ language chip
    label, colour, detail = header(st, now)
    chip = language_chip(st.language)
    chip_h = m.linespace("chip") + px(4)
    chip_w = m.measure("chip", chip) + 2 * px(9) if chip else 0
    y = px(14)
    row_h = max(m.linespace("state"), chip_h)
    mid = y + row_h // 2
    baseline = mid - m.linespace("state") // 2 + m.ascent("state")
    dot = px(9)
    pulse = 0.0
    if scene.animating:
        pulse = 0.5 - 0.5 * math.cos((now - st.state_since) * 2 * math.pi / 1.4)
        pulse = round(pulse * PULSE_STEPS) / PULSE_STEPS  # a few tones, so their images are made once
    ops.append(ShapeOp(pad, mid - dot // 2, dot, dot, dot // 2, blend(colour, BG, 0.45 * pulse)))
    x = pad + dot + px(9)
    label_room = width - pad - (chip_w + px(10) if chip else 0) - x
    if m.measure("state", label) > label_room:  # an outcome key we have no wording for can be long
        label = ellipsize(label, label_room, _fn(m, "state"))
    text(x, baseline_top("state", baseline), label, "state", colour)
    x += m.measure("state", label)
    room = width - pad - (chip_w + px(10) if chip else 0) - x - px(8)
    if detail and room > m.measure("detail", ELLIPSIS) * 3:
        detail = detail if m.measure("detail", detail) <= room else ellipsize(detail, room, _fn(m, "detail"))
        text(x + px(8), baseline_top("detail", baseline), detail, "detail", MUTED)
    if chip:
        cx = width - pad - chip_w
        ops.append(ShapeOp(cx, mid - chip_h // 2, chip_w, chip_h, chip_h // 2, SURFACE))
        text(cx + px(9), mid - m.linespace("chip") // 2, chip, "chip", SOFT)
    y += row_h

    content = bool(st.heard or st.decision or st.reply)
    if st.state == "ready" and not content:
        y += px(6)
        hint = f"Say “Hey Bol OS”, or press {format_hotkey(st.hotkey)}"
        for line in wrap(hint, inner, _fn(m, "hint"), 2):
            text(pad, y, line, "hint", MUTED)
            y += m.linespace("hint")

    # Microphone level, newest on the right: live while listening, frozen and faded while the words are
    # being transcribed (so the window doesn't shrink and grow again before they arrive).
    frozen = st.state == "transcribing" and not st.heard
    if st.state in METER_STATES or frozen:
        y += px(12)
        height, bar_w = px(30), max(px(3), 2)
        gap = (inner - bar_w * LEVEL_BARS) / (LEVEL_BARS - 1)
        bar = blend(colour, BG, 0.55) if frozen else colour
        for i, level in enumerate(st.levels):
            h = max(bar_w, round(height * min(level, 1.0) ** 0.6))
            bx = pad + round(i * (bar_w + gap))
            ops.append(ShapeOp(bx, y + (height - h) // 2, bar_w, h, bar_w // 2,
                               bar if level > 0.02 else blend(colour, BG, 0.7)))
        y += height

    if st.heard:
        y += px(12)
        for line in wrap(st.heard, inner, _fn(m, "heard"), 3):
            text(pad, y, line, "heard", FG)
            y += m.linespace("heard")

    if st.decision:
        y = _compose_decision(st.decision, ops, m, px, pad, inner, y + px(12), st.api_model)

    if st.reply:
        y += px(14)
        ops.append(ShapeOp(pad, y, inner, max(px(1), 1), 0, HAIRLINE))
        y += px(12)
        indent = 0
        if m.icons:
            indent = m.measure("icon", ICON_SPEAKER) + px(10)
            icon_top = y + (m.linespace("reply") - m.linespace("icon")) // 2
            text(pad, icon_top, ICON_SPEAKER, "icon", MUTED)
        for line in wrap(st.reply, inner - indent, _fn(m, "reply"), 3):
            text(pad + indent, y, line, "reply", SOFT)
            y += m.linespace("reply")

    scene.height = y + px(16)
    return scene


def _compose_decision(decision: Decision, ops: list, m: Metrics, px, pad: int, inner: int, y: int,
                      api_model: str | None) -> int:
    badge = badge_text(decision.source, decision.ms, api_model)
    if badge:
        colour = SOURCE_COLOURS.get(decision.source, MUTED)
        h = m.linespace("badge") + px(4)
        w = m.measure("badge", badge) + 2 * px(9) + px(11)
        ops.append(ShapeOp(pad, y, w, h, h // 2, blend(colour, BG, 0.84)))
        dot = px(6)
        ops.append(ShapeOp(pad + px(9), y + (h - dot) // 2, dot, dot, dot // 2, colour))
        ops.append(TextOp(pad + px(9) + px(11), y + px(2), badge, "badge", colour))
        y += h + px(10)

    numbered = len(decision.steps) > 1
    rows = [describe_step(step) for step in decision.steps]
    num_w = px(20) if numbered else 0
    label_x = pad + (num_w + px(10) if numbered else 0)
    label_w = max(m.measure("label", label) for label, _, _ in rows)
    value_x = label_x + label_w + px(12)
    value_w = pad + inner - value_x
    line_h = m.linespace("value")
    for i, (label, value, context) in enumerate(rows):
        if i:
            y += px(6)
        baseline = y + m.ascent("value")
        if numbered:
            mid = y + line_h // 2
            ops.append(ShapeOp(pad, mid - num_w // 2, num_w, num_w, num_w // 2, SURFACE))
            n = str(i + 1)
            ops.append(TextOp(pad + (num_w - m.measure("num", n)) // 2, mid - m.linespace("num") // 2, n, "num",
                              MUTED))
        ops.append(TextOp(label_x, baseline - m.ascent("label"), label, "label", MUTED))
        lines = wrap(value, value_w, _fn(m, "value"), 3) if value else []
        if lines and value.endswith("”") and lines[-1].endswith(ELLIPSIS):  # cut short: keep the closing quote
            room = value_w - m.measure("value", "”")
            lines[-1] = ellipsize(lines[-1].removesuffix(ELLIPSIS), room, _fn(m, "value")) + "”"
        if context:
            gap = m.measure("value", " ")
            if lines and m.measure("value", lines[-1]) + gap + m.measure("value", context) <= value_w:
                ops.append(TextOp(value_x + m.measure("value", lines[-1]) + gap, y + line_h * (len(lines) - 1),
                                  context, "value", MUTED))
            else:
                context_lines = wrap(context, value_w, _fn(m, "value"), 1)
                for j, line in enumerate(context_lines):
                    ops.append(TextOp(value_x, y + line_h * (len(lines) + j), line, "value", MUTED))
                lines = lines + [""] * len(context_lines)
        for j, line in enumerate(lines):
            if line:
                ops.append(TextOp(value_x, y + line_h * j, line, "value", FG))
        y += line_h * max(len(lines), 1)
    return y


def changed_ops(old: list, new: list) -> list[int] | None:
    """Indices of the ops in `new` that differ from `old`, or None when the scene's structure changed (a
    different number or kind of ops), which means drawing everything again."""
    if len(old) != len(new) or any(type(a) is not type(b) for a, b in zip(old, new, strict=True)):
        return None
    return [i for i, (a, b) in enumerate(zip(old, new, strict=True)) if a != b]


def shape_pixels(w: int, h: int, r: int, colour: str, bg: str = BG) -> list[list[str]]:
    """Rows of hex colours for a rounded rectangle on `bg`, edges antialiased by pixel coverage of the
    signed distance (Tk's canvas draws shapes without antialiasing, which looks jagged on video)."""
    r = min(r, w / 2, h / 2)
    tones = {}
    rows = []
    for py in range(h):
        row = []
        for qx in range(w):
            dx = max(abs(qx + 0.5 - w / 2) - (w / 2 - r), 0.0)
            dy = max(abs(py + 0.5 - h / 2) - (h / 2 - r), 0.0)
            coverage = min(max(r - math.hypot(dx, dy) + 0.5, 0.0), 1.0) if r > 0 else 1.0
            step = round(coverage * 16)
            if step not in tones:
                tones[step] = blend(bg, colour, step / 16)
            row.append(tones[step])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------------------
# Windows plumbing


CORNERS = ("bottom-right", "top-right", "bottom-left", "top-left")


def place(width: int, height: int, area: tuple[int, int, int, int], margin: int,
          avoid: tuple[int, int, int, int] | None, corner: str = "bottom-right") -> tuple[int, int]:
    """Where the card goes: the chosen corner of the work area, else the first other corner that does not cover
    the guide's ring (seen live: the card hid a website's cookie banner buttons the ring was pointing at)."""
    left, top, right, bottom = area
    spots = {"bottom-right": (right - width - margin, max(bottom - height - margin, top)),
             "top-right": (right - width - margin, top + margin),
             "bottom-left": (left + margin, max(bottom - height - margin, top)),
             "top-left": (left + margin, top + margin)}
    first = corner if corner in spots else "bottom-right"
    corners = [spots[first]] + [spots[c] for c in CORNERS if c != first]
    if avoid is None:
        return corners[0]
    pad = margin * 2
    al, at, ar, ab = avoid[0] - pad, avoid[1] - pad, avoid[2] + pad, avoid[3] + pad
    for x, y in corners:
        if x + width <= al or x >= ar or y + height <= at or y >= ab:
            return x, y
    return corners[0]


def make_dpi_aware() -> None:
    """Per-monitor DPI awareness, so Windows doesn't bitmap-stretch the window (blurry text). Must run before
    the first window is created; harmless if the process already chose (pywinauto does the same)."""
    try:
        import ctypes

        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_V2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_byte, wintypes.DWORD]
    return user32


def foreground_window() -> int:
    """The window that receives the user's keystrokes (0 when none)."""
    return _user32().GetForegroundWindow() or 0


def work_area() -> tuple[int, int, int, int]:
    """The primary monitor's area not covered by the taskbar, in physical pixels."""
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000


# ---------------------------------------------------------------------------------------------------------
# The window


class TkMetrics:
    def __init__(self, root, families: set[str]):
        import tkinter.font as tkfont

        self.fonts = {}
        for role, (family, size) in FONTS.items():
            if role == "icon" and family not in families:
                family = "Segoe MDL2 Assets"
            weight = "normal"
            if family not in families:  # e.g. no "Segoe UI Semibold" alias: same face, bold
                family, weight = "Segoe UI", "bold" if "Semibold" in family else "normal"
            self.fonts[role] = tkfont.Font(root=root, family=family, size=size, weight=weight)
        self.icons = bool({"Segoe Fluent Icons", "Segoe MDL2 Assets"} & families)
        self._widths: dict[tuple[str, str], int] = {}
        self._metrics = {role: font.metrics() for role, font in self.fonts.items()}

    def measure(self, font: str, text: str) -> int:
        key = (font, text)
        if key not in self._widths:
            if len(self._widths) > 4000:
                self._widths.clear()
            self._widths[key] = self.fonts[font].measure(text)
        return self._widths[key]

    def ascent(self, font: str) -> int:
        return self._metrics[font]["ascent"]

    def linespace(self, font: str) -> int:
        return self._metrics[font]["linespace"]


class Overlay:
    """The status window. Create and `run` it on the main thread; call `set` from any thread."""

    TICK_MS = 33

    def __init__(self, hotkey: str = "ctrl+space", *, click_only: bool = False, api_model: str | None = None):
        import tkinter as tk
        import tkinter.font as tkfont

        make_dpi_aware()
        self.updates: queue.SimpleQueue = queue.SimpleQueue()
        self.model = OverlayState(hotkey=hotkey, state_since=time.monotonic(), api_model=api_model)
        self.click_only = click_only
        self.details_enabled = True
        self.anchor = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Bol OS")
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.metrics = TkMetrics(self.root, set(tkfont.families(self.root)))
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._images: dict[tuple, object] = {}
        self._ops: list = []  # the scene on the canvas: op i is drawn as canvas item _items[i]
        self._items: list[int] = []
        self._size = (0, 0)
        self._origin = (0, 0)
        self.hwnd = 0
        self.visible = not click_only
        self._notice_visible = False
        if self.visible:
            self._show_without_focus()
            self._render()
        self.root.after(self.TICK_MS, self._tick)

    # Public, thread-safe

    def set(self, kind: str, value=None) -> None:
        """A status event (see the design brief): ("state", "listening"), ("level", 0.4), ("heard", text),
        ("language", "ta"), ("decision", {...}), ("thinking", 3.2), ("reply", text), ("trigger", "wake")."""
        self.updates.put((kind, value))

    status = set

    def close(self) -> None:
        self.updates.put(("_close", None))

    def show(self, visible: bool) -> None:
        """Show or hide the card (from the dashboard). Hidden, it keeps following the state, so it is current
        when shown again."""
        self.updates.put(("_visible", bool(visible)))

    def toggle_details(self, x: int, y: int, area: tuple[int, int, int, int] | None, gap: int) -> None:
        self.updates.put(("_details", (x, y, area, gap)))

    def move_details(self, x: int, y: int, area: tuple[int, int, int, int] | None, gap: int) -> None:
        self.updates.put(("_details_moved", (x, y, area, gap)))

    def run(self) -> None:
        self.root.mainloop()

    # Tk thread

    def _show_without_focus(self) -> None:
        """Map the window without ever making it the foreground window. Tk calls SetForegroundWindow when it
        creates the top-level wrapper (even hidden), so that happens while foreground changes are locked;
        then the styles make it no-activate and click-through before it is shown."""
        user32 = _user32()
        locked = user32.LockSetForegroundWindow(1)  # LSFW_LOCK
        try:
            self.root.update_idletasks()  # creates the (hidden) wrapper window
        finally:
            if locked:
                user32.LockSetForegroundWindow(2)  # LSFW_UNLOCK
        self.hwnd = int(self.root.wm_frame(), 16)
        style = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style)
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 2)  # LWA_ALPHA, opaque
        self._round_corners()
        self._place(*self._scene_size())
        user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        # HWND_TOPMOST; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        locked = user32.LockSetForegroundWindow(1)
        try:
            self.root.wm_state("normal")  # tell Tk it is mapped; Tk would activate it here too
            self.root.update_idletasks()
        finally:
            if locked:
                user32.LockSetForegroundWindow(2)
        # Mapping resets the position given before it to +0+0 (seen live: the idle card sat in the top-left
        # corner until its first command changed its size), so place it again now that it is mapped.
        self._size = (0, 0)
        self._place(*self._scene_size())

    def _set_visible(self, visible: bool) -> None:
        if visible == self.visible or not self.hwnd:
            return
        self.visible = visible
        user32 = _user32()
        if visible:
            # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW, above everything: never takes focus
            user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        else:
            user32.ShowWindow(self.hwnd, 0)  # SW_HIDE

    def _round_corners(self) -> None:
        """Windows 11 rounded corners and a hairline border (ignored on Windows 10)."""
        try:
            import ctypes

            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)  # CORNER: ROUND
            r, g, b = (int(HAIRLINE[i:i + 2], 16) for i in (1, 3, 5))
            dwm.DwmSetWindowAttribute(self.hwnd, 34, ctypes.byref(ctypes.c_uint(r | g << 8 | b << 16)), 4)
        except (AttributeError, OSError):
            pass

    def _scene_size(self) -> tuple[int, int]:
        scene = compose(self.model, time.monotonic(), self.metrics, self.scale)
        return scene.width, scene.height

    def _place(self, width: int, height: int) -> None:
        """The chosen corner of the work area, unless the guide's ring is there. At the bottom the window grows
        upwards, at the top downwards."""
        if self.anchor:
            ax, ay, area, gap = self.anchor
            left, top, right, bottom = area or work_area()
            x = ax + gap if ax + gap + width <= right else ax - gap - width
            x = max(left, min(x, right - width))
            y = max(top, min(ay - height // 2, bottom - height))
        else:
            x, y = place(width, height, work_area(), round(16 * self.scale), self.model.avoid, self.model.corner)
        if (width, height, x, y) == self._size + self._origin:
            return
        if self.anchor and self.hwnd:
            if (width, height) != self._size:
                self.root.geometry(f"{width}x{height}")
                self.root.update_idletasks()
            _user32().SetWindowPos(self.hwnd, -1, x, y, 0, 0, 0x0001 | 0x0010)
        else:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self._size, self._origin = (width, height), (x, y)

    def _tick(self) -> None:
        now = time.monotonic()
        changed = False
        try:
            while True:
                kind, value = self.updates.get_nowait()
                if kind == "_close":
                    self.root.destroy()
                    return
                if kind == "_shot":
                    self._render()
                    self.root.update_idletasks()
                    save_png(value, self.capture())
                    continue
                if kind == "_call":
                    value()
                    continue
                if kind == "_visible":
                    if self.click_only:
                        self.details_enabled = value
                        if not value:
                            self._set_visible(False)
                            self._notice_visible = False
                    else:
                        self._set_visible(value)
                    continue
                if kind == "_details":
                    if self.details_enabled and self.visible:
                        self._set_visible(False)
                        self._notice_visible = False
                    elif self.details_enabled:
                        self.anchor = value
                        if not self.hwnd:
                            self._show_without_focus()
                            self.visible = True
                        else:
                            self._set_visible(True)
                        changed = True
                    continue
                if kind == "_details_moved":
                    if self.visible:
                        self.anchor = value
                        changed = True
                    continue
                try:
                    applied = self.model.apply(kind, value, now)
                    changed |= applied
                    if applied and kind == "state" and self.click_only:
                        self._show_attention(value)
                except Exception:  # a bad event must never take the window down
                    pass
        except queue.Empty:
            pass
        if not self.click_only:
            changed |= self.model.settle(now)
        if (self.visible or not self.click_only) and (changed or self._animating):
            self._render()
        self.root.after(self.TICK_MS, self._tick)

    def _show_attention(self, state: str) -> None:
        if state in ATTENTION_STATES:
            if self.details_enabled and not self.visible:
                self.anchor = None
                if not self.hwnd:
                    self._show_without_focus()
                    self.visible = True
                else:
                    self._set_visible(True)
                self._notice_visible = True
        elif self._notice_visible:
            self._set_visible(False)
            self._notice_visible = False

    _animating = False

    def _render(self) -> None:
        """Draw the current scene, touching only the canvas items whose op changed: while listening, only the
        meter and the status dot move, and recreating every item 30 times a second costs a quarter of a core
        (on the GIL the audio threads need)."""
        scene = compose(self.model, time.monotonic(), self.metrics, self.scale)
        self._animating = scene.animating
        self._place(scene.width, scene.height)
        if len(self._images) > 600:  # unused shape images pile up over a long session
            self._images.clear()
            self._ops = []  # every item's image is gone: draw them all again
        canvas = self.canvas
        changed = changed_ops(self._ops, scene.ops)
        if changed is None:
            canvas.delete("all")
            self._items = [self._create(op) for op in scene.ops]
        else:
            for i in changed:
                op, item = scene.ops[i], self._items[i]
                canvas.coords(item, op.x, op.y)
                if isinstance(op, ShapeOp):
                    canvas.itemconfigure(item, image=self._shape(op))
                else:
                    canvas.itemconfigure(item, text=op.text, font=self.metrics.fonts[op.font], fill=op.colour)
        self._ops = scene.ops

    def _create(self, op) -> int:
        if isinstance(op, ShapeOp):
            return self.canvas.create_image(op.x, op.y, image=self._shape(op), anchor="nw")
        return self.canvas.create_text(op.x, op.y, text=op.text, font=self.metrics.fonts[op.font], fill=op.colour,
                                       anchor="nw")

    def _shape(self, op: ShapeOp):
        import tkinter as tk

        key = (op.w, op.h, op.r, op.colour)
        image = self._images.get(key)
        if image is None:
            image = tk.PhotoImage(master=self.root, width=op.w, height=op.h)
            rows = shape_pixels(op.w, op.h, op.r, op.colour)
            image.put(" ".join("{" + " ".join(row) + "}" for row in rows))
            self._images[key] = image
        return image

    def capture(self) -> tuple[int, int, bytes]:
        """The window's own pixels (not the screen behind it) as (width, height, RGBA rows), with the
        Windows 11 corner rounding applied as alpha so the PNG looks like the window on screen."""
        import win32gui
        import win32ui

        width, height = self._size
        window_dc = win32gui.GetWindowDC(self.hwnd)
        source = win32ui.CreateDCFromHandle(window_dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        try:
            import ctypes

            ctypes.windll.user32.PrintWindow(self.hwnd, memory.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            bgra = bitmap.GetBitmapBits(True)
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, window_dc)
        return width, height, framed_rgba(width, height, bgra, round(8 * self.scale), HAIRLINE)


def framed_rgba(width: int, height: int, bgra: bytes, radius: int, border: str | None = None) -> bytes:
    """BGRA pixels -> RGBA, cut to a rounded rectangle (transparent, antialiased corners) with a 1 px
    `border` along its edge: what DWM composites on screen but PrintWindow doesn't see."""
    out = bytearray(len(bgra))
    out[0::4], out[1::4], out[2::4] = bgra[2::4], bgra[1::4], bgra[0::4]
    out[3::4] = b"\xff" * (width * height)
    edge = [int(border[i:i + 2], 16) for i in (1, 3, 5)] if border else None
    band = radius + 2  # pixels further in than this from every edge are untouched

    def clamp(v: float) -> float:
        return min(max(v, 0.0), 1.0)

    for y in range(height):
        near_y = y < band or y >= height - band
        xs = range(width) if near_y else [*range(min(band, width)), *range(max(width - band, band), width)]
        for x in xs:
            qx = abs(x + 0.5 - width / 2) - (width / 2 - radius)
            qy = abs(y + 0.5 - height / 2) - (height / 2 - radius)
            d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius  # < 0 inside
            outer, inner = clamp(0.5 - d), clamp(-0.5 - d)
            i = (y * width + x) * 4
            if edge and outer > 0:
                for c in range(3):
                    out[i + c] = round((edge[c] * (outer - inner) + out[i + c] * inner) / outer)
            out[i + 3] = round(outer * 255)
    return bytes(out)


def save_png(path, image: tuple[int, int, bytes]) -> None:
    """Write RGBA pixels as a PNG (no imaging library needed)."""
    width, height, rgba = image

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------------------------------------
# Demo: every state and language, for screenshots and the demo video


def _decision(source: str, ms: float | None, *steps: tuple[str, dict]) -> dict:
    return {"source": source, "ms": ms, "steps": [{"action": a, "fields": f} for a, f in steps]}


# (language, trigger, heard, decision, final state, reply). Spoken commands arrive in Latin letters
# (Prisma transliterates); trigger None is a typed command, which can be in native script.
LANGUAGE_SCENES = [
    ("hi", "wake", "Chrome kholo aur Bangalore ka weather search karo",
     _decision("system1", 1.4, ("open_app", {"app": "Chrome"}),
               ("web_search", {"query": "Bangalore ka weather", "browser": "Chrome"})),
     "done", "Chrome खोल दिया और Bangalore ka weather सर्च कर दिया।"),
    ("en", "tap", "Open YouTube and play Arijit Singh songs",
     _decision("habit", 0.3, ("open_website", {"site": "YouTube"}),
               ("play_media", {"query": "Arijit Singh songs", "app": "YouTube"})),
     "done", "Opened YouTube and playing Arijit Singh songs."),
    ("kn", "hotkey", "Notepad ತೆರೆ ಮತ್ತೆ meeting naale hattu gantege ide anta bari",
     _decision("system2", 8900, ("open_app", {"app": "Notepad"}),
               ("type_text", {"text": "meeting naale hattu gantege ide"})),
     "done", "Notepad ತೆರೆದಿದ್ದೇನೆ ಮತ್ತು ಬರೆದಿದ್ದೇನೆ."),
    ("ta", None, "Spotify ல அனிருத் பாட்டு போடு",
     _decision("system1", 0.9, ("play_media", {"query": "அனிருத் பாட்டு", "app": "Spotify"})),
     "done", "Spotify இல் அனிருத் பாட்டு ப்ளே செய்கிறேன்."),
    ("te", "wake", "YouTube lo Bahubali trailer pettu",
     _decision("system1", 1.1, ("play_media", {"query": "Bahubali trailer", "app": "YouTube"})),
     "done", "YouTube లో Bahubali trailer ప్లే చేస్తున్నాను."),
    ("mr", "hotkey", "Aai la message pathav ki mi ardhya tasat yetoy",
     _decision("system2_fields", 7400, ("send_message", {"contact": "Aai", "text": "mi ardhya tasat yetoy"})),
     "confirming", "Aai ला पाठवू: mi ardhya tasat yetoy? हो किंवा नाही सांगा."),
    ("ml", "tap", "Flipkart il running shoes thirayu",
     _decision("system1", 1.3, ("web_search", {"query": "running shoes", "site": "Flipkart"})),
     "done", "Flipkart ൽ running shoes തിരഞ്ഞു."),
    ("bn", None, "গানটা বন্ধ করো",
     _decision("system1", 0.8, ("media_control", {"control": "pause"})),
     "done", "গান বন্ধ করে দিলাম।"),
    ("gu", None, "Chrome માં Amazon ખોલો",
     _decision("habit", 0.4, ("open_website", {"site": "Amazon", "browser": "Chrome"})),
     "done", "Chrome માં Amazon ખોલી દીધું."),
    ("pa", "hotkey", "Gurdas Maan de gaane lagao",
     _decision("user", 0.5, ("play_media", {"query": "Gurdas Maan de gaane", "app": "YouTube"})),
     "done", "YouTube ਤੇ Gurdas Maan de gaane ਚਲਾ ਰਹੀ ਹਾਂ।"),
]


def window_process(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


class FocusWatch:
    """Samples the foreground window every 20 ms while the demo runs: the overlay passes only if no window
    of this process ever became the foreground window (switches between the user's own windows are fine)."""

    def __init__(self):
        self.start = foreground_window()
        self.samples = 0
        self.stolen: list[int] = []  # foreground windows that belonged to this process
        self.switches = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="focus-watch")
        self._thread.start()

    def _run(self) -> None:
        import os

        me, last = os.getpid(), self.start
        while not self._stop.wait(0.02):
            hwnd = foreground_window()
            self.samples += 1
            if hwnd and window_process(hwnd) == me and hwnd not in self.stolen:
                self.stolen.append(hwnd)
            if hwnd != last:
                self.switches, last = self.switches + 1, hwnd

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(1)


class Demo:
    """Drives an Overlay from a worker thread the way the pipeline does."""

    def __init__(self, overlay: Overlay, shots: Path | None, speed: float):
        self.overlay, self.shots, self.speed = overlay, shots, speed

    def send(self, kind: str, value=None) -> None:
        self.overlay.set(kind, value)

    def wait(self, seconds: float) -> None:
        time.sleep(seconds / self.speed)

    def shot(self, name: str) -> None:
        if self.shots:
            done = threading.Event()
            self.overlay.set("_shot", self.shots / f"{name}.png")
            self.overlay.set("_call", done.set)
            done.wait(5)
            print(f"saved {self.shots / f'{name}.png'}")

    def speak_levels(self, seconds: float, loud: bool) -> None:
        """A speech-like microphone envelope at ~20 levels per second."""
        start = time.monotonic()
        while time.monotonic() - start < seconds / self.speed:
            t = time.monotonic() - start
            syllables = abs(math.sin(t * 7.3)) * (0.55 + 0.45 * math.sin(t * 2.1 + 1))
            level = (0.18 + 0.75 * syllables) if loud else 0.03 + 0.03 * abs(math.sin(t * 13))
            self.send("level", min(level, 1.0))
            time.sleep(0.05)

    def think(self, seconds: float) -> None:
        start = time.monotonic()
        while (elapsed := (time.monotonic() - start) * self.speed) < seconds:
            self.send("thinking", elapsed)
            time.sleep(0.1)

    def states(self) -> None:
        s = self.send
        s("state", "ready")
        self.wait(1.5)
        self.shot("state-ready")

        s("trigger", "wake")
        s("state", "listening")
        self.speak_levels(1.2, loud=False)
        self.shot("state-listening")
        s("state", "hearing")
        self.speak_levels(1.6, loud=True)
        self.shot("state-hearing")
        s("state", "transcribing")
        self.wait(0.6)
        self.shot("state-transcribing")
        s("heard", "Lata Mangeshkar ke baare mein batao")
        s("language", "hi")
        s("state", "understanding")
        self.wait(0.6)
        self.shot("state-understanding")
        s("state", "thinking")
        self.think(4.2)
        self.shot("state-thinking")
        s("decision", _decision("system2", 8900, ("web_search", {"query": "Lata Mangeshkar"})))
        s("state", "executing")
        self.wait(0.8)
        self.shot("state-executing")
        s("reply", "Lata Mangeshkar सर्च कर दिया।")
        s("state", "done")
        self.wait(2)
        self.shot("state-done")

        s("trigger", "tap")
        s("state", "listening")
        self.speak_levels(1.0, loud=True)
        self.shot("state-listening-again")
        s("state", "transcribing")
        s("heard", "Mummy ko message bhejo ki main das minute mein ghar pahunch raha hoon, khana garam rakhna")
        s("language", "hi")
        s("state", "understanding")
        s("decision", _decision("system1", 1.6, ("send_message", {
            "contact": "Mummy", "text": "main das minute mein ghar pahunch raha hoon, khana garam rakhna"})))
        s("reply", "Mummy को भेजूँ: main das minute mein ghar pahunch raha hoon, khana garam rakhna? "
                   "हाँ या ना बोलिए।")
        s("state", "confirming")
        self.wait(2.5)
        self.shot("state-confirming")
        s("trigger", "followup")
        s("state", "listening")
        self.speak_levels(1.0, loud=True)
        self.shot("state-listening-followup")
        s("reply", "Okay, not sent.")
        s("state", "not_sent")
        self.wait(1.5)
        self.shot("state-not-sent")

        s("trigger", "hotkey")
        s("state", "listening")
        self.speak_levels(0.8, loud=True)
        s("heard", "Raju ko WhatsApp karo")
        s("language", "hi")
        s("state", "understanding")
        s("decision", _decision("system1", 1.2, ("send_message", {"contact": "Raju", "text": ""})))
        s("reply", "कुछ गड़बड़ हो गई, फिर से कोशिश कीजिए।")
        s("state", "error")
        self.wait(2)
        self.shot("state-error")

        s("trigger", "hotkey")
        s("state", "listening")
        s("heard", "likho ki aaj ki meeting mein humne teen cheezein tay ki: launch agle mangalvaar ko hoga, "
                   "design review shukravaar tak khatam karna hai, aur Priya naye customers ki list "
                   "bhejegi jo humein somvaar se pehle chahiye kyunki uske baad sales team ko bhi dekhni hai")
        s("language", "hi")
        s("decision", _decision("system1", 2.1, ("type_text", {
            "text": "aaj ki meeting mein humne teen cheezein tay ki: launch agle mangalvaar ko hoga, design review "
                    "shukravaar tak khatam karna hai, aur Priya naye customers ki list bhejegi jo humein somvaar "
                    "se pehle chahiye kyunki uske baad sales team ko bhi dekhni hai"})))
        s("state", "executing")
        s("reply", "लिख दिया।")
        s("state", "done")
        self.wait(2)
        self.shot("state-long-text")

        s("trigger", "tap")
        s("state", "listening")
        self.speak_levels(1.0, loud=True)
        s("heard", "Settings mein Bluetooth wala page kaise kholte hain, dikhao")
        s("language", "hi")
        s("state", "thinking")
        self.think(3.0)
        s("decision", _decision("system1", 0.8, ("show_how", {"goal": "Bluetooth page"})))
        s("guide", {"step": 1})
        s("reply", "नीचे टास्कबार में “Settings” पर क्लिक कीजिए।")
        s("state", "guiding")
        self.wait(2.5)
        self.shot("state-guiding")
        s("guide", {"step": 2})
        s("reply", "बाईं ओर “Bluetooth & devices” पर क्लिक कीजिए।")
        self.wait(2.5)
        s("reply", "Bluetooth वाला पेज खुल गया है।")
        s("state", "done")
        self.wait(2)
        self.shot("state-guide-done")

    def languages(self) -> None:
        for code, trigger, heard, decision, final, reply in LANGUAGE_SCENES:
            s = self.send
            if trigger:
                s("trigger", trigger)
                s("state", "listening")
                self.speak_levels(0.9, loud=True)
                s("state", "transcribing")
                self.wait(0.3)
            s("heard", heard)
            s("language", code)
            s("state", "understanding")
            if decision["source"] in ("system2", "system2_fields"):
                s("state", "thinking")
                self.think(1.5)
            s("decision", decision)
            if final == "done":
                s("state", "executing")
                self.wait(0.5)
            s("reply", reply)
            s("state", final)
            self.wait(2.2)
            self.shot(f"lang-{code}")
            if final == "confirming":
                s("state", "not_sent")

    def run(self, loop: bool) -> None:
        try:
            while True:
                self.states()
                self.languages()
                if not loop:
                    break
        finally:
            self.overlay.close()


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m bolos.overlay")
    sub = cli.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="cycle through every state and language")
    demo.add_argument("--shots", type=Path, help="save a PNG of each scene in this folder")
    demo.add_argument("--speed", type=float, default=1.0, help="faster (>1) or slower (<1) than real time")
    demo.add_argument("--loop", action="store_true", help="repeat until closed (for recording a video)")
    demo.add_argument("--hotkey", default="ctrl+space")
    args = cli.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    watch = FocusWatch()  # before the window exists: creating it is when Tk tries to activate it
    overlay = Overlay(args.hotkey)
    runner = Demo(overlay, args.shots, max(args.speed, 0.1))
    threading.Thread(target=runner.run, args=(args.loop,), daemon=True).start()
    try:
        overlay.run()
    finally:
        watch.stop()
    if watch.stolen:
        print(f"FOCUS STOLEN: the overlay process owned the foreground window {watch.stolen}")
        return 1
    print(f"Focus kept: {watch.samples} foreground samples, none was the overlay (window {overlay.hwnd}); "
          f"the user's foreground window was {watch.start}, {watch.switches} switch(es) by the user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
