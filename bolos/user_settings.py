"""Settings changed from the dashboard, saved in settings.json next to .env.

Each option has a default: for the ones .env can also set (HOTKEY, USER_LANGUAGE, ...) that is the .env value,
so the dashboard always shows what Bol OS actually uses. Only what was changed here is saved, and a saved value
wins over .env. `live` options take effect at once in a running Bol OS (see bolos.controls); the others are
read when it starts.

    store = UserSettings.load(settings_path(), env_defaults(load_settings()))
    store.update({"widget_visible": False})       # checked and saved; returns what changed
    load_settings(store.overrides())              # Settings with the saved values applied
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from bolos.config import ROOT, Settings

log = logging.getLogger(__name__)

LANGUAGES = {"hi-IN": "Hindi", "en-IN": "English", "kn-IN": "Kannada", "ta-IN": "Tamil", "te-IN": "Telugu",
             "mr-IN": "Marathi", "ml-IN": "Malayalam", "bn-IN": "Bengali", "gu-IN": "Gujarati",
             "pa-IN": "Punjabi"}
# Timbre v3.0 voices speak all 10 languages. Only the women's voices: Bol OS's replies use feminine grammar
# ("main dikhati hoon"), which a man's voice would contradict.
VOICES = ("Shivani", "Lakshmi", "Kalpana", "Chetana", "Vasudha", "Riyaa", "Tarika")
CORNERS = {"bottom-right": "Bottom right", "bottom-left": "Bottom left", "top-right": "Top right",
           "top-left": "Top left"}
DECIDERS = {"system1": "System 1, System 2 when unsure", "fast": "System 1 only (offline)",
            "llm": "System 2 for everything", "laya": "Laya model"}
PROVIDERS = {"codex": "Codex CLI", "openai": "OpenAI-compatible API (Gemini, ...)"}
EFFORTS = {"low": "Low (fastest)", "medium": "Medium", "high": "High (slowest)"}


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    group: str
    kind: str  # "toggle", "choice", "choices" (several), "text", "hotkey", "input_device", "output_device"
    default: object
    live: bool = True  # applied to a running Bol OS at once; else on the next start
    help: str = ""
    choices: dict[str, str] = field(default_factory=dict)
    setting: str | None = None  # the bolos.config.Settings field it overrides, if any

    def check(self, value):
        """The value to save, or SettingError."""
        if self.kind == "toggle":
            if not isinstance(value, bool):
                raise SettingError(f"{self.label}: expected on or off")
            return value
        if self.kind == "choice":
            if value not in self.choices:
                raise SettingError(f"{self.label}: {value!r} is not one of the choices")
            return value
        if self.kind == "choices":
            if not isinstance(value, list) or not value or any(v not in self.choices for v in value):
                raise SettingError(f"{self.label}: choose at least one")
            return [k for k in self.choices if k in value]  # canonical order, no duplicates
        if not isinstance(value, str):
            raise SettingError(f"{self.label}: expected text")
        value = " ".join(value.split())
        if self.kind == "hotkey":
            from bolos.hotkey import HotkeyError, parse

            value = "+".join(p.strip().lower() for p in value.split("+") if p.strip())
            try:
                flags, _mods, _key = parse(value)
            except HotkeyError as e:
                raise SettingError(str(e)) from None
            if not flags:
                raise SettingError("The hotkey needs Ctrl, Alt, Shift or Win, else it would swallow a normal key")
            return value
        if self.kind == "text" and not value:
            raise SettingError(f"{self.label}: cannot be empty")
        return value  # devices: a name, "" for the Windows default


def options(defaults: dict[str, object]) -> list[Option]:
    """Every option, in the order the dashboard shows them. `defaults` holds the .env-backed defaults."""
    return [
        Option("input_device", "Microphone", "Sound", "input_device", "",
               help="The microphone Bol OS listens with. Windows default follows the Sound settings."),
        Option("output_device", "Speaker", "Sound", "output_device", "",
               help="Where replies and chimes are played."),
        Option("chimes", "Start and stop chimes", "Sound", "toggle", True,
               help="A short tone when Bol OS starts and stops listening."),
        Option("hotkey", "Push-to-talk hotkey", "Voice", "hotkey", defaults["hotkey"], setting="hotkey",
               help="Hold it while speaking, or tap it and speak."),
        Option("user_language", "Reply language", "Voice", "choice", defaults["user_language"],
               choices=LANGUAGES, setting="user_language",
               help="Used when the language of a command is unclear (romanised Hindi and English look alike)."),
        Option("stt_languages", "Languages to recognise", "Voice", "choices", defaults["stt_languages"],
               choices=LANGUAGES, setting="stt_languages",
               help="Fewer languages make Prisma more accurate. Applies from the next command."),
        Option("tts_voice", "Voice", "Voice", "choice", defaults["tts_voice"],
               choices={v: v for v in VOICES}, setting="tts_voice", help="Gnani Timbre voice for replies."),
        Option("buddy_visible", "Show the Sahur companion", "Widget", "toggle", True,
               help="Drag Sahur to move it. When disabled, Sahur appears during commands."),
        Option("widget_visible", "Allow click-to-open details", "Widget", "toggle", True,
               help="Click Sahur to show the latest transcript, reply, and command details."),
        Option("widget_corner", "Card position", "Widget", "choice", "bottom-right", choices=CORNERS,
               help="Used by the standalone status card. Sahur's details open beside the sprite."),
        Option("screen_images", "Let System 2 see the screen", "Privacy", "toggle", True,
               help="\"Screen par kya hai\" and guides send a screenshot to System 2. Off: only the text of "
                    "the window is used. Forms with your details typed in are never sent either way."),
        Option("keep_history", "Keep command history", "Privacy", "toggle", True,
               help="Saved on this computer only, for the statistics here and for Bol OS to learn from."),
        Option("decider", "Who decides", "Brain", "choice", defaults["decider"], live=False, choices=DECIDERS,
               setting="decider", help="System 1 is Bol OS's own model on this computer (about 10 ms)."),
        Option("llm_provider", "System 2", "Brain", "choice", defaults["llm_provider"], live=False,
               choices=PROVIDERS, setting="llm_provider"),
        Option("codex_model", "Codex model", "Brain", "text", defaults["codex_model"], live=False,
               setting="codex_model"),
        Option("codex_effort", "Codex reasoning effort", "Brain", "choice", defaults["codex_effort"],
               live=False, choices=EFFORTS, setting="codex_effort", help="Lower is faster."),
        Option("codex_fast", "Codex Fast mode", "Brain", "toggle", defaults["codex_fast"],
               live=False, setting="codex_fast", help="Request the Fast service tier for Codex replies."),
        Option("wake_enabled", "\"Hey Bol OS\" wake word", "Brain", "toggle", defaults["wake_enabled"],
               live=False, setting="wake_enabled",
               help="Needs a trained wake word model (train/wake); without one, use the hotkey."),
    ]


def env_defaults(settings: Settings) -> dict[str, object]:
    """The defaults the .env (or the built-in defaults) give the options that override Settings."""
    languages = [code.strip() for code in settings.stt_languages.split(",") if code.strip() in LANGUAGES]
    return {"hotkey": settings.hotkey, "user_language": settings.user_language,
            "stt_languages": languages or ["hi-IN"], "tts_voice": settings.tts_voice, "decider": settings.decider,
            "llm_provider": settings.llm_provider, "codex_model": settings.codex_model,
            "codex_effort": settings.codex_effort, "codex_fast": settings.codex_fast,
            "wake_enabled": settings.wake_enabled}


def settings_path() -> Path:
    return Path(os.environ.get("BOLOS_SETTINGS", str(ROOT / "settings.json")))


class UserSettings:
    """The saved settings. Thread-safe."""

    def __init__(self, path: Path | None, options_: list[Option], saved: dict | None = None):
        self.path = path
        self.options = {o.key: o for o in options_}
        self._saved: dict[str, object] = {}
        self._lock = threading.Lock()
        for key, value in (saved or {}).items():
            option = self.options.get(key)
            try:
                if option is not None:
                    self._saved[key] = option.check(value)
            except SettingError as e:
                log.warning("Ignoring a saved setting: %s", e)

    @classmethod
    def load(cls, path: Path | None, defaults: dict[str, object]) -> "UserSettings":
        saved = {}
        if path is not None and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                log.warning("%s is unreadable; using the defaults", path)
            if not isinstance(saved, dict):
                saved = {}
        return cls(path, options(defaults), saved)

    def get(self, key: str):
        with self._lock:
            return self._saved.get(key, self.options[key].default)

    def values(self) -> dict[str, object]:
        with self._lock:
            return {key: self._saved.get(key, o.default) for key, o in self.options.items()}

    def changed(self) -> set[str]:
        """The keys saved with a value other than the default."""
        with self._lock:
            return {k for k, v in self._saved.items() if v != self.options[k].default}

    def overrides(self) -> dict[str, object]:
        """Saved values for bolos.config.load_settings, by Settings field name."""
        with self._lock:
            out = {}
            for key, value in self._saved.items():
                option = self.options[key]
                if option.setting:
                    out[option.setting] = ",".join(value) if isinstance(value, list) else value
            return out

    def update(self, changes: dict[str, object]) -> dict[str, object]:
        """Check every change, then save them all (or none: SettingError). Returns the changes that were
        actually different from before."""
        checked = self.check(changes)
        with self._lock:
            before = {key: self._saved.get(key, self.options[key].default) for key in checked}
            for key, value in checked.items():
                if value == self.options[key].default:
                    self._saved.pop(key, None)  # back to the default: follow .env again
                else:
                    self._saved[key] = value
            self._write()
        return {k: v for k, v in checked.items() if before[k] != v}

    def check(self, changes: dict[str, object]) -> dict[str, object]:
        """The changes as they would be saved, or SettingError for the first bad one."""
        checked = {}
        for key, value in changes.items():
            option = self.options.get(key)
            if option is None:
                raise SettingError(f"Unknown setting {key!r}")
            checked[key] = option.check(value)
        return checked

    def reset(self) -> dict[str, object]:
        """Everything back to the defaults."""
        return self.update({key: o.default for key, o in self.options.items()})

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._saved, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash leaves the old file or the new one
