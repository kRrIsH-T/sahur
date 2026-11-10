"""Entry-point integration without microphones, speakers, real apps, or network calls."""

import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bolos import __main__ as main
from bolos.config import load_settings
from bolos.contacts import ContactBook
from bolos.listen import Capture, Heard, Listener
from bolos.speech import PrismaStream


class FakeListener:
    def __init__(self):
        self.started = self.closed = self.commands = self.answers = 0
        self.private_turn = False
        self.private_hints = []
        self.spoken = []

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def next_command(self):
        self.commands += 1
        return Heard("Chrome kholo", "tap", "stream")

    def followup(self):
        self.answers += 1
        return Heard("private detail" if self.private_turn else "haan", "followup", "stream")

    def speak(self, text, language):
        self.spoken.append((text, language))
        return True

    @contextmanager
    def private(self, hints=()):
        self.private_turn = True
        self.private_hints.append(list(hints))
        try:
            yield
        finally:
            self.private_turn = False

    @contextmanager
    def speaking(self, private=False):
        yield threading.Event()


def test_spoken_voice_routes_commands_and_automatic_followup(capsys):
    listener = FakeListener()
    voice = main.SpokenVoice(load_settings(), ContactBook([]), listener=listener)
    voice.start()
    assert voice.listen() == ("Chrome kholo", None)
    assert voice.last_heard.trigger == "tap"
    assert voice.followup() == ("haan", None)
    assert voice.speak("Done.", "en")
    voice.close()
    assert (listener.started, listener.closed, listener.commands, listener.answers) == (1, 1, 1, 1)
    assert listener.spoken == [("Done.", "en")]
    assert "Chrome kholo" in capsys.readouterr().out


def test_private_answers_do_not_print_or_replace_command_metadata(capsys):
    listener = FakeListener()
    voice = main.SpokenVoice(load_settings(), ContactBook([]), listener=listener)
    voice.listen()
    previous = voice.last_heard
    capsys.readouterr()
    assert voice.listen_private(hints=["Sunita", "Kumar"]) == ("private detail", None)
    assert listener.private_hints == [["Sunita", "Kumar"]]
    assert voice.last_heard is previous
    assert listener.spoken == []
    assert "private detail" not in capsys.readouterr().out


def test_a_private_turn_uses_its_own_hints_and_then_the_command_ones_again():
    listener = Listener(replace(load_settings(), gnani_api_key=""), ["WhatsApp", "Chrome"], use_mic=False,
                        use_hotkey=False)
    assert listener._keyterms() == ["WhatsApp", "Chrome"]
    with listener.private(["Sunita", "Mysuru"]):
        assert listener._keyterms() == ["Sunita", "Mysuru"]
    with listener.private():
        assert listener._keyterms() == ["WhatsApp", "Chrome"]  # no hints: the usual words
    assert listener._keyterms() == ["WhatsApp", "Chrome"]


def test_dry_run_entrypoint_never_constructs_api_clients_or_executor(monkeypatch, tmp_path, capsys):
    import bolos.executor.apps
    import bolos.llm
    import bolos.tts_cache

    settings = replace(load_settings(), gnani_api_key="configured-but-unused", decider="llm",
                       contacts_path=tmp_path / "contacts.json", preferences_path=tmp_path / "preferences.json")
    monkeypatch.setattr(main, "load_settings", lambda overrides=None: settings)
    monkeypatch.setenv("BOLOS_SETTINGS", str(tmp_path / "settings.json"))  # not the user's own dashboard choices
    monkeypatch.setattr(bolos.executor.apps, "Catalog", lambda *_: object())

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run tried to use a real service")

    monkeypatch.setattr(bolos.llm, "make_llm", forbidden)
    monkeypatch.setattr(bolos.tts_cache, "CachedTimbre", forbidden)
    monkeypatch.setattr(main, "Executor", forbidden)
    modes, handled = [], []
    monkeypatch.setattr(main, "make_parser", lambda settings, llm, mode: modes.append(mode) or object())

    class FakeAssistant:
        def __init__(self, parser, llm, runner, voice, **kwargs):
            assert llm is None
            assert isinstance(runner, main.DryRun)
            assert not kwargs["learning"]
            assert kwargs["history"] is None and kwargs["forms"] is None
            assert kwargs["language_detector"]("Notepad open cheyyandi") == "te"

        def handle(self, *args, **kwargs):
            handled.append((args, kwargs))

    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    lines = iter(["Chrome kholo"])

    def read(_):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr("sys.argv", ["bolos", "--text", "--dry-run", "--no-overlay", "--no-dashboard"])
    main.main()
    assert modes == ["fast"]
    assert handled == [(("Chrome kholo", None), {"trigger": "typed"})]
    assert not (tmp_path / "preferences.json").exists()
    assert not (tmp_path / "history.jsonl").exists()
    assert "Ready." in capsys.readouterr().out


def test_hints_are_deduplicated_and_capped_even_with_many_contacts():
    class Contacts:
        def names(self):
            return ["Chrome", "chrome"] + [f"Person {i}" for i in range(250)]

    hints = main.recognition_hints(Contacts(), ["Editor"])
    assert len(hints) == 200
    assert len({hint.casefold() for hint in hints}) == 200


def test_close_unblocks_a_waiting_listener():
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)
    listener.start()
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(listener.next_command()))
    waiter.start()
    listener.close()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert answers == [None]


def test_private_listener_does_not_log_transcript_or_exception(monkeypatch, caplog):
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)
    caplog.set_level("INFO")
    monkeypatch.setattr(listener, "_transcribe", lambda cap: Heard("secret value", "followup", "stream"))
    with listener.private():
        assert listener._run(Capture("followup", [], 0, 1)).text == "secret value"

    def fail(cap):
        raise ValueError("secret value")

    monkeypatch.setattr(listener, "_transcribe", fail)
    with listener.private():
        assert listener._run(Capture("followup", [], 0, 1)) is None
    assert "secret value" not in caplog.text


def test_device_start_failure_closes_listener(monkeypatch):
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)

    def fail():
        raise RuntimeError("microphone unavailable")

    monkeypatch.setattr(listener, "_start_devices", fail)

    with pytest.raises(RuntimeError, match="microphone unavailable"):
        listener.start()
    assert not listener._running
    assert listener._pump is None


def test_listener_stays_open_and_retries_a_missing_microphone(monkeypatch):
    import sounddevice

    attempts = []
    events = []

    class Microphone:
        closed = False

        def __init__(self, **kwargs):
            attempts.append(self)

        def start(self):
            if len(attempts) == 1:
                raise sounddevice.PortAudioError("no audio device")

        def stop(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(sounddevice, "InputStream", Microphone)
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_hotkey=False,
                        status=lambda *event: events.append(event))
    with listener:
        assert listener.device_issue()[0] == "microphone_unavailable"
        assert "Windows" in listener.mic_error
        assert attempts[0].closed
        listener._on_hotkey()
        assert listener.next_command(0) is None  # pressing the shortcut cannot send empty audio to Prisma
        listener.set_input_device(None)
        assert listener.device_issue() is None
        assert listener.mic_error is None
        assert events[-2:] == [("reply", ""), ("state", "ready")]
    assert len(attempts) == 2 and all(mic.closed for mic in attempts)


@pytest.mark.parametrize("shortcut", ["ctrl+space", "space"])
def test_unavailable_startup_shortcut_can_be_replaced_live(monkeypatch, shortcut):
    from bolos import hotkey

    attempted = []

    class Shortcut:
        def __init__(self, combination, callback):
            self.combination = combination

        def start(self):
            attempted.append(self.combination)
            if len(attempted) == 1 and self.combination == "ctrl+space":
                raise hotkey.HotkeyError("shortcut taken")

        def stop(self):
            pass

    monkeypatch.setattr(hotkey, "GlobalHotkey", Shortcut)
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, hotkey=shortcut)
    with listener:
        assert listener.device_issue()[0] == "shortcut_unavailable"
        assert "Sahur Settings" in listener.hotkey_error
        retry = "ctrl+alt+k" if shortcut == "space" else shortcut
        listener.set_hotkey(retry)
        assert listener.device_issue() is None
        assert listener.hotkey == retry
    assert attempted == (["ctrl+alt+k"] if shortcut == "space" else [shortcut, shortcut])


@pytest.mark.parametrize("error", [FileNotFoundError, main.BadZipFile, ValueError])
def test_optional_language_model_does_not_block_startup(monkeypatch, error):
    from bolos.fast import model

    def unavailable():
        raise error("model is missing or damaged")

    monkeypatch.setattr(model, "LanguageModel", unavailable)
    assert main.make_language_detector(None) is None


def test_voice_dry_run_has_no_reply_synthesizer(monkeypatch, capsys):
    from bolos import tts_cache

    def forbidden(*args):
        raise AssertionError("dry run created a speech synthesizer")

    monkeypatch.setattr(tts_cache, "CachedTimbre", forbidden)
    settings = replace(load_settings(), gnani_api_key="speech-recognition-only")
    voice = main.SpokenVoice(settings, ContactBook([]), silent=True)
    assert voice.listener.tts is None
    assert voice.speak("Test complete.", "en")
    assert "Test complete." in capsys.readouterr().out


def test_startup_opens_settings_when_the_microphone_needs_help(monkeypatch, tmp_path, capsys):
    import webbrowser

    import sounddevice

    from bolos.dashboard import server
    from bolos.executor import apps

    settings = replace(load_settings(), gnani_api_key="speech-test", contacts_path=tmp_path / "contacts.json",
                       preferences_path=tmp_path / "preferences.json")
    monkeypatch.setenv("BOLOS_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(main, "load_settings", lambda overrides=None: settings)
    monkeypatch.setattr(apps, "Catalog", lambda *_: SimpleNamespace(names=lambda: []))
    monkeypatch.setattr(Listener, "set_hotkey", lambda *args: None)

    def no_microphone(*args):
        raise sounddevice.PortAudioError("private system error details")

    def exit_loop(*args):
        raise EOFError

    monkeypatch.setattr(Listener, "_open_mic", no_microphone)
    monkeypatch.setattr(Listener, "next_command", exit_loop)
    opened, closed = [], []

    class Dashboard:
        url = "http://127.0.0.1:8765/"

        def __init__(self, *args):
            pass

        def start(self):
            return self.url

        def close(self):
            closed.append(True)

    monkeypatch.setattr(server, "Dashboard", Dashboard)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr("sys.argv", ["bolos", "--dry-run", "--no-overlay"])
    main.main()
    assert opened == [Dashboard.url] and closed == [True]
    output = capsys.readouterr().out
    assert "Sahur can't use the microphone" in output
    assert "private system error details" not in output


def test_startup_shows_setup_settings_when_the_speech_key_is_missing(monkeypatch, tmp_path, capsys):
    from bolos.dashboard import __main__ as dashboard_main

    settings = replace(load_settings(), gnani_api_key="")
    monkeypatch.setattr(main, "load_settings", lambda overrides=None: settings)
    monkeypatch.setenv("BOLOS_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr("sys.argv", ["bolos"])
    opened = []
    monkeypatch.setattr(dashboard_main, "main", opened.append)
    main.main()
    assert opened == [[]]
    assert "Voice setup is incomplete" in capsys.readouterr().out


@pytest.mark.parametrize("typed", [True, False])
def test_form_speech_consent_is_wired_only_for_voice_mode(monkeypatch, tmp_path, typed):
    from bolos import llm
    from bolos.executor import apps

    settings = replace(load_settings(), gnani_api_key="speech-test", contacts_path=tmp_path / "contacts.json",
                       preferences_path=tmp_path / "preferences.json")
    monkeypatch.setenv("BOLOS_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(main, "load_settings", lambda overrides=None: settings)
    monkeypatch.setattr(llm, "make_llm", lambda *_: None)
    monkeypatch.setattr(apps, "Catalog", lambda *_: SimpleNamespace(names=lambda: []))
    monkeypatch.setattr(Listener, "_start_devices", lambda *_: None)

    def exit_loop(*args):
        raise EOFError

    monkeypatch.setattr(Listener, "next_command", exit_loop)
    monkeypatch.setattr("builtins.input", exit_loop)
    attached = []
    monkeypatch.setattr(main, "Assistant", lambda *args, **kwargs: attached.append(kwargs["forms"]))
    flags = ["--text", "--no-speech"] if typed else []
    monkeypatch.setattr("sys.argv", ["bolos", "--no-overlay", "--no-dashboard", *flags])
    main.main()
    forms, = attached
    assert forms.ask_consent is not typed
    if typed:
        assert forms.consent is None
    else:
        assert forms.consent.path == tmp_path / ".cache" / "form-speech-consent"
        assert not forms.consent.given()  # startup cannot grant permission on the user's behalf


def test_second_tap_preserves_microphone_frames_waiting_in_the_queue():
    from bolos.audio import FRAME

    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)
    listener._running = True
    cap = Capture("tap", [], time.monotonic(), 5)
    listener._capture = cap
    listener.feed(np.zeros(FRAME * 3, dtype=np.int16))
    listener._hotkey_turn()
    assert not cap.ended  # the stop follows the already recorded frames
    listener._frames.put(None)
    listener._pump_loop()
    assert cap.reason == "released"
    assert cap.end_index == 3


def test_malformed_speech_response_does_not_leak_personal_details(caplog):
    stream = PrismaStream(load_settings())
    stream._ws = iter(["private value in malformed response"])
    stream._receive()
    assert "private value" not in caplog.text
    assert "malformed JSON" in caplog.text


def test_shutdown_does_not_send_partial_audio_to_rest(monkeypatch):
    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)
    cap = Capture("tap", [], time.monotonic(), 5)
    cap.end("closed")

    def forbidden(*args, **kwargs):
        raise AssertionError("shutdown sent private audio")

    monkeypatch.setattr(listener.rest, "transcribe", forbidden)
    assert listener._transcribe_rest(cap) is None


def test_private_readback_audio_is_never_captured_even_during_hotkey_barge_in():
    from bolos.audio import FRAME

    listener = Listener(replace(load_settings(), gnani_api_key=""), use_mic=False, use_hotkey=False)
    cap = Capture("hotkey", [], time.monotonic(), 5, hold=True)
    listener._capture = cap
    with listener.speaking(private=True):
        listener._on_frame(np.ones(FRAME, dtype=np.int16), time.monotonic())
    listener._on_frame(np.ones(FRAME, dtype=np.int16), time.monotonic())
    assert cap.frames == []  # includes the acoustic tail after private speech


def test_typed_reply_survives_a_speech_service_outage_without_logging_text(monkeypatch, caplog, capsys):
    from bolos.speech import SpeechError

    voice = main.TypedVoice(replace(load_settings(), gnani_api_key=""))

    class FailedSpeech:
        def synthesize(self, text, language):
            raise SpeechError("service failure containing private reply")

    voice.tts = FailedSpeech()
    assert voice.speak("the visible reply", "en") is False
    assert "the visible reply" in capsys.readouterr().out
    assert "private reply" not in caplog.text
