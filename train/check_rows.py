"""Validate labelled rows against train/SLOTS.md.

    python train/check_rows.py train/data/slots/*.jsonl train/data/r3/*.jsonl

Prints every bad row with the reason and exits non-zero if any. A row is good when its action is known,
its slots are the ones that action allows, required slots are present, and every span slot is an exact,
contiguous piece of the text. Round 3 rows may also carry:
- "lang": one of the 10 language codes, or "none" for a language-neutral command ("Spotify", "next");
- for "action": "multi" (a compound command), "slots": {} and "steps": two or more steps, each
  {"action", "slots", "control"?} checked like a row of its own against the same text, plus "joiners":
  the conjunction words or suffixes used ("aur", "ke", "mattu"), each a piece of the text.
"""

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bolos.actions import ACTION_NAMES, REQUIRED_FIELDS, SLOT_FIELDS  # noqa: E402

# The slotless form actions the round 3 data teaches ("is form ko samjhao", "form bharne mein madad karo", "yeh
# site asli hai kya"). bolos.actions lists them; they are named here too so the data stays usable with an
# actions.py from before them. ACTIONS keeps bolos.actions' order, which the trained models' class lists follow.
FORM_ACTIONS = ("explain_form", "fill_form", "check_site")
ACTIONS = [*ACTION_NAMES, *(a for a in FORM_ACTIONS if a not in ACTION_NAMES)]
# Per action: (slots it may have, slots it must have), straight from the schema the executor accepts.
SPAN_SLOTS = {a: (set(SLOT_FIELDS.get(a, ())), set(REQUIRED_FIELDS.get(a, ()))) for a in ACTIONS}
CONTROLS = {"pause", "resume", "next", "previous", "volume_up", "volume_down", "mute"}
LANGS = {"hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa", "none"}
MULTI = "multi"
MAX_STEPS = 3  # matches bolos.compound.MAX_PARTS and bolos.llm.MAX_STEPS
STEP_ACTIONS = set(SPAN_SLOTS) - {"unknown", "cancel"}  # a step must do something


def _single_problem(text: str, action, slots, row: dict) -> str | None:
    """A single action's slots and control, checked against the command's text."""
    if action not in SPAN_SLOTS:
        return f"unknown action {action!r}"
    if not isinstance(slots, dict):
        return "slots is not an object"
    allowed, required = SPAN_SLOTS[action]
    if action == "media_control":
        if row.get("control") not in CONTROLS:
            return f"media_control needs control in {sorted(CONTROLS)}"
    elif "control" in row:
        return "control is only for media_control"
    if extra := set(slots) - allowed:
        return f"{action} does not take {sorted(extra)}"
    if (missing := required - set(slots)) and not row.get("incomplete"):
        return f"{action} needs {sorted(missing)}"
    for name, value in slots.items():
        if not isinstance(value, str) or not value.strip():
            return f"{name} is empty (leave it out instead)"
        if value != value.strip():
            return f"{name} has surrounding spaces"
        if value not in text:
            return f"{name}={value!r} is not an exact piece of the text"
    spans = sorted((text.index(v), text.index(v) + len(v), k) for k, v in slots.items())
    for (_, e1, k1), (s2, _, k2) in zip(spans, spans[1:], strict=False):
        if s2 < e1:
            return f"{k1} and {k2} overlap"
    return None


def _multi_problem(text: str, row: dict) -> str | None:
    if row.get("slots", {}) != {}:
        return "multi rows have no slots of their own (they go in the steps)"
    if "control" in row:
        return "control is only for media_control"
    steps = row.get("steps")
    if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
        return f"multi needs steps: a list of 2-{MAX_STEPS} steps"
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return f"step {i} is not an object"
        if step.get("action") not in STEP_ACTIONS:
            return f"step {i}: action {step.get('action')!r} can't be a step"
        if reason := _single_problem(text, step["action"], step.get("slots", {}), step):
            return f"step {i}: {reason}"
    joiners = row.get("joiners")
    if joiners is not None:
        if not isinstance(joiners, list) or not joiners:
            return "joiners must be a non-empty list"
        for j in joiners:
            if not isinstance(j, str) or not j.strip():
                return "a joiner is empty"
            if j.strip().casefold() not in text.casefold():
                return f"joiner {j!r} is not in the text"
    return None


def problem(row) -> str | None:
    if not isinstance(row, dict):
        return "not an object"
    text, action = row.get("text"), row.get("action")
    if not isinstance(text, str) or not text.strip():
        return "empty text"
    if "lang" in row and row["lang"] not in LANGS:
        return f"lang {row['lang']!r} is not one of {sorted(LANGS)}"
    if action == MULTI:
        return _multi_problem(text, row)
    if "steps" in row or "joiners" in row:
        return "steps and joiners are only for multi rows"
    return _single_problem(text, action, row.get("slots", {}), row)


def check(paths: list[Path]) -> int:
    bad = total = 0
    for path in paths:
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                reason = f"invalid JSON: {e}"
            else:
                reason = problem(row)
            if reason:
                bad += 1
                print(f"{path.name}:{n}: {reason}: {line[:160]}")
    print(f"{total - bad}/{total} rows OK")
    return bad


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    files = [Path(p) for arg in sys.argv[1:] for p in sorted(glob.glob(arg))] or sys.exit(__doc__)
    sys.exit(1 if check(files) else 0)
