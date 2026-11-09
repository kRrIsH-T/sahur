"""The user's own details for forms, kept only on this PC and encrypted with Windows DPAPI (current user).

Values are saved only after the user agrees, can be deleted by voice, and never leave this module except into
the form field the user confirmed. They are never sent to System 2, never logged and never part of a repr.
"""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from bolos.forms.kinds import PROFILE_CONCEPTS

_ENTROPY = b"Bol OS form profile v1"  # binds the blob to this purpose; not a secret


def _dpapi_protect(data: bytes) -> bytes:
    import win32crypt

    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN: never show a prompt.
    return win32crypt.CryptProtectData(data, "Bol OS profile", _ENTROPY, None, None, 0x1)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0x1)[1]


def profile_path(preferences_path: str | os.PathLike) -> Path:
    """Next to preferences.json, like history.jsonl and corrections.jsonl."""
    return Path(preferences_path).with_name("form_profile.bin")


class ProfileError(RuntimeError):
    """The profile could not be read or written. Never carries values."""


class Profile:
    def __init__(self, path: str | os.PathLike, protect: Callable[[bytes], bytes] = _dpapi_protect,
                 unprotect: Callable[[bytes], bytes] = _dpapi_unprotect):
        self.path = Path(path)
        self._protect = protect
        self._unprotect = unprotect
        self._values: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"Profile({str(self.path)!r}, {len(self._load())} saved)"

    def _load(self) -> dict[str, str]:
        if self._values is None:
            if not self.path.exists():
                self._values = {}
            else:
                try:
                    data = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
                except Exception:
                    # Another Windows user's or a damaged file: unreadable is the same as empty, but never
                    # overwritten silently; forget() removes it.
                    raise ProfileError("The saved profile cannot be decrypted on this account") from None
                self._values = {k: v for k, v in data.items() if k in PROFILE_CONCEPTS and isinstance(v, str)}
        return self._values

    def _save(self):
        blob = self._protect(json.dumps(self._load(), ensure_ascii=False).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".profile-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def kinds(self) -> list[str]:
        """Which details are saved (names only), so a session can offer them."""
        try:
            return sorted(self._load())
        except ProfileError:
            return []

    def get(self, kind: str) -> str | None:
        try:
            return self._load().get(kind)
        except ProfileError:
            return None

    def set(self, kind: str, value: str):
        self.update({kind: value})

    def update(self, values: dict[str, str]):
        """Save several details at once (one encrypted write). Unknown kinds are ignored."""
        current = self._load()
        changed = False
        for kind, value in values.items():
            if kind in PROFILE_CONCEPTS and value and current.get(kind) != value:
                current[kind] = value
                changed = True
        if changed:
            self._save()

    def forget(self, kind: str | None = None):
        """Delete one detail, or everything (the file itself) when kind is None."""
        if kind is None:
            self._values = {}
            self.path.unlink(missing_ok=True)
            return
        try:
            current = self._load()
        except ProfileError:
            return
        if current.pop(kind, None) is not None:
            if current:
                self._save()
            else:
                self.path.unlink(missing_ok=True)
