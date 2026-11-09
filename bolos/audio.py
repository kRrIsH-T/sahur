"""Microphone, speaker and signal helpers: WAV I/O, an interruptible player, earcons and a local VAD.

The listener (bolos.listen) keeps one microphone stream open all the time; everything here is a building block
for it and is kept free of threads and policy so it can be tested on synthetic audio.
"""

import io
import logging
import re
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Iterable

import keyboard
import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512  # samples per microphone block: 32 ms, exactly one 1024-byte Prisma websocket frame
MAX_SECONDS = 29  # Prisma REST accepts clips up to 30 s
MIN_SECONDS = 0.3


def to_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(np.asarray(samples).astype(np.int16).tobytes())
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """(int16 mono samples, sample rate). Multi-channel audio is mixed down."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM, got {8 * width}-bit")
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples, rate


def resample(samples: np.ndarray, rate: int, target: int) -> np.ndarray:
    """Linear-interpolation resampling; good enough for speech going to a recogniser or a speaker."""
    if rate == target or len(samples) == 0:
        return np.asarray(samples, dtype=np.int16)
    n = int(round(len(samples) * target / rate))
    x = np.linspace(0, len(samples) - 1, n)
    return np.interp(x, np.arange(len(samples)), samples.astype(np.float64)).astype(np.int16)


def dbfs(samples: np.ndarray) -> float:
    """RMS level in dB relative to full scale (-100 for digital silence)."""
    if len(samples) == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return 20 * np.log10(rms / 32768) if rms > 0 else -100.0


def level(samples: np.ndarray, floor_db: float = -60.0, ceil_db: float = -12.0) -> float:
    """Microphone level for a meter, 0..1 on a dB scale (speech at arm's length sits around 0.5-0.8)."""
    return float(np.clip((dbfs(samples) - floor_db) / (ceil_db - floor_db), 0.0, 1.0))


# Local voice activity detection


try:
    import webrtcvad
except ImportError:  # the energy gate alone still endpoints clean speech
    webrtcvad = None


class NoiseFloor:
    """The room's background level in dBFS, by minimum statistics over 30 ms frames.

    It drops quickly to any quieter frame (quiet moments are the truth about the room) and rises only towards
    the quietest frame of the last `window_s`, once that much has been seen. Speech always has near-silent
    gaps within a few seconds, so talking does not drag the floor up and mute its own ending; a fan that never
    stops does, within a few seconds.
    """

    def __init__(self, db: float = -70.0, window_s: float = 3.0, frame_s: float = 0.03, rise: float = 0.02):
        self.db = db
        self.rise = rise
        self.recent: deque[float] = deque(maxlen=max(1, round(window_s / frame_s)))

    def update(self, frame_db: float) -> float:
        self.recent.append(frame_db)
        if frame_db < self.db:
            self.db += (frame_db - self.db) * 0.3
        elif len(self.recent) == self.recent.maxlen:
            quietest = min(self.recent)
            if quietest > self.db:
                self.db += (quietest - self.db) * self.rise
        return self.db

    def copy(self) -> "NoiseFloor":
        twin = NoiseFloor(self.db, rise=self.rise)
        twin.recent = deque(self.recent, maxlen=self.recent.maxlen)
        return twin


class Vad:
    """Is each 30 ms frame speech? WebRTC's VAD, gated by an adaptive noise floor.

    WebRTC's VAD alone fires on fans, keyboards and music; requiring the frame to also stand clearly above the
    room's own noise floor (tracked continuously) removes most of that, while quiet but real speech still
    passes because the floor follows the room, not a fixed level.
    """

    FRAME = 480  # 30 ms at 16 kHz, a size webrtcvad accepts

    def __init__(self, aggressiveness: int = 2, margin_db: float = 9.0, min_db: float = -58.0,
                 floor: NoiseFloor | None = None):
        self.webrtc = webrtcvad.Vad(aggressiveness) if webrtcvad else None
        self.margin_db = margin_db
        self.min_db = min_db
        # Start from the listener's running estimate (a copy), so a new turn already knows the room.
        self.floor = floor or NoiseFloor()
        self._pending = np.zeros(0, dtype=np.int16)

    @property
    def floor_db(self) -> float:
        return self.floor.db

    def feed(self, samples: np.ndarray) -> list[bool]:
        """One decision per complete 30 ms frame in `samples` (leftovers wait for the next call)."""
        return [self._decide(frame) for frame in self._frames(samples)]

    def track(self, samples: np.ndarray) -> None:
        """Follow the room's noise floor without deciding anything (cheap: no WebRTC call). The listener runs
        this on every frame between turns."""
        for frame in self._frames(samples):
            self.floor.update(dbfs(frame))

    def _frames(self, samples: np.ndarray) -> list[np.ndarray]:
        buf = np.concatenate([self._pending, np.asarray(samples, dtype=np.int16)])
        n = len(buf) // self.FRAME
        self._pending = buf[n * self.FRAME:]
        return [buf[i * self.FRAME:(i + 1) * self.FRAME] for i in range(n)]

    def _decide(self, frame: np.ndarray) -> bool:
        db = dbfs(frame)
        floor = self.floor.update(db)
        loud = db > max(self.min_db, floor + self.margin_db)
        if not loud:
            return False
        if self.webrtc is None:
            return True
        return self.webrtc.is_speech(frame.tobytes(), SAMPLE_RATE)


class Endpointer:
    """Turns per-frame speech decisions into "speech started" and "speech ended" events.

    Speech starts when most of the last ~300 ms is voiced (a click or a cough is too short) and ends after
    `end_silence_ms` without voice.
    """

    def __init__(self, frame_ms: float = 30.0, start_ms: float = 180.0, window_ms: float = 300.0,
                 end_silence_ms: float = 1000.0):
        self.frame_ms = frame_ms
        self.window: deque[bool] = deque(maxlen=max(1, round(window_ms / frame_ms)))
        self.start_frames = max(1, round(start_ms / frame_ms))
        self.end_frames = max(1, round(end_silence_ms / frame_ms))
        self.started = False
        self.ended = False
        self.silent_run = 0
        self.voiced_ms = 0.0

    def update(self, voiced: bool) -> str | None:
        """"start", "end" or None for one frame."""
        if self.ended:
            return None
        self.window.append(voiced)
        if voiced:
            self.voiced_ms += self.frame_ms
        if not self.started:
            if sum(self.window) >= self.start_frames:
                self.started = True
                self.silent_run = 0
                return "start"
            return None
        self.silent_run = 0 if voiced else self.silent_run + 1
        if self.silent_run >= self.end_frames:
            self.ended = True
            return "end"
        return None


# Playback


def earcon(kind: str, rate: int = 24000, volume: float = 0.12) -> np.ndarray:
    """A short, soft two-note chime: rising for "start" (listening), falling for "stop".

    Synthesised rather than shipped as files: a sine with a little second harmonic, a 6 ms attack and an
    exponential decay, so it sounds like a soft marimba tap rather than a beep.
    """
    notes = {"start": (659.25, 987.77), "stop": (987.77, 659.25)}[kind]  # E5 -> B5, or back
    parts = []
    for i, freq in enumerate(notes):
        dur = 0.075 if i == 0 else 0.11
        t = np.arange(int(dur * rate)) / rate
        tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t)
        env = np.minimum(1.0, t / 0.006) * np.exp(-t / (dur / 3.2))
        parts.append(tone * env)
    wave_ = np.concatenate(parts + [np.zeros(int(0.02 * rate))])
    return (wave_ / np.max(np.abs(wave_)) * volume * 32767).astype(np.int16)


class Player:
    """Plays int16 mono audio on the default speaker, in small blocks so `stop()` interrupts it quickly.

    One OutputStream per play call keeps it simple and lets earcons (played on their own thread) mix with
    speech on Windows' shared-mode mixer. `stop()` ends every play that started before it, never a later one.
    """

    BLOCK_S = 0.04

    def __init__(self, device: int | None = None):
        self.device = device  # a sounddevice output index (see devices()); None: the Windows default
        self._generation = 0
        self._busy = 0
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy > 0

    def stop(self) -> None:
        """Interrupt whatever is playing now (returns at once; playing threads end within ~50 ms)."""
        with self._lock:
            self._generation += 1

    def play(self, samples: np.ndarray, rate: int) -> bool:
        """Block until played. False if interrupted."""
        block = max(1, int(rate * self.BLOCK_S))
        samples = np.asarray(samples, dtype=np.int16)
        return self.play_stream((samples[i:i + block] for i in range(0, len(samples), block)), rate)

    def play_stream(self, chunks: Iterable[np.ndarray], rate: int, prebuffer_s: float = 0.0) -> bool:
        """Play chunks as they arrive (e.g. from streaming TTS). The first `prebuffer_s` of audio is collected
        before the speaker starts, so a slow network chunk does not cause an audible gap. False if
        interrupted."""
        with self._lock:
            generation = self._generation
            self._busy += 1
        stopped = lambda: self._generation != generation  # noqa: E731
        stream = None
        try:
            pending: list[np.ndarray] = []
            pending_n = 0
            for chunk in chunks:
                if stopped():
                    return False
                chunk = np.asarray(chunk, dtype=np.int16).reshape(-1)
                if stream is None:
                    pending.append(chunk)
                    pending_n += len(chunk)
                    if pending_n < prebuffer_s * rate:
                        continue
                    stream = _open_output(rate, self.device)
                    chunk = np.concatenate(pending)
                if not _write(stream, chunk, rate, stopped):
                    return False
            if stream is None and pending:  # all of it was shorter than the prebuffer
                stream = _open_output(rate, self.device)
                if not _write(stream, np.concatenate(pending), rate, stopped):
                    return False
            return not stopped()
        finally:
            if stream is not None:
                try:
                    if stopped():
                        stream.abort()
                    else:
                        stream.stop()  # waits for the queued audio to finish playing
                    stream.close()
                except sd.PortAudioError:
                    log.debug("closing the output stream failed", exc_info=True)
            with self._lock:
                self._busy -= 1

    def chime(self, kind: str) -> threading.Thread:
        """Play an earcon without blocking (it is ~0.2 s and mixes with anything else playing)."""
        tone = earcon(kind)
        thread = threading.Thread(target=_play_detached, args=(tone, 24000, self.device), daemon=True,
                                  name=f"earcon-{kind}")
        thread.start()
        return thread


def _open_output(rate: int, device: int | None = None):
    stream = sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=device)
    stream.start()
    return stream


def _write(stream, chunk: np.ndarray, rate: int, stopped) -> bool:
    block = max(1, int(rate * Player.BLOCK_S))
    for i in range(0, len(chunk), block):
        if stopped():
            return False
        stream.write(chunk[i:i + block].reshape(-1, 1))
    return True


def _play_detached(samples: np.ndarray, rate: int, device: int | None = None) -> None:
    try:
        with sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=device) as stream:
            stream.write(samples.reshape(-1, 1))
    except sd.PortAudioError:
        log.warning("Could not play an earcon", exc_info=True)


def play_wav(data: bytes, device: int | None = None) -> None:
    samples, rate = read_wav(data)
    Player(device).play(samples, rate)


def record_while_held(hotkey: str) -> bytes | None:
    """Block until the hotkey is pressed, record until it is released. None if the press was too short.

    The original push-to-talk path; bolos.listen.Listener supersedes it (it also handles taps, the wake word
    and follow-up answers) but this stays for the plain text-mode tools that only need one recording."""
    keyboard.wait(hotkey, suppress=True)
    chunks: list[np.ndarray] = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        callback=lambda data, *_: chunks.append(data.copy())):
        start = time.monotonic()
        while keyboard.is_pressed(hotkey) and time.monotonic() - start < MAX_SECONDS:
            time.sleep(0.02)
    if not chunks:
        return None
    samples = np.concatenate(chunks)[:, 0]
    if len(samples) < MIN_SECONDS * SAMPLE_RATE:
        return None
    return to_wav(samples)


# Choosing the microphone and speaker


@dataclass(frozen=True)
class Device:
    name: str  # the full name, as Windows' Sound settings show it
    index: int  # the sounddevice index to open it with
    default: bool  # Windows' default device for this direction


_SKIP_DEVICES = re.compile(r"sound mapper|primary sound", re.IGNORECASE)


def _plain(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def devices(kind: str) -> list[Device]:
    """The microphones (kind "input") or speakers ("output") Windows offers.

    They are opened through MME, the API every stream here already uses: shared with other apps, and Windows
    converts the sample rate. MME cuts names to 31 characters ("Microphone Array (Intel(R) Smart "), so the
    full name comes from the WASAPI list of the same devices. PortAudio reads the list once, when the process
    starts: a device plugged in later shows up after a restart."""
    channels = "max_input_channels" if kind == "input" else "max_output_channels"
    apis = sd.query_hostapis()
    every = sd.query_devices()
    mme = next((i for i, api in enumerate(apis) if api["name"] == "MME"), sd.default.hostapi)
    full_names = [every[i]["name"] for api in apis if api["name"] == "Windows WASAPI" for i in api["devices"]
                  if every[i][channels] > 0]
    default = apis[mme]["default_input_device" if kind == "input" else "default_output_device"]
    found, seen = [], set()
    for index in apis[mme]["devices"]:
        device = every[index]
        if device[channels] <= 0 or _SKIP_DEVICES.search(device["name"]):
            continue
        short = _plain(device["name"])
        name = next((full for full in full_names if short and _plain(full).startswith(short)), device["name"])
        if name not in seen:
            seen.add(name)
            found.append(Device(name.strip(), index, index == default))
    return found


def find_device(name: str, kind: str) -> Device | None:
    """The device with this name (as devices() gives it); None for "" (the Windows default) or when it is not
    connected now."""
    if not name:
        return None
    every = devices(kind)
    return (next((d for d in every if d.name == name), None)
            or next((d for d in every if _plain(name).startswith(_plain(d.name)) and _plain(d.name)), None))
