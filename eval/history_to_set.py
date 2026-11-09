"""Turn real commands from history.jsonl into a labelled set: the ones the user confirmed (Bol OS did the
right thing) or corrected (with what it should have done).

    uv run python eval/history_to_set.py --review                 # go through the log and label commands
    uv run python eval/history_to_set.py                          # -> eval/history_set.jsonl, for evaluate.py
    uv run python eval/history_to_set.py --format train           # -> train/data/history/history_train.jsonl

Corrections come from the app ("galat tha, Spotify pe chalao"). Confirmations come from --review, which shows
each command not labelled yet: y = it was right, n = it was wrong (then type what it should have done, like
`play_media query=Arijit app=Spotify` or `media_control control=pause`), Enter = skip, q = quit.

Formats (one JSON object per line):
- eval (default): the format of eval/commands_v5.jsonl, so `eval/evaluate.py --sets history_set.jsonl` scores
  it. "slots" holds the right value of every field the command needed, plus optional fields the user said
  (a service they didn't name was their usual one, not something to understand).
- train: the format of train/data rows (train/SLOTS.md): slot values are copied from the transcript, so only
  fields the user said become labels; messages, typing, unknown and cancel are left out.

Pick one use per command: train/build_dataset.py treats every eval/*.jsonl as test data and drops training
rows close to it, so a command in eval/history_set.jsonl is kept out of training (corrections.jsonl
included), which keeps the score fair. That is why the train rows are written outside eval/.

Commands whose words were redacted (messages and dictation, unless BOLOS_LOG_MESSAGES=1 was set when they
were logged) are never included. The same transcript labelled twice keeps its latest label.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bolos import actions as a  # noqa: E402
from bolos.history import (  # noqa: E402
    REDACTED,
    History,
    history_path,
    read_entries,
    short_lang,
    span_slots,
    training_row,
)
from bolos.preferences import habit_key  # noqa: E402


def gold_steps(entry: dict) -> list[dict] | None:
    """What the command should have done, if the user said so."""
    if entry.get("corrected"):
        return entry.get("corrected_to") or None
    if entry.get("confirmed"):
        return entry.get("steps") or None
    return None


def is_redacted(entry: dict, steps: list[dict]) -> bool:
    if entry.get("redacted") or REDACTED in (entry.get("transcript") or ""):
        return True
    return any(REDACTED in str(v) for s in steps for v in s.values())


def eval_slots(text: str, step: dict) -> dict[str, str]:
    """The fields a right answer must have: required ones always, optional ones when the user said them."""
    name = step.get("action")
    said = span_slots(text, step)
    required = a.REQUIRED_FIELDS.get(name, set())
    return {f: step[f] for f in a.SLOT_FIELDS.get(name, []) if step.get(f) and (f in required or f in said)}


def eval_row(entry: dict, steps: list[dict]) -> dict:
    text = entry["transcript"].strip()
    row = {"id": entry["id"], "lang": short_lang(entry.get("language")) or "unknown", "text": text}
    if len(steps) == 1:
        step = steps[0]
        row |= {"action": step["action"], "slots": eval_slots(text, step)}
        if step.get("control"):
            row["control"] = step["control"]
    else:
        row |= {"action": "multi", "slots": {},
                "steps": [{"action": s["action"], "slots": eval_slots(text, s),
                           **({"control": s["control"]} if s.get("control") else {})} for s in steps]}
    return row | {"label": "corrected" if entry.get("corrected") else "confirmed",
                  "decided_by": entry.get("source"), "time": entry.get("time")}


def train_row(entry: dict, steps: list[dict]) -> dict | None:
    text, lang = entry["transcript"], entry.get("language")
    if len(steps) == 1:
        return training_row(text, steps[0], lang, source="history", time=entry.get("time"))
    parts = [training_row(text, s, lang) for s in steps]
    if any(p is None for p in parts):
        return None
    row = {"text": text.strip(), "action": "multi", "slots": {},
           "steps": [{k: p[k] for k in ("action", "slots", "control") if k in p} for p in parts]}
    if code := short_lang(lang):
        row["lang"] = code
    return row | {"source": "history", "time": entry.get("time")}


def build(entries: list[dict], fmt: str) -> list[dict]:
    out: dict[str, dict] = {}
    for entry in entries:
        steps = gold_steps(entry)
        text = (entry.get("transcript") or "").strip()
        if not steps or not text or is_redacted(entry, steps):
            continue
        if any(s.get("action") not in a.ACTION_DESCRIPTIONS for s in steps):
            continue
        row = eval_row(entry, steps) if fmt == "eval" else train_row(entry, steps)
        if row:
            out.pop(habit_key(text), None)  # the latest label wins, and moves to the end
            out[habit_key(text)] = row
    return list(out.values())


# Reviewing


def parse_spoken_action(line: str) -> a.Action:
    """`play_media query=Arijit Singh app=Spotify` -> PlayMedia. Values run to the next name=. JSON works too."""
    if line.strip().startswith("{"):
        return a.parse_action(json.loads(line))
    name, _, rest = line.strip().partition(" ")
    fields = {m.group(1): m.group(2).strip() for m in re.finditer(r"(\w+)=(.*?)(?=\s+\w+=|$)", rest)}
    return a.parse_action({"action": name, **{k: v for k, v in fields.items() if v}})


def describe(steps: list[dict]) -> str:
    return " -> ".join(" ".join([s.get("action", "?")] + [f"{k}={v}" for k, v in s.items() if k != "action"])
                       for s in steps) or "(not understood)"


def review(history: History, ask=input, show=print) -> int:
    """Ask about each unlabelled command, newest first. Returns how many were labelled."""

    def answer_to(prompt: str) -> str:
        return ask(prompt).replace("﻿", "").strip()  # answers piped in from PowerShell start with a BOM

    labelled = 0
    todo = [e for e in reversed(history.entries()) if not e.get("corrected") and not e.get("confirmed")
            and (e.get("transcript") or "").strip() and not is_redacted(e, e.get("steps") or [])]
    show(f"{len(todo)} commands to review. y = right, n = wrong, Enter = skip, q = quit.")
    for entry in todo:
        steps = entry.get("steps") or []
        show(f"\n{entry.get('time', '')}  [{entry.get('language') or '?'}]  {entry['transcript']}\n"
             f"    did: {describe(steps)}  ({entry.get('source') or '-'}, {entry.get('outcome') or '-'})")
        answer = answer_to("    right? ").lower()
        if answer == "q":
            break
        if answer == "y" and steps:
            history.mark_confirmed(entry["id"])
            labelled += 1
        elif answer == "y":
            show("    nothing was done; answer n and say what it should have done")
        elif answer == "n":
            while line := answer_to("    should have done (e.g. open_app app=Notepad; Enter = skip): "):
                try:
                    corrected = parse_spoken_action(line)
                except ValueError as e:
                    show(f"    not a valid action: {str(e).splitlines()[0]}")
                    continue
                if [corrected.model_dump(exclude_none=True)] == steps:  # what it did after all
                    history.mark_confirmed(entry["id"])
                else:
                    history.mark_corrected(entry["id"], corrected)
                labelled += 1
                break
    return labelled


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--history", type=Path, help="default: history.jsonl next to PREFERENCES_PATH")
    p.add_argument("--format", choices=["eval", "train"], default="eval")
    p.add_argument("--out", type=Path,
                   help="default: eval/history_set.jsonl (eval) or train/data/history/history_train.jsonl (train)")
    p.add_argument("--review", action="store_true", help="label unlabelled commands first")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    path = args.history
    if path is None:
        from bolos.config import load_settings

        path = history_path(load_settings().preferences_path)
    if not path.exists():
        sys.exit(f"No history at {path} yet: use Bol OS for a while first.")
    if args.review:
        review(History(path))
    rows = build(read_entries(path), args.format)
    out = args.out or (ROOT / "eval" / "history_set.jsonl" if args.format == "eval"
                       else ROOT / "train" / "data" / "history" / "history_train.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    labels = [r.get("label", "") for r in rows]
    print(f"Wrote {len(rows)} commands to {out}"
          + (f" ({labels.count('confirmed')} confirmed, {labels.count('corrected')} corrected)"
             if args.format == "eval" else ""))


if __name__ == "__main__":
    main()
