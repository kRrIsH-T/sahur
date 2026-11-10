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
        self._last_attempt = 0.0
        self._closed = False
        self._lock = threading.Lock()

    def read(self, device: int | None) -> float:
        with self._lock:
            if self._closed:
                return 0.0
            self._seen = time.monotonic()
            if self._stream is not None and device != self._device:
                self._close()
            if self._stream is None and (not self.error or device != self._device
                                         or self._seen - self._last_attempt >= 1.0):
                self._open(device)
            self.level *= 0.85  # decays between reads when no audio arrives
            return self.level

    def _open(self, device: int | None) -> None:
        import sounddevice as sd

        from bolos.audio import FRAME, SAMPLE_RATE, level

        def callback(data, *_):
            self.level = max(level(data[:, 0]), self.level)

        self._device = device
        self._last_attempt = self._seen
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                                          callback=callback, device=device)
            self._stream.start()
            self.error = None
            threading.Thread(target=self._close_when_idle, daemon=True, name="mic-meter").start()
        except Exception:
            self._close()
            self.error = "Check that your microphone is connected and allowed in Windows microphone settings."
            log.debug("Could not start the dashboard microphone check", exc_info=True)

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
        self.level = 0.0
        if stream is not None:
            for operation in (stream.stop, stream.close):
                try:
                    operation()
                except Exception:
                    log.debug("closing the meter's microphone failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close()


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

    @property
    def voice_commands(self) -> bool:
        return bool(getattr(self.listener, "use_mic", False))

    def settings(self) -> Settings:
        return load_settings(self.store.overrides())

    def images_allowed(self) -> bool:
        """Whether a screenshot may go to System 2 now ("Let System 2 see the screen")."""
        return bool(self.store.get("screen_images"))

    def update(self, changes: dict[str, object]) -> dict[str, object]:
        """Keep live updates in order when two requests arrive together."""
        with self._lock:
            return self._update(changes)

    def _update(self, changes: dict[str, object]) -> dict[str, object]:
        from bolos.hotkey import HotkeyError

        checked = self.store.check(changes)
        before = self.store.values()
        listener = self.listener
        restore = []
        try:
            if listener is not None:
                if "hotkey" in checked and (checked["hotkey"] != before["hotkey"]
                                            or getattr(listener, "hotkey_error", None)):
                    original = listener.hotkey
                    try:
                        listener.set_hotkey(checked["hotkey"])
                    except HotkeyError as e:
                        log.warning("Could not switch the keyboard shortcut: %s", e)
                        raise SettingError("Sahur couldn't use that shortcut. "
                                           "Choose a different key combination.") from e
                    if listener.hotkey != original:
                        restore.append((listener.set_hotkey, original))
                if "input_device" in checked and (checked["input_device"] != before["input_device"]
                                                  or listener.mic_error):
                    name = checked["input_device"]
                    device = self._device(name, "input")
                    if name and device is None:
                        raise SettingError("That microphone is disconnected. Plug it in or choose Windows default.")
                    original = listener.input_device
                    try:
                        listener.set_input_device(device)
                    except Exception as e:
                        log.warning("Could not switch the microphone: %s", e)
                        raise SettingError("Sahur couldn't use that microphone. Check that it's connected and allowed "
                                           "in Windows microphone settings, or choose a different one.") from e
                    if listener.input_device != original:
                        restore.append((listener.set_input_device, original))
            diff = self.store.update(checked)
        except SettingError:
            for apply, original in reversed(restore):
                try:
                    apply(original)
                except Exception:
                    log.warning("Could not restore a live setting after a dashboard error", exc_info=True)
            raise
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

        try:
            device = find_device(name, kind)
        except Exception:
            log.warning("Could not find the %s device; using the Windows default", kind, exc_info=True)
            return None
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

    def close(self) -> None:
        self._meter.close()

    # Sound checks

    def test(self, what: str) -> str:
        """Run a dashboard check and return what worked."""
        if what == "microphone":
            self.update({"input_device": self.store.get("input_device")})
            return "microphone"
        if what == "welcome":
            if self.buddy is None:
                raise SettingError("Open Sahur to see the companion on your desktop.")
            self.welcome()
            return "welcome"
        if what not in {"chime", "speaker"}:
            raise SettingError("Choose one of the sound checks on this page.")
        from bolos.audio import Player, earcon

        settings = self.settings()
        language = settings.user_language
        output = self.store.get("output_device")
        player = Player(self._device(output, "output"))
        if what == "speaker" and not settings.has_gnani:
            raise SettingError("Voice replies aren't set up. Try the chime to check your speakers, or ask the person "
                               "who set up Sahur to help with voice replies.")
        try:
            if what == "chime" and player.play(earcon("start"), 24000):
                return "chime"
            if what == "speaker":
                from bolos.responses import say

                text = say("speaker_test", language)
                if self._speak_test(settings, text, language, player):
                    return text
        except Exception:
            log.warning("The speaker check could not play", exc_info=True)
        if what == "speaker":
            raise SettingError("Sahur couldn't say the test sentence. Try the chime first. If you hear the chime, "
                               "check your internet connection or ask for help with the voice service.")
        raise SettingError("Sahur couldn't play the sound. Check your speaker and volume in Windows Sound settings, "
                           "then try again.")

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

    def _speak_test(self, settings: Settings, text: str, language: str, player) -> bool:
        if self.listener is not None:
            return self.listener.speak(text, language)
        from bolos.tts_cache import CachedTimbre

        return CachedTimbre(settings).speak(text, language, player)

    # Health

    def checks(self) -> list[Check]:
        """What to look at before a demo, worst first."""
        from bolos.audio import devices
        from bolos.wake import MODEL_PATH

        settings = self.settings()
        values = self.store.values()
        out = []
        if self.running:
            if self.dry_run:
                mode = "Sahur will show what it would do in practice mode."
            elif self.voice_commands:
                mode = "You can change your microphone and keyboard shortcut below."
            else:
                mode = "Open Sahur in voice mode to give it spoken commands."
            out.append(Check("running", "Sahur is open", "warn" if self.dry_run else "ok", mode))
        else:
            out.append(Check("running", "Sahur is closed", "fail",
                             "Open Sahur to give it voice commands. You can test your sound here first."))
        out.append(Check("gnani", "Voice service set up" if settings.has_gnani else "Voice service needs setup",
                         "ok" if settings.has_gnani else "fail",
                         "Play a test sentence to check that you can hear Sahur." if settings.has_gnani else
                         "Ask the person who set up Sahur to connect the Gnani speech service."))
        out.append(self._system2(settings))
        for kind, key, label in (("input", "input_device", "Microphone"), ("output", "output_device", "Speaker")):
            name = values[key]
            try:
                available = devices(kind)
            except Exception:
                log.debug("Could not check the %s devices", kind, exc_info=True)
                available = []
            if not available:
                out.append(Check(key, f"No {label.lower()} found", "fail",
                                 f"Connect a {label.lower()} and check Windows Sound settings. Then reopen Sahur."))
            elif name and not any(d.name == name for d in available) and self._device(name, kind) is None:
                out.append(Check(key, f"{label} not connected", "fail",
                                 f"Reconnect {name} and reopen Sahur, or choose Windows default below."))
            else:
                default = next((d.name for d in available if d.default), "")
                out.append(Check(key, label, "ok", name or (f"{default} (Windows default)" if default else
                                                              "Windows default")))
        listener = self.listener
        if listener is not None and listener.mic_error:
            out.append(Check("mic_error", "Check your microphone", "fail",
                             f"{listener.mic_error} Select Retry microphone when you're ready."))
        if listener is not None and getattr(listener, "hotkey_error", None):
            out.append(Check("hotkey_error", "Keyboard shortcut unavailable", "fail", listener.hotkey_error))
        out.append(Check("widget", "Sahur details", "ok" if values["widget_visible"] else "warn",
                         "Click Sahur to open" if values["widget_visible"] else "Disabled"))
        if values["screen_images"]:
            out.append(Check("screen_images", "Screen help can share screenshots", "warn",
                             "Close private chats and tabs before asking for help with your screen."))
        if settings.wake_enabled and not MODEL_PATH.exists():
            out.append(Check("wake", "Wake word unavailable", "warn", "Use your keyboard shortcut to speak."))
        order = {"fail": 0, "warn": 1, "ok": 2}
        return sorted(out, key=lambda c: order[c.level])

    def _system2(self, settings: Settings) -> Check:
        if settings.decider == "fast":
            return Check("system2", "Advanced help is off", "warn", "Sahur can still follow basic commands.")
        if settings.llm_provider == "codex":
            found = settings.codex_path or shutil.which("codex")
            if found:
                return Check("system2", "Advanced help set up", "ok", "Uses Codex for more complex requests.")
            return Check("system2", "Advanced help needs setup", "warn",
                         "Ask the person who set up Sahur to check the Codex connection.")
        if settings.has_llm:
            return Check("system2", "Advanced help set up", "ok", "Uses an AI service for more complex requests.")
        return Check("system2", "Advanced help needs setup", "warn",
                     "Ask the person who set up Sahur to connect the AI service for more complex requests.")
