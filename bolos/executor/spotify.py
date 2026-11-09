"""Driving the Spotify desktop app: search, then press the top result's Play button.

Spotify is a Chromium app and only shows its page to UI Automation when started with
--force-renderer-accessibility, so Bol OS starts it that way (and restarts it once if it was started
without). The search page has two "Search results" grids: the first holds the top result, whose row has
its own Play button; the player bar's and side panel's Play buttons are elsewhere and never pressed.
Spotify has no keyboard shortcut or link that does this.
"""

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

ACCESSIBILITY_FLAG = "--force-renderer-accessibility"
START_SECONDS = 15
RESULTS_SECONDS = 30  # usually about a second; much longer while Windows throttles a hidden window
SETTLE_SECONDS = 1.5


class SpotifyUnavailable(Exception):
    pass


def _window():
    """Spotify's main window (a UIA wrapper), or None if it isn't running."""
    from pywinauto import Desktop

    for w in Desktop(backend="uia").windows(class_name="Chrome_WidgetWin_1", visible_only=False):
        if w.window_text() and _exe_of(w.handle).name.casefold() == "spotify.exe":
            return w
    return None


def _running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/NH"], capture_output=True, text=True,
                         check=False).stdout
    return "spotify.exe" in out.casefold()


def _exe_of(hwnd: int) -> Path:
    import win32process

    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return Path()
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return Path(buf.value) if ok else Path()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _readable(window) -> bool:
    """Whether Spotify's page is visible to UI Automation (it was started with the flag)."""
    return any(d.element_info.control_type == "Document" for d in window.descendants(control_type="Document"))


def _installed_exe() -> Path | None:
    path = Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe"
    return path if path.exists() else None


def _start(exe: Path):
    subprocess.Popen([str(exe), ACCESSIBILITY_FLAG])
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if (w := _window()) is not None and _readable(w):
            return w
        time.sleep(0.5)
    raise SpotifyUnavailable("Spotify did not start")


def ensure_running():
    """Spotify's window, started (or restarted once) so that its page can be read."""
    window = _window()
    if window is None and _running():  # minimised to the tray: bring the window back first
        os.startfile("spotify:")
        deadline = time.monotonic() + 5
        while (window := _window()) is None and time.monotonic() < deadline:
            time.sleep(0.3)
    if window is not None and _readable(window):
        return window
    exe = _exe_of(window.handle) if window is not None else _installed_exe()
    if not exe or not exe.exists():
        raise SpotifyUnavailable("Spotify desktop app not found")
    if window is not None:  # running without accessibility: restart it once with it
        log.info("Restarting Spotify with %s", ACCESSIBILITY_FLAG)
        subprocess.run(["taskkill", "/IM", exe.name, "/F"], capture_output=True, check=False)
        time.sleep(1.5)
    return _start(exe)


def _search_box_value(window) -> str:
    for box in window.descendants(control_type="ComboBox"):
        try:
            return box.iface_value.CurrentValue or ""
        except Exception:  # not a value control
            continue
    return ""


def _top_result_play(window):
    """The Play button of the first row of the first "Search results" grid, or None."""
    grids = [g for g in window.descendants(control_type="DataGrid") if g.window_text() == "Search results"]
    if not grids:
        return None
    top = min(grids, key=lambda g: g.rectangle().top)
    rows = top.children(control_type="DataItem")
    if not rows:
        return None
    for button in rows[0].descendants(control_type="Button"):
        name = button.window_text()
        if name == "Play" or name.startswith("Play "):
            return button
    return None


def _same_search(box: str, query: str) -> bool:
    """The search box shows spaces from a spotify:search: link as "+"."""
    def norm(s: str) -> str:
        return " ".join(s.replace("+", " ").casefold().split())

    return norm(box) == norm(query)


def _row_id(window) -> str | None:
    grids = [g for g in window.descendants(control_type="DataGrid") if g.window_text() == "Search results"]
    if not grids:
        return None
    rows = min(grids, key=lambda g: g.rectangle().top).children(control_type="DataItem")
    return repr(rows[0].element_info.runtime_id) if rows else None


def play_top_result(query: str) -> str:
    """Search Spotify for query and start the top result. Returns the top result's name."""
    window = ensure_running()
    previous_top = _row_id(window)  # the page may still show the last search: never press its Play
    os.startfile(f"spotify:search:{quote(query)}")
    deadline = time.monotonic() + RESULTS_SECONDS
    matched_at = None
    while time.monotonic() < deadline:
        if _same_search(_search_box_value(window), query):
            matched_at = matched_at or time.monotonic()
            fresh = _row_id(window) != previous_top or time.monotonic() - matched_at >= SETTLE_SECONDS
            if fresh and (button := _top_result_play(window)) is not None:
                name = button.window_text().removeprefix("Play").strip()
                button.invoke()
                return name
        time.sleep(0.3)
    raise SpotifyUnavailable(f"no Spotify results for {query!r}")


def open_app() -> None:
    """Open Spotify (readable by UI Automation from the start)."""
    window = ensure_running()
    window.set_focus()
