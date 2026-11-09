"""Hands-free listening: wake phrase matching, the local VAD, captures, and whole turns against a local fake of
Prisma's realtime websocket (same protocol: headers, 1024-byte frames, speech_start / transcript / commit /
update_settings / settings_updated)."""

import json
import queue
import threading
import time
from dataclasses import replace
from functools import lru_cache
from http import HTTPStatus

import numpy as np
import pytest
from websockets.sync.server import serve

from bolos import listen as L
from bolos.audio import FRAME, SAMPLE_RATE, Endpointer, NoiseFloor, Vad, earcon, level
from bolos.config import load_settings
from bolos.listen import Capture, Heard, Listener, clean_transcript, match_wake
from bolos.speech import PrismaStream, SpeechError, Transcript

SR = SAMPLE_RATE


# Synthetic audio


@lru_cache(maxsize=None)
def _vowel(seconds: float, f0: float = 140.0, amp: int = 6000) -> np.ndarray:
    """Voiced, speech-like audio: a glottal pulse train with intonation through three formant resonators,
    with a syllable rhythm. WebRTC's VAD calls it speech; pure tones it does not."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    phase = 2 * np.pi * np.cumsum(f0 * (1 + 0.08 * np.sin(2 * np.pi * 3 * t))) / SR
    src = sum(np.sin(k * phase) / k for k in range(1, 30))
    out = np.zeros(n)
    for fc, bw in ((700, 110), (1200, 120), (2600, 160)):
        r, w = np.exp(-np.pi * bw / SR), 2 * np.pi * fc / SR
        a1, a2 = -2 * r * np.cos(w), r * r
        y = np.zeros(n)
        for i in range(2, n):
            y[i] = src[i] - a1 * y[i - 1] - a2 * y[i - 2]
        out += y
    out *= 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    out = out / np.max(np.abs(out)) * amp
    return out.astype(np.int16)


def speech(seconds: float) -> np.ndarray:
    return _vowel(round(seconds, 3)).copy()


def silence(seconds: float, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 20, int(seconds * SR)).astype(np.int16)


def cat(*parts) -> np.ndarray:
    return np.concatenate(parts)


# Wake phrase


@pytest.mark.parametrize("text, rest", [
    ("Hey Bol OS, Chrome kholo", "Chrome kholo"),
    ("Hey Bolos Chrome kholo", "Chrome kholo"),
    ("hey bolo es chrome kholo", "chrome kholo"),
    ("he bolos YouTube chalao", "YouTube chalao"),
    ("a bol OS notepad kholo", "notepad kholo"),
    ("Hey, Bowl O.S. what is the time", "what is the time"),
    ("Hey Bol O S. Spotify pe gaana", "Spotify pe gaana"),
    ("heybolos weather batao", "weather batao"),
    ("Hay bolos, Notepad tere", "Notepad tere"),
    ("Hi Bolos open Chrome", "open Chrome"),
    ("Hey Bholos gaana rok do", "gaana rok do"),
    ("Hey Bolls agla gaana", "agla gaana"),
    ("Hey Voles Chrome kholo", "Chrome kholo"),
    ("Hey Paul OS Chrome kholo", "Chrome kholo"),
    ("Hey Ball OS, Chrome kholo", "Chrome kholo"),
    ("Hey Bol OS", ""),
    ("Hey Bolos.", ""),
    ("Bol OS Chrome kholo", "Chrome kholo"),
    ("Bolos, Chrome kholo", "Chrome kholo"),
    ("Hey Bolo S, YouTube kholo", "YouTube kholo"),
    ("Hey bol o yes chrome kholo", "chrome kholo"),
    ("haan to hey bolos chrome kholo", "chrome kholo"),  # the pre-roll caught the end of earlier speech
    ("Heyy Bolos Chrome", "Chrome"),
    ("Hey Bol OS ಕ್ರೋಮ್ ತೆರೆ", "ಕ್ರೋಮ್ ತೆರೆ"),
])
def test_wake_phrase_is_found_and_stripped(text, rest):
    assert match_wake(text) == (True, rest)


@pytest.mark.parametrize("text", [
    "hey bolo",
    "hey bolo Chrome kholo",
    "hey boss",
    "Bolo na kya hua",
    "Mummy ko bolo ki main aa raha hoon",
    "hello Chrome kholo",
    "Hey Google",
    "Alexa play music",
    "Chrome kholo",
    "bol",
    "",
    "one two three four hey bolos",  # too far in: the wake phrase starts the pre-rolled audio
    "hey Boston weather",
    "he is bold",
])
def test_near_misses_are_not_the_wake_phrase(text):
    matched, rest = match_wake(text)
    assert not matched
    assert rest == text


def test_clean_transcript_drops_non_words():
    assert clean_transcript(" . ") == ""
    assert clean_transcript(None) == ""
    assert clean_transcript(" Chrome kholo. ") == "Chrome kholo."
    assert clean_transcript("ಕ್ರೋಮ್") == "ಕ್ರೋಮ್"


# Signal building blocks


def test_vad_hears_speech_not_room_noise():
    vad = Vad()
    assert not any(vad.feed(silence(1.0)))
    decisions = vad.feed(speech(1.0))
    assert sum(decisions) > 0.8 * len(decisions)


def test_vad_keeps_partial_frames_for_the_next_call():
    vad = Vad()
    assert vad.feed(speech(0.02)) == []  # 320 samples: less than one 30 ms frame
    assert len(vad.feed(speech(0.02))) == 1


def test_vad_noise_floor_follows_the_room():
    loud_room = np.random.default_rng(3).normal(0, 600, SR * 6).astype(np.int16)  # a fan, about -35 dBFS
    vad = Vad()
    vad.track(loud_room)
    assert -43 < vad.floor_db < -34
    # Seeded with that floor, a new VAD does not take the fan for speech, and still hears speech over it.
    assert sum(Vad(floor=vad.floor.copy()).feed(loud_room[:SR])) == 0
    voice = speech(1.0).astype(np.int32) * 3 + loud_room[:SR]
    decisions = Vad(floor=vad.floor.copy()).feed(np.clip(voice, -32768, 32767).astype(np.int16))
    assert sum(decisions) > 0.6 * len(decisions)


def test_noise_floor_is_not_dragged_up_by_talking():
    floor = NoiseFloor()
    for db in [-62.0] * 120:  # a quiet room
        floor.update(db)
    for _ in range(10):  # 20 s of steady talking with short gaps
        for db in [-25.0] * 60 + [-60.0] * 6:
            floor.update(db)
    assert floor.db < -55
    for _ in range(400):  # then a fan starts and never stops
        floor.update(-40.0)
    assert floor.db > -42


def test_endpointer_needs_a_run_of_voice_and_ends_after_silence():
    ep = Endpointer(start_ms=180, window_ms=300, end_silence_ms=300)
    assert [ep.update(v) for v in [True, False, True, False]] == [None] * 4  # a click, not speech
    events = [ep.update(True) for _ in range(6)]
    assert "start" in events
    events = [ep.update(False) for _ in range(10)]
    assert events.count("end") == 1
    assert ep.update(True) is None  # ended for good


def test_level_is_a_0_to_1_meter():
    assert level(np.zeros(512, dtype=np.int16)) == 0.0
    assert level(np.full(512, 32767, dtype=np.int16)) == 1.0
    assert 0.3 < level(speech(0.1)) < 1.0


def test_earcons_are_short_quiet_and_rise_or_fall():
    def pitch(x):
        spectrum = np.abs(np.fft.rfft(x.astype(float)))
        return np.argmax(spectrum) * 24000 / len(x)

    for kind in ("start", "stop"):
        tone = earcon(kind)
        assert tone.dtype == np.int16
        assert len(tone) / 24000 < 0.3
        assert np.max(np.abs(tone)) <= 0.13 * 32767
    start, stop = earcon("start"), earcon("stop")
    half = int(0.075 * 24000)
    assert pitch(start[:half]) < pitch(start[half:])  # rising: "I'm listening"
    assert pitch(stop[:half]) > pitch(stop[half:])  # falling: "got it"


# Captures


def _frames(samples):
    return [samples[i:i + FRAME] for i in range(0, len(samples) - FRAME + 1, FRAME)]


def _add_all(cap, samples, t0=0.0):
    started = False
    for i, f in enumerate(_frames(samples)):
        started |= cap.add(f, t0 + i * FRAME / SR)
    return started


def test_capture_ends_after_speech_then_silence():
    cap = Capture("tap", [], 0.0, no_speech_s=5.0)
    assert _add_all(cap, cat(silence(0.3), speech(1.0), silence(1.5)))
    assert cap.reason == "endpoint"
    assert 1.2 < cap.speech_end < 1.4  # the last voiced frame, not the end of the silence
    assert cap.endpointer.voiced_ms > 600


def test_capture_gives_up_when_nobody_speaks():
    cap = Capture("tap", [], 0.0, no_speech_s=1.0)
    assert not _add_all(cap, silence(1.5))
    assert cap.reason == "no_speech"


def test_held_capture_ignores_pauses_until_release():
    cap = Capture("hotkey", [], 0.0, no_speech_s=1.0, hold=True)
    _add_all(cap, cat(speech(0.5), silence(2.0), speech(0.5)))
    assert not cap.ended  # neither the pause nor the no-speech timeout ends a held key
    cap.release(3.1, tap=False)
    assert cap.reason == "released"
    assert cap.speech_end == pytest.approx(3.0, abs=0.1)


def test_tap_release_switches_to_end_of_speech():
    cap = Capture("hotkey", [], 0.0, no_speech_s=1.0, hold=True)
    cap.release(0.2, tap=True)
    assert cap.trigger == "tap" and not cap.ended
    _add_all(cap, cat(speech(1.0), silence(1.5)), t0=0.2)
    assert cap.reason == "endpoint"


def test_capture_skips_its_own_earcon():
    cap = Capture("tap", [], 0.0, no_speech_s=5.0, vad_ignore_s=0.4)
    _add_all(cap, speech(0.35))  # inside the ignore window
    assert not cap.endpointer.started


def test_reopened_capture_keeps_the_audio_and_listens_again():
    cap = Capture("wake", [silence(0.032)[:FRAME]], 0.0, no_speech_s=5.0)
    _add_all(cap, cat(speech(0.6), silence(1.2)))
    assert cap.ended
    n = len(cap.frames)  # frames kept arriving after the local endpoint
    assert len(cap.take(0)) < n  # but the turn's audio stops at the endpoint
    cap.add(silence(0.04)[:FRAME], 2.0)  # arrives while the session decides
    cap.reopen(2.0, no_speech_s=6.0)
    assert not cap.ended
    assert len(cap.take(0)) == n + 1  # nothing lost
    _add_all(cap, cat(speech(0.8), silence(1.2)), t0=2.1)
    assert cap.reason == "endpoint"


def test_capture_flags_very_long_speech(monkeypatch):
    monkeypatch.setattr(L, "MAX_SPEECH_S", 1.0)
    cap = Capture("tap", [], 0.0, no_speech_s=5.0)
    _add_all(cap, speech(1.5))
    assert cap.max_length


# A local fake of Prisma's realtime websocket


class FakePrisma:
    """Speaks Prisma's streaming protocol on localhost. Its VAD is an energy gate over the received frames;
    each closed utterance gets the next text from `texts` after `latency_s` (from a separate thread, as the
    real server transcribes asynchronously)."""

    def __init__(self, texts=("Chrome kholo",), latency_s=0.05, refuse=False, drop_after_frames=None,
                 api_key="test-key"):
        self.texts = list(texts)
        self.latency_s = latency_s
        self.refuse = refuse
        self.drop_after_frames = drop_after_frames
        self.api_key = api_key
        self.headers = None
        self.frame_sizes: list[int] = []
        self.controls: list[dict] = []
        self.connections = 0
        self.sent: list[dict] = []
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._check)
        self.url = f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()

    def _check(self, connection, request):
        if request.headers.get("x-api-key-id") != self.api_key:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad key\n")
        return None

    def _handle(self, ws):
        self.connections += 1
        self.headers = dict(ws.request.headers)
        lock = threading.Lock()

        def send(msg):
            with lock:
                self.sent.append(msg)
                try:
                    ws.send(json.dumps(msg))
                except Exception:
                    pass

        if self.refuse:
            send({"type": "error", "message": "concurrent session limit"})
            ws.close(1008)
            return
        send({"type": "connected", "config": {"sample_rate": 16000, "chunk_size": 512}})
        silence_frames = max(1, int(self.headers.get("x-min-silence-ms", "500")) // 32)
        state = {"open": None, "voiced": 0, "quiet": 0, "n": 0}

        def close_utterance():
            utt = state["open"]
            state["open"], state["voiced"], state["quiet"] = None, 0, 0
            send({"type": "processing"})
            text = self.texts.pop(0) if self.texts else ""
            timer = threading.Timer(self.latency_s, send, [{"type": "transcript", "text": text,
                                                             "utterance_id": utt, "is_final": True}])
            timer.start()

        frames = 0
        for message in ws:
            if isinstance(message, bytes):
                self.frame_sizes.append(len(message))
                frames += 1
                if self.drop_after_frames and frames >= self.drop_after_frames:
                    ws.close(1011)
                    return
                rms = np.sqrt(np.mean(np.frombuffer(message, dtype=np.int16).astype(float) ** 2))
                if state["open"] is None:
                    state["voiced"] = state["voiced"] + 1 if rms > 300 else 0
                    if state["voiced"] >= 3:
                        state["n"] += 1
                        state["open"] = f"utt-{state['n']}"
                        state["quiet"] = 0
                        send({"type": "speech_start", "utterance_id": state["open"]})
                else:
                    state["quiet"] = 0 if rms > 300 else state["quiet"] + 1
                    if state["quiet"] >= silence_frames:
                        close_utterance()
            else:
                msg = json.loads(message)
                self.controls.append(msg)
                if msg.get("type") == "commit" and state["open"] is not None:
                    close_utterance()
                elif msg.get("type") == "update_settings":
                    send({"type": "settings_updated", "config": {"format": msg.get("format")}})


@pytest.fixture
def settings():
    return replace(load_settings(), gnani_api_key="test-key", stt_format="transliterate",
                   stt_model="prisma-v3.0")


@pytest.fixture
def prisma():
    servers = []

    def make(**kw):
        server = FakePrisma(**kw)
        servers.append(server)
        return server

    yield make
    for s in servers:
        s.close()


# PrismaStream against the fake


def test_stream_headers_frames_and_controls(settings, prisma):
    server = prisma(texts=["Ravi ko call karo"])
    stream = PrismaStream(settings, ["రవి", "Chrome"], url=server.url, min_silence_ms=640, catchup=50)
    stream.open()
    try:
        assert stream.connect_ms is not None
        stream.send_audio(cat(silence(0.2), speech(0.8), silence(0.1)).tobytes()[:-100])  # not a whole frame
        stream.flush()
        stream.commit()
        stream.send_control({"type": "update_settings", "format": "transliterate"})
        kinds = []
        deadline = time.monotonic() + 5
        while "transcript" not in kinds or "settings_updated" not in kinds:
            assert time.monotonic() < deadline
            kinds.append(stream.events().get(timeout=5)["type"])
        assert kinds.count("speech_start") == 1
    finally:
        stream.close()
    h = server.headers
    assert h["model"] == "prisma-v3.0" and h["format"] == "transliterate"
    assert h["x-sample-rate"] == "16000" and h["x-min-silence-ms"] == "640"
    assert "language_code" not in h  # transliterate always runs indic-auto
    assert h["keyterms"] == '["\\u0c30\\u0c35\\u0c3f", "Chrome"]'  # ASCII-only header, JSON with \u escapes
    assert json.loads(h["keyterms"]) == ["రవి", "Chrome"]
    assert set(server.frame_sizes) == {1024}  # the partial last frame was padded with silence
    assert [c["type"] for c in server.controls] == ["commit", "update_settings"]


def test_stream_open_fails_cleanly(settings, prisma):
    with pytest.raises(SpeechError):
        PrismaStream(replace(settings, gnani_api_key="wrong"), url=prisma().url).open()  # HTTP 401
    with pytest.raises(SpeechError):
        PrismaStream(settings, url=prisma(refuse=True).url).open()  # error, then close 1008
    with pytest.raises(SpeechError):
        PrismaStream(settings, url="ws://127.0.0.1:9", open_timeout=1).open()  # nothing listening


# Whole turns through the Listener


class FakePlayer:
    def __init__(self):
        self.chimes: list[str] = []
        self.played = 0
        self.stops = 0

    def chime(self, kind):
        self.chimes.append(kind)
        t = threading.Thread(target=lambda: None)
        t.start()
        return t

    def play(self, samples, rate):
        self.played += 1
        return True

    def play_stream(self, chunks, rate, prebuffer_s=0.0):
        for _ in chunks:
            pass
        return True

    def stop(self):
        self.stops += 1


class FakeRest:
    def __init__(self, text="Notepad kholo", fail=False):
        self.text = text
        self.fail = fail
        self.calls = []

    def transcribe(self, wav, keyterms=()):
        self.calls.append((wav, list(keyterms)))
        if self.fail:
            raise SpeechError("REST down")
        return Transcript(self.text, None, 0.3)


class FakeWake:
    """Fires on given frame numbers (1-based count of frames fed to it)."""

    def __init__(self, fire_at=()):
        self.fire_at = set(fire_at)
        self.fed = 0
        self.resets = 0

    def feed(self, samples):
        self.fed += 1
        return self.fed in self.fire_at

    def reset(self):
        self.resets += 1


class FakeTts:
    def __init__(self):
        self.spoken = []

    def speak(self, text, language, player):
        self.spoken.append((text, language))
        return True

    def prewarm(self, phrases):
        return None


class Events:
    def __init__(self):
        self.items: list[tuple[str, object]] = []
        self.lock = threading.Lock()

    def __call__(self, kind, value):
        with self.lock:
            self.items.append((kind, value))

    def of(self, kind):
        with self.lock:
            return [v for k, v in self.items if k == kind]


@pytest.fixture
def make_listener(settings):
    made = []

    def make(server=None, rest=None, wake=None, earcons=True, **kw):
        # Most tests feed audio faster than real time to stay quick, so the stream would always lag and
        # every pause would be squeezed. They test the protocol; squeezing has its own tests.
        kw.setdefault("squeeze", False)
        events = Events()
        url = server.url if server else "ws://127.0.0.1:9"
        listener = Listener(
            settings, ["Chrome"], events, wake=wake, use_mic=False, use_hotkey=False, tts=FakeTts(),
            rest=rest or FakeRest(), player=FakePlayer(), earcons=earcons,
            stream_factory=lambda terms: PrismaStream(settings, terms, url=url, catchup=50, open_timeout=2),
            **kw).start()
        made.append(listener)
        return listener, events

    yield make
    for listener in made:
        listener.close()


def feed(listener, samples, speed=None):
    """Feed audio as the microphone would: all at once, or paced at `speed` x real time."""
    if speed is None:
        listener.feed(samples)
        return
    for frame in _frames(samples):
        listener.feed(frame)
        time.sleep(FRAME / SR / speed)


def in_thread(fn, *args):
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("result", fn(*args)), daemon=True)
    thread.start()
    return thread, box


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_tap_turn_ends_on_the_server_transcript(prisma, make_listener):
    server = prisma(texts=["Chrome kholo"])
    listener, events = make_listener(server)
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(silence(0.4), speech(1.0), silence(0.7)))  # the server closes at 600 ms, locally 1000
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    heard = box["result"]
    assert isinstance(heard, Heard)
    assert (heard.text, heard.trigger, heard.via) == ("Chrome kholo", "tap", "stream")
    assert {"speech_end_to_transcript", "trigger_to_transcript", "connect", "speech"} <= set(heard.timings)
    assert heard.wav[:4] == b"RIFF"
    assert listener.player.chimes == ["start", "stop"]
    assert events.of("trigger") == ["tap"]
    assert events.of("state")[:2] == ["listening", "hearing"]
    assert all(0.0 <= v <= 1.0 for v in events.of("level")) and events.of("level")
    assert listener._capture is None  # ready for the next turn


def test_local_endpoint_backs_up_a_slow_server(prisma, make_listener):
    server = prisma(texts=["YouTube kholo"], latency_s=0.4)
    listener, events = make_listener(server)
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(1.0), silence(1.5)))  # the local endpoint fires before the transcript arrives
    thread.join(5)
    assert box["result"].text == "YouTube kholo"
    assert "transcribing" in events.of("state")
    assert [c["type"] for c in server.controls] == ["commit", "update_settings"]


def test_hold_collects_every_segment_until_release(prisma, make_listener):
    server = prisma(texts=["Raju ko message bhejo", "ki main aa raha hoon"])
    listener, events = make_listener(server)
    pressed = threading.Event()
    pressed.set()
    listener._key_pressed = pressed.is_set
    thread, box = in_thread(listener.next_command, 5.0)
    listener._on_hotkey()
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(0.8), silence(1.2), speech(0.8), silence(0.2)))  # a pause the server splits at
    time.sleep(0.6)  # still held
    pressed.clear()
    thread.join(6)
    heard = box["result"]
    assert heard.text == "Raju ko message bhejo ki main aa raha hoon"
    assert heard.trigger == "hotkey"
    assert events.of("trigger") == ["hotkey"]


def test_quick_tap_listens_until_the_user_stops(prisma, make_listener):
    server = prisma(texts=["Notepad kholo"])
    listener, events = make_listener(server)
    pressed = threading.Event()
    pressed.set()
    listener._key_pressed = pressed.is_set
    thread, box = in_thread(listener.next_command, 5.0)
    listener._on_hotkey()
    time.sleep(0.1)
    pressed.clear()  # a tap
    wait_for(lambda: "tap" in events.of("trigger"))
    feed(listener, cat(silence(0.3), speech(1.0), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    assert box["result"].text == "Notepad kholo"
    assert box["result"].trigger == "tap"


def test_second_tap_stops_listening(prisma, make_listener):
    server = prisma(texts=["Chrome kholo"])
    listener, _ = make_listener(server)
    listener._key_pressed = lambda: False
    thread, box = in_thread(listener.next_command, 5.0)
    listener._on_hotkey()
    wait_for(lambda: listener._capture is not None and not listener._capture.hold)
    feed(listener, speech(1.0))  # still talking...
    wait_for(lambda: not listener._hotkey_down)
    listener._on_hotkey()  # ...tap again: stop now
    thread.join(5)
    assert box["result"].text == "Chrome kholo"


def test_wake_word_turn_streams_the_preroll_and_strips_the_phrase(prisma, make_listener):
    server = prisma(texts=["Hey Bol OS, Chrome kholo"])
    wake_audio = cat(silence(1.0), speech(0.9))
    fire = len(_frames(wake_audio))  # the detector fires as the phrase ends
    wake = FakeWake(fire_at=[fire])
    listener, events = make_listener(server, wake=wake)
    thread, box = in_thread(listener.next_command, 5.0)
    feed(listener, cat(wake_audio, speech(0.8), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    heard = box["result"]
    assert (heard.text, heard.trigger) == ("Chrome kholo", "wake")
    assert events.of("trigger") == ["wake"]
    assert "Bol OS" in json.loads(server.headers["keyterms"])
    # ~1.5 s of audio from before the trigger went to Prisma too: it holds the wake phrase.
    assert len(server.frame_sizes) * FRAME / SR > 1.5 + 0.8
    assert listener.player.chimes == ["stop"]  # no start chime before the phrase is verified
    assert wake.resets >= 1  # restarted after the turn


def test_squeezer_shortens_long_silences_only_while_the_stream_lags():
    keep = L.SilenceSqueezer().keep
    floor = -64.0  # silence() is about -64 dBFS
    n_lead, n_speech, n_gap, n_speech2, n_tail = 32, 16, 3, 8, 32  # in frames, so the parts align with them
    s = FRAME / SR
    frames = _frames(cat(silence(n_lead * s), speech(n_speech * s), silence(n_gap * s, seed=1),
                         speech(n_speech2 * s), silence(n_tail * s, seed=2)))
    squeezer = L.SilenceSqueezer()
    pcm = squeezer.take(frames, floor, sent_bytes=0)  # nothing sent yet: far behind
    assert squeezer.dropped == (n_lead - keep) + (n_tail - keep)
    assert len(pcm) == (len(frames) - squeezer.dropped) * FRAME * 2 == squeezer.queued_bytes
    # The speech and the short gap inside it arrive intact and in order.
    body = np.frombuffer(pcm, np.int16)[keep * FRAME:(keep + n_speech) * FRAME]
    assert np.array_equal(body, np.concatenate(frames[n_lead:n_lead + n_speech]))

    live = L.SilenceSqueezer()  # the server has everything already: nothing is dropped
    for frame in frames:
        live.take([frame], floor, sent_bytes=live.queued_bytes)
    assert live.dropped == 0 and live.dropped_ms == 0.0


def test_lagging_wake_turn_squeezes_preroll_silence_and_still_verifies(prisma, make_listener):
    server = prisma(texts=["Hey Bol OS, Chrome kholo"])
    wake_audio = cat(silence(1.0), speech(0.9))
    wake = FakeWake(fire_at=[len(_frames(wake_audio))])
    listener, _ = make_listener(server, wake=wake, squeeze=True)
    thread, box = in_thread(listener.next_command, 6.0)
    feed(listener, wake_audio)  # already recorded when the detector fires: the stream starts ~1.5 s behind
    feed(listener, cat(silence(0.15), speech(0.8), silence(1.5)), speed=1.0)  # then live, at mic pace
    thread.join(6)
    heard = box["result"]
    assert (heard.text, heard.trigger, heard.via) == ("Chrome kholo", "wake", "stream")
    assert heard.timings["squeezed"] >= 200  # the silence before "Hey" did not delay the command
    assert not any(c.get("type") == "commit" for c in server.controls[:-2])  # no early cut


def test_false_wake_trigger_is_ignored_silently(prisma, make_listener):
    server = prisma(texts=["haan bolo kya hua"])
    wake = FakeWake(fire_at=[30])
    listener, events = make_listener(server, wake=wake)
    thread, box = in_thread(listener.next_command, 5.0)
    feed(listener, cat(speech(1.0), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    assert box["result"] is None
    assert listener.player.chimes == []  # no sound at all
    assert events.of("state")[-1] == "ready"


def test_wake_phrase_alone_then_the_command(prisma, make_listener):
    server = prisma(texts=["Hey Bolos.", "Notepad kholo"])
    wake = FakeWake(fire_at=[28])
    listener, events = make_listener(server, wake=wake)
    thread, box = in_thread(listener.next_command, 8.0)
    feed(listener, cat(speech(0.9), silence(0.7)))
    feed(listener, silence(0.8), speed=2.0)  # the user waits for the chime
    wait_for(lambda: "start" in listener.player.chimes)
    feed(listener, cat(speech(1.0), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    assert box["result"].text == "Notepad kholo"
    assert listener.player.chimes == ["start", "stop"]


def test_wake_preroll_with_a_fragment_of_earlier_speech(prisma, make_listener):
    server = prisma(texts=["haan", "Hey Bol OS Chrome kholo"], latency_s=0.3)
    audio = cat(silence(0.5), speech(0.25), silence(0.65), speech(0.55))  # the last 1.5 s holds both
    wake = FakeWake(fire_at=[len(_frames(audio))])
    listener, _ = make_listener(server, wake=wake)
    thread, box = in_thread(listener.next_command, 6.0)
    feed(listener, cat(audio, speech(0.6), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    assert box["result"].text == "Chrome kholo"


def test_nobody_speaks_gives_up_quietly(prisma, make_listener):
    server = prisma()
    listener, events = make_listener(server)
    thread, box = in_thread(listener.listen_now, "followup", 0.5)
    wait_for(listener._capture_ready.is_set)
    start = time.monotonic()
    threading.Thread(target=feed, args=(listener, silence(3.0), 1.0), daemon=True).start()
    thread.join(5)
    gave_up_after = time.monotonic() - start
    assert box["result"] is None
    assert gave_up_after < 2.0  # the 0.5 s no-speech timeout, not the end of the audio (3 s)
    assert listener.player.chimes == ["start"]  # no stop or error sound for silence
    assert events.of("state")[-1] == "ready"


def test_followup_listens_without_a_trigger(prisma, make_listener):
    server = prisma(texts=["haan bhej do"])
    listener, events = make_listener(server)
    thread, box = in_thread(listener.followup, 3.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(0.6), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    thread.join(5)
    assert (box["result"].text, box["result"].trigger) == ("haan bhej do", "followup")
    assert events.of("trigger") == ["followup"]


def test_very_long_speech_is_committed(prisma, make_listener, monkeypatch):
    monkeypatch.setattr(L, "MAX_SPEECH_S", 0.6)
    server = prisma(texts=["bahut lambi baat"])
    listener, _ = make_listener(server, min_silence_ms=2000)
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, speech(1.5), speed=4.0)
    thread.join(5)
    assert box["result"].text == "bahut lambi baat"
    assert server.controls[0] == {"type": "commit"}


def test_websocket_refused_falls_back_to_rest(prisma, make_listener):
    rest = FakeRest("Calculator kholo")
    listener, _ = make_listener(prisma(refuse=True), rest=rest)
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(1.0), silence(1.5)))
    thread.join(5)
    assert (box["result"].text, box["result"].via) == ("Calculator kholo", "rest")
    assert rest.calls and rest.calls[0][0][:4] == b"RIFF"


def test_websocket_dropping_mid_turn_falls_back_to_rest(prisma, make_listener):
    rest = FakeRest("Excel kholo")
    listener, _ = make_listener(prisma(drop_after_frames=10), rest=rest)
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(1.0), silence(1.2)), speed=8.0)
    thread.join(5)
    assert (box["result"].text, box["result"].via) == ("Excel kholo", "rest")


def test_rest_fallback_checks_the_wake_phrase(prisma, make_listener):
    rest = FakeRest("hello ji")
    listener, _ = make_listener(prisma(refuse=True), rest=rest, wake=FakeWake(fire_at=[30]))
    thread, box = in_thread(listener.next_command, 5.0)
    feed(listener, cat(speech(1.0), speech(1.0), silence(1.5)))
    thread.join(5)
    assert box["result"] is None
    assert "Bol OS" in rest.calls[0][1]


def test_rest_is_skipped_when_nobody_spoke(make_listener):
    rest = FakeRest()
    listener, _ = make_listener(None, rest=rest)  # no server at all
    thread, box = in_thread(listener.listen_now, "tap", 0.3)
    wait_for(listener._capture_ready.is_set)
    feed(listener, silence(1.0), speed=2.0)
    thread.join(5)
    assert box["result"] is None and rest.calls == []


def test_microphone_is_ignored_while_speaking(make_listener, monkeypatch):
    monkeypatch.setattr(L, "MUTE_TAIL_S", 0.1)
    wake = FakeWake()
    listener, _ = make_listener(None, wake=wake)
    feed(listener, silence(0.2))
    wait_for(lambda: wake.fed == 6)
    with listener.speaking():
        feed(listener, speech(0.5))
        time.sleep(0.1)
        assert wake.fed == 6  # Bol OS's own voice never reaches the wake detector
    resets = wake.resets
    time.sleep(0.15)  # past the tail
    feed(listener, silence(0.1))
    wait_for(lambda: wake.fed == 9)
    assert wake.resets == resets + 1  # restarted once it can hear again
    assert len(listener._ring) == 3  # and the pre-roll holds nothing of Bol OS's voice


def test_speak_plays_clips_and_text_while_muted(make_listener):
    listener, _ = make_listener(None)
    seen = []
    listener.player.play = lambda samples, rate: seen.append(listener._speaking) or True
    from bolos.audio import to_wav

    assert listener.speak(to_wav(speech(0.2)), "hi")
    assert seen == [1]
    assert listener.speak("ho gaya", "hi")
    assert listener.tts.spoken == [("ho gaya", "hi")]
    assert listener._speaking == 0


def test_hotkey_interrupts_speech(prisma, make_listener):
    listener, _ = make_listener(prisma(texts=["ruko"]))
    listener._key_pressed = lambda: False
    with listener.speaking():
        listener._on_hotkey()
        wait_for(listener._capture_ready.is_set)
    assert listener.player.stops == 1  # barge-in stops playback at once
    feed(listener, cat(speech(0.6), silence(0.7)))
    feed(listener, silence(1.0), speed=1.0)
    assert listener.next_command(5.0).text == "ruko"


def test_level_meter_reports_about_20_times_a_second(prisma, make_listener):
    listener, events = make_listener(prisma(texts=["Chrome kholo"]))
    thread, box = in_thread(listener.listen_now, "tap", 5.0)
    wait_for(listener._capture_ready.is_set)
    feed(listener, cat(speech(1.0), silence(0.7)), speed=1.0)
    thread.join(5)
    assert 12 <= len(events.of("level")) <= 32


def _finished_capture(trigger="hotkey", seconds=1):
    cap = Capture(trigger, [np.zeros(int(seconds * SR), dtype=np.int16)], time.monotonic(), 5)
    cap.endpointer.started = True
    cap.end("released")
    return cap


def test_rest_replaces_already_transcribed_segments_instead_of_duplicating(make_listener):
    rest = FakeRest("open Chrome then search weather")
    listener, _ = make_listener(rest=rest)
    heard = listener._transcribe_rest(_finished_capture(), prefix=["open Chrome"])
    assert heard.text == "open Chrome then search weather"
    assert len(rest.calls) == 1


def test_rest_rechecks_wake_phrase_even_after_a_verified_stream_segment(make_listener):
    rest = FakeRest("open Chrome")
    listener, _ = make_listener(rest=rest)
    assert listener._transcribe_rest(_finished_capture("wake"), prefix=["open Chrome"]) is None
    rest.text = "Hey Bol OS open Chrome"
    assert listener._transcribe_rest(_finished_capture("wake"), prefix=["open Chrome"]).text == "open Chrome"


def test_rest_never_truncates_a_long_capture(make_listener):
    rest = FakeRest("open Chrome")
    listener, _ = make_listener(rest=rest)
    assert listener._transcribe_rest(_finished_capture(seconds=30), prefix=["open Chrome"]) is None
    assert rest.calls == []


@pytest.mark.parametrize("termination", ["closed", "timeout"])
def test_unfinished_stream_cannot_return_only_its_first_instruction(make_listener, monkeypatch, termination):
    rest = FakeRest("open Chrome then search weather")
    listener, _ = make_listener(rest=rest)
    monkeypatch.setattr(L, "BARRIER_WAIT_S", 0.03)

    class PartialStream:
        connect_ms = 1
        sent_bytes = 0

        def __init__(self):
            self.messages = queue.Queue()
            self.messages.put({"type": "transcript", "text": "open Chrome", "is_final": True,
                               "utterance_id": "one"})
            self.messages.put({"type": "speech_start", "utterance_id": "two"})
            if termination == "closed":
                self.messages.put({"type": "closed"})

        def open(self):
            pass

        def send_audio(self, pcm):
            self.sent_bytes += len(pcm)

        def flush(self):
            pass

        def commit(self):
            pass

        def send_control(self, message):
            pass

        def events(self):
            return self.messages

        def close(self, wait=True):
            pass

    listener._stream_factory = lambda terms: PartialStream()
    heard = listener._transcribe(_finished_capture())
    assert heard.text == "open Chrome then search weather"
    assert heard.via == "rest"
    assert len(rest.calls) == 1


def test_stream_ignores_an_explicit_nonfinal_transcript(make_listener):
    from types import SimpleNamespace

    listener, _ = make_listener()
    messages = queue.Queue()
    messages.put({"type": "transcript", "text": "open Chrome", "is_final": False, "utterance_id": "one"})
    messages.put({"type": "transcript", "text": "do not open Chrome", "is_final": True, "utterance_id": "one"})
    stream = SimpleNamespace(connect_ms=0, events=lambda: messages, send_audio=lambda pcm: None)
    cap = Capture("tap", [], time.monotonic(), 5)
    assert listener._stream_turn(cap, stream).text == "do not open Chrome"


def test_hard_capped_stream_does_not_return_an_incomplete_command(make_listener):
    from types import SimpleNamespace

    listener, _ = make_listener()
    cap = _finished_capture()
    cap.reason = "max_capture"
    assert listener._stream_turn(cap, SimpleNamespace(events=queue.Queue)) is None
