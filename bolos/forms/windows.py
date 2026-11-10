"""Labelled fields in the foreground document via Windows UI Automation.

Two layers. WindowsFormBackend (FormAssistant's backend): no browser debugging port, injected script or
keystrokes, and values are never collected; every mutation rechecks the foreground window and complete
metadata fingerprint and uses the captured control itself. Below it, scan()/fill()/choose()/... (see there).
"""

import difflib
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from dataclasses import field as _dc_field
from urllib.parse import urlsplit

from bolos.forms.fields import Field, Form, Snapshot, field_kind


class FormChanged(RuntimeError):
    pass


class UnsupportedField(RuntimeError):
    pass


def _runtime_id(ctrl) -> str:
    runtime = getattr(ctrl.element_info, "runtime_id", None)
    if not runtime:
        raise UnsupportedField("Control has no stable runtime identity")
    return ":".join(str(p) for p in runtime)


def _property(ctrl, name, default=False):
    try:
        return getattr(ctrl.element_info.element, name)
    except Exception:
        return default


def _visible(ctrl) -> bool:
    try:
        return ctrl.is_visible() and ctrl.is_enabled()
    except Exception:
        return False


def metadata(ctrl) -> Field | None:
    """Only accessible names/properties, never ValuePattern.CurrentValue or get_value()."""
    info = ctrl.element_info
    field_types = {"Edit", "ComboBox", "CheckBox", "RadioButton"}
    if info.control_type not in field_types | {"Button"}:
        return None
    label = re.sub(r"\s+", " ", info.name or "").strip()
    if not label or len(label) > 180:
        return (Field(_runtime_id(ctrl), "Unlabelled field", "manual", protected=True)
                if info.control_type in field_types else None)
    # Automation IDs can disambiguate a CAPTCHA whose visual label says only 'characters shown'.
    # They are metadata, never current control values, and never spoken back.
    identifier = re.sub(r"[_-]+", " ", getattr(info, "automation_id", "") or "")
    kind = field_kind(f"{label} {identifier}", info.control_type, bool(_property(ctrl, "CurrentIsPassword")))
    if info.control_type == "Button" and kind not in {"secret", "file"}:
        return None
    protected = kind in {"secret", "file"} or bool(_property(ctrl, "CurrentIsPassword"))
    required = bool(_property(ctrl, "CurrentIsRequiredForForm")) or "*" in label
    # ComboBox options are only those already exposed; never expand a control to discover choices.
    options = ()
    if info.control_type == "ComboBox":
        options = tuple(dict.fromkeys(c.element_info.name.strip() for c in ctrl.descendants()
                                      if c.element_info.control_type == "ListItem" and c.element_info.name.strip()))
    if info.control_type == "RadioButton":
        # 'Yes' alone does not say whether this is a PEP declaration, consent or another question.
        # Do not automate radios until reliable group-label extraction is implemented.
        kind, protected = "manual", True
    if not _visible(ctrl):
        kind, protected = "manual", True
    return Field(_runtime_id(ctrl), label, kind, required, options, protected)


class WindowsFormBackend:
    def __init__(self):
        self._controls = {}
        self._buttons = {}

    def snapshot(self) -> Snapshot:
        import win32gui
        from pywinauto import Desktop

        handle = win32gui.GetForegroundWindow()
        window = Desktop(backend="uia").window(handle=handle).wrapper_object()
        # Restrict to one document; the browser toolbar/search box are never form fields.
        docs = [c for c in window.descendants(control_type="Document") if _visible(c)]
        if len(docs) != 1:
            raise UnsupportedField("A single accessible foreground document is required")
        document = docs[0]
        controls, buttons, fields = {}, {}, []
        errors = 0
        for ctrl in document.descendants():
            # Offscreen fields still count so completing the visible portion cannot silently submit
            # a partly inspected form. Such fields are metadata-only/manual in this first adapter.
            field = metadata(ctrl)
            if field is not None:
                controls[field.key] = ctrl
                fields.append(field)
            elif ctrl.element_info.control_type == "Button" and _visible(ctrl):
                label = (ctrl.element_info.name or "").casefold().strip()
                if label in {"submit", "submit form", "जमा करें", "जमा करें।", "सबमिट", "सबमिट करें"}:
                    buttons[_runtime_id(ctrl)] = ctrl
            if ctrl.element_info.control_type == "Text":
                label = ctrl.element_info.name or ""
                if re.search(r"\b(required|invalid|must have|must be|is missing|not valid)\b", label, re.I):
                    # Only live region / alert text counts, not the form's ordinary instructions.
                    live = _property(ctrl, "CurrentLiveSetting", 0)
                    if live:
                        errors += 1
        fields_tuple = tuple(fields)
        submit = next(iter(buttons)) if len(buttons) == 1 else None
        identity = (handle, _runtime_id(document), fields_tuple, tuple(buttons))
        self._controls, self._buttons = controls, buttons
        # Address-bar retrieval would require collecting a ValuePattern. Keep URL unknown until a
        # dedicated, tested browser adapter can identify it without accidentally reading a form field.
        return Snapshot(identity, fields_tuple, submit, errors=errors)

    def _check(self, snapshot: Snapshot):
        current = self.snapshot()
        if current.identity != snapshot.identity:
            raise FormChanged("Foreground document or form controls changed")
        return current

    def set_value(self, snapshot: Snapshot, field: Field, value: str):
        self._check(snapshot)
        if field not in snapshot.fields or field.protected or field.kind in {"secret", "file"}:
            raise UnsupportedField("Protected control")
        ctrl = self._controls[field.key]
        if not _visible(ctrl):
            raise FormChanged("Field is no longer visible and enabled")
        if field.kind == "checkbox":
            try:
                if ctrl.element_info.control_type == "RadioButton":
                    if value != "true":
                        return  # do not choose another option on a spoken no
                    ctrl.iface_selection_item.Select()
                else:
                    expected = int(value == "true")
                    state = ctrl.iface_toggle.CurrentToggleState
                    if state not in (0, 1):
                        raise UnsupportedField("Indeterminate checkbox")
                    if state != expected:
                        ctrl.iface_toggle.Toggle()
                return
            except UnsupportedField:
                raise
            except Exception:
                raise UnsupportedField("Control cannot be set without keystrokes") from None
        if field.kind == "choice":
            if not field.options or value not in field.options:
                raise UnsupportedField("Choices unavailable")
            matches = [c for c in ctrl.descendants() if c.element_info.control_type == "ListItem"
                       and c.element_info.name == value]
            if len(matches) != 1:
                raise UnsupportedField("Ambiguous choice")
            matches[0].iface_selection_item.Select()
            return
        try:
            if ctrl.iface_value.CurrentIsReadOnly:
                raise UnsupportedField("Read only")
            ctrl.iface_value.SetValue(value)
        except UnsupportedField:
            raise
        except Exception:
            raise UnsupportedField("Control does not support direct value setting") from None

    def submit(self, snapshot: Snapshot):
        current = self._check(snapshot)
        if not snapshot.submit or current.errors:
            raise UnsupportedField("Submission unavailable or page has errors")
        # Invoke exactly the reviewed submit control, never a fuzzy label or keyboard Enter.
        self._buttons[snapshot.submit].iface_invoke.Invoke()


def public_host(url: str) -> str | None:
    """Only a hostname, never a path, query, credentials or fragment. No claim about authenticity."""
    try:
        parsed = urlsplit(url)
        return parsed.hostname if parsed.scheme in {"http", "https"} else None
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------------------------
# Whole-form scan and verified filling: the implementation behind bolos.forms.fields.scan/fill/choose/...
#
# Unlike WindowsFormBackend above, this reads current values (so a guided session can skip filled fields and
# verify every entry by reading it back), except for passwords and secrets (OTP, PIN, CVV, CAPTCHA), whose value
# is never read. It works on any window: a Chrome/Edge page (one Document, the URL is its UIA value) or a desktop
# dialog (the whole window). Chromium builds its accessibility tree when a UI Automation client first asks, so
# nobody needs to restart their browser with --force-renderer-accessibility; scan() waits up to ~3 s for that.
#
# One cross-process call fetches the whole tree (a CacheRequest over the subtree); the tree is converted into
# plain Node objects and `form_from_tree` turns those into a Form with no COM involved, which keeps the
# label/group/error logic unit-testable with fakes.
# ------------------------------------------------------------------------------------------------------------------

_CONTROL_TYPES = ("Button", "CheckBox", "ComboBox", "Document", "Edit", "Group", "Hyperlink", "Image", "List",
                  "ListItem", "RadioButton", "Spinner", "Text", "Window", "Pane", "Custom")
_FIELD_CTS = {"Edit", "ComboBox", "CheckBox"}
_MAX_NODES = 20000
_ACTIVATION_WAIT = 3.0  # seconds for Chromium to build its accessibility tree after the first UIA query
_VERIFY_WAIT = 1.0
# The first option of many dropdowns is a prompt, not a choice ("Select state", "-- choose --", "चुनें").
_PLACEHOLDER = re.compile(r"^\s*$|^\s*-|^\s*(select|choose|please|pick)\b|चुन|निवड|ಆಯ್ಕೆ|தேர்ந்தெடு|"
                          r"ఎంచుకో|തിരഞ്ഞെടു|নির্বাচন|বাছাই|પસંદ|ਚੁਣ", re.I)
_tls = threading.local()
_PAN_FORM_EDITS = {
    "f_name_end": "First Name",
    "m_name_end": "Middle Name",
    "l_name_end": "Last Name / Surname*",
    "date_of_birth_reg": "Date of Birth / Incorporation / Formation (DD/MM/YYYY)*",
    "email_id2": "Email ID*",
    "rvContactNo": "Mobile Number*",
}


def _pan_registration(url: str | None) -> bool:
    try:
        site = urlsplit(url or "")
        return (site.scheme == "https" and site.hostname == "onlineservices.proteantech.in"
                and site.path.casefold() == "/paam/enduserregistercontact.html")
    except ValueError:
        return False


@dataclass
class Node:
    """One UI Automation element, as plain data (see `_read`). `el` is the live element for mutations."""

    ct: str
    name: str = ""
    aid: str = ""
    role: str = ""  # ARIA role, e.g. "alert", "heading"
    aria: str = ""  # ARIA properties string, e.g. "invalid=true;required=true"
    desc: str = ""  # full description (aria-describedby text)
    help: str = ""  # help text (placeholder)
    value: str | None = None
    readonly: bool = False
    toggle: int | None = None
    selected: bool | None = None
    range_value: float | None = None
    range_max: float | None = None
    required: bool = False
    valid: bool | None = None
    password: bool = False
    offscreen: bool = False
    enabled: bool = True
    live: int = 0
    rect: tuple[int, int, int, int] | None = None
    runtime_id: tuple = ()
    children: list = _dc_field(default_factory=list)
    el: object = None


def _api():
    """(IUIAutomation, generated UIAutomationClient module) with COM initialised in this thread."""
    if not getattr(_tls, "com", False):
        import comtypes

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # this thread already has an apartment; UIA clients work in either kind
        _tls.com = True
    from pywinauto.uia_defines import IUIA

    uia = IUIA()
    return uia.iuia, uia.UIA_dll


def _props(m) -> dict:
    names = ("Name", "ControlType", "AutomationId", "AriaRole", "AriaProperties", "FullDescription", "HelpText",
             "ValueValue", "ValueIsReadOnly", "ToggleToggleState", "SelectionItemIsSelected", "RangeValueValue",
             "RangeValueMaximum", "IsRequiredForForm", "IsDataValidForForm", "IsPassword", "IsOffscreen",
             "IsEnabled", "LiveSetting", "BoundingRectangle", "RuntimeId")
    return {n: getattr(m, f"UIA_{n}PropertyId") for n in names}


def _cached(el, pid, kinds=(str, int, float, bool, tuple)):
    try:
        value = el.GetCachedPropertyValue(pid)
    except Exception:
        return None
    # Unsupported properties come back as a COM "not supported" sentinel object.
    return value if isinstance(value, kinds) else None


def _read(el, ia, m) -> Node:
    """Fetch the subtree under `el` in one cross-process call and convert it to Nodes."""
    props = _props(m)
    request = ia.CreateCacheRequest()
    for pid in props.values():
        request.AddProperty(pid)
    request.TreeScope = m.TreeScope_Subtree
    request.TreeFilter = ia.RawViewCondition  # the control view drops the <span id=...> groups of help texts
    root = el.BuildUpdatedCache(request)
    types = {getattr(m, f"UIA_{n}ControlTypeId"): n for n in _CONTROL_TYPES}
    budget = [_MAX_NODES]

    def text(e, key):
        return _cached(e, props[key], (str,)) or ""

    def convert(e) -> Node:
        budget[0] -= 1
        rect = _cached(e, props["BoundingRectangle"], (tuple,))
        if rect and len(rect) == 4:
            left, top, width, height = (int(v) for v in rect)
            rect = (left, top, left + width, top + height)
        else:
            rect = None
        toggle = _cached(e, props["ToggleToggleState"], (int,))
        node = Node(
            ct=types.get(_cached(e, props["ControlType"], (int,)), "Other"),
            name=text(e, "Name"), aid=text(e, "AutomationId"), role=text(e, "AriaRole"),
            aria=text(e, "AriaProperties"), desc=text(e, "FullDescription"), help=text(e, "HelpText"),
            value=_cached(e, props["ValueValue"], (str,)),
            readonly=bool(_cached(e, props["ValueIsReadOnly"], (bool, int))),
            toggle=None if toggle is None else int(toggle),
            selected=_cached(e, props["SelectionItemIsSelected"], (bool,)),
            range_value=_cached(e, props["RangeValueValue"], (float, int)),
            range_max=_cached(e, props["RangeValueMaximum"], (float, int)),
            required=bool(_cached(e, props["IsRequiredForForm"], (bool,))),
            valid=_cached(e, props["IsDataValidForForm"], (bool,)),
            password=bool(_cached(e, props["IsPassword"], (bool,))),
            offscreen=bool(_cached(e, props["IsOffscreen"], (bool,))),
            enabled=_cached(e, props["IsEnabled"], (bool,)) is not False,
            live=_cached(e, props["LiveSetting"], (int,)) or 0,
            rect=rect, runtime_id=_cached(e, props["RuntimeId"], (tuple,)) or (), el=e)
        try:
            kids = e.GetCachedChildren()
        except Exception:
            kids = None
        if kids:
            for i in range(kids.Length):
                if budget[0] <= 0:
                    break
                node.children.append(convert(kids.GetElement(i)))
        return node

    return convert(root)


def _clean(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").replace("*", " ")).strip().rstrip(":").strip()


def _texts(node: Node) -> str:
    """Visible text of a node: its name, else its descendants' names joined."""
    if node.name.strip():
        return node.name.strip()
    return " ".join(t for t in (_texts(c) for c in node.children) if t).strip()


def _descendants(node: Node):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _date_parts(node: Node) -> tuple | None:
    """((("day", el), ("month", el), ("year", el)), ISO value or "") for a Chromium date input, else None."""
    roles = {}
    for s in (n for n in _descendants(node) if n.ct == "Spinner"):
        top = s.range_max or 0
        role = "day" if top == 31 else "month" if top == 12 else "year" if top >= 1000 else None
        if role is None or role in roles:
            return None  # a time, week or month input: not a plain date
        roles[role] = s
    if set(roles) != {"day", "month", "year"}:
        return None
    parts = tuple((r, roles[r].el) for r in ("day", "month", "year"))
    nums = [int(roles[r].range_value or 0) for r in ("year", "month", "day")]
    return parts, ("%04d-%02d-%02d" % tuple(nums) if all(nums) else "")


def _invalid(node: Node) -> bool:
    return node.valid is False or "invalid=true" in node.aria.replace(" ", "").casefold()


def _error_text(node: Node, id_texts: dict) -> str | None:
    """The field's error message: its aria-describedby text without the help text that is always there."""
    if not _invalid(node) or not node.desc.strip():
        return None
    desc = node.desc.strip()
    errorish = [t for aid, t in id_texts.items() if t and t in desc and re.search(r"err|invalid|warn|alert", aid, re.I)]
    if errorish:
        return " ".join(dict.fromkeys(errorish))
    helps = [t for t in id_texts.values() if t and t in desc]
    if not helps:
        return desc
    for t in helps:
        desc = desc.replace(t, " ")
    return re.sub(r"\s+", " ", desc).strip() or None


def _key(node: Node) -> str:
    return ":".join(str(p) for p in (node.runtime_id or (id(node),)))


def form_from_tree(root: Node, hwnd: int | None = None, title: str | None = None,
                   url: str | None = None) -> Form:
    """Build a Form from a Node tree (pure: no UI Automation calls). Fields keep document (reading) order."""
    entries = []  # ("field", node, preceding text) or ("radio", group key), in reading order
    radio_groups: dict = {}
    buttons, button_elements, headings, alerts = [], {}, [], []
    id_texts: dict[str, str] = {}
    last_text = [""]
    last_star = [False]  # the last label text was marked required ("Mobile Number*"); _clean drops the star
    # A checkbox's words come after it ("[ ] I agree to ..."), unlike a box's label: the first text after an
    # unnamed checkbox labels it (seen on the PAN form: the consent box took the "Mobile Number" before it).
    following: dict[int, str] = {}
    waiting: list = []

    def group_key(ancestors, node):
        named = next((a for a in reversed(ancestors) if a.ct == "Group" and _clean(a.name)), None)
        holder = named or (ancestors[-1] if ancestors else node)
        return _key(holder), named

    def walk(node: Node, ancestors: list):
        for child in node.children:
            if child.aid and _texts(child):
                id_texts.setdefault(child.aid, _texts(child))
            if child.ct in _FIELD_CTS:
                entries.append(("field", child, last_text[0] + ("*" if last_star[0] else "")))
                waiting.clear()  # the next field ends the previous checkbox's sentence
                if child.ct == "CheckBox" and not _clean(child.name):
                    waiting.append(child)
            elif child.ct == "RadioButton":
                key, named = group_key(ancestors, child)
                if key not in radio_groups:
                    radio_groups[key] = {"named": named, "fallback": last_text[0], "radios": []}
                    entries.append(("radio", key))
                radio_groups[key]["radios"].append(child)
            elif child.ct == "Button":
                if child.value and child.value.strip():
                    entries.append(("field", child, last_text[0]))  # <input type=file>: its name ends in its value
                elif _clean(child.name):
                    buttons.append(_clean(child.name))
                    button_elements.setdefault(_clean(child.name), []).append(child.el)
            elif child.role == "alert" or (child.live == 2 and child.ct in {"Text", "Group", "Pane", "Custom"}):
                if _texts(child):
                    alerts.append(_texts(child))
            elif child.role == "heading":
                if _texts(child):
                    headings.append(_texts(child))
            else:
                if child.ct == "Text" and child.name.strip() == "*":
                    last_star[0] = True  # "Mobile Number" <span>*</span>: the star is its own text
                if child.ct == "Text" and _clean(child.name):
                    text = _clean(child.name)
                    # "Select PAN Application Type*" then "(New or Change Request)": one label, not the note.
                    if text.startswith("(") and last_text[0] and not last_text[0].endswith(")"):
                        last_text[0] = f"{last_text[0]} {text}"
                    else:
                        last_text[0], last_star[0] = text, "*" in child.name
                    # The sentence can be split by links ("... web site <link> you give your consent ..."):
                    # collect it up to the next field.
                    for box in waiting:
                        following[id(box)] = f"{following.get(id(box), '')} {text}".strip()
                walk(child, ancestors + [child])

    walk(root, [])
    # Protean changes these visible labels after choosing the application type and category, and some
    # of its help text appears between the label and input. Keep the known controls' labels stable.
    pan_edits = _PAN_FORM_EDITS if _pan_registration(url or root.value) else {}
    fields = [_radio_field(radio_groups[e[1]], id_texts, hwnd) if e[0] == "radio"
              else _plain_field(e[1], following.get(id(e[1]), e[2]) if e[1].ct == "CheckBox" else e[2],
                                id_texts, hwnd, pan_edits.get(e[1].aid) if e[1].ct == "Edit" else None)
              for e in entries]
    errors = list(dict.fromkeys(alerts)) + [f"{f.label}: {f.error}" for f in fields if f.error]
    return Form(title=title if title is not None else root.name, url=url, fields=fields, buttons=buttons,
                errors=errors, hwnd=hwnd, headings=headings, button_elements=button_elements)


def _radio_field(group: dict, id_texts: dict, hwnd) -> Field:
    radios, named = group["radios"], group["named"]
    label = _clean(named.name) if named else group["fallback"]
    names = tuple(_clean(r.name) for r in radios)
    kind = field_kind(label, "RadioButton")
    # A group is only automated when its question (legend) and every option are labelled.
    protected = kind == "secret" or not label or not all(names) or not all(r.enabled for r in radios)
    return Field(
        key=_key(radios[0]), label=label or "Unlabelled choice", kind="manual" if protected and kind != "secret"
        else kind, required=any(r.required for r in radios) or "*" in (named.name if named else ""),
        options=names, protected=protected, control_type="RadioButton",
        value=next((_clean(r.name) for r in radios if r.selected), ""),
        error=next((e for e in (_error_text(r, id_texts) for r in radios) if e), None), rect=radios[0].rect,
        element=named.el if named else radios[0].el, hwnd=hwnd,
        parts=tuple(zip(names, (r.el for r in radios), strict=True)))


_LABEL_WORDS = 16  # a checkbox's sentence is spoken as its label: its first words only


def _plain_field(node: Node, fallback: str, id_texts: dict, hwnd, pan_label: str | None = None) -> Field:
    name = _clean(pan_label or node.name)
    value = node.value
    # An unlabelled dropdown reports its selected option as its name ("----Please Select------", later
    # "INDIVIDUAL"): that is not its label, and it would change as soon as a choice is made.
    if node.ct == "ComboBox":
        name = _choice_text(name)
        value = _choice_text(value or "")
        # Select2 may expose its displayed choice as a read-only textbox without a Value pattern.
        if name and not value and any(n.role == "textbox" and name == _choice_text(n.name)
                                      for n in _descendants(node)):
            value = name
        if name and (_PLACEHOLDER.search(name) or name == value):
            name = ""
        if _PLACEHOLDER.search(value):
            value = ""
    # The required star in the label before the field ("Last Name / Surname*"), when that label is this field's.
    required_mark = "*" in (fallback or "") and (not name or _clean(fallback) == name)
    fallback = " ".join((fallback or "").replace("*", " ").split())
    if node.ct == "CheckBox" and not name:
        fallback = " ".join(re.sub(r"https?://\S+", " ", fallback).split())  # a label is spoken: no web addresses
    sentence = ""
    if node.ct == "CheckBox" and not name and len(fallback.split()) > _LABEL_WORDS:
        sentence, fallback = fallback, " ".join(fallback.split()[:_LABEL_WORDS]) + "..."
    label = name or _clean(node.help) or fallback
    identifier = re.sub(r"[_-]+", " ", node.aid or "")
    control, parts = node.ct, ()
    if node.ct == "Button":  # file input: "Upload Aadhaar (front side): No file chosen"
        control, kind, value = "File", "file", None
        label = _clean(node.name[: len(node.name) - len(node.value or "")].rstrip(": ")) or fallback
    else:
        date = _date_parts(node) if node.ct == "Edit" else None
        if date:
            control, (parts, value) = "Date", date
        kind = field_kind(f"{label} {identifier}", node.ct, node.password)
        if control == "Date" and kind != "secret":
            kind = "date"
        elif node.ct == "Edit" and any(n.ct == "Spinner" for n in _descendants(node)):
            kind = "manual"  # time, week or month inputs: no verified way to enter them yet
    if node.ct == "CheckBox":
        value = {0: "false", 1: "true"}.get(node.toggle)
    secret = kind == "secret" or node.password
    protected = secret or kind in {"file", "manual"} or not label or not node.enabled or (
        node.readonly and node.ct == "Edit")
    return Field(
        key=_key(node), label=label or "Unlabelled field", kind=kind if label else "manual",
        required=node.required or "*" in (pan_label or node.name) or required_mark, protected=protected,
        help=sentence or (node.help if node.help and _clean(node.help) != label else ""), control_type=control,
        value=None if secret else value, error=_error_text(node, id_texts), is_password=node.password,
        rect=node.rect, element=node.el, parts=parts, hwnd=hwnd)


def _top(hwnd: int | None) -> int:
    import win32gui

    hwnd = hwnd or win32gui.GetForegroundWindow()
    if not hwnd:
        raise UnsupportedField("No foreground window")
    return win32gui.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT


def _url_like(value) -> bool:
    return isinstance(value, str) and re.match(r"^(https?|file|ftp|data|chrome|edge|about):", value, re.I) is not None


def _document(ia, m, window):
    """The window's web page Document (the largest visible one), or None."""
    condition = ia.CreatePropertyCondition(m.UIA_ControlTypePropertyId, m.UIA_DocumentControlTypeId)
    found = window.FindAll(m.TreeScope_Descendants, condition)
    best, area = None, -1
    for i in range(found.Length if found else 0):
        doc = found.GetElement(i)
        try:
            if doc.CurrentIsOffscreen:
                continue
            value = doc.GetCurrentPropertyValue(m.UIA_ValueValuePropertyId)
            if doc.CurrentAutomationId != "RootWebArea" and not _url_like(value):
                continue
            r = doc.CurrentBoundingRectangle
            size = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        except Exception:
            continue
        if size > area:
            best, area = doc, size
    return best


def _is_chromium(hwnd: int) -> bool:
    import win32gui

    return win32gui.GetClassName(hwnd).startswith("Chrome_WidgetWin")


def scan(hwnd: int | None = None, options: bool = True, *, option_key: str | None = None) -> Form:
    """The form in window `hwnd` (default: foreground). With `options`, dropdowns whose choices are not in the
    tree (Chromium's <select>) are briefly opened to list them, then closed; their selection is not changed."""
    import win32gui

    ia, m = _api()
    hwnd = _top(hwnd)
    window = ia.ElementFromHandle(hwnd)
    doc = _document(ia, m, window)
    deadline = time.monotonic() + _ACTIVATION_WAIT
    while doc is None and _is_chromium(hwnd) and time.monotonic() < deadline:
        time.sleep(0.1)
        doc = _document(ia, m, window)
    root = _read(doc if doc is not None else window, ia, m)
    while doc is not None and not root.children and time.monotonic() < deadline + _ACTIVATION_WAIT:
        time.sleep(0.1)  # the page is still loading or its tree is still being built
        root = _read(doc, ia, m)
    url = root.value if doc is not None and _url_like(root.value) else None
    title = root.name if doc is not None else win32gui.GetWindowText(hwnd)
    form = form_from_tree(root, hwnd=hwnd, title=title, url=url)
    if options:
        page_items = {n.runtime_id for n in _descendants(root) if n.ct == "ListItem" or n.role == "option"}
        for i, f in enumerate(form.fields):
            if f.control_type == "ComboBox" and not f.protected and (option_key is None or f.key == option_key):
                form.fields[i] = _with_options(f, _combo_items(f.element, window, page_items, ia, m))
    return form


def _choice_text(value: str) -> str:
    # Icon fonts used in custom dropdowns add a Private Use character to accessible names and values.
    return _clean("".join(c for c in value if unicodedata.category(c) != "Co"))


def _with_options(field: Field, items: list) -> Field:
    names = [n for n, _ in items]
    value = field.value or ""
    if names and _PLACEHOLDER.search(names[0]):
        if value == names[0]:
            value = ""
        names = names[1:]
    return replace(field, options=tuple(dict.fromkeys(n for n in names if n)), value=value)


def _pattern(el, m, pattern: str):
    try:
        got = el.GetCurrentPattern(getattr(m, f"UIA_{pattern}PatternId"))
        return got.QueryInterface(getattr(m, f"IUIAutomation{pattern}Pattern")) if got else None
    except Exception:
        return None


def _list_items(root, page_items, ia, m, skip=frozenset()) -> list:
    """[(text, element)] of the ListItems under `root`, minus the page's own <li> items and `skip` ids."""
    condition = ia.CreateOrCondition(
        ia.CreatePropertyCondition(m.UIA_ControlTypePropertyId, m.UIA_ListItemControlTypeId),
        ia.CreatePropertyCondition(m.UIA_AriaRolePropertyId, "option"),
    )
    found = root.FindAll(m.TreeScope_Descendants, condition)
    items = []
    for i in range(found.Length if found else 0):
        item = found.GetElement(i)
        try:
            rid = tuple(item.GetRuntimeId())
            if rid in page_items or rid in skip:
                continue
            name = _choice_text(item.CurrentName)
            if name and item.CurrentIsEnabled:
                items.append((name, item))
        except Exception:
            continue
    return items


def _combo_items(el, window, page_items, ia, m, keep_open=False) -> list:
    """[(option text, element)] of a dropdown. Desktop combo boxes list their items as children; Chromium shows
    a <select>'s options only in a popup while it is open, so it is opened (and closed again unless keep_open).
    Items of a previous dropdown's popup can linger for a moment, so only items that appear after opening count,
    unless the old ones include the current selection (the same dropdown opened again)."""
    items = _list_items(el, page_items, ia, m)
    if items:
        return items
    ec = _pattern(el, m, "ExpandCollapse")
    if ec is None:
        return []
    stale = _list_items(window, page_items, ia, m)
    stale_ids = frozenset(tuple(item.GetRuntimeId()) for _, item in stale)
    try:
        ec.Expand()
        deadline = time.monotonic() + _VERIFY_WAIT
        previous = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            items = _list_items(window, page_items, ia, m, stale_ids)
            if items and previous is not None and len(items) == len(previous):
                break  # the popup is complete when two reads agree
            previous = items
        if not items:
            current = _pattern(el, m, "Value")
            current = _choice_text(current.CurrentValue) if current is not None else None
            items = stale if current and any(name == current for name, _ in stale) else []
    except Exception:
        return []
    finally:
        if not keep_open:
            _collapse(ec)
    return items


def _collapse(ec):
    try:
        if ec.CurrentExpandCollapseState != 0:
            ec.Collapse()
    except Exception:
        pass  # the popup already closed itself


def _wait(check, timeout: float = _VERIFY_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if check():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _same(a: str | None, b: str) -> bool:
    return (a or "").replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


def _fillable(field: Field) -> bool:
    return field.element is not None and not field.protected and not field.is_password and \
        field.kind not in {"secret", "file", "manual"}


def focus(field: Field) -> tuple[int, int, int, int] | None:
    """Scroll the field into view and give it the keyboard focus (a radio group: its first choice), so the
    caret blinks where the answer will go. Returns the field's rectangle (physical pixels) after scrolling."""
    el = field.parts[0][1] if field.control_type == "RadioButton" and field.parts else field.element
    if el is None:
        return field.rect
    _, m = _api()
    scroll = _pattern(el, m, "ScrollItem")
    try:
        if scroll is not None:
            scroll.ScrollIntoView()
    except Exception:
        pass
    try:
        if field.control_type not in {"ComboBox", "File"}:  # focusing these can open a list or a file dialog
            el.SetFocus()
    except Exception:
        pass
    target = field.element if field.element is not None else el
    try:
        r = target.CurrentBoundingRectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return field.rect
    return rect if rect[2] > rect[0] and rect[3] > rect[1] else field.rect


def fill(field: Field, value: str) -> bool:
    if not _fillable(field):
        return False
    if field.control_type in {"ComboBox", "RadioButton"}:
        return choose(field, value)
    if field.control_type == "CheckBox":
        return check_box(field, value.strip().casefold() in {"true", "yes", "1", "on", "checked"})
    if field.control_type == "Date":
        return _fill_date(field, value)
    _, m = _api()
    el = field.element
    try:
        if el.CurrentIsPassword or not el.CurrentIsEnabled:
            return False  # the page changed the control since the scan
    except Exception:
        return False
    vp = _pattern(el, m, "Value")
    if vp is None:
        return False
    try:
        if vp.CurrentIsReadOnly:
            return False
        vp.SetValue(value)
    except Exception:
        return False
    return _wait(lambda: _same(vp.CurrentValue, value))


def _parse_date(value: str) -> tuple[int, int, int] | None:
    from datetime import date

    text = value.strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        y, mo, d = (int(g) for g in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[/.\- ](\d{1,2})[/.\- ](\d{4})", text)
        if not match:
            return None
        d, mo, y = (int(g) for g in match.groups())
    try:
        date(y, mo, d)
    except ValueError:
        return None
    return y, mo, d


def _fill_date(field: Field, value: str) -> bool:
    """Chromium's date input ignores UIA SetValue (on the input and on its day/month/year spin buttons), so each
    segment is focused through UIA and its digits are posted as key messages to the form's own window. Posted
    messages reach only that window: unlike SendInput they cannot land in another app if the user switches
    windows meanwhile, and they leave no key held down. Each segment is verified before the next one."""
    import win32api
    import win32con

    parsed = _parse_date(value)
    parts = dict(field.parts)
    if parsed is None or set(parts) != {"day", "month", "year"} or not field.hwnd:
        return False
    _, m = _api()
    y, mo, d = parsed
    for role, number, digits in (("day", d, "%02d" % d), ("month", mo, "%02d" % mo), ("year", y, "%04d" % y)):
        el = parts[role]
        spin = _pattern(el, m, "RangeValue")
        if spin is None:
            return False
        try:
            el.SetFocus()
            if not _wait(lambda el=el: el.CurrentHasKeyboardFocus, 0.5):
                return False
            for ch in digits:
                win32api.PostMessage(field.hwnd, win32con.WM_KEYDOWN, ord(ch), 1)
                win32api.PostMessage(field.hwnd, win32con.WM_KEYUP, ord(ch), 1 | 3 << 30)
                time.sleep(0.03)
        except Exception:
            return False
        if not _wait(lambda spin=spin, number=number: int(spin.CurrentValue) == number, 1.5):
            return False
    return True


def _norm_option(s: str) -> str:
    text = unicodedata.normalize("NFC", s).casefold()
    return " ".join("".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text).split())


def match_option(options, wanted: str) -> str | None:
    """The option meant by `wanted`: exact (ignoring case, spacing, punctuation), else a unique prefix or
    substring match of at least 3 letters, else a unique near-identical spelling. Never a guess between two."""
    key = _norm_option(wanted)
    if not key:
        return None
    if wanted in options:
        return wanted
    exact = [o for o in options if _norm_option(o) == key]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if len(key) >= 3:
        partial = [o for o in options if _norm_option(o).startswith(key)] or \
                  [o for o in options if key in _norm_option(o)]
        if len(partial) == 1:
            return partial[0]
    close = difflib.get_close_matches(key, [_norm_option(o) for o in options], n=2, cutoff=0.88)
    if len(close) == 1:
        return next(o for o in options if _norm_option(o) == close[0])
    return None


def choose(field: Field, option: str) -> bool:
    if not _fillable(field) or field.control_type not in {"ComboBox", "RadioButton"}:
        return False
    ia, m = _api()
    if field.control_type == "RadioButton":
        target = match_option([n for n, _ in field.parts], option)
        sel = _pattern(dict(field.parts)[target], m, "SelectionItem") if target else None
        if sel is None:
            return False
        try:
            sel.Select()
        except Exception:
            return False
        return _wait(lambda: bool(sel.CurrentIsSelected))
    target = match_option(field.options, option) if field.options else None
    if field.options and target is None:
        return False
    window = ia.ElementFromHandle(field.hwnd) if field.hwnd else ia.GetRootElement()
    ec = _pattern(field.element, m, "ExpandCollapse")
    try:
        items = _combo_items(field.element, window, set(), ia, m, keep_open=True)
        if not field.options:
            target = match_option([name for name, _ in items], option)
        matches = [el for name, el in items if name == target]
        if len(matches) != 1:
            return False
        option_el, = matches
        value = _pattern(field.element, m, "Value")

        def committed() -> bool:
            current = value.CurrentValue if value is not None else None
            return _choice_text(current or field.element.CurrentName or "") == target

        # Native dropdowns implement SelectionItem. Web dropdowns can expose an invokable option or
        # a legacy default action instead. Check the combobox itself after each action to verify it committed.
        for pattern, action in (("SelectionItem", "Select"), ("Invoke", "Invoke"),
                                ("LegacyIAccessible", "DoDefaultAction")):
            interface = _pattern(option_el, m, pattern)
            if interface is None:
                continue
            try:
                getattr(interface, action)()
            except Exception:
                continue
            if _wait(committed, 0.3):
                return True
    except Exception:
        return False
    finally:
        if ec is not None:
            _collapse(ec)
    # Some native dropdowns reflect the new value only after the popup closes.
    return _wait(committed)


def check_box(field: Field, on: bool) -> bool:
    if not _fillable(field) or field.control_type != "CheckBox":
        return False
    _, m = _api()
    tp = _pattern(field.element, m, "Toggle")
    if tp is None:
        return False
    want = 1 if on else 0
    try:
        for _ in range(3):  # a tri-state box may need two toggles
            if tp.CurrentToggleState == want:
                return True
            tp.Toggle()
            if _wait(lambda: tp.CurrentToggleState == want, 0.5):
                return True
    except Exception:
        return False
    return False


def press(form: Form, button_name: str) -> bool:
    """Invoke the one button whose label equals `button_name` (ignoring case and punctuation)."""
    key = _norm_option(button_name)
    matches = [el for name, els in form.button_elements.items() if _norm_option(name) == key for el in els]
    if len(matches) != 1:
        return False  # missing or ambiguous: never guess which button submits or pays
    _, m = _api()
    invoke = _pattern(matches[0], m, "Invoke")
    if invoke is None:
        return False
    try:
        invoke.Invoke()
    except Exception:
        return False
    return True


def errors(hwnd: int | None = None) -> list[str]:
    return scan(hwnd, options=False).errors


def browser_url(hwnd: int | None = None) -> str | None:
    """The page URL of a Chrome/Edge window: the Document's UIA value (full, with scheme), else the address bar
    text (Chromium hides "https://" there, so the scheme can be missing). None for other windows."""
    ia, m = _api()
    hwnd = _top(hwnd)
    if not _is_chromium(hwnd):
        return None
    window = ia.ElementFromHandle(hwnd)
    deadline = time.monotonic() + _ACTIVATION_WAIT
    while True:
        doc = _document(ia, m, window)
        value = doc.GetCurrentPropertyValue(m.UIA_ValueValuePropertyId) if doc is not None else None
        if _url_like(value):
            return value
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    condition = ia.CreateAndConditionFromArray([
        ia.CreatePropertyCondition(m.UIA_ControlTypePropertyId, m.UIA_EditControlTypeId),
        ia.CreatePropertyCondition(m.UIA_NamePropertyId, "Address and search bar")])
    try:
        bar = window.FindFirst(m.TreeScope_Descendants, condition)
        value = bar.GetCurrentPropertyValue(m.UIA_ValueValuePropertyId) if bar else None
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None
