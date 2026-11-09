"""Hands-free listening: one always-open microphone, four ways to start a command, streaming recognition.

    mic (16 kHz, 32 ms frames) -> pump thread -> level meter (status "level", ~20/s while listening)
                                              -> 1.5 s ring buffer (pre-roll)
                                              -> wake detector (when idle and not speaking)
                                              -> the current Capture (local VAD, endpointing)
    next_command() / followup() -> Capture -> PrismaStream (websocket, transcribes while the user speaks)
                                           -> REST Prisma on the captured audio if the websocket fails

Triggers: hold the hotkey (records while held), tap it (< 0.4 s: listens until the user stops talking; a
second tap stops early), say "Hey Bol OS" (the ~1.5 s before the trigger is streamed too, so the transcript
starts with the wake phrase: if it does not, it was a false trigger and is ignored silently), or a follow-up
(after a question, listen at once). Half duplex: while Bol OS plays audio, and ~300 ms after, the microphone
is ignored, so it never hears itself; pressing the hotkey interrupts it (barge-in).

Measured (2026-11-04, this laptop, Timbre voice Shivani): Timbre SSE starts audio after ~0.44 s (median of 5
languages, 0.38-0.97 s) against ~1.38 s (0.98-2.42 s) for the whole REST clip, so uncached replies stream.

    uv run python -m bolos.listen          listen with the real microphone and hotkey, print what was heard
"""

import logging
import queue
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from bolos.audio import (
    FRAME,
    MAX_SECONDS,
    SAMPLE_RATE,
    Endpointer,
    NoiseFloor,
    Player,
    Vad,
    dbfs,
    level,
    read_wav,
    to_wav,
)
from bolos.config import Settings
from bolos.speech import Prisma, PrismaStream, SpeechError

log = logging.getLogger(__name__)

Status = Callable[[str, object], None]

TAP_S = 0.4  # a shorter hotkey press is a tap: listen until the user stops talking
PREROLL_WAKE_S = 1.5  # audio before a wake trigger that is streamed too (it holds the wake phrase)
PREROLL_HOTKEY_S = 0.3  # catches a first syllable spoken while the key goes down
NO_SPEECH_S = 5.0  # give up quietly if nobody speaks this long after a tap or wake word
WAKE_PAUSE_S = 6.0  # "Hey Bol OS" ... pause ... "Chrome kholo": wait this long for the command
MAX_SPEECH_S = 20.0  # then ask Prisma to transcribe what it has (commit)
MAX_CAPTURE_S = 60.0  # hard cap on one turn, whatever happens
LOCAL_END_SILENCE_MS = 1000  # local endpoint: a backstop behind the server's VAD (MIN_SILENCE_MS)
MIN_SILENCE_MS = 600  # the server closes an utterance after this much silence
BARRIER_WAIT_S = 4.0  # after the speech ends, how long to wait for the server's last transcript
MUTE_TAIL_S = 0.3  # the speaker's sound dies away (and echoes) for a moment after playback
LEVEL_INTERVAL_S = 0.05  # 20 level updates a second
EARCON_VAD_IGNORE_S = 0.35  # the start earcon is not the user starting to speak
MIN_HOLD_S = 0.6  # a held press this long is transcribed by REST even if the local VAD heard nothing
WAKE_KEYTERM = "Bol OS"
SQUEEZE_KEEP_S = 0.16  # every pause keeps at least this much silence (a natural gap between words)
SQUEEZE_LAG_S = 0.064  # squeeze only while the websocket is this far behind the microphone
SQUEEZE_MARGIN_DB = 6.0  # "quiet": below the room's noise floor plus this (the VAD needs +9 dB for speech)
SQUEEZE_QUIET_DB = -60.0  # and always quiet below this: the VAD never calls anything under -58 dBFS speech


# Wake phrase verification


_GREETINGS = ("hey", "hay", "hai", "hei", "he", "hi", "hee", "ey", "eh", "ay", "ae", "ai", "a", "e", "o", "i")


def _fold(text: str) -> str:
    """A rough phonetic spelling of romanised speech, so the ways Prisma writes "Bol OS" look alike:
    "Bowl OS", "Bol O.S.", "bolo es", "Bolls", "bholos" all fold to "bolos" or one letter from it."""
    s = re.sub(r"[^a-z]", "", text.lower())
    for a, b in (("bh", "b"), ("ph", "f"), ("oh", "o"), ("ou", "o"), ("ow", "o"), ("oa", "o"), ("au", "o"),
                 ("aw", "o"), ("ee", "i"), ("z", "s")):
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s)
    return re.sub(r"(?:oy?es|ose)$", "os", s)  # "bolo es", "bol o yes", "bolose"


_FOLDED_GREETINGS = tuple(sorted({_fold(g) for g in _GREETINGS}, key=len, reverse=True))


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_bolos(s: str) -> bool:
    """The folded name itself: starts like "bol" (b, or v/p as Prisma sometimes hears it), ends in the "s" of
    "OS" (so "bolo", Hindi for "speak", is not it), at most one letter away from "bolos"."""
    if len(s) < 4 or s[0] not in "bvp" or not s.endswith("s"):
        return False
    return _edit_distance("b" + s[1:], "bolos") <= 1


def match_wake(text: str, max_skip: int = 3) -> tuple[bool, str]:
    """Does the transcript start with "Hey Bol OS"? Returns (matched, the rest of the transcript after it).

    Tolerant of how Prisma romanises the phrase: "Hey Bolos", "he bolo es", "a bol OS", "Hey, Bowl O.S.",
    "heybolos", "Bol OS" alone. Up to `max_skip` words may come first: the pre-roll can catch the tail of
    something said just before. Near misses fail: "hey bolo", "hey boss", "bolo na".
    """
    words = list(re.finditer(r"\w+", text))
    for skip in range(min(max_skip, len(words)) + 1):
        for k in range(1, min(5, len(words) - skip) + 1):
            joined = _fold("".join(m.group() for m in words[skip:skip + k]))
            if any(joined.startswith(g) and _is_bolos(joined[len(g):]) for g in ("",) + _FOLDED_GREETINGS):
                rest = text[words[skip + k].start():].strip() if skip + k < len(words) else ""
                return True, rest
    return False, text


def clean_transcript(text: str) -> str:
    """Prisma's text without the bits that are not words (a lone "." for a cough)."""
    text = (text or "").strip()
    return text if re.search(r"\w", text) else ""


class SilenceSqueezer:
    """Shortens long silences in audio that is queued behind the live microphone.

    Prisma wants frames at real-time cadence, never bursts. But a turn's first frames are behind before the
    first byte goes out: the pre-roll (0.3 s for the hotkey, 1.5 s for the wake word) plus the websocket
    handshake (~0.35 s). Sent strictly in real time, that lag never shrinks, and the transcript arrives that
    much later after the user stops (measured: 1.7 s after a tap, 3.3 s after a wake command). Silence
    carries nothing, so while the stream is more than `lag_s` behind, every quiet run longer than `keep_s`
    loses its excess frames: the leading silence of a turn, the pause after "Hey Bol OS", a breath between
    phrases. Each frame still goes out at the 32 ms cadence; the server simply hears shorter pauses, and
    once the stream is live nothing is dropped, so the server's own end-of-speech silence is untouched.
    """

    def __init__(self, keep_s: float = SQUEEZE_KEEP_S, lag_s: float = SQUEEZE_LAG_S,
                 margin_db: float = SQUEEZE_MARGIN_DB):
        self.keep = max(1, round(keep_s * SAMPLE_RATE / FRAME))
        self.lag_bytes = lag_s * SAMPLE_RATE * 2
        self.margin_db = margin_db
        self.quiet_run = 0
        self.queued_bytes = 0  # handed to the stream so far
        self.dropped = 0  # frames

    def take(self, frames: list[np.ndarray], floor_db: float, sent_bytes: int) -> bytes:
        """The PCM of `frames` to queue, given the noise floor and how much the stream has sent so far."""
        out = []
        for frame in frames:
            # The floor estimate needs a few seconds of room to settle (it starts at -70 dBFS), so a turn
            # right after start-up still counts the room's hiss as quiet through the absolute bound.
            quiet = dbfs(frame) < max(floor_db + self.margin_db, SQUEEZE_QUIET_DB)
            self.quiet_run = self.quiet_run + 1 if quiet else 0
            if quiet and self.quiet_run > self.keep and self.queued_bytes - sent_bytes > self.lag_bytes:
                self.dropped += 1
                continue
            pcm = frame.tobytes()
            out.append(pcm)
            self.queued_bytes += len(pcm)
        return b"".join(out)

    @property
    def dropped_ms(self) -> float:
        return self.dropped * FRAME / SAMPLE_RATE * 1000


# One listening turn


@dataclass(frozen=True)
class Heard:
    """What the user said in one turn.

    timings (ms): speech_end_to_transcript (the latency the user feels), trigger_to_transcript, connect
    (websocket handshake), speech (voiced audio seen by the local VAD), squeezed (silence not streamed
    because the websocket was behind, see SilenceSqueezer)."""

    text: str
    trigger: str  # "hotkey" (held), "tap", "wake" or "followup"
    via: str  # "stream" (websocket) or "rest" (fallback)
    timings: dict[str, float] = field(default_factory=dict)
    wav: bytes = field(default=b"", repr=False)  # the captured audio, pre-roll included


@dataclass(frozen=True)
class _Fallback:
    """The websocket failed mid-turn: REST replaces every segment using the complete captured audio."""

    prefix: list[str]


class Capture:
    """The audio of one turn, filled by the pump thread, endpointed by a local VAD.

    `hold` is true while the hotkey decides it (held: the turn ends on release). Otherwise the turn ends when
    the local VAD hears `LOCAL_END_SILENCE_MS` of silence after speech, when nobody speaks within the
    no-speech timeout, or when the session ends it (the server's transcript arrived, a false wake trigger).
    Frames keep arriving after the end until the session lets go, so a turn can be reopened (the wake phrase
    alone, then a pause) without losing audio.
    """

    def __init__(self, trigger: str, preroll: list[np.ndarray], now: float, no_speech_s: float,
                 hold: bool = False, vad_ignore_s: float = 0.0, floor: NoiseFloor | None = None):
        self.trigger = trigger
        self.hold = hold
        self.frames: list[np.ndarray] = list(preroll)
        self.n_preroll = len(preroll)
        self.started = now
        self.vad = Vad(floor=floor)
        self.endpointer = Endpointer(end_silence_ms=LOCAL_END_SILENCE_MS)
        self.no_speech_deadline = now + no_speech_s
        self.ignore_until = self._ignore_until(vad_ignore_s)  # frame index: counted in audio, not wall time
        self.speech_started: float | None = None
        self.last_voice: float | None = None
        self.reason: str | None = None  # why it ended
        self.ended_at: float | None = None
        self.end_index: int | None = None
        self.released_at: float | None = None
        self.max_length = False  # voiced longer than MAX_SPEECH_S: the session commits once
        self.lock = threading.Lock()

    @property
    def ended(self) -> bool:
        return self.reason is not None

    def _ignore_until(self, seconds: float) -> int:
        """The VAD skips the next `seconds` of audio (an earcon playing into the microphone)."""
        return len(self.frames) + int(round(seconds * SAMPLE_RATE / FRAME))

    def add(self, frame: np.ndarray, now: float) -> bool:
        """Append one frame. True when this frame started speech (for the "hearing" state)."""
        with self.lock:
            index = len(self.frames)
            self.frames.append(frame)
            if self.ended:
                return False
            started = False
            if index < self.ignore_until:
                decisions = [False] * len(self.vad.feed(frame))
            else:
                decisions = self.vad.feed(frame)
            for voiced in decisions:
                if voiced:
                    self.last_voice = now
                event = self.endpointer.update(voiced)
                if event == "start":
                    self.speech_started = now
                    started = True
                elif event == "end" and not self.hold:
                    self._end("endpoint", now)
                    return started
            if self.endpointer.voiced_ms >= MAX_SPEECH_S * 1000:
                self.max_length = True
            self._check_deadlines(now)
            return started

    def tick(self, now: float) -> None:
        """Apply the time limits even when no audio arrives (a microphone that stopped delivering)."""
        with self.lock:
            if not self.ended:
                self._check_deadlines(now)

    def _check_deadlines(self, now: float) -> None:
        if not self.hold and not self.endpointer.started and now >= self.no_speech_deadline:
            self._end("no_speech", now)
        elif now - self.started >= MAX_CAPTURE_S:
            self._end("max_capture", now)

    def end(self, reason: str, now: float | None = None) -> None:
        with self.lock:
            if not self.ended:
                self._end(reason, time.monotonic() if now is None else now)

    def _end(self, reason: str, now: float) -> None:
        self.reason = reason
        self.ended_at = now
        self.end_index = len(self.frames)

    def release(self, now: float, tap: bool) -> None:
        """The hotkey went up: a tap keeps listening (VAD endpointing from here), a hold ends the turn."""
        with self.lock:
            self.hold = False
            if tap:
                self.trigger = "tap"
                self.no_speech_deadline = max(self.no_speech_deadline, now + NO_SPEECH_S)
            else:
                self.released_at = now
                if not self.ended:
                    self._end("released", now)

    def reopen(self, now: float, no_speech_s: float, vad_ignore_s: float = 0.0) -> None:
        """Listen on after the wake phrase alone: a fresh endpointer and no-speech timeout."""
        with self.lock:
            self.reason = self.ended_at = self.end_index = None
            self.endpointer = Endpointer(end_silence_ms=LOCAL_END_SILENCE_MS)
            self.no_speech_deadline = now + no_speech_s
            self.ignore_until = self._ignore_until(vad_ignore_s)
            self.last_voice = None

    def take(self, cursor: int) -> list[np.ndarray]:
        """Frames from `cursor` up to now (or up to the end, once ended)."""
        with self.lock:
            stop = self.end_index if self.end_index is not None else len(self.frames)
            return self.frames[cursor:stop]

    @property
    def speech_end(self) -> float | None:
        """When the user stopped speaking, as well as we know: the last voiced frame, else the end."""
        return self.last_voice or self.released_at or self.ended_at

    def audio(self) -> np.ndarray:
        with self.lock:
            stop = self.end_index if self.end_index is not None else len(self.frames)
            frames = self.frames[:stop]
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)


@dataclass(frozen=True)
class _ReleaseCapture:
    capture: Capture
    at: float
    tap: bool


class Listener:
    """Owns the microphone, the hotkey and the speaker for the app. Thread-safe: next_command(), followup()
    and listen_now() serialise (one turn at a time); speak() may be called from any thread.

        listener = Listener(settings, keyterms, status, wake=WakeDetector.load(path))
        listener.start()
        while True:
            heard = listener.next_command()       # blocks until a trigger; None if nothing usable was said
            ...
            listener.speak("Bhej doon?", "hi")    # mic ignored meanwhile
            answer = listener.followup(6.0)
    """

    def __init__(self, settings: Settings, keyterms=(), status: Status | None = None, wake=None, *,
                 hotkey: str | None = None, use_mic: bool = True, use_hotkey: bool = True, tts=None,
                 stream_factory: Callable[[list[str]], PrismaStream] | None = None, rest=None,
                 player: Player | None = None, earcons: bool = True, min_silence_ms: int = MIN_SILENCE_MS,
                 squeeze: bool = True, input_device: int | None = None):
        self.settings = settings
        self.input_device = input_device  # a sounddevice input index; None: the Windows default
        self.keyterms = list(keyterms)
        self.status = status or (lambda *_: None)
        self.wake = wake
        self.hotkey = hotkey or settings.hotkey
        self.use_mic = use_mic
        self.use_hotkey = use_hotkey
        self.player = player or Player()
        self.earcons = earcons
        self.min_silence_ms = min_silence_ms
        self.squeeze = squeeze  # shorten silences while the websocket lags the microphone (SilenceSqueezer)
        if tts is None and settings.has_gnani:
            from bolos.tts_cache import CachedTimbre

            tts = CachedTimbre(settings)
        self.tts = tts
        self.rest = rest or Prisma(settings)
        self._stream_factory = stream_factory or (
            lambda terms: PrismaStream(self.settings, terms, min_silence_ms=self.min_silence_ms))

        self._frames: queue.Queue = queue.Queue()
        self._lock = threading.Lock()  # guards the capture, the ring and the mute state
        self._ring: deque[np.ndarray] = deque(maxlen=int(np.ceil(PREROLL_WAKE_S * SAMPLE_RATE / FRAME)))
        self._noise = NoiseFloor()  # tracks the room's noise floor between turns, to seed each turn's VAD
        self._capture: Capture | None = None
        self._capture_ready = threading.Event()
        self._turn = threading.Lock()
        self._speaking = 0
        self._private_speaking = 0
        self._private_mute_until = 0.0
        self._interrupt = threading.Event()
        self._mute_until = 0.0
        self._was_muted = False
        self._last_level = 0.0
        self._level_peak = 0.0
        self.mic_level = 0.0  # 0..1, decaying peak of every frame (the dashboard's meter), even when idle
        self.mic_error: str | None = None  # why the microphone could not be opened, if it could not
        self._mic_lock = threading.Lock()
        self._hotkey_down = False
        self._key_pressed = None  # set in start(); tests replace it
        self._hotkey = None
        self._mic = None
        self._pump: threading.Thread | None = None
        self._running = False
        self._private = 0  # private form answers must never reach transcript logs
        self._turn_terms: list[str] | None = None  # keyterms for this turn only (Listener.private)

    # Lifecycle

    def start(self) -> "Listener":
        if self._running:
            return self
        self._running = True
        self._pump = threading.Thread(target=self._pump_loop, daemon=True, name="listen-pump")
        self._pump.start()
        try:
            self._start_devices()
        except BaseException:
            self.close()
            raise
        return self

    def _start_devices(self) -> None:
        if self.use_mic:
            self._open_mic(self.input_device)
        if self.use_hotkey:
            from bolos import hotkey

            self._key_pressed = self._key_pressed or (lambda: hotkey.is_down(self.hotkey))
            self._hotkey = hotkey.GlobalHotkey(self.hotkey, self._on_hotkey)
            self._hotkey.start()

    def _open_mic(self, device: int | None) -> None:
        import sounddevice as sd

        def callback(data, frames, _time, flags):
            if flags.input_overflow:
                log.debug("microphone overflow")
            self._frames.put(data[:, 0].copy())

        mic = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                             callback=callback, device=device)
        mic.start()
        self._mic, self.input_device, self.mic_error = mic, device, None

    def set_input_device(self, device: int | None) -> None:
        """Listen with another microphone from now on (None: the Windows default). If it cannot be opened,
        the one before stays (and mic_error says why)."""
        if not self.use_mic or not self._running:
            self.input_device = device
            return
        with self._mic_lock:
            old = self._mic
            try:
                self._open_mic(device)
            except Exception as e:
                self.mic_error = str(e) or type(e).__name__
                log.warning("Could not switch the microphone: %s", self.mic_error)
                raise
            if old is not None:
                for step in (old.stop, old.close):
                    try:
                        step()
                    except Exception:
                        log.debug("closing the old microphone failed", exc_info=True)
            with self._lock:
                self._ring.clear()  # the old microphone's audio is not the pre-roll of the new one

    def set_hotkey(self, combination: str) -> None:
        """Use another push-to-talk combination from now on. HotkeyError (the old one stays) if another
        program already has it."""
        if combination == self.hotkey:
            return
        from bolos import hotkey

        if not self.use_hotkey or not self._running or hotkey.parse(combination) == hotkey.parse(self.hotkey):
            self.hotkey = combination  # the same keys spelled differently stay registered as they are
            return
        new = hotkey.GlobalHotkey(combination, self._on_hotkey)
        new.start()  # raises HotkeyError before anything changed
        old, self._hotkey = self._hotkey, new
        self.hotkey = combination
        if old is not None:
            old.stop()

    def update_settings(self, settings: Settings) -> None:
        """New settings (voice, languages to recognise) for the next turn and reply."""
        self.settings = settings
        for user in (self.rest, self.tts, getattr(self.tts, "timbre", None)):
            if user is not None and hasattr(user, "s"):
                user.s = settings

    def close(self) -> None:
        self._running = False
        self._interrupt.set()
        with self._lock:
            if self._capture is not None:
                self._capture.end("closed")
            self._capture_ready.set()  # release a caller blocked in next_command()
        self._frames.put(None)
        # The overlay thread and command loop may both finish at once. Detach before closing so a
        # second close cannot stop or close the same PortAudio stream concurrently.
        mic, self._mic = self._mic, None
        if mic is not None:
            try:
                mic.stop()
            except Exception:
                log.warning("Microphone stop failed during shutdown")
            try:
                mic.close()
            except Exception:
                log.warning("Microphone close failed during shutdown")
        registered, self._hotkey = self._hotkey, None
        if registered is not None:
            registered.stop()
        self.player.stop()
        if self._pump is not None and self._pump is not threading.current_thread():
            self._pump.join(timeout=1.0)
        self._pump = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def feed(self, samples: np.ndarray) -> None:
        """Audio as if from the microphone (tests and benchmarks run with use_mic=False). Pace it yourself."""
        samples = np.asarray(samples, dtype=np.int16)
        for i in range(0, len(samples) - FRAME + 1, FRAME):
            self._frames.put(samples[i:i + FRAME])

    # The pump: every microphone frame passes through here, on one thread

    def _pump_loop(self) -> None:
        while True:
            frame = self._frames.get()
            if frame is None or not self._running:
                return
            try:
                if isinstance(frame, _ReleaseCapture):
                    frame.capture.release(frame.at, frame.tap)
                    if frame.tap:
                        self.status("trigger", "tap")
                    continue
                self._on_frame(frame, time.monotonic())
            except Exception:
                log.exception("Listener failed on a microphone frame")

    def _muted(self, now: float) -> bool:
        return self._speaking > 0 or now < self._mute_until

    def _on_frame(self, frame: np.ndarray, now: float) -> None:
        trigger_wake = False
        self.mic_level = max(level(frame), self.mic_level * 0.8)
        with self._lock:
            cap = self._capture
            muted = self._muted(now)
            private_audio = self._private_speaking > 0 or now < self._private_mute_until
            if cap is not None and not private_audio and (not muted or cap.trigger in ("hotkey", "tap")):
                if cap.add(frame, now):
                    self.status("state", "hearing")
                if not cap.ended:
                    self._meter(frame, now)
            if muted:
                self._was_muted = True
                return
            if self._was_muted:  # Bol OS just stopped talking: forget what the mic heard of it
                self._was_muted = False
                self._ring.clear()
                self._reset_wake()
            self._ring.append(frame)
            if cap is None:
                self._noise.update(dbfs(frame))
            if cap is None and self.wake is not None:
                try:
                    trigger_wake = bool(self.wake.feed(frame))
                except Exception:
                    log.exception("Wake detector failed; wake word is off")
                    self.wake = None
            if trigger_wake:
                self._begin("wake", PREROLL_WAKE_S, WAKE_PAUSE_S, now)
        if trigger_wake:
            log.info("wake word detected")

    def _meter(self, frame: np.ndarray, now: float) -> None:
        self._level_peak = max(self._level_peak, level(frame))
        if now - self._last_level >= LEVEL_INTERVAL_S:
            self.status("level", round(self._level_peak, 3))
            self._last_level = now
            self._level_peak = 0.0

    def _reset_wake(self) -> None:
        if self.wake is not None:
            try:
                self.wake.reset()
            except Exception:
                log.exception("Wake detector reset failed")

    def _begin(self, trigger: str, preroll_s: float, no_speech_s: float, now: float, hold: bool = False,
               vad_ignore_s: float = 0.0) -> Capture | None:
        """Start a capture (caller holds self._lock). None if one is already running or waiting."""
        if self._capture is not None:
            return None
        n = int(round(preroll_s * SAMPLE_RATE / FRAME))
        preroll = list(self._ring)[-n:] if n else []
        self._capture = Capture(trigger, preroll, now, no_speech_s, hold=hold, vad_ignore_s=vad_ignore_s,
                                floor=self._noise.copy())
        self._capture_ready.set()
        self.status("trigger", trigger)
        self.status("state", "listening")
        return self._capture

    def _finish(self, cap: Capture) -> None:
        with self._lock:
            if self._capture is cap:
                self._capture = None
                self._capture_ready.clear()
            self._ring.clear()
            self._reset_wake()

    # Hotkey (called on the keyboard hook's thread: must return quickly)

    def _on_hotkey(self) -> None:
        if not self._running or self._hotkey_down:  # key auto-repeat while held
            return
        self._hotkey_down = True
        threading.Thread(target=self._hotkey_turn, daemon=True, name="hotkey").start()

    def _hotkey_turn(self) -> None:
        try:
            press = time.monotonic()
            with self._lock:
                cap = self._capture
                if cap is not None and not cap.ended and not cap.hold:
                    # Stop after frames already queued by the microphone. Ending immediately on this
                    # thread could discard the last words when the audio pump is briefly busy.
                    self._frames.put(_ReleaseCapture(cap, press, False))
                    cap = None
                    new = None
                elif cap is not None:
                    new = None  # a turn is still being transcribed
                else:
                    self._interrupt.set()
                    self.player.stop()  # barge-in: stop talking, the user wants to speak
                    new = self._begin("hotkey", PREROLL_HOTKEY_S, NO_SPEECH_S, press, hold=True,
                                      vad_ignore_s=EARCON_VAD_IGNORE_S if self.earcons else 0.0)
            if new is not None and self.earcons:
                self.player.chime("start")
            pressed = self._key_pressed or (lambda: False)
            while self._running and pressed() and time.monotonic() - press < MAX_CAPTURE_S:
                time.sleep(0.01)
            if new is not None:
                now = time.monotonic()
                if now - press >= MAX_CAPTURE_S:
                    new.end("max_capture", now)
                    return
                tap = now - press < TAP_S
                self._frames.put(_ReleaseCapture(new, now, tap))
        finally:
            self._hotkey_down = False

    # Half duplex output

    @contextmanager
    def private(self, hints=()):
        """Do not log recognized personal details during this listening turn. `hints`, when given, replace the
        command keyterms for the turn: the words this one answer is likely to contain (common names for a name
        field, cities for a city), not app and contact names."""
        self._private += 1
        previous = self._turn_terms
        if hints:
            self._turn_terms = list(hints)
        try:
            yield
        finally:
            self._private -= 1
            self._turn_terms = previous

    def _keyterms(self) -> list[str]:
        return list(self._turn_terms) if self._turn_terms is not None else list(self.keyterms)

    @contextmanager
    def speaking(self, private: bool = False):
        """While inside, the microphone is ignored (plus MUTE_TAIL_S after), and the wake detector restarts
        after, so Bol OS never hears or triggers on itself."""
        with self._lock:
            if self._speaking == 0:
                self._interrupt.clear()
            self._speaking += 1
            self._private_speaking += int(private)
            self._ring.clear()  # a barge-in's pre-roll must not reach back to before Bol OS spoke
        try:
            yield self._interrupt
        finally:
            with self._lock:
                self._speaking -= 1
                self._private_speaking -= int(private)
                self._mute_until = time.monotonic() + MUTE_TAIL_S
                if private:
                    self._private_mute_until = self._mute_until

    def speak(self, what: bytes | str, language: str | None = None) -> bool:
        """Say a WAV clip (bytes) or text (from the reply cache, else Timbre, streamed). Blocks until done.
        False if it was interrupted (the user pressed the hotkey) or could not be spoken."""
        with self.speaking():
            if isinstance(what, (bytes, bytearray)):
                samples, rate = read_wav(bytes(what))
                return self.player.play(samples, rate)
            if self.tts is None:
                log.warning("No Gnani key: spoken reply unavailable")
                return False
            self.status("speaking", True)
            try:
                return self.tts.speak(what, language, self.player)
            except Exception:
                log.warning("Spoken reply unavailable")
                return False
            finally:
                self.status("speaking", False)

    def prewarm(self, phrases) -> threading.Thread | None:
        """Fill the reply cache with (text, language) pairs in the background."""
        return self.tts.prewarm(phrases) if self.tts is not None else None

    # Turns

    def next_command(self, timeout: float | None = None) -> Heard | None:
        """Wait for a trigger (hotkey, tap or wake word) and return what was said. None when nothing usable
        was said (a false wake trigger, silence, an empty transcript) or on timeout."""
        if not self._capture_ready.wait(timeout):
            return None
        with self._turn:
            if not self._running:
                return None
            cap = self._capture
            if cap is None:
                return None
            return self._run(cap)

    def followup(self, timeout_s: float = 6.0) -> Heard | None:
        """Listen right now, without a trigger (after a question such as "send it?"). None if nobody speaks
        within `timeout_s`."""
        return self.listen_now("followup", timeout_s)

    def listen_now(self, trigger: str = "tap", timeout_s: float = NO_SPEECH_S) -> Heard | None:
        """Start a turn now and end it when the user stops speaking (record_set.py and follow-ups)."""
        with self._turn:
            if not self._running:
                return None
            with self._lock:
                cap = self._capture  # the user may already have pressed the hotkey
            if cap is None:
                # The chime is not the user speaking (the local VAD skips it; Prisma's VAD ignores it, checked
                # live), so listening starts at once and an answer said over the chime is not lost.
                with self._lock:
                    cap = self._begin(trigger, 0.0, timeout_s, time.monotonic(),
                                      vad_ignore_s=EARCON_VAD_IGNORE_S if self.earcons else 0.0)
                    if cap is not None and self.earcons:
                        self.player.chime("start")
                    cap = cap or self._capture
            return self._run(cap)

    def _run(self, cap: Capture) -> Heard | None:
        try:
            heard = self._transcribe(cap)
        except Exception:
            if self._private:
                log.warning("Listening failed for a private answer")
            else:
                log.exception("Listening failed")
            heard = None
        finally:
            cap.end("done")
            self._finish(cap)
        if heard is None:
            self.status("state", "ready")
        elif not self._private:
            log.info("heard (%s, %s, %.0f ms after speech end): %s", heard.trigger, heard.via,
                     heard.timings.get("speech_end_to_transcript", -1), heard.text)
        return heard

    def _transcribe(self, cap: Capture) -> Heard | None:
        terms = self._keyterms() + ([WAKE_KEYTERM] if cap.trigger == "wake" else [])
        stream = self._stream_factory(terms)
        try:
            stream.open()
        except SpeechError as e:
            if self._private:
                log.warning("Private speech stream unavailable; using REST instead")
            else:
                log.warning("%s; transcribing with REST instead", e)
            return self._transcribe_rest(cap)
        result = None
        try:
            result = self._stream_turn(cap, stream)
        finally:
            # With the transcript in hand the caller need not wait for the socket to close. Before a REST
            # fallback it must: Gnani allows one active request per key.
            stream.close(wait=isinstance(result, _Fallback))
        if isinstance(result, _Fallback):
            return self._transcribe_rest(cap, prefix=result.prefix)
        return result

    def _stream_turn(self, cap: Capture, stream: PrismaStream) -> "Heard | None | _Fallback":
        """Feed the capture to the websocket and collect transcripts until the turn is over.

        A tap, wake or follow-up turn ends at the server's first transcript (its VAD closes the utterance
        ~0.6 s after the user stops). A held turn collects every transcript until release. Whenever the
        capture ends any other way (release, local endpoint, no speech), the rest of the audio is flushed and
        committed, followed by a no-op settings update: the server answers it in order, so once
        "settings_updated" is back, every utterance the server opened is known and only their transcripts
        are left to wait for."""
        events = stream.events()
        cursor = 0
        squeezer = SilenceSqueezer(lag_s=SQUEEZE_LAG_S if self.squeeze else float("inf"))
        segments: list[str] = []
        open_utterances: set[str] = set()
        verified = cap.trigger != "wake"
        rejected_once = False
        flushed = barrier = False
        committed_long = False
        heard_state = False
        transcript_at: float | None = None
        hard_deadline = cap.started + MAX_CAPTURE_S + BARRIER_WAIT_S + 5
        barrier_deadline: float | None = None

        while True:
            if not self._running:
                return None
            now = time.monotonic()
            cap.tick(now)
            if cap.reason in {"closed", "max_capture"}:
                return None  # capture was interrupted, so its text would be an incomplete command
            frames = cap.take(cursor)
            if frames:
                pcm = squeezer.take(frames, cap.vad.floor_db, stream.sent_bytes)
                if pcm:
                    stream.send_audio(pcm)
                cursor += len(frames)
            if cap.max_length and not committed_long:
                committed_long = True
                stream.commit()
            if cap.ended and not flushed and cursor >= (cap.end_index or 0):
                flushed = True
                if cap.reason not in ("server", "rejected"):
                    if cap.reason not in ("no_speech", "max_capture"):
                        if self.earcons:
                            self.player.chime("stop")
                        self.status("state", "transcribing")
                    stream.flush()
                    stream.commit()
                    stream.send_control({"type": "update_settings", "format": self.settings.stt_format})
                    # Pre-roll still travels at real-time cadence. Start the response allowance after
                    # the audio backlog can have reached the server, rather than timing out its FIFO.
                    queued_s = max(0, squeezer.queued_bytes - stream.sent_bytes) / (SAMPLE_RATE * 2)
                    barrier_deadline = now + queued_s + BARRIER_WAIT_S
            if barrier and not open_utterances:
                break
            if (barrier_deadline is not None and now > barrier_deadline) or now > hard_deadline:
                log.warning("Prisma did not finish the utterance in time")
                return _Fallback(segments)  # an earlier segment cannot stand in for an unfinished command

            try:
                ev = events.get(timeout=0.01)
            except queue.Empty:
                continue
            kind = ev.get("type")
            if kind == "speech_start":
                open_utterances.add(ev.get("utterance_id") or "")
                if not heard_state:
                    heard_state = True
                    self.status("state", "hearing")
            elif kind == "settings_updated" and flushed:
                barrier = True
            elif kind == "transcript":
                if ev.get("is_final") is False:
                    continue
                open_utterances.discard(ev.get("utterance_id") or "")
                text = clean_transcript(ev.get("text", ""))
                if not text:
                    continue
                transcript_at = ev.get("_t", time.monotonic())
                if not verified:
                    matched, text = match_wake(text)
                    if not matched:
                        if open_utterances and not rejected_once:
                            # The pre-roll caught the tail of earlier speech as its own segment; the wake
                            # phrase is the utterance still open. Check that one before giving up.
                            rejected_once = True
                            continue
                        if not self._private:
                            log.info("False wake trigger, heard: %r", ev.get("text"))
                        cap.end("rejected")
                        return None
                    verified = True
                    if not text:  # the wake phrase alone: the command comes after a pause
                        cap.reopen(time.monotonic(), WAKE_PAUSE_S,
                                   vad_ignore_s=EARCON_VAD_IGNORE_S if self.earcons else 0.0)
                        flushed = barrier = False
                        barrier_deadline = None
                        transcript_at = None
                        if self.earcons:
                            self.player.chime("start")  # "yes?": the wake word was heard, say the command
                        self.status("state", "listening")
                        continue
                segments.append(text)
                if not cap.ended and not cap.hold and cap.trigger != "hotkey":
                    cap.end("server")
                    if self.earcons:
                        self.player.chime("stop")
                    break
                if cap.ended and cap.reason in ("server",):
                    break
            elif kind == "error":
                log.warning("Prisma stream error: %s", "[private]" if self._private else ev.get("message"))
            elif kind == "closed":
                log.warning("Prisma stream closed: %s", "[private]" if self._private else ev.get("reason"))
                return _Fallback(segments)

        if not segments:
            return None
        heard = self._heard(" ".join(segments), cap, "stream", transcript_at, stream.connect_ms)
        heard.timings["squeezed"] = round(squeezer.dropped_ms, 1)
        return heard

    def _transcribe_rest(self, cap: Capture, prefix: list[str] = ()) -> Heard | None:
        """The websocket failed: wait for the local endpoint, then send the captured audio to REST Prisma."""
        while not cap.ended:
            cap.tick(time.monotonic())
            time.sleep(0.02)
        if not self._running:
            return None
        cap.end("rest")
        audio = cap.audio()
        if cap.reason in {"closed", "max_capture"} or len(audio) > MAX_SECONDS * SAMPLE_RATE:
            log.warning("Capture was interrupted or exceeds the REST limit; please use a shorter command")
            return None
        if not prefix and not cap.endpointer.started:
            # Nobody spoke, as far as the local VAD can tell. A held key is the user's own word that they
            # did speak, so a long enough hold is transcribed anyway (quiet voices, far from the mic).
            if cap.trigger != "hotkey" or len(audio) < MIN_HOLD_S * SAMPLE_RATE:
                return None
        self.status("state", "transcribing")
        try:
            terms = self._keyterms() + ([WAKE_KEYTERM] if cap.trigger == "wake" else [])
            clip = to_wav(audio)
            text = clean_transcript(self.rest.transcribe(clip, keyterms=terms).text)
        except Exception as e:
            log.warning("Prisma REST failed too: %s", "[private]" if self._private else e)
            return None
        transcript_at = time.monotonic()
        if cap.trigger == "wake" and text:
            matched, text = match_wake(text)
            if not matched:
                if not self._private:
                    log.info("False wake trigger (REST), heard: %r", text)
                return None
        # REST saw the entire capture, including every earlier stream segment. Prepending `prefix`
        # would duplicate instructions; empty/failed REST must never resurrect a partial stream result.
        return self._heard(text, cap, "rest", transcript_at, None) if text else None

    def _heard(self, text: str, cap: Capture, via: str, transcript_at: float | None,
               connect_ms: float | None) -> Heard:
        transcript_at = transcript_at or time.monotonic()
        timings = {"trigger_to_transcript": (transcript_at - cap.started) * 1000,
                   "speech": cap.endpointer.voiced_ms}
        if cap.speech_end is not None:
            timings["speech_end_to_transcript"] = (transcript_at - cap.speech_end) * 1000
        if connect_ms is not None:
            timings["connect"] = connect_ms
        return Heard(text=text, trigger=cap.trigger, via=via,
                     timings={k: round(v, 1) for k, v in timings.items()}, wav=to_wav(cap.audio()))


def main() -> None:
    """Listen with the real microphone and hotkey and print each turn (no commands are run)."""
    import sys

    from bolos.config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    if not settings.has_gnani:
        sys.exit("Needs GNANI_API_KEY in .env")
    wake = None
    try:
        from bolos.wake import WakeDetector

        wake = WakeDetector.load()
    except Exception as e:  # no wake module or model yet
        log.info("Wake word off: %s", e)
    def status(kind, value):
        if kind != "level":
            print(f"  [{kind}] {value}")

    with Listener(settings, status=status, wake=wake) as listener:
        print(f"Hold or tap {settings.hotkey}{', or say Hey Bol OS' if wake else ''}. Ctrl+C to stop.")
        try:
            while True:
                heard = listener.next_command()
                if heard:
                    print(f"you ({heard.trigger}, {heard.via}): {heard.text}  {heard.timings}")
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
