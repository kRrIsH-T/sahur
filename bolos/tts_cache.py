"""Spoken replies without waiting: a disk cache of Timbre clips, background pre-warming, streaming playback.

Most replies are fixed phrases per language ("ho gaya", "ek second", ...): each is synthesised once, ever,
and then plays instantly from .cache/tts/. Replies with fields ("Spotify pe Arijit chala rahi hoon") are new
text; they stream from Timbre's SSE endpoint and start playing before synthesis finishes, and the finished
clip is cached too. Every Timbre request holds bolos.speech.timbre_turn() for its whole duration (one active
request per API key), and never the same (model, voice, language, text) twice.
"""

import hashlib
import json
import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import numpy as np

from bolos.audio import read_wav, to_wav
from bolos.config import ROOT, Settings
from bolos.speech import SpeechError, Timbre, tts_language

log = logging.getLogger(__name__)

CACHE_DIR = ROOT / ".cache" / "tts"


class TtsCache:
    """WAV clips on disk keyed by (model, voice, language, text). Writes are atomic (temp file + rename), so
    two processes filling the same cache never leave a half-written clip."""

    def __init__(self, folder: Path = CACHE_DIR):
        self.folder = Path(folder)

    @staticmethod
    def key(model: str, voice: str, language: str, text: str) -> str:
        raw = json.dumps([model, voice, language, text.strip()], ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path(self, model: str, voice: str, language: str, text: str) -> Path:
        return self.folder / f"{self.key(model, voice, language, text)}.wav"

    def get(self, model: str, voice: str, language: str, text: str) -> bytes | None:
        try:
            return self.path(model, voice, language, text).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, model: str, voice: str, language: str, text: str, wav: bytes) -> None:
        path = self.path(model, voice, language, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(wav)
        os.replace(tmp, path)


class CachedTimbre:
    """Timbre behind the disk cache.

    `streaming`: play uncached text through SSE (playback starts at the first chunk) instead of waiting for
    the whole REST clip. Measured on this machine and key (see bolos/listen.py's module docstring), SSE's
    first audio arrives well before REST's full clip, so it is on by default.
    """

    PREBUFFER_S = 0.25  # audio collected before the speaker starts, so network jitter does not cause gaps

    def __init__(self, settings: Settings, cache: TtsCache | None = None, timbre: Timbre | None = None,
                 streaming: bool = True):
        self.s = settings
        self.cache = cache or TtsCache()
        self.timbre = timbre or Timbre(settings)
        self.streaming = streaming
        self._foreground = 0
        self._fg_lock = threading.Lock()
        self.last_first_audio_ms: float | None = None  # time to first audio of the last uncached reply

    def _key(self, text: str, language: str | None) -> tuple[str, str, str, str]:
        return self.s.tts_model, self.s.tts_voice, tts_language(language), text.strip()

    def cached(self, text: str, language: str | None) -> bytes | None:
        return self.cache.get(*self._key(text, language))

    def synthesize(self, text: str, language: str | None) -> bytes:
        """WAV bytes for the text: from the cache, else from Timbre REST (and then cached)."""
        hit = self.cached(text, language)
        if hit is not None:
            return hit
        with self._in_foreground():
            wav = self.timbre.synthesize(text, language)
        self.cache.put(*self._key(text, language), wav)
        return wav

    def speak(self, text: str, language: str | None, player) -> bool:
        """Play the text on `player` (bolos.audio.Player). Cached clips play at once; new text streams.
        False if playback was interrupted."""
        hit = self.cached(text, language)
        if hit is not None:
            self.last_first_audio_ms = 0.0
            samples, rate = read_wav(hit)
            return player.play(samples, rate)
        if not self.streaming:
            start = time.monotonic()
            wav = self.synthesize(text, language)
            self.last_first_audio_ms = (time.monotonic() - start) * 1000
            samples, rate = read_wav(wav)
            return player.play(samples, rate)
        return self._speak_streaming(text, language, player)

    def _speak_streaming(self, text: str, language: str | None, player) -> bool:
        """Stream from SSE into the player. A reader thread drains the response (holding Timbre's turn only
        for the request) and caches the whole clip even if the user interrupts playback. If SSE fails before
        any audio, falls back to REST."""
        chunks: queue.Queue = queue.Queue()
        done = object()
        start = time.monotonic()

        def read() -> None:
            pcm = bytearray()
            try:
                with self._in_foreground():
                    stream = self.timbre.stream(text, language)
                    try:
                        for data in stream:
                            pcm += data
                            chunks.put(bytes(data))
                    finally:
                        stream.close()
                if pcm:
                    wav = to_wav(np.frombuffer(bytes(pcm[: len(pcm) // 2 * 2]), dtype=np.int16),
                                 self.timbre.SAMPLE_RATE)
                    self.cache.put(*self._key(text, language), wav)
            except Exception as e:  # network, credits, server error
                chunks.put(e)
            finally:
                chunks.put(done)

        threading.Thread(target=read, daemon=True, name="timbre-sse").start()

        first: list[float] = []
        failure: list[Exception] = []

        def samples():
            leftover = b""
            while True:
                item = chunks.get()
                if item is done:
                    return
                if isinstance(item, Exception):
                    failure.append(item)
                    return
                data = leftover + item
                cut = len(data) // 2 * 2  # a chunk can end mid-sample
                leftover = data[cut:]
                if cut:
                    if not first:
                        first.append(time.monotonic())
                    yield np.frombuffer(data[:cut], dtype=np.int16)

        played = player.play_stream(samples(), self.timbre.SAMPLE_RATE, prebuffer_s=self.PREBUFFER_S)
        if first:
            self.last_first_audio_ms = (first[0] - start) * 1000
        if failure and not first:
            log.warning("Timbre streaming failed (%s); using REST", failure[0])
            return self.speak_rest(text, language, player)
        if failure:
            log.warning("Timbre streaming broke off mid-reply: %s", failure[0])
        return played

    def speak_rest(self, text: str, language: str | None, player) -> bool:
        start = time.monotonic()
        wav = self.synthesize(text, language)
        self.last_first_audio_ms = (time.monotonic() - start) * 1000
        samples, rate = read_wav(wav)
        return player.play(samples, rate)

    def prewarm(self, phrases: Iterable[tuple[str, str | None]], done=None) -> threading.Thread:
        """Synthesise every (text, language) not yet cached, in a background thread, one request at a time.

        It yields to foreground replies: before each request it waits while the app is speaking something
        uncached, so pre-warming never delays what the user is waiting to hear. Stops after 3 failures in a
        row (no credits, no network). `done()` is called at the end."""
        phrases = [(t, lang) for t, lang in phrases if t and t.strip()]

        def run() -> None:
            failures = 0
            made = 0
            for text, language in phrases:
                if self.cached(text, language) is not None:
                    continue
                while self._foreground:
                    time.sleep(0.05)
                try:
                    wav = self.timbre.synthesize(text, language)
                except SpeechError as e:
                    failures += 1
                    log.warning("Pre-warming %r failed: %s", text, e)
                    if failures >= 3:
                        log.warning("Stopped pre-warming the reply cache after 3 failures")
                        break
                    continue
                except Exception:
                    failures += 1
                    log.warning("Pre-warming %r failed", text, exc_info=True)
                    if failures >= 3:
                        break
                    continue
                failures = 0
                made += 1
                self.cache.put(*self._key(text, language), wav)
            log.info("Reply cache pre-warmed: %d new clips", made)
            if done:
                done()

        thread = threading.Thread(target=run, daemon=True, name="tts-prewarm")
        thread.start()
        return thread

    @contextmanager
    def _in_foreground(self):
        """Marks a request the user is waiting for, so pre-warming holds back."""
        with self._fg_lock:
            self._foreground += 1
        try:
            yield
        finally:
            with self._fg_lock:
                self._foreground -= 1
