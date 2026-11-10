"""System 1: understanding a command locally, in about a millisecond.

A Parse is the action, whether System 1 is sure of it, the fields it extracted, and which of those it is
unsure of. The pipeline acts on a sure parse with sure fields directly and asks System 2 (the LLM) only for
the rest. Laya is kept as a baseline: it only picks the action, so its fields always come from the LLM.

A Parse also says which language the command is in, when that is certain: native-script text by its
script (Devanagari is Hindi or Marathi, the model picks which), romanised text by System 1's calibrated
language head. Replies prefer this over the previous command's language and USER_LANGUAGE.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bolos.actions import ACTION_DESCRIPTIONS, REQUIRED_FIELDS, SLOT_FIELDS

if TYPE_CHECKING:
    from bolos.fast.model import Prediction

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Parse:
    action: str
    sure: bool  # of the action (and, for media_control, of which control)
    slots: dict[str, str] = field(default_factory=dict)
    unsure: frozenset[str] = frozenset()  # fields System 1 can't vouch for: present but doubtful, or missing
    control: str | None = None
    # The command's language (hi, en, kn, ta, te, mr, ml, bn, gu, pa), only when sure: by script, or by the
    # language head above its threshold. None for language-neutral commands ("Spotify", "next") and when unsure.
    language: str | None = None
    confidence: float | None = None  # System 1's calibrated confidence in the action; None when it can't say

    @property
    def complete(self) -> bool:
        return self.sure and not self.unsure


# Unicode blocks of the Indic scripts our languages are written in, and which languages each can be.
_SCRIPTS: list[tuple[int, int, tuple[str, ...]]] = [
    (0x0900, 0x097F, ("hi", "mr")),  # Devanagari
    (0x0980, 0x09FF, ("bn",)),  # Bengali
    (0x0A00, 0x0A7F, ("pa",)),  # Gurmukhi
    (0x0A80, 0x0AFF, ("gu",)),  # Gujarati
    (0x0B80, 0x0BFF, ("ta",)),  # Tamil
    (0x0C00, 0x0C7F, ("te",)),  # Telugu
    (0x0C80, 0x0CFF, ("kn",)),  # Kannada
    (0x0D00, 0x0D7F, ("ml",)),  # Malayalam
]


def script_languages(text: str) -> tuple[str, ...]:
    """The languages the text's main Indic script can be (most characters wins), or () for Latin-only text.

    A script is far stronger evidence than any model: "Notepad ತೆರೆ" is Kannada however few native words it has."""
    counts: dict[tuple[str, ...], int] = {}
    for ch in text:
        cp = ord(ch)
        for lo, hi, languages in _SCRIPTS:
            if lo <= cp <= hi:
                counts[languages] = counts.get(languages, 0) + 1
                break
    return max(counts, key=counts.__getitem__) if counts else ()


class FastParser:
    """The from-scratch System 1: action model + slot tagger (bolos/fast)."""

    def __init__(self, model_path: Path, slot_path: Path):
        from bolos.fast.model import FastModel
        from bolos.fast.slots import SlotModel

        self.model = FastModel(model_path)
        self.slots = SlotModel(slot_path)

    def parse(self, command: str) -> Parse:
        from bolos.fast.features import normalize

        if not normalize(command):  # nothing but punctuation or silence: certainly not a command
            return Parse("unknown", True)
        pred = self.model.predict(command)
        sure = self.model.accepts(pred)
        about = {"language": self._language(command, pred), "confidence": pred.action_confidence}
        if pred.action == "media_control":
            return Parse(pred.action, sure, control=pred.control, **about)
        if pred.action not in SLOT_FIELDS:  # includes "multi": the pipeline splits it and parses each part
            return Parse(pred.action, sure, **about)
        found = self.slots.extract(command, pred.action)
        slots = {name: s.value for name, s in found.slots.items()}
        unsure = {name for name, s in found.slots.items() if not self.slots.sure(name, s.confidence)}
        required = REQUIRED_FIELDS[pred.action]
        for name, confidence in found.absent.items():
            if name in required or not self.slots.sure(f"no_{name}", confidence):
                unsure.add(name)
        return Parse(pred.action, sure, slots, frozenset(unsure), **about)

    def detect_language(self, command: str) -> str | None:
        """Predict the language without deciding an action or extracting its slots."""
        return self._language(command, self.model.predict(command))

    def _language(self, command: str, pred: "Prediction") -> str | None:
        """Script first; within Devanagari (Hindi or Marathi) the model's likelier one; else the sure model guess."""
        if candidates := script_languages(command):
            if len(candidates) == 1 or pred.language_probs is None:
                return candidates[0]
            languages = self.model.languages
            known = [c for c in candidates if c in languages]
            if not known:
                return candidates[0]
            return max(known, key=lambda c: pred.language_probs[languages.index(c)])
        if self.model.accepts_language(pred) and pred.language != "none":
            return pred.language
        return None


QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "A person spoke this command to their Windows computer. What do they want it to do?",
        "criteria": ACTION_DESCRIPTIONS,
    },
}


class LayaParser:
    def __init__(self, model: str = "multilingual", threshold: float = 0.55, checkpoint: str | None = None):
        from laya import Router  # heavy import (torch), only when actually deciding

        self.router = Router(models={model: checkpoint}) if checkpoint else Router()
        self.model = model
        self.threshold = threshold

    def parse(self, command: str) -> Parse:
        answer = self.router.predict(command, QUESTIONS, model=self.model)["answers"]["action"]
        action = answer["choice"]
        # Laya's "unknown" only means it matched nothing: never final, System 2 gets a look.
        sure = answer["answer_confidence"] >= self.threshold and action != "unknown"
        return Parse(action, sure, unsure=frozenset(SLOT_FIELDS.get(action, [])))
