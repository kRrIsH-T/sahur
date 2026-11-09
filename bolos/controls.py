"""The dashboard's hands: apply saved settings to a running Bol OS, report how it is doing, and run the sound
checks.

A running Bol OS creates one Controls with its voice, overlay and history, and the dashboard server it starts
calls `update`. Without them (python -m bolos.dashboard while Bol OS is not running) changes are only saved,
and the microphone meter and sound checks open the devices themselves.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Callable

from bolos.config import Settings, load_settings
from bolos.user_settings import SettingError, UserSettings

log = logging.getLogger(__name__)

Status = Callable[[str, object], None]
METER_IDLE_S = 4.0  # the standalone microphone meter closes when nobody has looked at it this long


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    level: str  # "ok", "warn" or "fail"
    detail: str


class MicMeter:
    """The microphone level for the dashboard when Bol OS is not running: opens the device while someone is
    looking, closes it when they stop."""

    def __init__(self):
        self.level = 0.0
        self.error: str | None = None
        self._device: int | None = None
        self._stream = None
        self._seen = 0.0
        self._lock = threading.Lock()

    def read(self, device: int | None) -> float:
        with self._lock:
            self._seen = time.monotonic()
            if self._stream is not None and device != self._device:
                self._close()
            if self._stream is None:
                self._open(device)
            self.level *= 0.85  # decays between reads when no audio arrives
            return self.level

    def _open(self, device: int | None) -> None:
        import sounddevice as sd

        from bolos.audio import FRAME, SAMPLE_RATE, level

        def callback(data, *_):
            self.level = max(level(data[:, 0]), self.level)

        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                                          callback=callback, device=device)
            self._stream.start()
            self._device, self.error = device, None
            threading.Thread(target=self._close_when_idle, daemon=True, name="mic-meter").start()
        except Exception as e:
            self._stream, self.error = None, str(e) or type(e).__name__

    def _close_when_idle(self) -> None:
        while True:
            time.sleep(1.0)
            with self._lock:
                if self._stream is None:
                    return
                if time.monotonic() - self._seen > METER_IDLE_S:
                    self._close()
                    return

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("closing the meter's microphone failed", exc_info=True)


class Controls:
    def __init__(self, store: UserSettings, *, voice=None, overlay=None, history=None, llm=None,
                 dry_run: bool = False, buddy=None):
        self.store = store
        self.voice = voice
        self.overlay = overlay
        self.buddy = buddy  # bolos.buddy.Buddy, the pointer beside the cursor
        self.history = history
        self.llm = llm
        self.dry_run = dry_run
        self.started = time.time()
        self.at_start = store.values()  # what this Bol OS started with: changed non-live options need a restart
        self.live = {"state": "ready", "since": time.time(), "heard": "", "reply": ""}
        self._meter = MicMeter()
        self._lock = threading.Lock()

    # The settings

    @property
    def running(self) -> bool:
        return self.voice is not None

    def restart_needed(self) -> list[str]:
        """Options changed since Bol OS started that only apply when it starts again."""
        if not self.running:
            return []
        now = self.store.values()
        return [k for k, o in self.store.options.items() if not o.live and now[k] != self.at_start[k]]

    @property
    def listener(self):
        return getattr(self.voice, "listener", None)

    def settings(self) -> Settings:
        return load_settings(self.store.overrides())

    def images_allowed(self) -> bool:
        """Whether a screenshot may go to System 2 now ("Let System 2 see the screen")."""
        return bool(self.store.get("screen_images"))

    def update(self, changes: dict[str, object]) -> dict[str, object]:
        """Check, apply and save. What can fail when applied (a hotkey another program holds, a microphone that
        will not open) is tried first, so a failure changes nothing: SettingError."""
        checked = self.store.check(changes)
        before = self.store.values()
        listener = self.listener
        if listener is not None:
            if "hotkey" in checked and checked["hotkey"] != before["hotkey"]:
                from bolos.hotkey import HotkeyError

                try:
                    listener.set_hotkey(checked["hotkey"])
                except HotkeyError:
                    raise SettingError(f"Another program already uses {checked['hotkey']}. Choose another "
                                       "combination.") from None
            if "input_device" in checked and checked["input_device"] != before["input_device"]:
                device = self._device(checked["input_device"], "input")
                try:
                    listener.set_input_device(device)
                except Exception as e:
                    raise SettingError(f"Could not open that microphone: {e}") from None
        diff = self.store.update(checked)
        self._apply(diff)
        return diff

    def reset(self) -> dict[str, object]:
        return self.update({key: option.default for key, option in self.store.options.items()})

    def apply_all(self) -> None:
        """Apply every saved live setting (once, when Bol OS starts)."""
        values = self.store.values()
        listener = self.listener
        if listener is not None:
            listener.input_device = self._device(values["input_device"], "input")
        self._apply({key: values[key] for key in ("output_device", "chimes", "buddy_visible", "widget_visible",
                                                  "widget_corner", "keep_history")})

    def _apply(self, diff: dict[str, object]) -> None:
        options = self.store.options
        if any(options[key].setting for key in diff):
            settings = self.settings()
            if self.listener is not None:
                self.listener.update_settings(settings)
            if self.voice is not None:
                self.voice.user_language = settings.user_language
        if "output_device" in diff:
            device = self._device(diff["output_device"], "output")
            if self.voice is not None:
                if self.listener is not None:
                    self.listener.player.device = device
                else:
                    self.voice.device = device
        if "chimes" in diff and self.listener is not None:
            self.listener.earcons = bool(diff["chimes"])
        if self.overlay is not None:
            if "widget_visible" in diff:
                self.overlay.show(bool(diff["widget_visible"]))
            if "widget_corner" in diff:
                self.overlay.set("corner", diff["widget_corner"])
            if "hotkey" in diff:
                self.overlay.set("hotkey", diff["hotkey"])
        if "buddy_visible" in diff and self.buddy is not None:
            self.buddy.show(bool(diff["buddy_visible"]))
        if "keep_history" in diff and self.history is not None:
            self.history.enabled = bool(diff["keep_history"])

    @staticmethod
    def _device(name: str, kind: str) -> int | None:
        if not name:
            return None
        from bolos.audio import find_device

        device = find_device(name, kind)
        if device is None:
            log.warning("%s %r is not connected; using the Windows default", kind, name)
        return device.index if device else None

    # Live state

    def status(self, status: Status) -> Status:
        """`status` that also keeps what the dashboard shows live (state, last heard, last reply)."""

        def tap(kind: str, value=None) -> None:
            if kind == "state" and isinstance(value, str) and value != self.live["state"]:
                self.live.update(state=value, since=time.time())
            elif kind == "heard" and isinstance(value, str):
                self.live.update(heard=value, reply="")
            elif kind == "reply" and isinstance(value, str):
                self.live["reply"] = value
            status(kind, value)

        return tap

    def level(self) -> dict:
        """The microphone level (0..1) for the meter."""
        listener = self.listener
        if listener is not None and listener.use_mic:
            return {"level": round(listener.mic_level, 3), "error": listener.mic_error, "state": self.live["state"]}
        device = self._device(self.store.get("input_device"), "input")
        value = self._meter.read(device)
        return {"level": round(value, 3), "error": self._meter.error, "state": None}

    # Sound checks

    def test(self, what: str) -> str:
        """Play a sound check ("chime" or "speaker") or show the buddy's hello. Returns what it plays."""
        settings = self.settings()
        language = settings.user_language
        output = self.store.get("output_device")
        device = self._device(output, "output")
        if what == "chime":
            from bolos.audio import Player

            Player(device).chime("start")
            return "chime"
        if what == "speaker":
            from bolos.responses import say

            text = say("speaker_test", language)
            threading.Thread(target=self._speak_test, args=(settings, text, language, device), daemon=True,
                             name="speaker-test").start()
            return text
        if what == "welcome":
            if self.buddy is None:
                raise SettingError("Start Bol OS to see the pointer")
            self.welcome()
            return "welcome"
        raise SettingError(f"Unknown sound check {what!r}")

    def welcome(self) -> None:
        """The pointer's hello and how to start talking, in the reply language (first run, and on request)."""
        if self.buddy is None:
            return
        from bolos.overlay import format_hotkey
        from bolos.responses import say

        settings = self.settings()
        language = settings.user_language
        self.buddy.welcome(say("buddy_hello", language), say("buddy_hint", language, hotkey=format_hotkey(
            self.store.get("hotkey") or settings.hotkey)))

    def _speak_test(self, settings: Settings, text: str, language: str, device: int | None) -> None:
        try:
            listener = self.listener
            if listener is not None:
                listener.speak(text, language)
                return
            if not settings.has_gnani:
                return
            from bolos.audio import Player
            from bolos.tts_cache import CachedTimbre

            CachedTimbre(settings).speak(text, language, Player(device))
        except Exception:
            log.warning("The speaker check could not play", exc_info=True)

    # Health

    def checks(self) -> list[Check]:
        """What to look at before a demo, worst first."""
        from bolos.audio import devices, find_device
        from bolos.wake import MODEL_PATH

        settings = self.settings()
        values = self.store.values()
        out = []
        if self.running:
            mode = " (dry run: nothing is executed)" if self.dry_run else ""
            out.append(Check("running", "Bol OS is running", "warn" if self.dry_run else "ok",
                             f"Since {time.strftime('%H:%M', time.localtime(self.started))}{mode}"))
        else:
            out.append(Check("running", "Bol OS is not running", "fail",
                             "Changes are saved and used when it starts: uv run python -m bolos"))
        out.append(Check("gnani", "Gnani speech key", "ok" if settings.has_gnani else "fail",
                         "Prisma and Timbre are ready" if settings.has_gnani else "Set GNANI_API_KEY in .env"))
        out.append(self._system2(settings))
        for kind, key, label in (("input", "input_device", "Microphone"), ("output", "output_device", "Speaker")):
            name = values[key]
            if name and find_device(name, kind) is None:
                out.append(Check(key, f"{label} not connected", "fail",
                                 f"{name} is missing; the Windows default is used. Plugged in after Bol OS "
                                 "started? Restart it."))
            else:
                default = next((d.name for d in devices(kind) if d.default), "")
                out.append(Check(key, label, "ok", name or (f"{default} (Windows default)" if default else
                                                              "Windows default")))
        listener = self.listener
        if listener is not None and listener.mic_error:
            out.append(Check("mic_error", "Microphone error", "fail", listener.mic_error))
        out.append(Check("widget", "Sahur details", "ok" if values["widget_visible"] else "warn",
                         "Click Sahur to open" if values["widget_visible"] else "Disabled"))
        if values["screen_images"]:
            out.append(Check("screen_images", "Screenshots go to System 2", "warn",
                             "Close private chats and tabs before recording"))
        if settings.wake_enabled and not MODEL_PATH.exists():
            out.append(Check("wake", "Wake word unavailable", "warn", "No trained model: use the hotkey"))
        order = {"fail": 0, "warn": 1, "ok": 2}
        return sorted(out, key=lambda c: order[c.level])

    def _system2(self, settings: Settings) -> Check:
        if settings.decider == "fast":
            return Check("system2", "System 2 is off", "warn", "Only what System 1 is sure of runs")
        if settings.llm_provider == "codex":
            found = settings.codex_path or shutil.which("codex")
            if found:
                tier = ", Fast mode" if settings.codex_fast else ""
                return Check("system2", "System 2: Codex CLI", "ok",
                             f"{settings.codex_model}, {settings.codex_effort} effort{tier}")
            return Check("system2", "System 2: Codex CLI not found", "fail", "Install it or set CODEX_PATH")
        if settings.has_llm:
            return Check("system2", "System 2: API", "ok", settings.llm_model)
        return Check("system2", "System 2 has no key", "fail", "Set GEMINI_API_KEY or LLM_API_KEY in .env")
