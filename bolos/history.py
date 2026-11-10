"""A local log of every command Bol OS handles, so real use can be measured and turned into test and training
data (eval/history_to_set.py).

history.jsonl sits next to preferences.json and never leaves the PC. One JSON object per line, written with a
single write and flushed, so a crash loses at most the line being written and never damages earlier ones.
Lines are only appended, with one exception for privacy (below).

    {"id", "time", "transcript", "language", "source", "action", "steps", "outcome", "timings", "trigger",
     "corrected": false, "redacted"?: true, ...extra}        one command
    {"event": "corrected", "id", "time", "corrected_to": [steps]}   the user said what it should have done
    {"event": "confirmed", "id", "time"}                           the user said it was right

`action` is the action name ("multi" for several steps), `steps` the actions as dicts, `timings` milliseconds
per stage. `entries()` folds the events into the commands they refer to.

Messages and dictated text are personal: unless BOLOS_LOG_MESSAGES=1, the text of a send_message or type_text
step is replaced with "[redacted]", in the steps and in the transcript (the whole transcript when the text
can't be found in it). A command later corrected to a message or typing had its words logged in the clear, so
that one line is rewritten with them redacted.

This module also turns a transcript and the action it meant into a training row (`training_row`), the format
of corrections.jsonl and train/data rows: slot values are copied from the transcript, so only fields whose
values the user actually said become slot labels.
"""

import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from bolos import actions as a

log = logging.getLogger(__name__)

REDACTED = "[redacted]"
PRIVATE_TEXT_ACTIONS = {"send_message", "type_text"}  # their "text" is the user's own words
LANGS = ("hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa")
# Training rows are never made from these: messages and typing are personal, and "unknown"/"cancel" do
# nothing, so a correction can't meaningfully point at them.
NO_TRAINING_ACTIONS = PRIVATE_TEXT_ACTIONS | a.FORM_ACTIONS | {"unknown", "cancel"}


def log_messages_from_env() -> bool:
    return os.environ.get("BOLOS_LOG_MESSAGES", "").strip().lower() in {"1", "true", "yes", "on"}


def history_path(preferences_path: Path | None) -> Path | None:
    """history.jsonl lives next to preferences.json."""
    return preferences_path.with_name("history.jsonl") if preferences_path else None


def action_dict(action) -> dict:
    """An Action (pydantic) or a dict, as a plain dict without empty fields."""
    if hasattr(action, "model_dump"):
        return action.model_dump(exclude_none=True)
    return {k: v for k, v in dict(action).items() if v is not None}


def as_steps(steps) -> list[dict]:
    """One action, a list of actions, or nothing, as a list of action dicts."""
    if steps is None:
        return []
    if isinstance(steps, (list, tuple)):
        return [action_dict(s) for s in steps if s is not None]
    return [action_dict(steps)]


def short_lang(lang: str | None) -> str | None:
    """"hi-IN", "hi-en" (Hinglish) and "hi" are all "hi"; "none" stays; anything else is unknown."""
    if not lang:
        return None
    code = lang.strip().lower().split("-")[0].split("_")[0]
    return code if code in LANGS or code == "none" else None


def _find(text: str, value: str) -> re.Match | None:
    return re.search(re.escape(value), text, re.IGNORECASE)


def span_slots(text: str, action: dict) -> dict[str, str]:
    """The action's slot fields whose values occur in the text (ignoring case), copied from the text exactly,
    so they are valid span labels (train/check_rows.py): non-empty, no surrounding spaces, not overlapping."""
    text = unicodedata.normalize("NFC", text)
    taken: list[tuple[int, int]] = []
    slots: dict[str, str] = {}
    for name in a.SLOT_FIELDS.get(action.get("action"), []):
        value = action.get(name)
        if not isinstance(value, str) or not (value := unicodedata.normalize("NFC", value).strip()):
            continue
        for m in re.finditer(re.escape(value), text, re.IGNORECASE):
            piece = text[m.start():m.end()]
            # check_rows finds a value at its first exact occurrence: only that one is a usable label.
            if text.index(piece) != m.start() or any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            slots[name] = piece
            break
    return slots


def training_row(text: str, action, lang: str | None = None, **extra) -> dict | None:
    """A labelled row for System 1 from a transcript and the action it meant, or None when it must not be
    trained on (messages, typing, unknown, cancel, empty text). A required field the transcript doesn't
    contain ("mera editor kholo" -> Notepad) leaves the row "incomplete": its action is still a label."""
    act = action_dict(action)
    name = act.get("action")
    if not text or not text.strip() or name not in a.ACTION_DESCRIPTIONS or name in NO_TRAINING_ACTIONS:
        return None
    text = unicodedata.normalize("NFC", text.strip())
    slots = span_slots(text, act)
    row: dict = {"text": text, "action": name, "slots": slots}
    if name == "media_control":
        if act.get("control") not in a.CONTROLS:
            return None
        row["control"] = act["control"]
    if a.REQUIRED_FIELDS.get(name, set()) - set(slots):
        row["incomplete"] = True
    if code := short_lang(lang):
        row["lang"] = code
    return row | extra


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _replace_all(value, secrets: list[str]):
    """Every string inside value with each secret replaced (case-insensitively) by REDACTED."""
    if isinstance(value, str):
        for s in secrets:
            value = re.sub(re.escape(s), REDACTED, value, flags=re.IGNORECASE)
        return value
    if isinstance(value, dict):
        return {k: _replace_all(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_all(v, secrets) for v in value]
    return value


def private_texts(steps: list[dict]) -> list[str]:
    texts = [s.get("text", "").strip() for s in steps if s.get("action") in PRIVATE_TEXT_ACTIONS]
    return sorted({t for t in texts if t and t != REDACTED}, key=len, reverse=True)  # longest first


# Fields that name things rather than hold the user's words: never rewritten (a dictated "2026" must not
# eat into an id or a time).
_STRUCTURAL = {"id", "time", "event", "action", "control", "source", "outcome", "language", "trigger", "timings",
               "corrected", "confirmed", "redacted"}


def _redact_step(step: dict, secrets: list[str]) -> dict:
    private = step.get("action") in PRIVATE_TEXT_ACTIONS
    return {k: REDACTED if k == "text" and private else v if k in _STRUCTURAL else _replace_all(v, secrets)
            for k, v in step.items()}


def redact_entry(entry: dict, secrets: list[str]) -> dict:
    """The entry with message and dictated text gone: the "text" of every send_message / type_text step, and
    each secret wherever else it appears. If a secret isn't found in the transcript as it is (System 2 tidied
    the words, say), the whole transcript goes: a near copy of a message is still the message."""
    out: dict = {}
    for k, v in entry.items():
        if k == "transcript" and isinstance(v, str) and secrets:
            out[k] = REDACTED if any(_find(v, s) is None for s in secrets) else _replace_all(v, secrets)
        elif k in ("steps", "corrected_to") and isinstance(v, list):
            out[k] = [_redact_step(s, secrets) if isinstance(s, dict) else s for s in v]
        elif k in _STRUCTURAL:
            out[k] = v
        else:
            out[k] = _replace_all(v, secrets)
    if secrets:
        out["redacted"] = True
    return out


class History:
    """Appends to history.jsonl. Never raises: a full disk or a locked file must not break a command."""

    def __init__(self, path: Path | None, log_messages: bool | None = None):
        self.path = path
        self.log_messages = log_messages_from_env() if log_messages is None else log_messages
        self.enabled = True  # off (from the dashboard): nothing more is written
        self._lock = threading.Lock()
        self._checked_tail = False

    def record(self, transcript: str, language: str | None = None, source: str | None = None, action=None,
               steps=None, outcome: str | None = None, timings: dict | None = None, trigger: str | None = None,
               **extra) -> str:
        """Log one command; returns its id (for mark_corrected / mark_confirmed). `action` is one Action (or
        dict), `steps` several; `timings` maps a stage to milliseconds. Extra keyword fields are kept as they
        are, except that message and dictated text is redacted from them too."""
        # Readable and sortable, with enough randomness (48 bits) that ids never collide, even at thousands
        # of commands a second (6 hex digits did, in a crash test).
        entry_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}"
        try:
            all_steps = as_steps(steps if steps is not None else action)
            entry = {"id": entry_id, "time": now_iso(), "transcript": transcript, "language": language,
                     "source": source, "action": _action_name(all_steps), "steps": all_steps, "outcome": outcome,
                     "timings": _timings(timings), "trigger": trigger,
                     "corrected": False, **extra}
            if not self.log_messages:
                entry = redact_entry(entry, private_texts(all_steps))
            self._append(entry)
        except Exception:
            log.warning("Could not log the command", exc_info=True)
        return entry_id

    def mark_corrected(self, entry_id: str, corrected) -> None:
        """The user said command `entry_id` should have done `corrected` (an action, dict or list of them)."""
        try:
            steps = as_steps(corrected)
            if not self.log_messages and (secrets := private_texts(steps)):
                self._scrub(entry_id, secrets)  # the first try logged these words in the clear
                steps = [_redact_step(s, secrets) for s in steps]
            self._append({"event": "corrected", "id": entry_id, "time": now_iso(), "corrected_to": steps})
        except Exception:
            log.warning("Could not log the correction", exc_info=True)

    def mark_confirmed(self, entry_id: str) -> None:
        """The user said command `entry_id` did the right thing."""
        try:
            self._append({"event": "confirmed", "id": entry_id, "time": now_iso()})
        except Exception:
            log.warning("Could not log the confirmation", exc_info=True)

    def entries(self) -> list[dict]:
        """Every logged command, oldest first, with its events folded in: "corrected" (bool) and
        "corrected_to" (steps), "confirmed" (bool)."""
        return read_entries(self.path)

    def archive(self) -> Path | None:
        with self._lock:
            path = archive_history(self.path)
            if path is not None:
                self._checked_tail = False
            return path

    # Writing

    def _append(self, record: dict) -> None:
        if not self.path or not self.enabled:
            return
        with self._lock:
            append_line(self.path, json.dumps(record, ensure_ascii=False), check_tail=not self._checked_tail)
            self._checked_tail = True

    def _scrub(self, entry_id: str, secrets: list[str]) -> None:
        """Rewrite the one line logged for entry_id with the secrets redacted (atomically: a crash leaves the
        old file or the new one)."""
        if not self.path or not self.path.exists():
            return
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            changed = False
            for i, line in enumerate(lines):
                record = _loads(line)
                if record and record.get("id") == entry_id and "event" not in record:
                    redacted = redact_entry(record, secrets)
                    lines[i] = json.dumps(redacted, ensure_ascii=False)
                    changed = True
            if changed:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
                tmp.replace(self.path)


def append_line(path: Path, line: str, check_tail: bool = True) -> None:
    """Append one JSONL line with a single write, flushed, so a crash loses at most this line. If the file
    ends in a line cut short by an earlier crash, a newline first, so this line starts cleanly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if check_tail:
        try:
            with path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                prefix = "" if f.read(1) == b"\n" else "\n"
        except OSError:  # missing or empty
            pass
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{prefix}{line}\n")
        f.flush()


def _timings(timings: dict | None) -> dict[str, float]:
    """Milliseconds per stage, to 0.1 ms. A stage that didn't run (None) or a bad value is left out rather
    than costing the whole line."""
    out = {}
    for stage, ms in (timings or {}).items():
        try:
            out[str(stage)] = round(float(ms), 1)
        except (TypeError, ValueError):
            continue
    return out


def _action_name(steps: list[dict]) -> str | None:
    if not steps:
        return None
    return steps[0].get("action") if len(steps) == 1 else "multi"


def _loads(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def archive_history(path: Path | None) -> Path | None:
    if path is None:
        return None
    target = path.with_name(f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}{path.suffix}")
    try:
        return path.rename(target)
    except FileNotFoundError:
        return None


def read_entries(path: Path | None) -> list[dict]:
    """The commands in a history file with their events folded in. Unreadable lines (a crash mid-write) are
    skipped."""
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except FileNotFoundError:  # the dashboard can archive the log between reads
        return []
    entries: dict[str, dict] = {}
    for line in lines:
        record = _loads(line)
        if not record or not isinstance(record.get("id"), str):
            continue
        event = record.get("event")
        if event is None:
            entries[record["id"]] = {"confirmed": False, "corrected_to": None, **record}
        elif (entry := entries.get(record["id"])) is not None:
            if event == "corrected":
                entry.update(corrected=True, corrected_to=record.get("corrected_to"), confirmed=False)
            elif event == "confirmed":
                entry.update(confirmed=True)
    return list(entries.values())
