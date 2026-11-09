"""Windows UI Automation: type into the focused field, click by name, read the screen."""

import difflib
import re
import time

import win32clipboard
import win32gui
from pywinauto import Desktop
from pywinauto.keyboard import send_keys

CLICKABLE = {"Button", "Hyperlink", "MenuItem", "TabItem", "ListItem", "TreeItem", "CheckBox", "RadioButton"}
READABLE = {"Text", "Hyperlink", "ListItem", "Header", "DataItem"}
EDITABLE = {"Edit", "Document"}
CLICK_MATCH_CUTOFF = 0.8
_NOT_WORD = re.compile(r"[^\w\s]+")


def _label(text: str) -> str:
    """Button label without decoration: "Next >" and "OK..." become "next" and "ok"."""
    return " ".join(_NOT_WORD.sub(" ", text.casefold()).split())


class ControlNotFound(Exception):
    pass


class ProtectedControl(Exception):
    pass


def _focused_field_kind() -> str:
    """Inspect focus metadata, without fetching any existing value."""
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo

    from bolos.forms.fields import field_kind

    info = UIAElementInfo(IUIA().get_focused_element())
    if info.control_type not in EDITABLE:
        raise ProtectedControl("No verified editable field")
    label = f"{info.name or ''} {info.automation_id or ''}".replace("_", " ").replace("-", " ")
    if not label.strip():
        raise ProtectedControl("The focused field has no label")
    return field_kind(label, info.control_type,
                      bool(info.element.CurrentIsPassword))


def match_label(target: str, labels: list[str]) -> str | None:
    """The label (as given, already _label-normalised) to press for a spoken target, or None.

    Clicks require confirmation and only an exact label or a near-identical one (a recognition slip
    like "Sumbit") may be pressed; "Delete" must never match "Delete all", nor "Sign in" "Sign up".
    """
    key = _label(target)
    if key in labels:
        return key
    close = difflib.get_close_matches(key, labels, n=1, cutoff=CLICK_MATCH_CUTOFF)
    return close[0] if close else None


def _foreground():
    hwnd = win32gui.GetForegroundWindow()
    return _window(win32gui.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT, including UWP apps with a focused child


def _window(hwnd):
    if not hwnd:
        raise ControlNotFound("No foreground window")
    return Desktop(backend="uia").window(handle=hwnd).wrapper_object()


def _get_clipboard() -> str | None:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def _set_clipboard(text: str | None) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        if text is not None:
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def type_text(text: str) -> None:
    try:
        if _focused_field_kind() in {"secret", "file"}:
            raise ProtectedControl("Enter protected fields yourself")
    except ProtectedControl:
        raise
    except Exception:
        raise ProtectedControl("Focused field could not be verified") from None
    # Pasting, not simulated keystrokes: keystrokes mangle Indic vowel signs and conjuncts.
    previous = _get_clipboard()
    _set_clipboard(text)
    try:
        send_keys("^v")
        time.sleep(0.15)  # let the target app read the clipboard before restoring it
    finally:
        _set_clipboard(previous)


def click(target: str, *, binding=None) -> str:
    """Click the visible control whose name best matches the spoken target. Returns its name."""
    hwnd = win32gui.GetForegroundWindow()
    root = (win32gui.GetAncestor(hwnd, 2) or hwnd) if hwnd else 0
    if binding is not None and root != binding[0]:
        raise ControlNotFound(target)
    key = _label(target)
    candidates = {}
    for ctrl in _foreground().descendants():
        info = ctrl.element_info
        if info.control_type in CLICKABLE and info.name and ctrl.is_visible() and ctrl.is_enabled():
            if binding is not None and _label(info.name) != key:
                continue
            candidates.setdefault(_label(info.name), []).append(ctrl)
    hit = key if binding is not None else match_label(target, list(candidates))
    if len(candidates.get(hit, [])) != 1 or win32gui.GetForegroundWindow() != hwnd:
        raise ControlNotFound(target)
    control, = candidates[hit]
    label = control.element_info.name
    if binding is not None:
        from bolos.guide.screen import Rect

        rect = control.rectangle()
        current = Rect(rect.left, rect.top, rect.right, rect.bottom)
        at_point = win32gui.WindowFromPoint(current.center)
        if not current.near(binding[1], 6) or not at_point or (win32gui.GetAncestor(at_point, 2) or at_point) != root:
            raise ControlNotFound(target)
    control.click_input()
    return label


def _field_text(ctrl) -> str:
    """The contents of an edit box or document. window_text() returns the label for many edit boxes."""
    from bolos.forms.fields import field_kind

    info = ctrl.element_info
    try:
        label = f"{info.name or ''} {info.automation_id or ''}".replace("_", " ").replace("-", " ")
        if not label.strip() or field_kind(label, info.control_type,
                      bool(info.element.CurrentIsPassword)) in {"secret", "file"}:
            return ""
    except Exception:
        return ""
    try:
        return ctrl.iface_value.CurrentValue or ""
    except Exception:  # controls without the Value pattern (e.g. rich documents) expose text directly
        return ctrl.window_text()


def screen_text(max_chars: int = 4000, *, hwnd: int | None = None) -> str:
    """Visible text of the foreground window, top to bottom, without duplicates."""
    from bolos.guide.screen import user_window

    hwnd = user_window() if hwnd is None else hwnd
    if not hwnd:
        return ""
    window = _window(hwnd)
    seen: dict[str, None] = {window.window_text(): None}
    for ctrl in window.descendants():
        info = ctrl.element_info
        if not ctrl.is_visible():
            continue
        if info.control_type in EDITABLE:
            seen.setdefault(_field_text(ctrl).strip(), None)
        elif info.control_type in READABLE and info.name:
            seen.setdefault(info.name.strip(), None)
    text = "\n".join(t for t in seen if t)
    return text[:max_chars]
