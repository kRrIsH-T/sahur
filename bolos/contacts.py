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
        self._by_alias: dict[str, set[Contact]] = {}
        for contact in contacts:
            for name in (contact.name, *contact.aliases):
                self._by_alias.setdefault(_norm(name), set()).add(contact)

    @classmethod
    def load(cls, path: Path) -> "ContactBook":
        if not path.exists():
            return cls([])
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return cls([])
        rows = raw.get("contacts") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return cls([])
        contacts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name, phone, aliases = row.get("name"), row.get("phone"), row.get("aliases")
            if not isinstance(name, str) or not name.strip() or not isinstance(phone, str):
                continue
            if not any(char.isdecimal() for char in phone):
                continue
            aliases = aliases if isinstance(aliases, list) else []
            contacts.append(Contact(name.strip(), phone, tuple(a.strip() for a in aliases if isinstance(a, str)
                                                               and a.strip())))
        return cls(contacts)

    def names(self) -> list[str]:
        """Every spoken form, used as recognition hints for the speech model."""
        return list(dict.fromkeys(n for c in self.contacts for n in (c.name, *c.aliases)))

    def find(self, spoken: str) -> Contact | None:
        key = _norm(spoken)
        matches = self._by_alias.get(key)
        if matches is None:
            # A fuzzy match is usable only when the highest-scoring names all belong to one contact.
            best = 0.75
            matches = set()
            matcher = difflib.SequenceMatcher(b=key)
            for alias, contacts in self._by_alias.items():
                matcher.set_seq1(alias)
                score = matcher.ratio()
                if score > best:
                    matches = set(contacts)
                    best = score
                elif score == best:
                    matches.update(contacts)
        if len(matches) != 1:
            return None
        contact = next(iter(matches))
        # The executor looks up the name that Sahur reads back, so that name must identify this same person.
        return contact if self._by_alias[_norm(contact.name)] == {contact} else None
