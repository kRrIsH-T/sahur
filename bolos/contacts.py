"""Contact book: spoken names in any script map to a phone number."""

import difflib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Contact:
    name: str
    phone: str
    aliases: tuple[str, ...]


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold().strip()


class ContactBook:
    def __init__(self, contacts: list[Contact]):
        self.contacts = contacts
        self._by_alias = {_norm(alias): c for c in contacts for alias in (c.name, *c.aliases)}

    @classmethod
    def load(cls, path: Path) -> "ContactBook":
        if not path.exists():
            return cls([])
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([
            Contact(name=c["name"], phone=c["phone"], aliases=tuple(c.get("aliases", [])))
            for c in raw["contacts"]
        ])

    def names(self) -> list[str]:
        """Every spoken form, used as recognition hints for the speech model."""
        return list(dict.fromkeys(n for c in self.contacts for n in (c.name, *c.aliases)))

    def find(self, spoken: str) -> Contact | None:
        key = _norm(spoken)
        if key in self._by_alias:
            return self._by_alias[key]
        match = difflib.get_close_matches(key, list(self._by_alias), n=1, cutoff=0.75)
        return self._by_alias[match[0]] if match else None
