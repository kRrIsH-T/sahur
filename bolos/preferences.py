"""What Bol OS learns about this user, kept in one JSON file next to the app.

- Media services: which service the user names for music and for video. A request that names none
  ("Radiohead chalao") goes to the one they use most for that kind.
- App names: an app the user calls by a name that only loosely matches it ("visual studio" -> Visual Studio
  Code), once it opened, so next time it is found directly.
- Habits: a command only System 2 could work out (it chose the action), remembered with its action, so saying
  it again is instant. Messages and dictated text are never remembered.
- Corrections: "galat tha, Spotify pe chalao" teaches that the previous command meant the corrected action
  (a habit with source "user", even when System 1 made the first choice), and adds the pair to
  corrections.jsonl next to this file, which train/build_dataset.py adds to System 1's training data. What the
  user said beats what System 2 guessed: a later System 2 answer never replaces a "user" habit.

"Galat tha" / "that was wrong" right after a command undoes everything that command taught (a habit, a
name, a media count, a correction row), and forgets a habit that command replayed, even one learned in an
earlier session.
"""

import json
import logging
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path

from bolos import actions as a
from bolos.history import append_line, now_iso, training_row

log = logging.getLogger(__name__)

VIDEO_WORDS = re.compile(r"\b(video|videos|movie|movies|film|trailer|trailers|vlog|episode|clip|scene|"
                         r"cartoon|vidiyo|वीडियो|फिल्म|ವೀಡಿಯೊ|வீடியோ|వీడియో)\b", re.I)
DEFAULT_APPS = {"music": "Spotify", "video": "YouTube"}
MAX_HABITS = 500
# Personal or one-off: a message always goes through the full check, dictated text is never stored.
NEVER_HABITS = a.RISKY_ACTIONS | a.DIALOGUE_ACTIONS | {"type_text", "unknown", "cancel"}
# Punctuation, but not Indic vowel signs and viramas (which \w doesn't cover): "खोलो" and "खेलो" differ.
_PUNCT = re.compile("[^\\w\\sऀ-ॣ०-෿]")
_MISSING = object()
USER = "user"  # habit source: the user said it themselves, in a correction


def habit_key(command: str) -> str:
    """Spoken commands repeat with different casing and punctuation; compare without them."""
    return " ".join(_PUNCT.sub(" ", unicodedata.normalize("NFC", command).casefold()).split())


def service_key(app: str) -> str:
    """"Spotify", "spotify" and "Spoti fy" are one service for counting."""
    return re.sub(r"[^\wऀ-ॣ०-෿]", "", unicodedata.normalize("NFC", app).casefold())


def media_kind(command: str, query: str) -> str:
    return "video" if VIDEO_WORDS.search(command) or VIDEO_WORDS.search(query) else "music"


class Preferences:
    def __init__(self, path: Path | None):
        self.path = path
        self.corrections_path = path.with_name("corrections.jsonl") if path else None
        self._lock = threading.Lock()
        data = self._read()
        self.media: dict[str, Counter] = {}
        self.media_names: dict[str, str] = data.get("media_names", {})  # service key -> name as first said
        for kind, counts in data.get("media", {}).items():
            merged = self.media.setdefault(kind, Counter())
            for name, n in counts.items():  # older files counted raw names: merge spellings
                merged[service_key(name)] += n
                self.media_names.setdefault(service_key(name), name)
        self.app_names: dict[str, str] = data.get("app_names", {})
        self.habits: dict[str, dict] = data.get("habits", {})
        # What the current command changed: (table, key, value before), undone by forget_last.
        self._undo: list[tuple[str, str, object]] = []

    def _read(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            log.warning("Ignoring unreadable preferences file %s", self.path, exc_info=True)
            return {}

    def begin_command(self) -> None:
        """A new command starts: only what it changes can be undone next."""
        self._undo = []

    def has_undo(self) -> bool:
        return bool(self._undo)

    def _set(self, table: str, k: str, value) -> None:
        values = getattr(self, table)
        self._undo.append((table, k, values.get(k, _MISSING)))
        values[k] = value

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {"media": {k: dict(v) for k, v in self.media.items()}, "media_names": self.media_names,
                    "app_names": self.app_names, "habits": self.habits}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # Media services

    def media_app(self, command: str, query: str) -> str:
        """The service to use when the user named none."""
        kind = media_kind(command, query)
        counts = self.media.get(kind)
        if not counts:
            return DEFAULT_APPS[kind]
        top = counts.most_common(1)[0][0]
        return self.media_names.get(top, top)

    def note_media_app(self, command: str, query: str, app: str) -> None:
        kind, k = media_kind(command, query), service_key(app)
        if not k:
            return
        self._undo.append(("media", kind, k))
        self.media.setdefault(kind, Counter())[k] += 1
        self.media_names.setdefault(k, app)
        self.save()

    # App names

    def learn_app_name(self, spoken_key: str, app_name: str) -> None:
        if spoken_key and self.app_names.get(spoken_key) != app_name:
            self._set("app_names", spoken_key, app_name)
            self.save()

    # Habits

    def recall(self, command: str) -> a.Action | None:
        """The remembered action for this command. Replaying it makes it undoable ("galat tha" next forgets
        it) and keeps it from being evicted (least recently used goes first)."""
        k = habit_key(command)
        data = self.habits.get(k)
        if not data:
            return None
        try:
            action = a.parse_action({f: v for f, v in data.items() if f != "source"})
        except ValueError:
            return None
        self.habits[k] = self.habits.pop(k)  # most recently used last
        self._undo.append(("habits", k, _MISSING))
        return action

    def habit_source(self, command: str) -> str | None:
        """Who taught the habit for this command: "user" (a correction) or "system2". None if there is none."""
        data = self.habits.get(habit_key(command))
        return data.get("source", "system2") if data else None

    def learn_habit(self, command: str, action: a.Action, source: str = "system2") -> None:
        if action.action in NEVER_HABITS:
            return
        k = habit_key(command)
        current = self.habits.get(k)
        if source != USER and current and current.get("source") == USER:
            return  # the user's own correction wins over a model's answer
        before = self.habits.pop(k, _MISSING)  # re-inserted last: most recently used
        self._undo.append(("habits", k, before))
        self.habits[k] = {**action.model_dump(exclude_none=True), "source": source}
        self._evict()
        self.save()

    def _evict(self) -> None:
        """Least recently used first, but the user's corrections only once no model-made habit is left."""
        while len(self.habits) > MAX_HABITS:
            k = next((k for k, v in self.habits.items() if v.get("source") != USER), next(iter(self.habits)))
            self.habits.pop(k)

    # Corrections

    def learn_correction(self, command: str, action: a.Action, lang: str | None = None) -> None:
        """The previous command (its transcript `command`) should have done `action`: remember it as the
        user's habit and add a training row for System 1. Messages, typing, unknown and cancel teach nothing.
        Undone, like everything else, by a "galat tha" right after."""
        if action.action in NEVER_HABITS or not habit_key(command):
            return
        self.learn_habit(command, action, source=USER)
        row = training_row(command, action, lang, source="correction", time=now_iso())
        if not row or not self.corrections_path:
            return
        line = json.dumps(row, ensure_ascii=False)
        try:
            with self._lock:
                append_line(self.corrections_path, line)
        except OSError:  # the habit is learned; a training row lost to a locked file must not stop the command
            log.warning("Could not add the correction to %s", self.corrections_path, exc_info=True)
            return
        self._undo.append(("corrections", line, None))

    def _remove_correction(self, line: str) -> bool:
        """Take back the last copy of this row from corrections.jsonl (rewritten atomically)."""
        if not self.corrections_path or not self.corrections_path.exists():
            return False
        try:
            with self._lock:
                lines = self.corrections_path.read_text(encoding="utf-8").splitlines()
                for i in range(len(lines) - 1, -1, -1):
                    if lines[i] == line:
                        del lines[i]
                        tmp = self.corrections_path.with_suffix(".tmp")
                        tmp.write_text("".join(f"{x}\n" for x in lines), encoding="utf-8", newline="\n")
                        tmp.replace(self.corrections_path)
                        return True
        except OSError:
            log.warning("Could not take the correction back from %s", self.corrections_path, exc_info=True)
        return False

    def forget_last(self) -> bool:
        """Undo everything the last command taught. False if it taught nothing."""
        undo, self._undo = self._undo, []
        changed = False
        for table, k, before in reversed(undo):
            if table == "corrections":
                changed |= self._remove_correction(k)
                continue
            if table == "media":
                counts = self.media.get(k, Counter())
                if counts.get(before):
                    counts[before] -= 1
                    if counts[before] <= 0:
                        del counts[before]
                    changed = True
                continue
            values = getattr(self, table)
            if before is _MISSING:
                changed |= values.pop(k, None) is not None
            elif values.get(k) != before:
                values[k] = before
                changed = True
        if changed:
            self.save()
        return changed
