"""Open a demo page in a throwaway Chrome or Edge, for live tests and eval/forms_e2e.py.

The browser runs with its own temporary --user-data-dir, so it is a separate browser process: the user's own
Chrome/Edge windows, tabs and profile are never touched, and closing it (always, in a finally) closes only it.
Never call chrome.exe/msedge.exe without --user-data-dir: Windows hands such a call to the running browser.
"""

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

BROWSERS = {
    "chrome": (r"Google\Chrome\Application\chrome.exe",),
    "edge": (r"Microsoft\Edge\Application\msedge.exe",),
}


def find_browser(which: str) -> str | None:
    """Path of chrome.exe or msedge.exe, or None when it is not installed."""
    roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel in BROWSERS[which]:
            path = Path(root) / rel
            if path.is_file():
                return str(path)
    return None


def _windows_of(pid: int) -> list[int]:
    import win32gui
    import win32process

    found = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) and \
                win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return found


def bring_to_front(hwnd: int) -> bool:
    """Make our own browser window the foreground window (needed only for typing date segments)."""
    import win32con
    import win32gui

    try:
        # No synthetic key presses to unlock the foreground: they would land in the user's window. If Windows
        # refuses (another app is in front), callers report it instead of typing.
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.3)
    return win32gui.GetForegroundWindow() == hwnd


@contextlib.contextmanager
def private_browser(url: str, which: str = "chrome", timeout: float = 30.0, flags=()):
    """Yield (hwnd, process) of a new, private browser window showing `url`; close it and delete its profile."""
    from bolos.forms import fields

    exe = find_browser(which)
    if exe is None:
        raise FileNotFoundError(f"{which} is not installed")
    profile = tempfile.mkdtemp(prefix=f"bolos-{which}-")
    args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,msEdgeTranslate", "--disable-sync", "--new-window",
            "--window-size=1100,900", *flags, url]
    process = subprocess.Popen(args)
    try:
        deadline = time.monotonic() + timeout
        windows = []
        while not windows and time.monotonic() < deadline:
            time.sleep(0.25)
            windows = _windows_of(process.pid)
        if not windows:
            raise TimeoutError(f"{which} did not open a window")
        hwnd = windows[0]
        # The browser frame can appear before the renderer exposes the fixture to UI Automation.
        while time.monotonic() < deadline:
            if fields.scan(hwnd, options=False).url == url:
                break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"{which} did not expose the fixture page to UI Automation")
        yield hwnd, process
    finally:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(20):  # the profile stays locked for a moment after the browser exits
            shutil.rmtree(profile, ignore_errors=True)
            if not os.path.exists(profile):
                break
            time.sleep(0.25)
