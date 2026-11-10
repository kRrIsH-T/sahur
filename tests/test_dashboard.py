"""The dashboard: saved settings, applying them to a running Bol OS, statistics and the local web server."""

import json
import socket
from dataclasses import replace
from datetime import date, datetime
from http.client import HTTPConnection

import pytest

from bolos.config import load_settings
from bolos.controls import Controls, MicMeter
from bolos.dashboard import stats
from bolos.dashboard.server import Dashboard, find_dashboard
from bolos.history import History
from bolos.hotkey import HotkeyError
from bolos.overlay import OverlayState, place
from bolos.user_settings import SettingError, UserSettings, env_defaults

BASE = replace(load_settings(), hotkey="ctrl+space", user_language="hi-IN", stt_languages="hi-IN,en-IN",
               tts_voice="Shivani", decider="system1", llm_provider="codex", codex_model="gpt-6-luna",
               codex_effort="low", codex_fast=False, wake_enabled=False)


def store(tmp_path, saved=None):
    path = tmp_path / "settings.json"
    if saved is not None:
        path.write_text(json.dumps(saved), encoding="utf-8")
    return UserSettings.load(path, env_defaults(BASE))


# The saved settings


def test_defaults_come_from_env_and_only_changes_are_saved(tmp_path):
    s = store(tmp_path)
    assert s.get("hotkey") == "ctrl+space" and s.get("stt_languages") == ["hi-IN", "en-IN"]
    assert s.update({"widget_visible": False, "hotkey": "Ctrl + Alt + B"}) == {"widget_visible": False,
                                                                               "hotkey": "ctrl+alt+b"}
    assert json.loads((tmp_path / "settings.json").read_text()) == {"widget_visible": False, "hotkey": "ctrl+alt+b"}
    # Back to the default: the key leaves the file, so a later .env change is followed again.
    s.update({"hotkey": "ctrl+space"})
    assert json.loads((tmp_path / "settings.json").read_text()) == {"widget_visible": False}
    assert s.changed() == {"widget_visible"}


def test_saved_values_override_settings(tmp_path):
    s = store(tmp_path, {"stt_languages": ["ta-IN", "hi-IN"], "tts_voice": "Kalpana", "chimes": False})
    overrides = s.overrides()
    assert overrides == {"stt_languages": "hi-IN,ta-IN", "tts_voice": "Kalpana"}  # canonical order; no chimes
    settings = load_settings(overrides)
    assert settings.stt_languages == "hi-IN,ta-IN" and settings.tts_voice == "Kalpana"


@pytest.mark.parametrize("change", [
    {"hotkey": "space"},  # no modifier: it would swallow a normal key
    {"hotkey": "ctrl+nonsense"},
    {"stt_languages": []},
    {"stt_languages": ["xx-IN"]},
    {"stt_languages": ["hi-IN", {}]},
    {"user_language": []},
    {"tts_voice": "Avinash"},  # a man's voice would contradict the feminine replies
    {"widget_visible": "yes"},
    {"codex_model": "   "},
    {"no_such_setting": 1},
])
def test_bad_values_are_refused_and_nothing_is_saved(tmp_path, change):
    s = store(tmp_path)
    with pytest.raises(SettingError):
        s.update({"chimes": False, **change})
    assert s.get("chimes") is True and not (tmp_path / "settings.json").exists()


def test_an_unreadable_or_bad_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert store(tmp_path).get("widget_visible") is True
    s = store(tmp_path, {"widget_corner": "middle", "chimes": False})
    assert s.get("widget_corner") == "bottom-right" and s.get("chimes") is False
    s = store(tmp_path, {"user_language": {}, "stt_languages": ["hi-IN", {}], "chimes": False})
    assert s.get("user_language") == "hi-IN" and s.get("stt_languages") == ["hi-IN", "en-IN"]
    assert s.get("chimes") is False


def test_a_device_name_retains_its_exact_spacing(tmp_path):
    s = store(tmp_path)
    s.update({"input_device": "USB  microphone (headset)"})
    assert store(tmp_path).get("input_device") == "USB  microphone (headset)"


# Applying them to a running Bol OS


class FakePlayer:
    device = None


class FakeListener:
    use_mic = True

    def __init__(self, fail_hotkey=False, fail_mic=False):
        self.hotkey, self.input_device, self.earcons = "ctrl+space", None, True
        self.player, self.settings = FakePlayer(), None
        self.mic_level, self.mic_error, self.hotkey_error = 0.4, None, None
        self.fail_hotkey, self.fail_mic = fail_hotkey, fail_mic

    def set_hotkey(self, combination):
        if self.fail_hotkey:
            raise HotkeyError("taken")
        self.hotkey, self.hotkey_error = combination, None

    def set_input_device(self, device):
        if self.fail_mic:
            raise OSError("device busy")
        self.input_device, self.mic_error = device, None

    def update_settings(self, settings):
        self.settings = settings


class FakeVoice:
    def __init__(self, listener):
        self.listener, self.user_language = listener, "hi-IN"


class FakeOverlay:
    def __init__(self):
        self.events, self.visible = [], True

    def set(self, kind, value=None):
        self.events.append((kind, value))

    def show(self, visible):
        self.visible = visible


class FakeHistory:
    enabled = True


def running(tmp_path, monkeypatch, **listener):
    monkeypatch.setattr("bolos.controls.load_settings", lambda overrides=None: replace(BASE, **(overrides or {})))
    monkeypatch.setattr(Controls, "_device", staticmethod(lambda name, kind: {"USB mic": 3, "Speakers": 7}.get(name)))
    voice = FakeVoice(FakeListener(**listener))
    controls = Controls(store(tmp_path), voice=voice, overlay=FakeOverlay(), history=FakeHistory())
    return controls, voice


def test_changes_reach_the_running_parts_at_once(tmp_path, monkeypatch):
    controls, voice = running(tmp_path, monkeypatch)
    controls.update({"widget_visible": False, "widget_corner": "top-left", "chimes": False, "keep_history": False,
                     "hotkey": "ctrl+alt+b", "input_device": "USB mic", "output_device": "Speakers",
                     "tts_voice": "Lakshmi", "user_language": "ta-IN"})
    listener = voice.listener
    assert not controls.overlay.visible
    assert ("corner", "top-left") in controls.overlay.events and ("hotkey", "ctrl+alt+b") in controls.overlay.events
    assert listener.earcons is False and controls.history.enabled is False
    assert listener.hotkey == "ctrl+alt+b" and listener.input_device == 3 and listener.player.device == 7
    assert voice.user_language == "ta-IN"
    assert listener.settings.tts_voice == "Lakshmi"


@pytest.mark.parametrize("change, failure", [({"hotkey": "ctrl+alt+b"}, {"fail_hotkey": True}),
                                             ({"input_device": "USB mic"}, {"fail_mic": True})])
def test_a_change_that_cannot_be_applied_is_not_saved(tmp_path, monkeypatch, change, failure):
    controls, voice = running(tmp_path, monkeypatch, **failure)
    with pytest.raises(SettingError):
        controls.update({"chimes": False, **change})
    assert controls.store.get("chimes") is True and voice.listener.earcons is True
    assert voice.listener.hotkey == "ctrl+space" and voice.listener.input_device is None


def test_a_failed_second_live_change_restores_the_first(tmp_path, monkeypatch):
    controls, voice = running(tmp_path, monkeypatch, fail_mic=True)
    with pytest.raises(SettingError, match="couldn't use that microphone"):
        controls.update({"hotkey": "ctrl+alt+b", "input_device": "USB mic"})
    assert voice.listener.hotkey == controls.store.get("hotkey") == "ctrl+space"


def test_a_failed_settings_write_does_not_change_live_or_saved_settings(tmp_path, monkeypatch):
    controls, voice = running(tmp_path, monkeypatch)
    controls.store.path.mkdir()  # replacing a directory with the settings file must fail
    with pytest.raises(SettingError, match="couldn't save your changes"):
        controls.update({"hotkey": "ctrl+alt+b", "input_device": "USB mic", "chimes": False})
    assert voice.listener.hotkey == controls.store.get("hotkey") == "ctrl+space"
    assert voice.listener.input_device is None and controls.store.get("input_device") == ""
    assert voice.listener.earcons and controls.store.get("chimes")
    assert list(tmp_path.iterdir()) == [controls.store.path]  # no abandoned temporary files


def test_the_saved_microphone_and_shortcut_can_be_retried(tmp_path, monkeypatch):
    controls, voice = running(tmp_path, monkeypatch)
    voice.listener.mic_error, voice.listener.hotkey_error = "Connect your microphone.", "Choose a new shortcut."
    checks = {check.id: check for check in controls.checks()}
    assert checks["mic_error"].level == checks["hotkey_error"].level == "fail"
    assert controls.test("microphone") == "microphone"
    assert controls.update({"hotkey": "ctrl+space"}) == {}
    assert voice.listener.mic_error is voice.listener.hotkey_error is None


def test_a_failed_meter_start_closes_the_stream_and_backs_off(monkeypatch):
    calls = []

    class Stream:
        def __init__(self, **kwargs):
            calls.append("open")

        def start(self):
            raise OSError("device busy")

        def stop(self):
            raise OSError("never started")

        def close(self):
            calls.append("close")

    monkeypatch.setattr("sounddevice.InputStream", Stream)
    monkeypatch.setattr("bolos.controls.time.monotonic", lambda: 5)
    meter = MicMeter()
    assert meter.read(None) == meter.read(None) == 0
    assert "Windows microphone settings" in meter.error
    assert calls == ["open", "close"]


def test_options_read_at_start_ask_for_a_restart(tmp_path, monkeypatch):
    controls, _ = running(tmp_path, monkeypatch)
    controls.update({"codex_effort": "high", "chimes": False})
    assert controls.restart_needed() == ["codex_effort"]
    controls.update({"codex_effort": "low"})
    assert controls.restart_needed() == []


def test_screenshots_follow_the_privacy_switch(tmp_path, monkeypatch):
    controls, _ = running(tmp_path, monkeypatch)
    assert controls.images_allowed()
    controls.update({"screen_images": False})
    assert not controls.images_allowed()


def test_the_status_tap_keeps_the_live_state_and_passes_everything_on(tmp_path):
    controls = Controls(store(tmp_path))
    seen = []
    status = controls.status(lambda kind, value=None: seen.append((kind, value)))
    status("state", "hearing")
    status("heard", "Chrome kholo")
    status("level", 0.3)
    assert controls.live["state"] == "hearing" and controls.live["heard"] == "Chrome kholo"
    assert seen == [("state", "hearing"), ("heard", "Chrome kholo"), ("level", 0.3)]
    assert not controls.running and controls.restart_needed() == []


def test_the_live_meter_reads_the_listener(tmp_path, monkeypatch):
    controls, _ = running(tmp_path, monkeypatch)
    assert controls.level() == {"level": 0.4, "error": None, "state": "ready"}


# The status card


@pytest.mark.parametrize("corner, expected", [("bottom-right", (880, 990)), ("top-right", (880, 10)),
                                              ("bottom-left", (10, 990)), ("top-left", (10, 10)),
                                              ("nonsense", (880, 990))])
def test_the_card_goes_to_the_chosen_corner(corner, expected):
    assert place(100, 50, (0, 0, 990, 1050), 10, None, corner) == expected


def test_the_card_leaves_its_chosen_corner_for_the_guides_ring():
    # The first free corner, starting from the usual bottom right.
    assert place(100, 50, (0, 0, 990, 1050), 10, (0, 0, 200, 100), "top-left") == (880, 990)


def test_the_card_shows_the_new_hotkey_and_corner():
    st = OverlayState()
    assert st.apply("hotkey", "ctrl+alt+b", 0.0) and st.hotkey == "ctrl+alt+b"
    assert st.apply("corner", "top-left", 0.0) and st.corner == "top-left"
    assert not st.apply("corner", "middle", 0.0) and st.corner == "top-left"


# Statistics


def entry(time, outcome="opened", source="system1", ms=900.0, **extra):
    return {"id": time, "time": time, "transcript": "Chrome kholo", "language": "hi-IN", "source": source,
            "action": "open_app", "outcome": outcome, "timings": {"total": ms}, "trigger": "hotkey",
            "corrected": False, **extra}


def test_statistics_from_the_history():
    entries = [entry("2026-11-07T10:00:00+05:30"),
               entry("2026-11-08T10:00:00+05:30", source="system2", ms=9000.0),
               entry("2026-11-08T11:00:00+05:30", outcome="not_understood", source=None, ms=3000.0),
               entry("2026-11-08T12:00:00+05:30", outcome="custom", ok=False),  # a recorded ok wins
               entry("2026-11-08T13:00:00+05:30", corrected=True)]
    s = stats.summarize(entries, today=date(2026, 11, 8))
    assert s["total"] == 5 and s["today"] == 4
    assert s["success_rate"] == 0.4  # not understood, ok=False and corrected did not work
    assert s["local_rate"] == 0.75  # three of the four understood were System 1
    assert s["median_ms"] == 900 and s["corrected"] == 1
    assert len(s["per_day"]) == 7 and s["per_day"][-1] == {"date": "2026-11-08", "ok": 1, "failed": 3}
    assert {x["name"]: x["count"] for x in s["sources"]} == {"System 1": 3, "System 2": 1, "Not understood": 1}
    assert s["recent"][0]["time"].startswith("2026-11-08T13") and s["recent"][0]["corrected"]
    assert s["first"].startswith("2026-11-07")


def test_statistics_of_an_empty_history():
    s = stats.summarize([], today=date(2026, 11, 8))
    assert s["total"] == 0 and s["success_rate"] is None and s["median_ms"] is None and len(s["per_day"]) == 14


def test_the_daily_chart_covers_at_most_two_weeks():
    s = stats.summarize([entry("2026-09-01T10:00:00+05:30")], today=date(2026, 11, 8))
    assert len(s["per_day"]) == 14 and s["per_day"][0]["date"] == "2026-10-26"


# The web server


@pytest.fixture
def server(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps(entry("2026-11-08T10:00:00+05:30")) + "\n", encoding="utf-8")
    dashboard = Dashboard(Controls(store(tmp_path)), history, port=0)
    dashboard.start()
    yield dashboard
    dashboard.close()


def request(dashboard, method, path, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", dashboard.port, timeout=5)
    sent = {"Host": f"127.0.0.1:{dashboard.port}"}
    if body is not None:
        sent |= {"Content-Type": "application/json", "X-Bolos": "1"}
    sent |= headers or {}
    connection.request(method, path, json.dumps(body) if body is not None else None, sent)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data, response


def test_the_page_and_its_files_are_served(server):
    status, data, response = request(server, "GET", "/")
    assert status == 200 and b"Sahur" in data and "default-src 'self'" in response.getheader("Content-Security-Policy")
    for path in ("/app.js", "/app.css", "/favicon.svg"):
        assert request(server, "GET", path)[0] == 200
    assert request(server, "GET", "/../bolos/config.py")[0] == 404


def test_the_api_saves_settings_and_reports_errors(server):
    status, data, _ = request(server, "POST", "/api/settings", {"changes": {"widget_corner": "top-left"}})
    assert status == 200 and json.loads(data) == {"changed": {"widget_corner": "top-left"}}
    status, data, _ = request(server, "POST", "/api/settings", {"changes": {"hotkey": "q"}})
    assert status == 422 and "Ctrl, Alt, Shift or Win" in json.loads(data)["error"]
    state = json.loads(request(server, "GET", "/api/state")[1])
    assert state["values"]["widget_corner"] == "top-left" and not state["running"]
    assert any(o["key"] == "widget_corner" and o["changed"] for o in state["options"])
    stats_ = json.loads(request(server, "GET", "/api/stats")[1])
    assert stats_["total"] == 1


def test_the_api_handles_missing_devices_and_a_failed_sound_check(server, monkeypatch):
    def unavailable(*args):
        raise OSError("audio API unavailable")

    monkeypatch.setattr("bolos.audio.devices", unavailable)
    monkeypatch.setattr("bolos.audio.Player.play", unavailable)
    monkeypatch.setattr("bolos.controls.load_settings", lambda *_: replace(BASE, gnani_api_key=""))
    status, data, _ = request(server, "GET", "/api/state")
    assert status == 200
    checks = {c["id"]: c for c in json.loads(data)["checks"]}
    assert checks["input_device"]["label"] == "No microphone found"
    assert checks["gnani"]["label"] == "Voice service needs setup"
    status, data, _ = request(server, "POST", "/api/test", {"what": "speaker"})
    assert status == 422 and "Try the chime" in json.loads(data)["error"]
    status, data, _ = request(server, "POST", "/api/test", {"what": "chime"})
    assert status == 422 and "Windows Sound settings" in json.loads(data)["error"]


def test_stats_can_read_mixed_history_and_skip_truncated_records(server):
    data = [entry("2026-11-07T10:00:00"), entry("2026-11-08T10:00:00+05:30", ms=float("nan")),
            {"id": {}, "time": "2026-11-08"}, {"id": "legacy", "time": "2026-11-09", "language": []}]
    server.history_path.write_bytes("".join(json.dumps(e) + "\n" for e in data).encode() + b'{"id":"\xef')
    status, data, _ = request(server, "GET", "/api/stats")
    assert status == 200 and json.loads(data)["total"] == 3
    assert json.loads(data)["median_ms"] == 900


def test_other_sites_cannot_use_it(server):
    # DNS rebinding: a page on another name that resolves here.
    assert request(server, "GET", "/api/state", headers={"Host": "evil.example:80"})[0] == 403
    # A cross-site form post: no X-Bolos header (it would need a CORS preflight), or not JSON.
    status = request(server, "POST", "/api/reset", {}, headers={"X-Bolos": ""})[0]
    assert status == 403
    status = request(server, "POST", "/api/reset", {}, headers={"Content-Type": "text/plain"})[0]
    assert status == 415
    assert server.controls.store.get("widget_corner") == "bottom-right"


def test_starting_the_statistics_afresh_keeps_the_old_history(server):
    status, data, _ = request(server, "POST", "/api/history/archive", {})
    archived = json.loads(data)["archived"]
    assert status == 200 and archived.startswith("history-")
    assert not server.history_path.exists() and (server.history_path.parent / archived).exists()
    assert json.loads(request(server, "GET", "/api/stats")[1])["total"] == 0


def test_two_archives_in_the_same_second_keep_both_histories(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 10)

    monkeypatch.setattr("bolos.history.datetime", FrozenDateTime)
    history = History(tmp_path / "history.jsonl")
    history.record("open the calculator")
    first = history.archive()
    history.record("open the calendar")
    second = history.archive()
    assert first != second
    assert "open the calculator" in first.read_text(encoding="utf-8")
    assert "open the calendar" in second.read_text(encoding="utf-8")


def test_a_standalone_dashboard_steps_aside_for_a_starting_bol_os(server):
    assert json.loads(request(server, "GET", "/api/ping")[1]) == {"app": "bolos-dashboard", "running": False}
    status, _, _ = request(server, "POST", "/api/shutdown", {})
    assert status == 200 and server.stopped.wait(3)


def test_open_settings_finds_and_hands_off_a_fallback_port(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        standalone = Dashboard(Controls(store(tmp_path)), None, port=port)
        voice = Dashboard(Controls(store(tmp_path), voice=FakeVoice(FakeListener())), None, port=port)
        try:
            standalone.start()
            assert standalone.port != port and find_dashboard(port) == standalone.port
            voice.start()
            assert standalone.stopped.wait(3)
            assert voice.port == standalone.port and find_dashboard(port) == voice.port
        finally:
            voice.close()
            standalone.close()
