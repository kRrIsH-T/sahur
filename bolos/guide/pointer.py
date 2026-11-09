"""The guide's pointer: a glowing ring around the thing to click, with a ripple that keeps drawing the eye.

A per-pixel-alpha layered window (UpdateLayeredWindow) on its own thread, so it works with or without the Tk
status overlay (voice and typed mode), stays crisp at any DPI and never takes focus or clicks: it is a
no-activate, click-through tool window. It is visible to screen recorders on purpose (demo videos); Bol OS's
own change detection leaves layered windows out instead (bolos.guide.screen.fingerprint).

Drawing is pure numpy (`Sprite`), so its geometry is testable without a window.
"""

from __future__ import annotations

import ctypes
import logging
import math
import queue
import threading
import time
from ctypes import wintypes

import numpy as np

from bolos.guide.screen import Rect, cursor_position

log = logging.getLogger(__name__)

RING = (255, 138, 31)  # a warm orange that reads on light and dark apps
HALO = (12, 14, 20)  # a dark edge under the ring, so it also stands out on orange or white
FPS = 30
RIPPLE_S = 1.6  # one ripple every this many seconds
FADE_S = 0.15
RAISE_S = 0.5  # how often the shown ring re-asserts its place on top
MAX_RIPPLE_PIXELS = 1_500_000  # a window-sized target gets a calm static ring instead of per-frame ripples


class Flight:
    """Windows adaptation of Clicky's eased quadratic cursor flight. See docs/CLICKY_LICENSE.txt."""

    def __init__(self, start: tuple[int, int], end: tuple[int, int], scale: float, bounds: Rect | None):
        self.start, self.end = start, end
        distance = math.dist(start, end)
        self.duration = min(0.75, max(0.22, distance / 1900)) if distance > 24 * scale else 0
        self.arc = min(distance * 0.2, 80 * scale)
        self.bounds = bounds
        size = max(20, round(28 * scale))
        self.size = (size, size)
        y, x = np.mgrid[0:size, 0:size] / size
        outer = (y >= 0.12) & (y <= 0.88) & (np.abs(x - 0.5) <= y * 0.48)
        inner = (y >= 0.26) & (y <= 0.78) & (np.abs(x - 0.5) <= (y - 0.12) * 0.39)
        self.pixels = np.zeros((size, size, 4), np.uint8)
        self.pixels[outer] = (30, 19, 12, 255)
        self.pixels[inner] = (255, 169, 63, 255)  # BGRA

    def origin(self, elapsed: float) -> tuple[int, int]:
        progress = min(1.0, max(0.0, elapsed / self.duration)) if self.duration else 1.0
        eased = progress * progress * (3 - 2 * progress)
        x = self.start[0] + (self.end[0] - self.start[0]) * eased
        y = self.start[1] + (self.end[1] - self.start[1]) * eased - 2 * self.arc * eased * (1 - eased)
        if self.bounds is not None:
            y = max(y, self.bounds.top)
        return round(x - self.size[0] / 2), round(y - self.size[1] / 2)


def ui_scale(rect: Rect) -> float:
    """The display scale (1.0 at 96 DPI) of the monitor showing `rect`."""
    try:
        user32, shcore = ctypes.windll.user32, ctypes.windll.shcore
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(wintypes.POINT(*rect.center), 2)
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return max(dpi_x.value / 96, 1.0)
    except (AttributeError, OSError):
        return 1.0


class Sprite:
    """The pointer for one target: window placement, a static layer and the ripple, as premultiplied BGRA."""

    def __init__(self, target: Rect, scale: float = 1.0, bounds: Rect | None = None, snug: bool = False):
        s = scale
        # Snug: a form field, its label just above it. The ring hugs the box like a focus outline and the
        # ripple stays short, so neither crosses the label or the hint under the box.
        grow, minimum = (1 if snug else 6) * s, (20 if snug else 40) * s
        self.ring_width = 4 * s
        self.halo_width = self.ring_width + 4 * s
        width = max(target.width + 2 * grow, minimum)
        height = max(target.height + 2 * grow, minimum)
        cx, cy = (target.left + target.right) / 2, (target.top + target.bottom) / 2
        if bounds is not None:
            # A target at the screen's edge (the taskbar's Start button) would get half its ring drawn off
            # screen: keep the whole ring on the monitor, overlapping the target a little instead.
            inset = self.halo_width / 2 + 1
            left = max(cx - width / 2, bounds.left + inset)
            right = min(cx + width / 2, bounds.right - inset)
            top = max(cy - height / 2, bounds.top + inset)
            bottom = min(cy + height / 2, bounds.bottom - inset)
            if right - left >= minimum / 2 and bottom - top >= minimum / 2:
                cx, cy, width, height = (left + right) / 2, (top + bottom) / 2, right - left, bottom - top
        self.reach = (6 if snug else 22) * s  # how far the ripple travels outside the ring
        pad = math.ceil(self.reach + self.halo_width + 2 * s)
        left, top = math.floor(cx - width / 2 - pad), math.floor(cy - height / 2 - pad)
        self.window = Rect(left, top, math.ceil(cx + width / 2 + pad), math.ceil(cy + height / 2 + pad))
        self.size = (self.window.width, self.window.height)
        radius = min(12 * s, width / 2, height / 2)
        ys, xs = np.mgrid[0:self.size[1], 0:self.size[0]].astype(np.float32)
        qx = np.abs(xs + 0.5 - (cx - left)) - (width / 2 - radius)
        qy = np.abs(ys + 0.5 - (cy - top)) - (height / 2 - radius)
        # Signed distance to the rounded rectangle's edge: negative inside, positive outside.
        self.distance = (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0)
                         - radius).astype(np.float32)
        self.animated = self.size[0] * self.size[1] <= MAX_RIPPLE_PIXELS
        self.scale = s
        self.base = self._static()

    @staticmethod
    def _band(distance: np.ndarray, offset: float, half_width: float) -> np.ndarray:
        """Antialiased coverage of a stroke `half_width` wide each side of the edge moved out by `offset`."""
        return np.clip(half_width + 0.5 - np.abs(distance - offset), 0.0, 1.0)

    @staticmethod
    def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
        """Premultiplied RGBA `top` over `bottom`."""
        return top + bottom * (1.0 - top[..., 3:4])

    @staticmethod
    def _layer(alpha: np.ndarray, colour: tuple[int, int, int]) -> np.ndarray:
        rgb = np.asarray(colour, np.float32) / 255.0
        return np.concatenate([alpha[..., None] * rgb, alpha[..., None]], axis=2)

    def _static(self) -> np.ndarray:
        d = self.distance
        tint = self._layer(np.where(d < 0, 0.07, 0.0).astype(np.float32), RING)
        halo = self._layer(0.55 * self._band(d, 0.0, self.halo_width / 2), HALO)
        ring = self._layer(self._band(d, 0.0, self.ring_width / 2), RING)
        return self._over(ring, self._over(halo, tint))

    def frame(self, seconds: float) -> np.ndarray:
        """The pointer `seconds` after it appeared, as premultiplied BGRA bytes in an (h, w, 4) uint8 array."""
        image = self.base
        if self.animated:
            phase = (seconds % RIPPLE_S) / RIPPLE_S
            offset = self.halo_width / 2 + phase * self.reach
            strength = 0.75 * (1.0 - phase) ** 1.5
            ripple = self._layer(strength * self._band(self.distance, offset, 1.5 * self.scale), RING)
            image = self._over(image, ripple)
        out = np.empty(image.shape, np.uint8)
        out[..., 0], out[..., 1], out[..., 2] = (image[..., 2::-1] * 255 + 0.5).transpose(2, 0, 1)
        out[..., 3] = image[..., 3] * 255 + 0.5
        return out


# ---------------------------------------------------------------------------------------------------------
# The window

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
CLASS_NAME = "BolOSGuidePointer"

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                           ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                           wintypes.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    wintypes.UINT]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, gdi32, kernel32


class _Surface:
    """A DIB the size of one sprite, selected into a memory DC, that frames are copied into."""

    def __init__(self, gdi32, screen, size: tuple[int, int]):
        self.gdi32 = gdi32
        width, height = size
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                   biPlanes=1, biBitCount=32)
        self.bits = ctypes.c_void_p()
        self.dc = gdi32.CreateCompatibleDC(screen)
        self.bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bitmap)
        self.nbytes = width * height * 4

    def write(self, pixels: np.ndarray) -> None:
        ctypes.memmove(self.bits, np.ascontiguousarray(pixels).ctypes.data, self.nbytes)

    def close(self) -> None:
        self.gdi32.SelectObject(self.dc, self.old)
        self.gdi32.DeleteObject(self.bitmap)
        self.gdi32.DeleteDC(self.dc)


class Pointer:
    """Shows the ring. Thread-safe; the window lives on its own thread, started on first use."""

    def __init__(self, buddy=None):
        self._commands: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.buddy = buddy  # bolos.buddy.Buddy: when running, it flies to the target and the ring waits for it

    def show(self, target: Rect, bounds: Rect | None = None, label: str = "", snug: bool = False) -> None:
        """Ring `target` (physical pixels), kept inside `bounds` (its monitor) when given. With a buddy, the
        buddy flies there first and says `label`. `snug` hugs a form field instead of circling a button."""
        self._send(("show", (target, bounds, label, snug)))

    def hide_after(self, seconds: float) -> None:
        if self._thread is not None:
            self._commands.put(("expire", seconds))

    def hide(self) -> None:
        if self.buddy is not None:
            self.buddy.release()
        if self._thread is not None:
            self._commands.put(("hide", None))

    def close(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._commands.put(("close", None))
            thread.join(2.0)

    def __enter__(self) -> Pointer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send(self, command) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="bolos-pointer")
                self._thread.start()
        self._commands.put(command)

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:  # the pointer is a help, never a reason for a guide to fail
            log.exception("The guide pointer stopped")

    def _loop(self) -> None:
        user32, gdi32, kernel32 = _api()
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"{CLASS_NAME}-{threading.get_ident()}"
        procedure = WNDPROC(lambda hwnd, msg, wparam, lparam: user32.DefWindowProcW(hwnd, msg, wparam, lparam))
        window_class = _WNDCLASSEXW(cbSize=ctypes.sizeof(_WNDCLASSEXW), lpfnWndProc=procedure, hInstance=instance,
                                    lpszClassName=class_name)
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            log.warning("Could not register the guide pointer window (error %d)", ctypes.get_last_error())
            return
        hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                                      | WS_EX_NOACTIVATE, class_name, "Bol OS pointer", WS_POPUP,
                                      0, 0, 1, 1, None, None, instance, None)
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            log.warning("Could not create the guide pointer window (error %d)", ctypes.get_last_error())
            return
        screen = user32.GetDC(None)
        sprite: Sprite | None = None
        flight: Flight | None = None
        surface: _Surface | None = None
        surface_size = None
        expires_at = None
        shown_at = raised_at = 0.0
        visible = False
        message = wintypes.MSG()
        try:
            while True:
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                try:
                    command, value = self._commands.get(timeout=1 / FPS if sprite else 0.25)
                except queue.Empty:
                    command = None
                if command == "close":
                    return
                now = time.monotonic()
                if command == "expire":
                    expires_at = now + value
                if command == "hide" or (expires_at is not None and now >= expires_at):
                    user32.ShowWindow(hwnd, SW_HIDE)
                    sprite = None
                    expires_at = None
                    visible = False
                if command == "show":
                    target, bounds, label, snug = value
                    scale = ui_scale(target)
                    sprite = Sprite(target, scale, bounds, snug)
                    start = cursor_position()
                    flight = None
                    shown_at = now
                    if self.buddy is not None:
                        box = (target.left, target.top, target.right, target.bottom)
                        edges = (bounds.left, bounds.top, bounds.right, bounds.bottom) if bounds else None
                        shown_at = now + self.buddy.point(box, label, edges)  # the ring appears as it lands
                    elif bounds and bounds.contains(*start):
                        flight = Flight(start, target.center, scale, bounds)
                    expires_at = None
                if sprite is None:
                    continue
                age = now - shown_at
                if age < 0:
                    continue
                if flight is not None and age < flight.duration:
                    position, size, pixels = flight.origin(age), flight.size, flight.pixels
                else:
                    position = (sprite.window.left, sprite.window.top)
                    size, pixels = sprite.size, sprite.frame(max(0, age - (flight.duration if flight else 0)))
                if size != surface_size:
                    if surface is not None:
                        surface.close()
                    surface = _Surface(gdi32, screen, size)
                    surface_size = size
                surface.write(pixels)
                blend = _BLENDFUNCTION(0, 0, round(255 * min(1.0, age / FADE_S)), AC_SRC_ALPHA)
                origin = wintypes.POINT(*position)
                dimensions = wintypes.SIZE(*size)
                user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(origin), ctypes.byref(dimensions), surface.dc,
                                           ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
                if not visible:
                    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    visible = True
                    raised_at = 0.0
                if time.monotonic() - raised_at >= RAISE_S:
                    # The Start menu and Search are explorer-hosted XAML islands that open above other
                    # topmost windows; the ring has to come back on top of them to stay visible.
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                    raised_at = time.monotonic()
        finally:
            if surface is not None:
                surface.close()
            user32.ReleaseDC(None, screen)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, instance)
