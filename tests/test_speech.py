"""Prisma REST, Timbre REST and SSE clients (with requests faked), the Timbre lock, and the reply cache."""

import base64
import json
import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from bolos import speech
from bolos.audio import read_wav, to_wav
from bolos.config import load_settings
from bolos.speech import Prisma, SpeechError, Timbre, _sse_audio, timbre_turn, tts_language
from bolos.tts_cache import CachedTimbre, TtsCache


@pytest.fixture(autouse=True)
def private_timbre_lock(tmp_path, monkeypatch):
    """The real lock file is shared with any process using Timbre right now (data generators): tests must not
    wait for them, or hold them up."""
    monkeypatch.setattr(speech, "TIMBRE_LOCK", tmp_path / "timbre.lock")


@pytest.fixture
def settings():
    return replace(load_settings(), gnani_api_key="test-key", tts_model="timbre-v3.0", tts_voice="Shivani")


def pcm(n: int, value: int = 1000) -> bytes:
    return np.full(n, value, dtype=np.int16).tobytes()


def sse(*events) -> list[str]:
    """SSE lines as requests.iter_lines(decode_unicode=True) yields them."""
    lines = []
    for name, data in events:
        lines += [f"event: {name}", f"data: {json.dumps(data)}", ""]
    return lines


def chunk(i: int, audio: bytes) -> tuple[str, dict]:
    return "chunk", {"chunk_index": i, "audio": base64.b64encode(audio).decode(), "is_final": False}


COMPLETE = ("complete", {"chunk_index": 9, "audio": "", "is_final": True})


# Small helpers


@pytest.mark.parametrize("code, expected", [
    ("hi", "hi-IN"), ("ta", "ta-IN"), ("PA", "pa-IN"), ("gu-IN", "gu-IN"), ("bn", "bn-IN"),
    (None, "auto"), ("", "auto"), ("none", "auto"), ("fr", "auto"),
])
def test_tts_language(code, expected):
    assert tts_language(code) == expected


def test_all_ten_languages_are_supported():
    codes = ("hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa")
    assert {tts_language(c) for c in codes} == speech.TTS_LANGUAGES


# SSE parsing


def test_sse_audio_yields_decoded_chunks_in_order():
    lines = sse(("start", {"status": "streaming_started", "text": "x"}), chunk(0, b"ab"), chunk(1, b"cd"), COMPLETE)
    assert list(_sse_audio(iter(lines))) == [b"ab", b"cd"]


def test_sse_audio_stops_at_complete_and_handles_a_missing_last_blank_line():
    lines = sse(chunk(0, b"ab"), COMPLETE, chunk(2, b"never"))
    assert list(_sse_audio(iter(lines))) == [b"ab"]
    assert list(_sse_audio(iter(sse(chunk(0, b"ab"))[:-1]))) == [b"ab"]


def test_sse_error_event_raises():
    lines = sse(chunk(0, b"ab"), ("error", {"type": "synthesis_error", "message": "no credits"}))
    gen = _sse_audio(iter(lines))
    assert next(gen) == b"ab"
    with pytest.raises(SpeechError, match="no credits"):
        next(gen)


# REST and SSE clients with requests faked


class FakeResponse:
    def __init__(self, status=200, content=b"", lines=(), body=None, on_iter=None):
        self.status_code = status
        self.content = content
        self.text = content.decode("latin-1") if content else ""
        self._lines = list(lines)
        self._body = body
        self.on_iter = on_iter
        self.closed = False

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            if self.on_iter:
                self.on_iter()
            yield line

    def close(self):
        self.closed = True


def test_prisma_rest_sends_keyterms_and_reads_the_transcript(settings, monkeypatch):
    seen = {}

    def post(url, headers, files, data, timeout):
        seen.update(url=url, headers=headers, data=data)
        return FakeResponse(body={"transcript": " Chrome kholo ", "end_to_end_latency": 0.4})

    monkeypatch.setattr(speech.requests, "post", post)
    t = Prisma(settings).transcribe(b"RIFF", keyterms=["రవి", "Chrome"])
    assert t.text == "Chrome kholo" and t.latency == 0.4
    assert seen["headers"] == {"X-API-Key-ID": "test-key"}
    assert json.loads(seen["data"]["keyterms"]) == ["రవి", "Chrome"]
    assert seen["data"]["format"] == settings.stt_format


def test_prisma_rest_error_raises(settings, monkeypatch):
    monkeypatch.setattr(speech.requests, "post", lambda *a, **k: FakeResponse(500, b"oops"))
    with pytest.raises(SpeechError, match="500"):
        Prisma(settings).transcribe(b"RIFF")


def test_prisma_rejects_a_provider_truncated_command(settings, monkeypatch):
    monkeypatch.setattr(speech.requests, "post", lambda *a, **k: FakeResponse(
        body={"transcript": "open Chrome", "truncated": True}))
    with pytest.raises(SpeechError, match="truncated"):
        Prisma(settings).transcribe(b"RIFF")


def test_pcm_remains_realtime_paced_after_flush_and_controls_follow_audio(settings):
    sent = []

    class Socket:
        def send(self, item):
            sent.append((time.perf_counter(), item))  # monotonic() ticks in 15.6 ms on Windows before 3.13

    stream = speech.PrismaStream(settings, catchup=50)  # legacy argument must not bypass provider cadence
    stream._ws = Socket()
    stream.send_audio(bytes(speech.WS_FRAME_BYTES * 3 + 100))
    stream.flush()
    stream.commit()
    stream.send_control({"type": "update_settings"})
    stream._outgoing.put(None)
    stream._send()
    pcm = [(at, item) for at, item in sent if isinstance(item, bytes)]
    assert len(pcm) == 4 and {len(item) for _, item in pcm} == {1024}
    assert all(after[0] - before[0] >= 0.030 for before, after in zip(pcm, pcm[1:], strict=False))
    assert [json.loads(item)["type"] for _, item in sent[4:]] == ["commit", "update_settings"]


def test_queued_pcm_keeps_real_time_on_average_without_drift(settings):
    """Timer jitter must not add up: 25 queued frames span 24 x 32 ms, not 24 x ~46 ms (Event.wait on
    Windows rounds every wait up to the system tick, which made the stream lag ~2 s behind a 4 s turn)."""
    sent = []

    class Socket:
        def send(self, item):
            sent.append(time.perf_counter())  # monotonic() ticks in 15.6 ms on Windows before 3.13

    stream = speech.PrismaStream(settings)
    stream._ws = Socket()
    stream.send_audio(bytes(speech.WS_FRAME_BYTES * 25))
    stream._outgoing.put(None)
    stream._send()
    assert len(sent) == 25
    span = sent[-1] - sent[0]
    assert 24 * 0.030 <= span < 24 * speech.WS_FRAME_S * 1.15
    assert min(b - a for a, b in zip(sent, sent[1:], strict=False)) >= speech.WS_MIN_GAP_S - 0.001


def test_prisma_rest_waits_out_another_active_request(settings, monkeypatch):
    replies = [FakeResponse(429, b"busy"), FakeResponse(429, b"busy"), FakeResponse(body={"transcript": "haan"})]
    monkeypatch.setattr(speech.requests, "post", lambda *a, **k: replies.pop(0))
    assert Prisma(settings).transcribe(b"RIFF").text == "haan"
    monkeypatch.setattr(speech.requests, "post", lambda *a, **k: FakeResponse(429, b"busy"))
    start = time.monotonic()
    with pytest.raises(SpeechError, match="429"):
        Prisma(settings).transcribe(b"RIFF", busy_wait_s=0.3)
    assert time.monotonic() - start < 1.0


def test_timbre_stream_holds_the_lock_for_the_whole_response(settings, monkeypatch):
    held = []
    body = {}

    def lock_is_held():
        held.append(not _try_lock())

    def post(url, headers, json, timeout, stream):
        body.update(json)
        assert url == speech.TTS_SSE_URL and stream
        return FakeResponse(lines=sse(chunk(0, pcm(10)), chunk(1, pcm(10)), COMPLETE), on_iter=lock_is_held)

    monkeypatch.setattr(speech.requests, "post", post)
    assert b"".join(Timbre(settings).stream("ho gaya", "hi")) == pcm(20)
    assert held and all(held)
    assert _try_lock()  # released after
    assert body["language"] == "hi-IN" and body["audio_config"]["container"] == "raw"
    assert body["audio_config"]["sample_rate"] == Timbre.SAMPLE_RATE


def test_timbre_stream_http_error(settings, monkeypatch):
    monkeypatch.setattr(speech.requests, "post", lambda *a, **k: FakeResponse(403, b"no credits"))
    with pytest.raises(SpeechError, match="403"):
        list(Timbre(settings).stream("ho gaya", "hi"))
    assert _try_lock()


def _try_lock() -> bool:
    """Can another thread take Timbre's turn right now?"""
    got = []

    def attempt():
        try:
            with timbre_turn(timeout=0.05):
                got.append(True)
        except SpeechError:
            got.append(False)

    t = threading.Thread(target=attempt)
    t.start()
    t.join()
    return got[0]


def test_timbre_turn_serialises_threads():
    active, overlaps = [0], []

    def work():
        with timbre_turn():
            active[0] += 1
            overlaps.append(active[0])
            time.sleep(0.02)
            active[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [1, 1, 1, 1]


# The reply cache


def test_cache_key_covers_model_voice_language_and_text(tmp_path):
    cache = TtsCache(tmp_path)
    base = ("timbre-v3.0", "Shivani", "hi-IN", "ho gaya")
    keys = {cache.key(*base), cache.key("timbre-v2.5", *base[1:]), cache.key(base[0], "Ravi", *base[2:]),
            cache.key(*base[:2], "ta-IN", base[3]), cache.key(*base[:3], "ho gaya!")}
    assert len(keys) == 5
    assert cache.key(*base[:3], "  ho gaya ") == cache.key(*base)
    assert cache.get(*base) is None
    cache.put(*base, b"RIFFdata")
    assert cache.get(*base) == b"RIFFdata"
    assert [p.suffix for p in tmp_path.iterdir()] == [".wav"]  # no temp files left behind


class FakeTimbre:
    SAMPLE_RATE = 24000

    def __init__(self, fail_stream=False, fail_rest=False, delay=0.0):
        self.rest_calls: list[tuple[str, str]] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.fail_stream = fail_stream
        self.fail_rest = fail_rest
        self.delay = delay

    def synthesize(self, text, language, sample_rate=None):
        self.rest_calls.append((text, language))
        time.sleep(self.delay)
        if self.fail_rest:
            raise SpeechError("Timbre 403: no credits")
        return to_wav(np.full(2400, 500, dtype=np.int16), self.SAMPLE_RATE)

    def stream(self, text, language):
        self.stream_calls.append((text, language))
        if self.fail_stream:
            raise SpeechError("Timbre SSE 503")
        yield pcm(1201)[:-1]  # chunks may split a sample
        yield pcm(1200)[1:] + b"\x00"


class FakePlayer:
    def __init__(self):
        self.played: list[int] = []

    def play(self, samples, rate):
        self.played.append(len(samples))
        return True

    def play_stream(self, chunks, rate, prebuffer_s=0.0):
        self.played.append(sum(len(c) for c in chunks))
        return True


def test_cached_reply_plays_without_asking_timbre(settings, tmp_path):
    timbre = FakeTimbre()
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    tts.cache.put(*tts._key("ho gaya", "hi"), to_wav(np.zeros(100, dtype=np.int16), 24000))
    player = FakePlayer()
    assert tts.speak("ho gaya", "hi", player)
    assert player.played == [100] and timbre.rest_calls == timbre.stream_calls == []
    assert tts.last_first_audio_ms == 0.0


def test_new_reply_streams_and_is_never_synthesised_twice(settings, tmp_path):
    timbre = FakeTimbre()
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    player = FakePlayer()
    assert tts.speak("Spotify pe Arijit chala rahi hoon", "hi", player)
    assert player.played == [2400]
    wait_until(lambda: tts.cached("Spotify pe Arijit chala rahi hoon", "hi") is not None)
    samples, rate = read_wav(tts.cached("Spotify pe Arijit chala rahi hoon", "hi"))
    assert (len(samples), rate) == (2400, 24000)
    assert tts.speak("Spotify pe Arijit chala rahi hoon", "hi", player)
    assert len(timbre.stream_calls) == 1 and timbre.rest_calls == []
    assert tts.synthesize("Spotify pe Arijit chala rahi hoon", "hi")[:4] == b"RIFF"  # from the cache too
    assert timbre.rest_calls == []


def test_streaming_failure_falls_back_to_rest(settings, tmp_path):
    timbre = FakeTimbre(fail_stream=True)
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    player = FakePlayer()
    assert tts.speak("ho gaya", "ta", player)
    assert timbre.rest_calls == [("ho gaya", "ta")] and player.played[-1] == 2400
    assert tts.cached("ho gaya", "ta") is not None


def test_rest_mode_when_streaming_is_off(settings, tmp_path):
    timbre = FakeTimbre()
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre, streaming=False)
    assert tts.speak("ho gaya", "kn", FakePlayer())
    assert timbre.stream_calls == [] and timbre.rest_calls == [("ho gaya", "kn")]


def test_prewarm_fills_only_what_is_missing(settings, tmp_path):
    timbre = FakeTimbre()
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    tts.synthesize("ek second", "hi")
    phrases = [("ek second", "hi"), ("ondu nimisha", "kn"), ("oru nimisham", "ta"), ("", "hi"), ("ondu nimisha", "kn")]
    tts.prewarm(phrases).join(5)
    assert timbre.rest_calls == [("ek second", "hi"), ("ondu nimisha", "kn"), ("oru nimisham", "ta")]
    tts.prewarm(phrases).join(5)
    assert len(timbre.rest_calls) == 3  # never twice


def test_prewarm_gives_up_after_three_failures(settings, tmp_path):
    timbre = FakeTimbre(fail_rest=True)
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    done = threading.Event()
    tts.prewarm([(f"phrase {i}", "hi") for i in range(10)], done=done.set).join(5)
    assert len(timbre.rest_calls) == 3 and done.is_set()


def test_prewarm_waits_for_a_foreground_reply(settings, tmp_path):
    timbre = FakeTimbre(delay=0.01)
    tts = CachedTimbre(settings, TtsCache(tmp_path), timbre)
    with tts._in_foreground():
        thread = tts.prewarm([("ek second", "hi")])
        time.sleep(0.15)
        assert timbre.rest_calls == []  # held back while the user waits for a reply
    thread.join(5)
    assert timbre.rest_calls == [("ek second", "hi")]


def wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline
        time.sleep(0.01)
