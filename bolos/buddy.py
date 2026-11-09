"""Sahur is a draggable, non-activating Windows widget that animates during voice commands.

The standalone pointer animation uses Clicky's spring, flight and label timing. See docs/CLICKY_LICENSE.txt.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import queue
import random
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Colours (RGB). The arrow and bars are orange with a dark rim, so they read on white pages and dark apps.
ORANGE = (255, 138, 31)
RIM = (23, 25, 30)
INK = (23, 25, 30)  # bubble text: dark on orange has about 8:1 contrast, white on orange would have 2:1

# Geometry and timing, in points (scaled by the monitor's DPI) and seconds. The numbers are Clicky's.
OFFSET = (35, 25)  # where the buddy sits relative to the cursor's hot spot
SPRING_RESPONSE, SPRING_DAMPING = 0.2, 0.6
FLIGHT_SPEED, FLIGHT_MIN_S, FLIGHT_MAX_S = 800.0, 0.6, 1.4
ARC, ARC_MAX = 0.2, 80.0
SWELL = 0.3  # extra size at the top of the arc
LANDING = (8, 12)  # the buddy lands beside the target's centre, not on top of it
EDGE = 20  # and stays this far inside the monitor
REST_DEGREES = -35.0  # the arrowhead at rest points up and to the left, like a cursor
TURN_S = 0.3  # back to rest after landing
LETTER_S = (0.03, 0.06)
HOLD_S, BUBBLE_FADE_S, RETURN_PAUSE_S = 3.0, 0.5, 0.5
CANCEL_RETURN = 100.0  # moving the mouse this far while the buddy flies back sends it straight to the cursor
CROSSFADE_S = 0.2  # between arrow, bars and spinner
APPEAR_S, VANISH_S, TRANSIENT_PAUSE_S = 0.25, 0.4, 1.0
WELCOME_FADE_S = 2.0
FPS = 60
RAISE_S = 0.5  # how often the window re-asserts its place on top (the Start menu opens above topmost windows)

ARROW_SIZE = 16.0
BAR_WIDTH, BAR_GAP = 2.5, 2.0
BAR_PROFILE = (0.45, 1.0, 0.72, 0.3)  # the four bars of the Bol OS mark
SPINNER_DIAMETER, SPINNER_STROKE, SPINNER_TURN_S = 14.0, 2.5, 0.8
SPINNER_ARC = (0.15, 0.85)
GLOW = 8.0
FONT = "Nirmala UI"  # every Indic script Bol OS speaks plus Latin, so mixed labels share one baseline
FONT_POINTS = 11.0
BUBBLE_PAD = (8.0, 4.0)
BUBBLE_RADIUS = 6.0
BUBBLE_GAP = (10.0, 18.0)  # the bubble's left edge and vertical centre, relative to the buddy

LISTENING = {"listening", "hearing"}
THINKING = {"transcribing", "understanding", "thinking", "executing"}
RESTING = {"ready", "done", "error"}  # plus every outcome key: anything not listening or thinking
ON_A_TARGET = {"guiding", "filling"}  # pointing at something the user is about to answer about
SPRITE_SIZE = (110, 120)
SPRITE_FPS = {"idle": 5, "walk": 9, "hop": 10, "listen": 6, "answer": 8}
DRAG_THRESHOLD = 5


def visual_for(state: str) -> str:
    """Which drawing a Bol OS state gets: "bars", "spinner" or "arrow"."""
    if state in LISTENING:
        return "bars"
    if state in THINKING:
        return "spinner"
    return "arrow"


def busy(state: str) -> bool:
    """A command is under way (the transient buddy stays on screen)."""
    return state in LISTENING or state in THINKING or state in {"confirming", "guiding", "filling"}


# ---------------------------------------------------------------------------------------------------------
# Motion


class Spring:
    """A damped spring that pulls a point toward its target (SwiftUI's response/damping-fraction spring)."""

    def __init__(self, x: float, y: float, response: float = SPRING_RESPONSE, damping: float = SPRING_DAMPING):
        self.x, self.y, self.vx, self.vy = x, y, 0.0, 0.0
        self.k = (2 * math.pi / response) ** 2
        self.c = 4 * math.pi * damping / response

    def step(self, tx: float, ty: float, dt: float) -> tuple[float, float]:
        steps = max(1, math.ceil(dt / 0.004))
        h = min(dt, 0.1) / steps
        for _ in range(steps):
            self.vx += (self.k * (tx - self.x) - self.c * self.vx) * h
            self.vy += (self.k * (ty - self.y) - self.c * self.vy) * h
            self.x += self.vx * h
            self.y += self.vy * h
        return self.x, self.y

    def place(self, x: float, y: float) -> None:
        self.x, self.y, self.vx, self.vy = x, y, 0.0, 0.0

    def settled(self, tx: float, ty: float) -> bool:
        return abs(tx - self.x) + abs(ty - self.y) < 0.3 and abs(self.vx) + abs(self.vy) < 2.0


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    degrees: float
    swell: float  # 1.0 at rest


class Flight:
    """Clicky's arc: a quadratic bezier whose control point sits above the midpoint, eased with smoothstep."""

    def __init__(self, start: tuple[float, float], end: tuple[float, float], scale: float = 1.0):
        self.start, self.end = start, end
        distance = math.dist(start, end)
        self.duration = min(max(distance / scale / FLIGHT_SPEED, FLIGHT_MIN_S), FLIGHT_MAX_S)
        arc = min(distance * ARC, ARC_MAX * scale)
        self.control = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2 - arc)

    def pose(self, elapsed: float) -> Pose:
        linear = min(max(elapsed / self.duration, 0.0), 1.0)
        t = linear * linear * (3 - 2 * linear)
        u = 1 - t
        (x0, y0), (x1, y1), (x2, y2) = self.start, self.control, self.end
        x = u * u * x0 + 2 * u * t * x1 + t * t * x2
        y = u * u * y0 + 2 * u * t * y1 + t * t * y2
        tx = 2 * u * (x1 - x0) + 2 * t * (x2 - x1)
        ty = 2 * u * (y1 - y0) + 2 * t * (y2 - y1)
        # The arrowhead's tip points up at 0 degrees and atan2 is 0 for rightward travel: hence the +90.
        degrees = math.degrees(math.atan2(ty, tx)) + 90 if (tx or ty) else REST_DEGREES
        return Pose(x, y, degrees, 1 + math.sin(linear * math.pi) * SWELL)

    def done(self, elapsed: float) -> bool:
        return elapsed >= self.duration


def landing(target: tuple[int, int, int, int], bounds: tuple[int, int, int, int] | None,
            scale: float) -> tuple[float, float]:
    """Where the buddy lands for a target rectangle (left, top, right, bottom): beside its centre, inside
    its monitor."""
    left, top, right, bottom = target
    x = (left + right) / 2 + LANDING[0] * scale
    y = (top + bottom) / 2 + LANDING[1] * scale
    if bounds is not None:
        edge = EDGE * scale
        x = min(max(x, bounds[0] + edge), bounds[2] - edge)
        y = min(max(y, bounds[1] + edge), bounds[3] - edge)
    return x, y


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def _angle_between(a: float, b: float, t: float) -> float:
    """Interpolate angles the short way round."""
    delta = (b - a + 180) % 360 - 180
    return a + delta * _ease(t)


def pop(elapsed: float) -> float:
    """The bubble's entrance scale: from 0.5 to 1.0 with a little overshoot (a spring, response 0.4 s,
    damping 0.6)."""
    if elapsed <= 0:
        return 0.5
    omega = 2 * math.pi / 0.4
    zeta = 0.6
    wd = omega * math.sqrt(1 - zeta * zeta)
    decay = math.exp(-zeta * omega * elapsed)
    return 1.0 - 0.5 * decay * (math.cos(wd * elapsed) + zeta * omega / wd * math.sin(wd * elapsed))


# ---------------------------------------------------------------------------------------------------------
# Drawing: premultiplied RGBA float arrays, composed into one BGRA frame


def _band(distance: np.ndarray, half_width: float) -> np.ndarray:
    return np.clip(half_width + 0.5 - np.abs(distance), 0.0, 1.0)


def _fill(distance: np.ndarray) -> np.ndarray:
    return np.clip(0.5 - distance, 0.0, 1.0)


def _layer(alpha: np.ndarray, colour: tuple[int, int, int]) -> np.ndarray:
    rgb = np.asarray(colour, np.float32) / 255.0
    return np.concatenate([alpha[..., None] * rgb, alpha[..., None]], axis=2).astype(np.float32)


def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    return top + bottom * (1.0 - top[..., 3:4])


def _glow(distance: np.ndarray, radius: float, strength: float) -> np.ndarray:
    outside = np.maximum(distance, 0.0)
    return strength * np.exp(-((outside / max(radius, 0.5)) ** 2) * 2.2) * (distance > -1)


def _segment_distance(px: np.ndarray, py: np.ndarray, a: tuple[float, float], b: tuple[float, float]) -> np.ndarray:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0.0, 1.0)
    return np.hypot(px - ax - t * dx, py - ay - t * dy)


def arrow_points(cx: float, cy: float, size: float, degrees: float) -> list[tuple[float, float]]:
    """Bol OS's notched arrowhead (tip up at 0 degrees), rotated about its centre. The notch makes the
    direction unmistakable, which a plain triangle is not once it is turned."""
    points = [(0.0, -0.62 * size), (0.44 * size, 0.46 * size), (0.0, 0.2 * size), (-0.44 * size, 0.46 * size)]
    a = math.radians(degrees)
    cos, sin = math.cos(a), math.sin(a)
    return [(cx + x * cos - y * sin, cy + x * sin + y * cos) for x, y in points]


def _polygon_distance(px: np.ndarray, py: np.ndarray, points: list[tuple[float, float]]) -> np.ndarray:
    """Signed distance to a simple polygon: negative inside (even-odd rule), positive outside."""
    n = len(points)
    distance = np.minimum.reduce([_segment_distance(px, py, points[i], points[(i + 1) % n]) for i in range(n)])
    inside = np.zeros(px.shape, bool)
    for i in range(n):
        (ax, ay), (bx, by) = points[i], points[(i + 1) % n]
        crosses = (ay > py) != (by > py)
        x_at = ax + (py - ay) * (bx - ax) / ((by - ay) or 1e-9)
        inside ^= crosses & (px < x_at)
    return np.where(inside, -distance, distance)


def arrow(shape: tuple[int, int], cx: float, cy: float, size: float, degrees: float, glow: float) -> np.ndarray:
    """The arrowhead with a dark rim and an orange glow, as premultiplied RGBA (h, w, 4)."""
    h, w = shape
    py, px = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    signed = _polygon_distance(px, py, arrow_points(cx, cy, size, degrees))
    rim = max(1.0, size / 12)
    body = _layer(_fill(signed + rim * 0.5), ORANGE)
    edge = _layer(_fill(signed - rim * 0.5) * 0.85, RIM)
    halo = _layer(_glow(signed - rim * 0.5, glow, 0.5), ORANGE)
    return _over(body, _over(edge, halo))


def bar_heights(level: float, seconds: float, scale: float) -> list[float]:
    """Clicky's listening waveform formula, on the Bol OS mark's four bars: a gentle idle pulse plus the
    voice level, eased so quiet speech still moves them."""
    eased = min(max(level - 0.008, 0.0) * 2.85, 1.0) ** 0.76
    heights = []
    for i, profile in enumerate(BAR_PROFILE):
        phase = seconds * 3.6 + i * 0.35
        heights.append((3 + eased * 10 * profile + (math.sin(phase) + 1) / 2 * 1.5) * scale)
    return heights


def bars(shape: tuple[int, int], cx: float, cy: float, heights: list[float], scale: float) -> np.ndarray:
    h, w = shape
    py, px = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    width, gap = BAR_WIDTH * scale, BAR_GAP * scale
    total = len(heights) * width + (len(heights) - 1) * gap
    distance = np.full((h, w), np.inf, np.float32)
    for i, height in enumerate(heights):
        x = cx - total / 2 + width / 2 + i * (width + gap)
        half = max(height / 2 - width / 2, 0.0)
        # A capsule: distance to the vertical segment minus the radius.
        dy = np.maximum(np.abs(py - cy) - half, 0.0)
        distance = np.minimum(distance, np.hypot(px - x, dy) - width / 2)
    rim = max(0.8, scale * 0.7)
    body = _layer(_fill(distance), ORANGE)
    edge = _layer(_fill(distance - rim) * 0.7, RIM)
    halo = _layer(_glow(distance - rim, 6 * scale, 0.6), ORANGE)
    return _over(body, _over(edge, halo))


def spinner(shape: tuple[int, int], cx: float, cy: float, seconds: float, scale: float) -> np.ndarray:
    """An arc from 15 % to 85 % of a circle, fading in along its length, one turn every 0.8 s."""
    h, w = shape
    py, px = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    radius = (SPINNER_DIAMETER / 2) * scale
    stroke = SPINNER_STROKE * scale
    turn = (seconds / SPINNER_TURN_S) % 1.0
    angle = (np.arctan2(py - cy, px - cx) / (2 * np.pi) - turn) % 1.0
    start, end = SPINNER_ARC
    on_arc = (angle >= start) & (angle <= end)

    def end_point(fraction: float) -> tuple[float, float]:
        a = 2 * math.pi * (fraction + turn)
        return cx + radius * math.cos(a), cy + radius * math.sin(a)

    (sx, sy), (ex, ey) = end_point(start), end_point(end)
    to_start, to_end = np.hypot(px - sx, py - sy), np.hypot(px - ex, py - ey)
    # Distance to the arc (round caps at both ends), and how far along it: 0 at the faint end, 1 at the bright.
    distance = np.where(on_arc, np.abs(np.hypot(px - cx, py - cy) - radius), np.minimum(to_start, to_end))
    # Past the ends, blend softly between the two (a hard switch would cut the bright end's glow in half).
    beyond = 1 / (1 + np.exp(np.clip((to_end - to_start) / (1.5 * stroke), -30, 30)))
    along = np.where(on_arc, (angle - start) / (end - start), beyond)
    edge = distance - stroke / 2
    rim = max(0.8, scale * 0.7)
    body = _layer(_fill(edge) * along, ORANGE)
    dark = _layer(_fill(edge - rim) * along * 0.45, RIM)
    halo = _layer(_glow(edge - rim, 6 * scale, 0.6) * along, ORANGE)
    return _over(body, _over(dark, halo))


def rounded_rect_distance(shape: tuple[int, int], left: float, top: float, width: float, height: float,
                          radius: float) -> np.ndarray:
    h, w = shape
    py, px = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    qx = np.abs(px - (left + width / 2)) - (width / 2 - radius)
    qy = np.abs(py - (top + height / 2)) - (height / 2 - radius)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius


def to_bgra(image: np.ndarray, opacity: float = 1.0) -> np.ndarray:
    out = np.empty(image.shape, np.uint8)
    scaled = np.clip(image * opacity, 0.0, 1.0)
    out[..., 0], out[..., 1], out[..., 2] = (scaled[..., 2::-1] * 255 + 0.5).transpose(2, 0, 1)
    out[..., 3] = scaled[..., 3] * 255 + 0.5
    return out


def clusters(text: str) -> list[str]:
    """The text in typing order, never splitting a vowel sign or conjunct off its letter."""
    from bolos.overlay import clusters as split

    out: list[str] = []
    for i, word in enumerate(text.split(" ")):
        if i:
            out.append(" ")
        out.extend(split(word))
    return out


# ---------------------------------------------------------------------------------------------------------
# Bubble text through GDI (Uniscribe shapes Devanagari, Tamil and the rest; numpy cannot)

_FW_SEMIBOLD, _ANTIALIASED = 600, 4
_DT_SINGLELINE, _DT_NOPREFIX, _DT_CALCRECT = 0x20, 0x800, 0x400
_TRANSPARENT = 1


class _Text:
    """Renders one line of text onto the bubble colour, so its antialiasing blends with the real
    background. Lives on the buddy's thread."""

    def __init__(self, gdi32, user32):
        self.gdi32, self.user32 = gdi32, user32
        gdi32.CreateFontW.restype = wintypes.HFONT
        gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT),
                                     wintypes.UINT]
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        self.fonts: dict[int, int] = {}

    def _font(self, pixels: int):
        if pixels not in self.fonts:
            self.fonts[pixels] = self.gdi32.CreateFontW(-pixels, 0, 0, 0, _FW_SEMIBOLD, 0, 0, 0, 1, 0, 0,
                                                        _ANTIALIASED, 0, FONT)
        return self.fonts[pixels]

    def render(self, text: str, pixels: int, background: tuple[int, int, int],
               colour: tuple[int, int, int]) -> np.ndarray:
        """The text as an (h, w, 3) RGB array on `background`."""
        from bolos.guide.pointer import _BITMAPINFOHEADER

        gdi32, user32 = self.gdi32, self.user32
        screen = user32.GetDC(None)
        dc = gdi32.CreateCompatibleDC(screen)
        old_font = gdi32.SelectObject(dc, self._font(pixels))
        try:
            box = wintypes.RECT(0, 0, 0, 0)
            user32.DrawTextW(dc, text or " ", -1, ctypes.byref(box), _DT_SINGLELINE | _DT_NOPREFIX | _DT_CALCRECT)
            width, height = max(box.right, 1), max(box.bottom, 1)
            header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                       biPlanes=1, biBitCount=32)
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
            old_bitmap = gdi32.SelectObject(dc, bitmap)
            try:
                r, g, b = background
                brush = gdi32.CreateSolidBrush(r | g << 8 | b << 16)
                area = wintypes.RECT(0, 0, width, height)
                user32.FillRect(dc, ctypes.byref(area), brush)
                gdi32.DeleteObject(brush)
                gdi32.SetBkMode(dc, _TRANSPARENT)
                r, g, b = colour
                gdi32.SetTextColor(dc, r | g << 8 | b << 16)
                user32.DrawTextW(dc, text, -1, ctypes.byref(area), _DT_SINGLELINE | _DT_NOPREFIX)
                raw = ctypes.string_at(bits, width * height * 4)
            finally:
                gdi32.SelectObject(dc, old_bitmap)
                gdi32.DeleteObject(bitmap)
        finally:
            gdi32.SelectObject(dc, old_font)
            gdi32.DeleteDC(dc)
            user32.ReleaseDC(None, screen)
        return np.frombuffer(raw, np.uint8).reshape(height, width, 4)[..., 2::-1].copy()

    def close(self) -> None:
        for font in self.fonts.values():
            self.gdi32.DeleteObject(font)
        self.fonts.clear()


def bubble(text_rgb: np.ndarray, scale: float, pop_scale: float) -> tuple[np.ndarray, tuple[float, float]]:
    """The label bubble around rendered text, as premultiplied RGBA, and its (width, height) at full size.
    `pop_scale` < 1 shrinks it about its centre and brightens its glow (Clicky's "materialising" entrance)."""
    th, tw = text_rgb.shape[:2]
    pad_x, pad_y = BUBBLE_PAD[0] * scale, BUBBLE_PAD[1] * scale
    width, height = tw + 2 * pad_x, th + 2 * pad_y
    glow = (6 + (1 - pop_scale) * 16) * scale
    margin = math.ceil(glow * 1.6)
    shape = (math.ceil(height) + 2 * margin, math.ceil(width) + 2 * margin)
    s = max(pop_scale, 0.05)
    cx, cy = shape[1] / 2, shape[0] / 2
    distance = rounded_rect_distance(shape, cx - width * s / 2, cy - height * s / 2, width * s, height * s,
                                     BUBBLE_RADIUS * scale * s)
    image = _layer(_fill(distance), ORANGE)
    # The text, scaled with the bubble (nearest-neighbour is fine for the 0.2 s it is not at full size).
    ys = ((np.arange(shape[0]) + 0.5 - cy) / s + th / 2).astype(int)
    xs = ((np.arange(shape[1]) + 0.5 - cx) / s + tw / 2).astype(int)
    inside = (ys[:, None] >= 0) & (ys[:, None] < th) & (xs[None, :] >= 0) & (xs[None, :] < tw)
    texture = text_rgb[np.clip(ys, 0, th - 1)][:, np.clip(xs, 0, tw - 1)].astype(np.float32) / 255.0
    coverage = _fill(distance)[..., None]
    mask = inside[..., None] & (coverage > 0)
    image[..., :3] = np.where(mask, texture * coverage, image[..., :3])
    halo = _layer(_glow(distance, glow, min(1.0, 0.5 + (1 - pop_scale))), ORANGE)
    return _over(image, halo), (width, height)


# ---------------------------------------------------------------------------------------------------------
# The window


class Buddy:
    """The buddy. Thread-safe: every method queues a command for the window's own thread."""

    def __init__(self, visible: bool = True, on_click=None, *, on_move=None, position_path: Path | None = None):
        self._commands: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.visible = visible
        self.on_click = on_click
        self.on_move = on_move
        self.position_path = position_path
        self._position: tuple[float, float] | None = None  # where it is now, for estimating a flight
        self._scale = 1.0

    def set(self, kind: str, value=None) -> None:
        if kind in ("state", "level", "speaking"):
            self._send((kind, value))

    def show(self, visible: bool) -> None:
        """When disabled, show the widget only during commands."""
        self.visible = bool(visible)
        self._send(("visible", self.visible))

    def point(self, target: tuple[int, int, int, int], label: str = "",
              bounds: tuple[int, int, int, int] | None = None) -> float:
        """Acknowledge a guide target and return the delay before the ring should appear."""
        self._send(("point", (tuple(target), label.strip(), tuple(bounds) if bounds else None)))
        if self.on_click is not None:
            return 0.0  # the widget stays put while the ring highlights the target
        scale = self._scale
        start = self._position or (target[0], target[1])
        return Flight(start, landing(target, bounds, scale), scale).duration

    def release(self) -> None:
        """End the guide cue."""
        self._send(("release", None))

    def welcome(self, greeting: str, hint: str) -> None:
        """Preview the widget animation or show the standalone pointer's greeting."""
        self._send(("welcome", (greeting, hint)))

    def close(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._commands.put(("close", None))
            thread.join(2.0)

    def start(self) -> Buddy:
        self._send(("visible", self.visible))
        return self

    def _send(self, command) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="bolos-buddy")
                self._thread.start()
        self._commands.put(command)

    def _run(self) -> None:
        try:
            _Window(self).loop()
        except Exception:  # the buddy is a help; Bol OS works without it
            log.exception("The buddy stopped")


class _Window:
    """Everything that runs on the buddy's thread: the state machine and the drawing."""

    def __init__(self, owner: Buddy):
        from bolos.guide import pointer as p

        self.owner = owner
        self.p = p
        self.user32, self.gdi32, self.kernel32 = p._api()
        self.state = "ready"
        self.level = 0.0
        self.speaking = False
        self.sprites = {}
        self.widget = getattr(owner, "on_click", None) is not None
        self.position = None
        self.press_at = None
        self.press_center = None
        self.dragging = False
        if self.widget:
            try:
                with np.load(Path(__file__).with_name("assets") / "sahur.npz") as art:
                    for name in SPRITE_FPS:
                        rgba = art[name]
                        bgra = rgba[..., [2, 1, 0, 3]].copy()
                        bgra[..., :3] = (bgra[..., :3].astype(np.uint16) * bgra[..., 3:] + 127) // 255
                        self.sprites[name] = bgra
            except (OSError, KeyError, ValueError):
                self.sprites.clear()
                log.warning("Could not load Sahur artwork")
            self.position = self._load_position()
        self.sprite_state = "idle"
        self.sprite_since = 0.0
        self.preview_until = 0.0
        self.hit_frame = None
        self.visible = owner.visible
        self.mode = "follow"  # follow | flying | pointing | returning
        self.spring: Spring | None = None
        self.flight: Flight | None = None
        self.flight_at = 0.0
        self.degrees, self.turn_from, self.turned_at = REST_DEGREES, REST_DEGREES, 0.0
        self.swell = 1.0
        self.target_label = ""
        self.landed_at = 0.0
        self.letters: list[str] = []
        self.shown = 0
        self.next_letter_at = 0.0
        self.typed_at: float | None = None
        self.bubble_fade_at: float | None = None
        self.return_mouse: tuple[float, float] | None = None
        self.opacity = 0.0
        self.fade_speed = 1 / APPEAR_S
        self.idle_since: float | None = None
        self.was_active = False
        self.mix = {"arrow": 1.0, "bars": 0.0, "spinner": 0.0}
        self.welcome: list[str] = []
        self.welcome_text = ""
        self.welcome_letters: list[str] = []
        self.welcome_shown = 0
        self.welcome_next = 0.0
        self.welcome_hold_until: float | None = None
        self.welcome_alpha = 0.0
        self.text = None
        self.text_cache: tuple | None = None
        self.frame_scale = 1.0

    # Commands

    def handle(self, command: str, value, now: float, cursor: tuple[int, int]) -> None:
        if command == "state" and isinstance(value, str) and value:
            previous, self.state = self.state, value
            if value in LISTENING and previous not in LISTENING and previous not in ON_A_TARGET:
                # A new press of the hotkey: drop the hello, and come back from pointing to show the bars.
                # (A guide listens for "ho gaya", and a form session for the answer to the field it points at,
                # so not during those.)
                self.welcome.clear()
                self.welcome_text = ""
                if self.mode in ("flying", "pointing"):
                    self._fly_back(now, cursor)
        elif command == "speaking":
            self.speaking = bool(value)
        elif command == "level":
            try:
                self.level = min(max(float(value), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
        elif command == "visible":
            self.visible = bool(value)
        elif command == "point":
            if self.widget:
                self.preview_until = now + 0.6
                return
            target, label, bounds = value
            scale = self.scale_at(((target[0] + target[2]) // 2, (target[1] + target[3]) // 2))
            here = (self.spring.x, self.spring.y) if self.spring else self._rest(cursor, scale)
            self.flight = Flight(here, landing(target, bounds, scale), scale)
            self.flight_at = now
            self.mode = "flying"
            self.target_label = label
            self.letters, self.shown, self.typed_at, self.bubble_fade_at = [], 0, None, None
            self.welcome.clear()
            self.welcome_text = ""
        elif command == "release":
            if self.widget:
                self.preview_until = 0.0
            if self.mode in ("flying", "pointing"):
                self._fly_back(now, cursor)
        elif command == "welcome":
            if self.widget:
                self.preview_until = now + 1.5
                return
            greeting, hint = value
            self.welcome = [t for t in (greeting, hint) if t]
            self.opacity = 0.0
            self.fade_speed = 1 / WELCOME_FADE_S
            self.welcome_next = now + WELCOME_FADE_S * 0.6
            self._next_welcome(now)

    def _next_welcome(self, now: float) -> None:
        self.welcome_text = self.welcome.pop(0) if self.welcome else ""
        self.welcome_letters = clusters(self.welcome_text)
        self.welcome_shown = 0
        self.welcome_hold_until = None
        self.welcome_alpha = 1.0 if self.welcome_text else 0.0

    def _fly_back(self, now: float, cursor: tuple[int, int]) -> None:
        scale = self.scale_at(cursor)
        here = (self.spring.x, self.spring.y) if self.spring else self._rest(cursor, scale)
        self.flight = Flight(here, self._rest(cursor, scale), scale)
        self.flight_at = now
        self.mode = "returning"
        self.return_mouse = cursor
        self.bubble_fade_at = self.bubble_fade_at or now

    # Geometry helpers

    def scale_at(self, point: tuple[int, int]) -> float:
        rect = self.p.Rect(point[0], point[1], point[0] + 1, point[1] + 1)
        return self.p.ui_scale(rect)

    @staticmethod
    def _rest(cursor: tuple[int, int], scale: float) -> tuple[float, float]:
        return cursor[0] + OFFSET[0] * scale, cursor[1] + OFFSET[1] * scale

    def _load_position(self) -> tuple[int, int]:
        path = self.owner.position_path
        if path is not None:
            try:
                point = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(point, list) and len(point) == 2
                        and all(type(v) is int and abs(v) <= 32767 for v in point)):
                    return point[0], point[1]
            except (OSError, ValueError):
                pass
        left, top, right, bottom = _monitor_at((0, 0), work=True) or (0, 0, 800, 600)
        return right - round(98 * self.scale_at((left, top))), top + (bottom - top) * 2 // 3

    def _save_position(self) -> None:
        path = self.owner.position_path
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(self.position), encoding="utf-8")
                temp.replace(path)
            except OSError:
                log.warning("Could not save Sahur's position")

    def _advance_drag(self, cursor: tuple[int, int]) -> None:
        dx, dy = cursor[0] - self.press_at[0], cursor[1] - self.press_at[1]
        if self.dragging or math.hypot(dx, dy) >= DRAG_THRESHOLD * self.frame_scale:
            self.dragging = True
            self.position = self.press_center[0] + dx, self.press_center[1] + dy

    def _place_widget(self) -> None:
        area = _monitor_at(self.position, work=True)
        self.frame_scale = self.scale_at(self.position)
        x, y = self.position
        if area:
            left, top, right, bottom = area
            pad_x, pad_y = (math.ceil(size * self.frame_scale / 2) for size in SPRITE_SIZE)
            x = max(left + pad_x, min(x, right - pad_x))
            y = max(top + pad_y, min(y, bottom - pad_y))
        self.position = x, y
        if self.spring is None:
            self.spring = Spring(x, y)
        else:
            self.spring.place(x, y)

    def _finish_press(self, cursor: tuple[int, int]) -> None:
        self._advance_drag(cursor)
        moved = self.dragging
        self.press_at = self.press_center = None
        self.dragging = False
        if moved:
            self._place_widget()
            self._save_position()
            callback = self.owner.on_move
        else:
            callback = self.owner.on_click
        if callback:
            x, y = self.position
            gap = math.ceil((SPRITE_SIZE[0] / 2 + 8) * self.frame_scale)
            callback(x, y, _monitor_at(self.position, work=True), gap)

    def cursor_shown(self) -> bool:
        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE),
                        ("ptScreenPos", wintypes.POINT)]

        info = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
        if not self.user32.GetCursorInfo(ctypes.byref(info)):
            return True
        return bool(info.flags & 1)

    # One frame of state

    def update(self, now: float, dt: float, cursor: tuple[int, int]) -> None:
        if self.widget:
            if self.spring is None or self.dragging:
                self._place_widget()
        else:
            self._update_pointer(now, dt, cursor)

        active = (busy(self.state) or self.mode != "follow" or bool(self.welcome_text) or self.speaking
                  or now < self.preview_until or self.press_at is not None)
        if active:
            self.idle_since = None
            self.was_active = True
        elif self.was_active:
            self.idle_since, self.was_active = now, False
        lingering = self.idle_since is not None and now - self.idle_since < TRANSIENT_PAUSE_S
        show = (self.visible or active or lingering) and (self.widget or self.cursor_shown() or self.mode != "follow")
        goal = 1.0 if show else 0.0
        speed = self.fade_speed if goal > self.opacity else 1 / VANISH_S
        self.opacity = min(goal, self.opacity + dt * speed) if goal > self.opacity else max(goal,
                                                                                          self.opacity - dt * speed)
        if self.opacity >= 1.0:
            self.fade_speed = 1 / APPEAR_S
        self.owner._position = (self.spring.x, self.spring.y)
        self.owner._scale = self.frame_scale

    def _update_pointer(self, now: float, dt: float, cursor: tuple[int, int]) -> None:
        scale = self.frame_scale = self.scale_at(cursor)
        rest = self._rest(cursor, scale)
        if self.spring is None:
            self.spring = Spring(*rest)
        if self.mode == "follow":
            self.spring.step(*rest, dt)
            self.degrees, self.swell = REST_DEGREES, 1.0
        elif self.mode in ("flying", "returning"):
            elapsed = now - self.flight_at
            if self.mode == "returning" and self.return_mouse is not None:
                if math.dist(cursor, self.return_mouse) > CANCEL_RETURN * scale:
                    self.mode = "follow"  # the user moved on: snap back to trailing the cursor
                    self.spring.vx = self.spring.vy = 0.0
                    self.swell, self.degrees = 1.0, REST_DEGREES
                    return
            pose = self.flight.pose(elapsed)
            self.spring.place(pose.x, pose.y)
            self.degrees, self.swell = pose.degrees, pose.swell
            if self.flight.done(elapsed):
                self.swell = 1.0
                if self.mode == "flying":
                    self.mode = "pointing"
                    self.landed_at = now
                    self.turn_from, self.turned_at = self.degrees, now
                    self.letters = clusters(self.target_label)
                    self.shown = 0
                    self.next_letter_at = now
                    self.typed_at = None if self.letters else now
                else:
                    self.mode = "follow"
                    self.degrees = REST_DEGREES
        elif self.mode == "pointing":
            self.degrees = _angle_between(self.turn_from, REST_DEGREES, (now - self.turned_at) / TURN_S)
            while self.shown < len(self.letters) and now >= self.next_letter_at:
                self.shown += 1
                self.next_letter_at = now + random.uniform(*LETTER_S)
            if self.typed_at is None and self.shown >= len(self.letters):
                self.typed_at = now
            if self.typed_at is not None and self.bubble_fade_at is None and now - self.typed_at >= HOLD_S:
                self.bubble_fade_at = now
            if self.bubble_fade_at is not None and now - self.bubble_fade_at >= BUBBLE_FADE_S + RETURN_PAUSE_S:
                self._fly_back(now, cursor)

        # The welcome bubble types itself, holds two seconds, fades, then the hint; the hint stays ten.
        if self.welcome_text and now >= self.welcome_next:
            if self.welcome_shown < len(self.welcome_letters):
                self.welcome_shown += 1
                self.welcome_next = now + 0.03
            elif self.welcome_hold_until is None:
                self.welcome_hold_until = now + (2.0 if self.welcome else 10.0)
            elif now >= self.welcome_hold_until:
                self.welcome_alpha = max(0.0, self.welcome_alpha - dt / BUBBLE_FADE_S)
                if self.welcome_alpha == 0.0:
                    self.welcome_next = now + 0.3
                    self._next_welcome(now)

        # Arrow, bars or spinner: cross-fade toward the one this state wants (the arrow while pointing).
        wanted = "arrow" if self.mode != "follow" else visual_for(self.state)
        for name in self.mix:
            goal = 1.0 if name == wanted else 0.0
            step = dt / CROSSFADE_S
            self.mix[name] = min(goal, self.mix[name] + step) if goal > self.mix[name] else max(goal,
                                                                                                 self.mix[name] - step)

    # Drawing

    def _label_image(self, text: str, scale: float):
        key = (text, round(scale, 3))
        if self.text_cache is None or self.text_cache[0] != key:
            rgb = self.text.render(text, round(FONT_POINTS * scale * 96 / 72), ORANGE, INK)
            self.text_cache = (key, rgb)
        return self.text_cache[1]

    def compose(self, now: float, monitor: tuple[int, int, int, int] | None):
        """The frame: (left, top, BGRA pixels) or None when nothing shows."""
        if self.opacity <= 0.0:
            return None
        if self.sprites:
            return self._sprite_frame(now)
        s = self.frame_scale
        size = ARROW_SIZE * s * self.swell
        glow = (GLOW + (self.swell - 1) * 20) * s
        reach = math.ceil(size + glow * 1.6 + 16 * s)
        shape = (2 * reach, 2 * reach)
        cx = cy = reach
        layers = []
        if self.mix["arrow"] > 0:
            layers.append((arrow(shape, cx, cy, size, self.degrees, glow), self.mix["arrow"]))
        if self.mix["bars"] > 0:
            layers.append((bars(shape, cx, cy, bar_heights(self.level, now, s), s), self.mix["bars"]))
        if self.mix["spinner"] > 0:
            layers.append((spinner(shape, cx, cy, now, s), self.mix["spinner"]))
        canvas = np.zeros(shape + (4,), np.float32)
        for image, weight in layers:
            canvas = _over(image * weight, canvas)
        left, top = self.spring.x - cx, self.spring.y - cy
        pieces = [(left, top, canvas)]

        label, alpha, pop_scale = self._bubble_text(now)
        if label:
            image, (width, height) = bubble(self._label_image(label, s), s, pop_scale)
            bx = self.spring.x + BUBBLE_GAP[0] * s
            if monitor is not None and bx + width > monitor[2] - 4 * s:
                bx = self.spring.x - BUBBLE_GAP[0] * s - width  # no room on the right: put it on the left
            by = self.spring.y + BUBBLE_GAP[1] * s - height / 2
            margin_x = (image.shape[1] - width) / 2
            margin_y = (image.shape[0] - height) / 2
            pieces.append((bx - margin_x, by - margin_y, image * alpha))
        return _flatten(pieces, self.opacity)

    def _sprite_frame(self, now: float):
        if self.dragging:
            state = "walk"
        elif self.state in LISTENING:
            state = "listen"
        elif self.speaking:
            state = "answer"
        elif now < self.preview_until:
            state = "hop"
        else:
            state = "idle"
        if state != self.sprite_state:
            self.sprite_state, self.sprite_since = state, now
        frames = self.sprites[state]
        frame = frames[int((now - self.sprite_since) * SPRITE_FPS[state]) % len(frames)]
        height, width = frame.shape[:2]
        w, h = (max(1, round(size * self.frame_scale)) for size in SPRITE_SIZE)
        rows = np.arange(h) * height // h
        cols = np.arange(w) * width // w
        pixels = frame[rows[:, None], cols]
        if self.opacity < 1:
            pixels[:] = pixels * self.opacity
        return round(self.spring.x - w / 2), round(self.spring.y - h / 2), pixels

    def _bubble_text(self, now: float) -> tuple[str, float, float]:
        if self.widget:
            return "", 0.0, 1.0
        if self.mode in ("pointing", "returning") and self.shown:
            alpha = 1.0
            if self.bubble_fade_at is not None:
                alpha = max(0.0, 1 - (now - self.bubble_fade_at) / BUBBLE_FADE_S)
            if alpha <= 0:
                return "", 0.0, 1.0
            return "".join(self.letters[:self.shown]), alpha, pop(now - self.landed_at)
        if self.welcome_text and self.welcome_shown:
            return "".join(self.welcome_letters[:self.welcome_shown]), self.welcome_alpha, 1.0
        return "", 0.0, 1.0

    # The loop

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg in (0x007e, 0x001a):  # WM_DISPLAYCHANGE or WM_SETTINGCHANGE
            self.spring = None  # recheck work area before drawing
        if msg == 0x0021:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE
        x = ctypes.c_short(lparam & 0xffff).value
        y = ctypes.c_short((lparam >> 16) & 0xffff).value
        if msg == 0x0084:  # WM_NCHITTEST, with signed virtual-screen coordinates
            if self.hit_frame is not None:
                left, top, pixels = self.hit_frame
                px, py = x - left, y - top
                h, w = pixels.shape[:2]
                if 0 <= px < w and 0 <= py < h and pixels[py, px, 3] > 32:
                    return 1  # HTCLIENT
            return -1  # HTTRANSPARENT
        if msg == 0x0201 and self.hit_frame is not None:  # WM_LBUTTONDOWN
            left, top, _ = self.hit_frame
            self.press_at = left + x, top + y
            self.press_center = self.position
            return 0
        if msg == 0x0202 and self.press_at is not None and self.hit_frame is not None:  # WM_LBUTTONUP
            left, top, _ = self.hit_frame
            self._finish_press((left + x, top + y))
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def loop(self) -> None:
        from bolos.overlay import make_dpi_aware

        make_dpi_aware()
        p, user32, gdi32 = self.p, self.user32, self.gdi32
        instance = self.kernel32.GetModuleHandleW(None)
        class_name = f"BolOSBuddy-{threading.get_ident()}"
        procedure = p.WNDPROC(self._window_proc if self.widget else user32.DefWindowProcW)
        window_class = p._WNDCLASSEXW(cbSize=ctypes.sizeof(p._WNDCLASSEXW), lpfnWndProc=procedure,
                                      hInstance=instance, lpszClassName=class_name)
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            log.warning("Could not register the buddy window (error %d)", ctypes.get_last_error())
            return
        style = p.WS_EX_LAYERED | p.WS_EX_TOPMOST | p.WS_EX_TOOLWINDOW | p.WS_EX_NOACTIVATE
        if not self.widget:
            style |= p.WS_EX_TRANSPARENT
        user32.GetAsyncKeyState.restype = ctypes.c_short
        hwnd = user32.CreateWindowExW(style, class_name, "Bol OS buddy", p.WS_POPUP,
                                      0, 0, 1, 1, None, None, instance, None)
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            log.warning("Could not create the buddy window (error %d)", ctypes.get_last_error())
            return
        self.text = _Text(gdi32, user32)
        screen = user32.GetDC(None)
        surface = surface_size = None
        shown = False
        raised_at = 0.0
        last = time.monotonic()
        message = wintypes.MSG()
        from bolos.guide.screen import cursor_position

        try:
            while True:
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                now = time.monotonic()
                cursor = cursor_position()
                if self.press_at is not None:
                    # Poll during the gesture so a non-activating window can drag past its own pixels.
                    button = 2 if user32.GetSystemMetrics(23) else 1  # SM_SWAPBUTTON, VK_RBUTTON/VK_LBUTTON
                    if user32.GetAsyncKeyState(button) & 0x8000:
                        self._advance_drag(cursor)
                    else:
                        self._finish_press(cursor)
                while True:
                    try:
                        command, value = self.owner._commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "close":
                        return
                    self.handle(command, value, now, cursor)
                dt, last = min(now - last, 0.1), now
                self.update(now, dt, cursor)
                monitor = None if self.widget else _monitor_at((round(self.spring.x), round(self.spring.y)))
                frame = self.compose(now, monitor)
                self.hit_frame = frame
                if frame is None:
                    if shown:
                        user32.ShowWindow(hwnd, p.SW_HIDE)
                        shown = False
                    try:  # nothing on screen: wait for a command instead of spinning
                        command, value = self.owner._commands.get(timeout=1 / FPS)
                        if command == "close":
                            return
                        self.handle(command, value, time.monotonic(), cursor_position())
                    except queue.Empty:
                        pass
                    continue
                left, top, pixels = frame
                size = (pixels.shape[1], pixels.shape[0])
                if size != surface_size:
                    if surface is not None:
                        surface.close()
                    surface = p._Surface(gdi32, screen, size)
                    surface_size = size
                surface.write(pixels)
                blend = p._BLENDFUNCTION(0, 0, 255, p.AC_SRC_ALPHA)
                user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(left, top)),
                                           ctypes.byref(wintypes.SIZE(*size)), surface.dc,
                                           ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), p.ULW_ALPHA)
                if not shown:
                    user32.ShowWindow(hwnd, p.SW_SHOWNOACTIVATE)
                    shown = True
                if now - raised_at >= RAISE_S:
                    user32.SetWindowPos(hwnd, p.HWND_TOPMOST, 0, 0, 0, 0,
                                        p.SWP_NOSIZE | p.SWP_NOMOVE | p.SWP_NOACTIVATE)
                    raised_at = now
                spent = time.monotonic() - now
                time.sleep(max(0.0, 1 / FPS - spent))
        finally:
            if surface is not None:
                surface.close()
            self.text.close()
            user32.ReleaseDC(None, screen)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, instance)


def _flatten(pieces: list[tuple[float, float, np.ndarray]], opacity: float):
    """Place the pieces (left, top, premultiplied RGBA) on one canvas: (left, top, BGRA uint8)."""
    left = math.floor(min(x for x, _, _ in pieces))
    top = math.floor(min(y for _, y, _ in pieces))
    right = math.ceil(max(x + image.shape[1] for x, _, image in pieces))
    bottom = math.ceil(max(y + image.shape[0] for _, y, image in pieces))
    canvas = np.zeros((bottom - top, right - left, 4), np.float32)
    for x, y, image in pieces:  # each later piece goes under the earlier ones: the buddy stays over its bubble
        ox, oy = round(x) - left, round(y) - top
        h, w = image.shape[:2]
        canvas[oy:oy + h, ox:ox + w] = _over(canvas[oy:oy + h, ox:ox + w], image)
    return left, top, to_bgra(canvas, opacity)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _monitor_at(point: tuple[int, int], *, work: bool = False) -> tuple[int, int, int, int] | None:
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(wintypes.POINT(*point), 2)
        info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        r = info.rcWork if work else info.rcMonitor
        return r.left, r.top, r.right, r.bottom
    except (AttributeError, OSError):
        return None
