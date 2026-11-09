"""Gnani Prisma (speech to text) and Timbre (text to speech).

Prisma: REST for a finished recording, and a realtime websocket (PrismaStream) that transcribes while the user
is still speaking and closes the utterance with the server's own VAD. Timbre: REST for a whole clip, and SSE
streaming (Timbre.stream) so playback can start before synthesis ends.

Docs: https://docs.gnani.ai/api/STT/speech-to-text.md, https://docs.gnani.ai/api/STT/stt-websocket.md,
https://docs.gnani.ai/api/TTS/tts-inference.md and https://docs.gnani.ai/api/TTS/tts-sse.md
"""

import base64
import json
import logging
import msvcrt
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import requests

from bolos.config import ROOT, Settings

log = logging.getLogger(__name__)

STT_URL = "https://api.vachana.ai/stt/v3"
STT_WS_URL = "wss://api.vachana.ai/stt/v3/stream"
TTS_URL = "https://api.vachana.ai/api/v1/tts/inference"
TTS_SSE_URL = "https://api.vachana.ai/api/v1/tts/sse"
TTS_LANGUAGES = {"hi-IN", "en-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "bn-IN", "gu-IN"}
MAX_KEYTERMS = 200
WS_FRAME_BYTES = 1024  # 512 samples of 16-bit mono at 16 kHz: 32 ms
WS_FRAME_S = 0.032
WS_MIN_GAP_S = 0.030  # a frame late by timer jitter may follow the previous one this soon, never sooner


class SpeechError(Exception):
    pass


def tts_language(language: str | None) -> str:
    """Timbre's language for an app language code: "ta" and "ta-IN" both become "ta-IN"; anything else
    (None, "none", a code Timbre lacks) becomes "auto", which reads the script of the text."""
    if not language:
        return "auto"
    code = language if "-" in language else f"{language.lower()}-IN"
    return code if code in TTS_LANGUAGES else "auto"


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    latency: float | None


class Prisma:
    def __init__(self, settings: Settings):
        self.s = settings

    def transcribe(self, wav: bytes, keyterms: list[str] = (), busy_wait_s: float = 3.0) -> Transcript:
        """The transcript of a finished recording. Gnani allows one active request per key, so a 429 (another
        request still running, e.g. from an eval script) is retried for up to `busy_wait_s`."""
        data = {"model": self.s.stt_model, "language_code": self.s.stt_languages, "format": self.s.stt_format}
        if keyterms:
            data["keyterms"] = json.dumps(list(keyterms)[:MAX_KEYTERMS], ensure_ascii=False)
        deadline = time.monotonic() + busy_wait_s
        while True:
            r = requests.post(
                STT_URL,
                headers={"X-API-Key-ID": self.s.gnani_api_key},
                files={"audio_file": ("command.wav", wav, "audio/wav")},
                data=data,
                timeout=30,
            )
            if r.status_code != 429 or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        if r.status_code != 200:
            raise SpeechError(f"Prisma {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("truncated"):
            raise SpeechError("Prisma returned a truncated recording; please use a shorter command")
        return Transcript(
            text=body.get("transcript", "").strip(),
            language=body.get("detected_language_code"),
            latency=body.get("end_to_end_latency"),
        )


class PrismaStream:
    """One realtime Prisma session over the websocket.

    Audio is queued with `send_audio` (any length; it is cut into the exact 1024-byte frames the server
    wants). A sender thread sends it at real-time cadence; audio that piled up before the connection opened
    (the pre-roll, the first words) is still sent at the required 32 ms cadence; it must never be burst.
    `catchup` is retained as a compatibility argument but does not accelerate transmission.
    Server messages (dicts: "connected", "speech_start", "transcript", "error",
    plus a synthetic {"type": "closed"} when the connection ends) arrive on `events()`.
    """

    def __init__(self, settings: Settings, keyterms: list[str] = (), url: str = STT_WS_URL,
                 min_silence_ms: int = 600, vad_threshold: float = 0.6, catchup: float = 1.0,
                 open_timeout: float = 4.0, close_timeout: float = 0.2):
        self.s = settings
        self.keyterms = list(keyterms)[:MAX_KEYTERMS]
        self.url = url
        self.min_silence_ms = min_silence_ms
        self.vad_threshold = vad_threshold
        self.catchup = catchup
        self.open_timeout = open_timeout
        # Prisma never answers a close frame (measured: close() always ran into its timeout), so waiting
        # longer than a moment for the closing handshake only delays the caller; the TCP close ends it.
        self.close_timeout = close_timeout
        self._events: queue.Queue[dict] = queue.Queue()
        self._outgoing: queue.Queue = queue.Queue()
        self._partial = b""
        self._closed = threading.Event()
        self._ws = None
        self._threads: list[threading.Thread] = []
        self.connect_ms: float | None = None
        self.sent_bytes = 0

    def headers(self) -> dict[str, str]:
        """Connection headers. Keyterms go as a JSON array with \\u escapes: headers must be ASCII."""
        h = {
            "x-api-key-id": self.s.gnani_api_key,
            "model": self.s.stt_model,
            "format": self.s.stt_format,
            "x-sample-rate": "16000",
            "x-min-silence-ms": str(self.min_silence_ms),
            "x-vad-threshold": str(self.vad_threshold),
            "x-idle-timeout-sec": "30",
        }
        if self.s.stt_format != "transliterate":  # transliterate ignores the language list (indic-auto)
            h["language_code"] = self.s.stt_languages
        if self.keyterms:
            h["keyterms"] = json.dumps(self.keyterms, ensure_ascii=True)
        return h

    def open(self) -> None:
        """Connect and wait for the server's "connected" message. Raises SpeechError on failure."""
        from websockets.sync.client import connect

        start = time.monotonic()
        try:
            # entered as a context manager (websockets 17 wants that); close() leaves it
            self._ws = connect(self.url, additional_headers=self.headers(), open_timeout=self.open_timeout,
                               close_timeout=self.close_timeout, max_size=2**20).__enter__()
            first = json.loads(self._ws.recv(timeout=self.open_timeout))
        except Exception as e:  # DNS, TLS, HTTP 401/429, timeout: the caller falls back to REST
            self._closed.set()
            if self._ws is not None:
                self._ws.close()
            raise SpeechError(f"Prisma stream did not open: {e}") from e
        if first.get("type") != "connected":
            self.close()
            raise SpeechError(f"Prisma stream refused: {first}")
        self.connect_ms = (time.monotonic() - start) * 1000
        for target, name in ((self._receive, "prisma-recv"), (self._send, "prisma-send")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)

    def send_audio(self, pcm: bytes) -> None:
        data = self._partial + pcm
        n = len(data) // WS_FRAME_BYTES * WS_FRAME_BYTES
        self._partial = data[n:]
        for i in range(0, n, WS_FRAME_BYTES):
            self._outgoing.put(data[i:i + WS_FRAME_BYTES])

    def flush(self) -> None:
        """Queue the leftover partial frame padded with silence. All PCM retains the 32 ms cadence."""
        if self._partial:
            self._outgoing.put(self._partial + bytes(WS_FRAME_BYTES - len(self._partial)))
            self._partial = b""

    def commit(self) -> None:
        """Close the open utterance now (after all queued audio)."""
        self.send_control({"type": "commit"})

    def send_control(self, message: dict) -> None:
        """A JSON control frame (e.g. update_settings), sent in order after the audio queued so far."""
        self._outgoing.put(json.dumps(message))

    def events(self) -> "queue.Queue[dict]":
        return self._events

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self, wait: bool = True) -> None:
        """End the session. `wait=False` returns at once and finishes closing (at most `close_timeout`) on a
        background thread: for a turn whose transcript is already in hand."""
        self._closed.set()
        self._outgoing.put(None)
        if self._ws is None:
            return
        if not wait:
            threading.Thread(target=self._close_socket, daemon=True, name="prisma-close").start()
            return
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self._ws.close()
        except Exception:  # already gone
            log.debug("closing the Prisma stream failed", exc_info=True)

    def _receive(self) -> None:
        reason = "server closed"
        try:
            for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    # A malformed response may contain a personal form answer. Never log its payload.
                    log.warning("Prisma sent a malformed JSON response (%d characters)", len(raw))
                    continue
                message["_t"] = time.monotonic()
                self._events.put(message)
        except Exception as e:
            reason = str(e) or type(e).__name__
        finally:
            if not self._closed.is_set():
                self._closed.set()
                self._events.put({"type": "closed", "reason": reason, "_t": time.monotonic()})

    def _send(self) -> None:
        """Sends frames on a fixed 32 ms timeline (frame k at start + k * 32 ms), never closer than
        WS_MIN_GAP_S apart. The timeline is absolute so timer jitter does not accumulate: on Windows,
        Event.wait() rounds up to the ~15.6 ms system tick, and "now + 32 ms" after each send paced frames
        at ~46 ms (measured), 1.4x slower than real time, so a 4 s turn reached the server ~2 s late.
        time.sleep() uses a high-resolution timer. The timeline is read with time.perf_counter(): before
        Python 3.13, time.monotonic() on Windows is GetTickCount64 with 15.6 ms resolution, which let frames
        go out ~15 ms apart. When the queue runs dry (live audio), the timeline restarts at the next frame:
        no catch-up burst."""
        next_audio_at = float("-inf")  # perf_counter() has no defined zero: the first frame always starts it
        last_sent = float("-inf")
        try:
            while True:
                item = self._outgoing.get()
                if item is None or self._closed.is_set():
                    return
                if isinstance(item, bytes):
                    now = time.perf_counter()
                    if now > next_audio_at + WS_FRAME_S:  # waited for audio: back on the live edge
                        next_audio_at = now
                    at = max(next_audio_at, last_sent + WS_MIN_GAP_S)
                    if at > now:
                        time.sleep(at - now)
                    if self._closed.is_set():
                        return
                    self._ws.send(item)
                    last_sent = time.perf_counter()
                    next_audio_at = at + WS_FRAME_S
                    self.sent_bytes += len(item)
                else:
                    self._ws.send(item)  # controls remain FIFO after all earlier PCM frames
        except Exception as e:
            if not self._closed.is_set():
                self._closed.set()
                self._events.put({"type": "closed", "reason": f"send failed: {e}", "_t": time.monotonic()})


TIMBRE_LOCK = ROOT / ".cache" / "timbre.lock"
_thread_lock = threading.Lock()


@contextmanager
def timbre_turn(timeout: float = 120.0):
    """Timbre allows one active request per key. This lock serialises requests across threads and across
    processes (the app, eval scripts and training-data generators can run at the same time)."""
    TIMBRE_LOCK.parent.mkdir(exist_ok=True)
    deadline = time.monotonic() + timeout
    if not _thread_lock.acquire(timeout=timeout):
        raise SpeechError("Timbre is busy (another request in this process held it too long)")
    try:
        fd = os.open(TIMBRE_LOCK, os.O_RDWR | os.O_CREAT)
        try:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise SpeechError("Timbre is busy (another process held its lock too long)") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
    finally:
        _thread_lock.release()


class Timbre:
    SAMPLE_RATE = 24000

    def __init__(self, settings: Settings):
        self.s = settings

    def synthesize(self, text: str, language: str | None, sample_rate: int | None = None) -> bytes:
        """WAV bytes (16-bit mono PCM) for the text."""
        with timbre_turn():
            return self._synthesize(text, language, sample_rate or self.SAMPLE_RATE)

    def _body(self, text: str, language: str | None, sample_rate: int, container: str) -> dict:
        return {
            "text": text,
            "voice": self.s.tts_voice,
            "model": self.s.tts_model,
            "language": tts_language(language),
            "speed": 1.0,
            "audio_config": {
                "sample_rate": sample_rate, "num_channels": 1, "sample_width": 2,
                "encoding": "linear_pcm", "container": container,
            },
        }

    def _synthesize(self, text: str, language: str | None, sample_rate: int) -> bytes:
        r = requests.post(TTS_URL, headers={"X-API-Key-ID": self.s.gnani_api_key},
                          json=self._body(text, language, sample_rate, "wav"), timeout=30)
        if r.status_code != 200:
            raise SpeechError(f"Timbre {r.status_code}: {r.text[:300]}")
        return r.content

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        """Raw 16-bit mono PCM chunks at SAMPLE_RATE, as Timbre's SSE endpoint produces them.

        Holds timbre_turn() until the response is complete or the generator is closed, so consume it to the
        end (or close it) promptly."""
        with timbre_turn():
            r = requests.post(TTS_SSE_URL, headers={"X-API-Key-ID": self.s.gnani_api_key},
                              json=self._body(text, language, self.SAMPLE_RATE, "raw"), timeout=30, stream=True)
            try:
                if r.status_code != 200:
                    raise SpeechError(f"Timbre SSE {r.status_code}: {r.text[:300]}")
                yield from _sse_audio(r.iter_lines(decode_unicode=True))
            finally:
                r.close()


def _sse_audio(lines) -> Iterator[bytes]:
    """Audio bytes from Timbre's SSE lines ("event: chunk" / "data: {...audio: base64...}")."""
    event, data = "message", []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data:
                payload = json.loads("\n".join(data))
                if event == "error" or payload.get("type") == "error":
                    raise SpeechError(f"Timbre SSE error: {payload.get('message') or payload}")
                if payload.get("audio"):
                    yield base64.b64decode(payload["audio"])
                if event == "complete" or payload.get("is_final"):
                    return
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:  # a last event without the trailing blank line
        payload = json.loads("\n".join(data))
        if event == "error" or payload.get("type") == "error":
            raise SpeechError(f"Timbre SSE error: {payload.get('message') or payload}")
        if payload.get("audio"):
            yield base64.b64decode(payload["audio"])
