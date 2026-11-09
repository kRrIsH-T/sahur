"""What the guide sees: a screenshot of the monitor the user works on, the numbered things on it that can be
pointed at ("marks", from UI Automation), and a cheap screen fingerprint to notice when the screen changes.

Coordinates: everything Windows returns here is in physical pixels of the virtual screen (the process is
per-monitor DPI aware: pywinauto and the overlay set that up). The screenshot sent to System 2 is scaled to at
most 1280 pixels wide; `Observation.to_image` and `to_screen` convert between the two.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from bolos.overlay import make_dpi_aware

# Before any window, monitor or UI Automation call: otherwise Windows answers in scaled (logical) pixels and
# the screenshot, the marks and the pointer disagree on a 125 % or 150 % display.
make_dpi_aware()

IMAGE_WIDTH = 1280
MAX_MARKS = 150
MAX_NAME = 80
# A mark must be visibly there: at least this many physical pixels each way, and not a sliver (seen live: a
# Settings card scrolled out of view left a 956x9 strip named "Bluetooth & devices", and the ring went there).
MIN_VISIBLE = 12
MAX_ASPECT = 40

# UI Automation control type ids (UIAutomationClient.h).
BUTTON, CHECKBOX, COMBOBOX, EDIT, HYPERLINK, IMAGE, LISTITEM = 50000, 50002, 50003, 50004, 50005, 50006, 50007
MENUITEM, RADIOBUTTON, SLIDER, TABITEM, TEXT, TREEITEM = 50011, 50013, 50015, 50019, 50020, 50024
CUSTOM, GROUP, DATAITEM, DOCUMENT, SPLITBUTTON, HEADERITEM = 50025, 50026, 50029, 50030, 50031, 50035
ROLES = {BUTTON: "button", CHECKBOX: "checkbox", COMBOBOX: "dropdown", EDIT: "text box", HYPERLINK: "link",
         IMAGE: "image", LISTITEM: "list item", MENUITEM: "menu item", RADIOBUTTON: "option", SLIDER: "slider",
         TABITEM: "tab", TEXT: "text", TREEITEM: "tree item", CUSTOM: "item", GROUP: "group", DATAITEM: "cell",
         DOCUMENT: "document", SPLITBUTTON: "button", HEADERITEM: "column header"}
# Things a person clicks or types into come first; named text and images fill the rest of the budget, since
# web pages and modern apps often expose a clickable label only as text.
INTERACTIVE = {BUTTON, CHECKBOX, COMBOBOX, EDIT, HYPERLINK, LISTITEM, MENUITEM, RADIOBUTTON, SLIDER, TABITEM,
               TREEITEM, DATAITEM, DOCUMENT, SPLITBUTTON, HEADERITEM}
CONTEXT = {TEXT, IMAGE, CUSTOM, GROUP}
TYPING = {EDIT, DOCUMENT, COMBOBOX}

# Terminals (typed mode) and Bol OS's own windows are never "the window the user works in".
TERMINAL_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int, margin: int = 0) -> bool:
        return (self.left - margin <= x < self.right + margin) and (self.top - margin <= y < self.bottom + margin)

    def clip(self, other: Rect) -> Rect | None:
        r = Rect(max(self.left, other.left), max(self.top, other.top),
                 min(self.right, other.right), min(self.bottom, other.bottom))
        return r if r.width > 0 and r.height > 0 else None

    def near(self, other: Rect, tolerance: int = 4) -> bool:
        return all(abs(a - b) <= tolerance for a, b in zip(
            (self.left, self.top, self.right, self.bottom), (other.left, other.top, other.right, other.bottom),
            strict=True))


@dataclass(frozen=True)
class Mark:
    """Something on screen the guide can point at."""

    id: int
    name: str
    control_type: int
    rect: Rect  # physical pixels
    enabled: bool = True
    source: str = "window"  # "window": in the user's window; "taskbar": on the Windows taskbar

    @property
    def role(self) -> str:
        return ROLES.get(self.control_type, "item")

    @property
    def typing(self) -> bool:
        return self.control_type in TYPING


@dataclass
class Observation:
    """One look at the screen."""

    monitor: Rect
    scale: float  # image pixels per physical pixel
    window_title: str
    marks: list[Mark] = field(default_factory=list)
    image: Path | None = None
    focused: Mark | None = None  # the text box the keyboard types into, when there is one (id 0)
    hwnd: int | None = None  # None in callers that supply their own observations
    cursor: tuple[int, int] | None = None

    @property
    def image_size(self) -> tuple[int, int]:
        return round(self.monitor.width * self.scale), round(self.monitor.height * self.scale)

    def to_image(self, rect: Rect) -> tuple[int, int, int, int]:
        """(x, y, width, height) of a physical rectangle, in screenshot pixels."""
        s, m = self.scale, self.monitor
        return (round((rect.left - m.left) * s), round((rect.top - m.top) * s),
                max(1, round(rect.width * s)), max(1, round(rect.height * s)))

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """A screenshot point in physical pixels."""
        return round(self.monitor.left + x / self.scale), round(self.monitor.top + y / self.scale)

    def mark(self, mark_id: int) -> Mark | None:
        return next((m for m in self.marks if m.id == mark_id), None)

    def describe_marks(self) -> str:
        """The numbered list System 2 reads next to the screenshot."""
        lines = []
        for m in self.marks:
            x, y, w, h = self.to_image(m.rect)
            name = f'"{m.name}"' if m.name else "(no name)"
            state = "" if m.enabled else ", disabled"
            where = ", on the taskbar" if m.source == "taskbar" else ""
            focus = ", has keyboard focus" if self.focused and self.focused.rect == m.rect else ""
            lines.append(f"[{m.id}] {m.role} {name} at x={x} y={y} w={w} h={h}{state}{where}{focus}")
        return "\n".join(lines)

    def describe_cursor(self) -> str:
        if self.cursor is None or not self.monitor.contains(*self.cursor):
            return ""
        x, y = self.cursor
        image_x, image_y, _, _ = self.to_image(Rect(x, y, x + 1, y + 1))
        under = [mark for mark in self.marks if mark.rect.contains(x, y)]
        nearest = min(under, key=lambda mark: mark.rect.width * mark.rect.height, default=None)
        label = f" over mark [{nearest.id}]" if nearest else ""
        return f"User's pointer is at x={image_x} y={image_y}{label}."


# ---------------------------------------------------------------------------------------------------------
# Windows


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    return user32


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_title(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _cloaked(hwnd) -> bool:
    value = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(value), 4)  # CLOAKED
    except OSError:
        return False
    return bool(value.value)


def _usable(hwnd, user32) -> bool:
    """A normal visible window a person works in."""
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _cloaked(hwnd):
        return False
    if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW: Bol OS's overlay, floating palettes
        return False
    if _class_name(hwnd) in TERMINAL_CLASSES or _pid(hwnd) == os.getpid():
        return False
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left > 1 and rect.bottom - rect.top > 1


def user_window() -> int:
    """The window the user is working in: the foreground one, unless that is the terminal Bol OS runs in (typed
    mode), then the next window down the z-order."""
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT, for apps such as Calculator
    if hwnd and _usable(hwnd, user32):
        return hwnd
    candidate = user32.GetWindow(hwnd or user32.GetDesktopWindow(), 0 if not hwnd else 2)  # GW_HWNDNEXT
    for _ in range(500):
        if not candidate:
            break
        if _usable(candidate, user32) and window_title(candidate):
            return candidate
        candidate = user32.GetWindow(candidate, 2)
    # Nothing usable: the desktop. Never the minimized or hidden foreground window (seen live: Chrome stayed
    # "foreground" while minimized, and the guide pointed at its buttons over the empty desktop).
    return 0


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def monitor_of(hwnd) -> Rect:
    user32 = _user32()
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    r = info.rcMonitor
    return Rect(r.left, r.top, r.right, r.bottom)


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


# ---------------------------------------------------------------------------------------------------------
# Pixels


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


SRCCOPY, CAPTUREBLT, HALFTONE = 0x00CC0020, 0x40000000, 4


def _gdi32():
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.StretchBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 4 + [
        wintypes.DWORD]
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    return gdi32


def grab(area: Rect, size: tuple[int, int] | None = None, layered: bool = True) -> np.ndarray:
    """The screen pixels of `area` as an (h, w, 3) RGB array, optionally shrunk by GDI to `size` (w, h).
    `layered=False` leaves out layered windows: Bol OS's own overlay and pointer, so its own pulsing ring never
    looks like the screen changing (tooltips and some menus are layered too)."""
    user32, gdi32 = _user32(), _gdi32()
    width, height = size or (area.width, area.height)
    screen = user32.GetDC(None)
    if not screen:
        raise OSError("The screen is unavailable")
    memory = bitmap = old = None
    try:
        memory = gdi32.CreateCompatibleDC(screen)
        if not memory:
            raise OSError("Could not allocate a capture device")
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                   biPlanes=1, biBitCount=32)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise OSError("Could not allocate a screen bitmap")
        old = gdi32.SelectObject(memory, bitmap)
        rop = SRCCOPY | (CAPTUREBLT if layered else 0)
        if size is None:
            copied = gdi32.BitBlt(memory, 0, 0, width, height, screen, area.left, area.top, rop)
        else:
            gdi32.SetStretchBltMode(memory, HALFTONE)
            gdi32.SetBrushOrgEx(memory, 0, 0, None)
            copied = gdi32.StretchBlt(memory, 0, 0, width, height, screen, area.left, area.top, area.width, area.height,
                                      rop)
        if not copied:
            raise OSError("Could not capture screen pixels")
        raw = ctypes.string_at(bits, width * height * 4)
    finally:
        if old:
            gdi32.SelectObject(memory, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    bgra = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
    return bgra[:, :, 2::-1].copy()


def shrink(rgb: np.ndarray, width: int) -> np.ndarray:
    """Area-average an image down to `width` pixels wide (aspect kept). Averaging keeps small UI text legible
    where skipping pixels would break letters apart."""
    h, w = rgb.shape[:2]
    if w <= width:
        return rgb
    height = max(1, round(h * width / w))
    ys = np.linspace(0, h, height + 1).astype(int)
    xs = np.linspace(0, w, width + 1).astype(int)
    rows = np.add.reduceat(rgb, ys[:-1], axis=0, dtype=np.uint32)
    sums = np.add.reduceat(rows, xs[:-1], axis=1, dtype=np.uint32)
    counts = np.diff(ys)[:, None, None] * np.diff(xs)[None, :, None]
    return (sums / counts + 0.5).astype(np.uint8)


def save_png(path: Path, rgb: np.ndarray) -> None:
    from bolos.overlay import save_png as write

    h, w = rgb.shape[:2]
    rgba = np.concatenate([rgb, np.full((h, w, 1), 255, np.uint8)], axis=2)
    write(path, (w, h, rgba.tobytes()))


THUMB = (128, 72)


def fingerprint(area: Rect) -> np.ndarray:
    """A small grey picture of the screen (without Bol OS's own windows) for change detection."""
    rgb = grab(area, THUMB, layered=False).astype(np.int16)
    return (rgb[:, :, 0] * 3 + rgb[:, :, 1] * 6 + rgb[:, :, 2]) // 10


def changed(before: np.ndarray, after: np.ndarray, threshold: int = 18) -> float:
    """The fraction of the screen that visibly changed between two fingerprints."""
    if before.shape != after.shape:
        return 1.0
    return float(np.mean(np.abs(after - before) > threshold))


# ---------------------------------------------------------------------------------------------------------
# Marks


def _uia():
    from pywinauto.uia_defines import IUIA

    return IUIA()


_PROPERTIES = ("UIA_NamePropertyId", "UIA_ControlTypePropertyId", "UIA_BoundingRectanglePropertyId",
               "UIA_IsEnabledPropertyId", "UIA_IsPasswordPropertyId")


def _elements(hwnd) -> list[tuple[str, int, Rect, bool]]:
    """(name, control type, rect, enabled) of every on-screen control under a window, in one cached query."""
    uia = _uia()
    iuia, lib = uia.iuia, uia.UIA_dll
    cache = iuia.CreateCacheRequest()
    for name in _PROPERTIES:
        cache.AddProperty(getattr(lib, name))
    condition = iuia.CreateAndCondition(iuia.CreatePropertyCondition(lib.UIA_IsOffscreenPropertyId, False),
                                        iuia.ControlViewCondition)
    found = iuia.ElementFromHandle(hwnd).FindAllBuildCache(lib.TreeScope_Subtree, condition, cache)
    out = []
    for i in range(found.Length):
        element = found.GetElement(i)
        try:
            r = element.CachedBoundingRectangle
            name = (element.CachedName or "").strip()
            if element.CachedIsPassword:
                name = ""  # never echo anything from a password box
            out.append((name, element.CachedControlType, Rect(r.left, r.top, r.right, r.bottom),
                        bool(element.CachedIsEnabled)))
        except Exception:  # an element can vanish between the query and the read
            continue
    return out


def select_marks(elements: list[tuple[str, int, Rect, bool]], monitor: Rect, limit: int = MAX_MARKS,
                 source: str = "window") -> list[Mark]:
    """Number the elements worth pointing at: on this monitor, big enough to click, interactive ones first,
    duplicates (a list item and its own button with the same name and box) merged, in reading order."""
    picked: list[tuple[int, str, int, Rect, bool]] = []
    for name, control_type, rect, enabled in elements:
        clipped = rect.clip(monitor)
        if clipped is None or min(clipped.width, clipped.height) < MIN_VISIBLE:
            continue
        if max(clipped.width, clipped.height) > MAX_ASPECT * min(clipped.width, clipped.height):
            continue  # a sliver: a card scrolled almost out of view, a separator line
        if control_type in INTERACTIVE:
            rank = 0
        elif control_type in CONTEXT and name:
            rank = 1
        else:
            continue
        if clipped.width * clipped.height > 0.9 * monitor.width * monitor.height and control_type != DOCUMENT:
            continue  # a pane the size of the screen is not a target
        name = " ".join(name.split())[:MAX_NAME]
        duplicate = next((i for i, p in enumerate(picked) if p[1].casefold() == name.casefold()
                          and p[3].near(clipped)), None)
        if duplicate is not None:
            if rank < picked[duplicate][0]:
                picked[duplicate] = (rank, name, control_type, clipped, enabled)
            continue
        picked.append((rank, name, control_type, clipped, enabled))
    picked.sort(key=lambda p: p[0])
    return [Mark(0, name, control_type, rect, enabled, source)
            for _, name, control_type, rect, enabled in picked[:limit]]


def number(marks: list[Mark]) -> list[Mark]:
    """Ids 1, 2, ... in reading order (rows of about 12 pixels, then left to right)."""
    ordered = sorted(marks, key=lambda m: (m.rect.top // 12, m.rect.left))
    return [Mark(i + 1, m.name, m.control_type, m.rect, m.enabled, m.source) for i, m in enumerate(ordered)]


TASKBAR_SLOTS = 40


def focused_box(monitor: Rect) -> Mark | None:
    """The text box that has keyboard focus, if it is on this monitor (its name only, never its contents)."""
    try:
        element = _uia().iuia.GetFocusedElement()
        control_type = element.CurrentControlType
        if control_type not in TYPING:
            return None
        r = element.CurrentBoundingRectangle
        rect = Rect(r.left, r.top, r.right, r.bottom).clip(monitor)
        if rect is None or rect.width < 6 or rect.height < 6:
            return None
        name = "" if element.CurrentIsPassword else " ".join((element.CurrentName or "").split())[:MAX_NAME]
        return Mark(0, name, control_type, rect, bool(element.CurrentIsEnabled))
    except Exception:  # focus moving mid-query, or a window without UI Automation support
        return None


def taskbar() -> int:
    return ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None) or 0


def _active_window() -> int:
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    return (user32.GetAncestor(hwnd, 2) or hwnd) if hwnd else 0


def _window_area(hwnd: int, monitor: Rect) -> Rect | None:
    if not hwnd or _active_window() != hwnd:
        return None
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)):
        return None
    return Rect(rect.left, rect.top, rect.right, rect.bottom).clip(monitor)


def screenshot(folder: Path | None = None) -> Path | None:
    """A picture of the visible app area for describing the screen. The caller deletes it."""
    path = None
    try:
        hwnd = user_window()
        if not hwnd:
            return None
        area = _window_area(hwnd, monitor_of(hwnd))
        if area is None:
            return None
        pixels = grab(area)
        if _active_window() != hwnd:
            return None
        folder = folder or Path(os.environ.get("TEMP", ".")) / "bolos-guide"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screen-{os.getpid()}-{time.monotonic_ns()}.png"
        save_png(path, shrink(pixels, IMAGE_WIDTH))
        return path
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        return None


def observe(folder: Path | None = None, with_image: bool = True, *, foreground_only: bool = False) -> Observation:
    """Look at the screen: the monitor of the user's window, its marks and (optionally) a screenshot."""
    hwnd = user_window()
    monitor = monitor_of(hwnd) if hwnd else Rect(0, 0, ctypes.windll.user32.GetSystemMetrics(0),
                                                    ctypes.windll.user32.GetSystemMetrics(1))
    if foreground_only:
        area = _window_area(hwnd, monitor)
        with_image = with_image and area is not None
        monitor = area or monitor
    scale = min(1.0, IMAGE_WIDTH / max(monitor.width, monitor.height))
    cursor = cursor_position()

    def elements_of(root) -> list:
        try:
            return _elements(root) if root else []
        except Exception:  # a window closing mid-query; the other root still helps
            return []

    # Most lessons start on the taskbar (Start, search, a pinned app), so it keeps its own share of the marks
    # even when the window is busy.
    bar = 0 if foreground_only else taskbar()
    bar_marks = select_marks(elements_of(bar), monitor, TASKBAR_SLOTS, "taskbar") if bar and bar != hwnd else []
    window_marks = select_marks(elements_of(hwnd), monitor, MAX_MARKS - len(bar_marks))
    observation = Observation(monitor, scale, window_title(hwnd) if hwnd else "", number(window_marks + bar_marks),
                              focused=focused_box(monitor), hwnd=hwnd, cursor=cursor)
    if with_image:
        if user_window() != hwnd:
            raise OSError("The active window changed while reading its controls")
        pixels = grab(monitor, layered=not foreground_only)
        if user_window() != hwnd:
            raise OSError("The active window changed during capture")
        folder = folder or Path(os.environ.get("TEMP", ".")) / "bolos-guide"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screen-{os.getpid()}-{time.monotonic_ns()}.png"
        try:
            save_png(path, shrink(pixels, observation.image_size[0]))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        observation.image = path
    return observation
