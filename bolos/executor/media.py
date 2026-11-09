"""Music and video: playing on the service the user names, and controlling what is already playing.

Controls go through Windows' media sessions (the same thing the volume flyout and keyboard media keys use),
so "pause" pauses whichever app is playing, Spotify or a YouTube tab alike, and never accidentally resumes.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

PLAYING, PAUSED = 4, 5  # GlobalSystemMediaTransportControlsSessionPlaybackStatus


@dataclass(frozen=True)
class NowPlaying:
    app: str  # e.g. "Spotify.exe", "Chrome"
    title: str
    artist: str
    playing: bool


def _run(coro):
    return asyncio.run(coro)


async def _session(app_hint: str | None = None, want: int | None = None):
    """The media session of the app named by app_hint, else the one in the wanted playback state (pause acts
    on what is playing, resume on what is paused), else Windows' "current" one."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    manager = await Manager.request_async()
    sessions = list(manager.get_sessions())
    if app_hint:
        sessions = [s for s in sessions if app_hint.casefold() in s.source_app_user_model_id.casefold()]
        return sessions[0] if sessions else None
    current = manager.get_current_session()
    if want is not None:
        if current is not None and current.get_playback_info().playback_status == want:
            return current
        for s in sessions:
            if s.get_playback_info().playback_status == want:
                return s
    return current


async def _now_playing(app_hint: str | None) -> NowPlaying | None:
    session = await _session(app_hint)
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    status = session.get_playback_info().playback_status
    return NowPlaying(session.source_app_user_model_id, props.title or "", props.artist or "", status == PLAYING)


def now_playing(app_hint: str | None = None) -> NowPlaying | None:
    try:
        return _run(_now_playing(app_hint))
    except Exception:  # no media sessions API (old Windows) or the app vanished
        log.debug("media session unavailable", exc_info=True)
        return None


async def _transport(control: str, app_hint: str | None) -> bool:
    want = {"pause": PLAYING, "resume": PAUSED}.get(control)
    session = await _session(app_hint, want)
    if session is None:
        return False
    call = {"pause": session.try_pause_async, "resume": session.try_play_async,
            "next": session.try_skip_next_async, "previous": session.try_skip_previous_async}[control]
    return bool(await call())


VOLUME_KEYS = {"volume_up": "volume up", "volume_down": "volume down", "mute": "volume mute"}
MEDIA_KEYS = {"pause": "play/pause media", "resume": "play/pause media", "next": "next track",
              "previous": "previous track"}
VOLUME_STEPS = 5  # each key press is 2%: one command moves the volume 10%


def control(control: str, app_hint: str | None = None) -> bool:
    """Pause, resume, skip or change the volume. False if nothing is playing to control."""
    import keyboard

    if control in VOLUME_KEYS:
        for _ in range(1 if control == "mute" else VOLUME_STEPS):
            keyboard.send(VOLUME_KEYS[control])
        return True
    try:
        return _run(_transport(control, app_hint))
    except Exception:
        log.warning("media session control failed, using media keys", exc_info=True)
        if control in ("pause", "resume"):
            current = now_playing(app_hint)
            if current is not None and current.playing == (control == "resume"):
                return True  # already in the wanted state; a toggle key would undo it
        keyboard.send(MEDIA_KEYS[control])
        return True


def wait_for_session(app_hint: str, timeout: float = 10.0) -> bool:
    """Wait until an app (just started) has registered its media session."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _run(_session(app_hint)) is not None:
                return True
        except Exception:
            log.debug("media session unavailable", exc_info=True)
        time.sleep(0.3)
    return False


def wait_until_playing(app_hint: str, changed_from: NowPlaying | None, timeout: float = 8.0,
                       settle: float = 2.5) -> NowPlaying | None:
    """What started playing in that app; None if nothing did in time.

    Right after a new track starts, the media session can still report the previous title for a moment,
    so a track that looks unchanged is only accepted after `settle` seconds (the user may have asked for
    the same song again)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        current = now_playing(app_hint)
        if current and current.playing:
            same = changed_from is not None and (current.title, current.artist) == (changed_from.title,
                                                                                    changed_from.artist)
            if not same or time.monotonic() - start >= settle:
                return current
        time.sleep(0.3)
    return None
