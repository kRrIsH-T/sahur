"""The push-to-talk hotkey, registered with Windows' own RegisterHotKey instead of a keyboard hook.

A low-level hook with suppression (what the `keyboard` package does) keeps its own table of pressed keys.
When that table drifts from the real keyboard, Ctrl looks held down for every later keystroke. RegisterHotKey
keeps no state of ours: Windows swallows the combination and calls back once per press, and whether the
combination is still held is read from the OS (GetAsyncKeyState), so there is nothing that can get stuck.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_MODIFIERS = {  # name -> (RegisterHotKey flag, virtual key that reads "either side held")
    "ctrl": (MOD_CONTROL, 0x11), "control": (MOD_CONTROL, 0x11),
    "alt": (MOD_ALT, 0x12),
    "shift": (MOD_SHIFT, 0x10),
    "win": (MOD_WIN, 0x5B), "windows": (MOD_WIN, 0x5B), "cmd": (MOD_WIN, 0x5B),
}
_NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
               "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
               "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "capslock": 0x14}


class HotkeyError(RuntimeError):
    pass


def format_hotkey(hotkey: str) -> str:
    """Display a combination as Ctrl+Space."""
    return "+".join(part.strip().capitalize() for part in hotkey.split("+") if part.strip()) or hotkey


def parse(hotkey: str) -> tuple[int, list[int], int]:
    """"ctrl+space" -> (RegisterHotKey modifier flags, virtual keys of the modifiers, virtual key of the key)."""
    flags, modifier_vks, key = 0, [], None
    for part in (p.strip().lower() for p in hotkey.split("+")):
        if not part:
            continue
        if part in _MODIFIERS:
            flag, vk = _MODIFIERS[part]
            flags |= flag
            modifier_vks.append(vk)
        elif key is not None:
            raise HotkeyError(f"Hotkey {hotkey!r} has more than one non-modifier key")
        elif part in _NAMED_KEYS:
            key = _NAMED_KEYS[part]
        elif len(part) == 1 and part.isascii() and part.isalnum():
            key = ord(part.upper())
        elif part.startswith("f") and len(part) <= 3 and part.isascii() and part[1:].isdigit() \
                and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            raise HotkeyError(f"Unknown key {part!r} in hotkey {hotkey!r}")
    if key is None:
        raise HotkeyError(f"Hotkey {hotkey!r} needs a key besides its modifiers")
    return flags, modifier_vks, key


def is_down(hotkey: str) -> bool:
    """Whether the whole combination is physically held right now, as Windows sees it."""
    _flags, modifier_vks, key = parse(hotkey)
    state = ctypes.windll.user32.GetAsyncKeyState
    return all(state(vk) & 0x8000 for vk in [*modifier_vks, key])


class GlobalHotkey:
    """Calls `callback` (on its own thread, so it must return quickly) each time the combination goes down."""

    _next_id = 0xB010

    def __init__(self, hotkey: str, callback: Callable[[], None]):
        self.hotkey = hotkey
        self.callback = callback
        self._flags, _mods, self._vk = parse(hotkey)
        self._id = GlobalHotkey._next_id
        GlobalHotkey._next_id += 1
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkey-loop")
        self._thread.start()
        if not self._ready.wait(5.0):
            self.stop()
            raise HotkeyError(f"Windows did not start the {self.hotkey} shortcut. Try it again in Settings.")
        if self._error:
            raise HotkeyError(self._error)

    def _loop(self) -> None:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if self._stopping.is_set():
            return
        if not user32.RegisterHotKey(None, self._id, self._flags | MOD_NOREPEAT, self._vk):
            self._error = (f"Could not register {self.hotkey}: another program already uses it "
                           f"(Windows error {ctypes.get_last_error() or kernel32.GetLastError()}).")
            self._ready.set()
            return
        try:
            msg = wintypes.MSG()
            # The message queue must exist before stop() can post WM_QUIT to this thread.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self._ready.set()
            if self._stopping.is_set():
                return
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self._id:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, self._id)

    def stop(self) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is None or not thread.is_alive():
            return
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=2.0)
