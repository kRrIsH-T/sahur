"""Score how Bol OS understands commands, through the app's own pipeline (Assistant.understand), without
executing anything. Habits are off, so every command is understood from scratch.

    python eval/evaluate.py                          System 1 alone, on every test set
    python eval/evaluate.py --system2 cached         also System 1 + System 2 and System 2 alone, from saved answers
    python eval/evaluate.py --system2 call           ... asking System 2 (Codex) for answers not saved yet
    python eval/evaluate.py --sets commands_v4.jsonl --errors

Per set and system: action accuracy; on v4 (slot gold) also "fully right" (action and every field), with
fields compared exactly (after case and punctuation) and loosely (same meaning: the query "Arijit Singh"
for "Arijit"); how often System 1 acts alone ("S1 acts") and how often System 2 is asked.

System 2 answers are saved in eval/results/system2_answers.json, keyed by backend, model and command, so
numbers can be re-checked without new calls. Dev sets (v1, v2, v4) guided data and model choices (v4 after
its errors were studied); v3 and v5 were frozen before the models they score were trained.
"""

import argparse
import csv
import difflib
import hashlib
import json
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bolos import actions as a  # noqa: E402
from bolos.config import load_settings  # noqa: E402
from bolos.fast.features import fold  # noqa: E402
from bolos.pipeline import Assistant  # noqa: E402

SETS = {
    "commands.csv": "dev v1 typed", "commands_speech.csv": "dev v1 speech",
    "commands_v2.csv": "dev v2 typed", "commands_v2_speech.csv": "dev v2 speech",
    "commands_v3.csv": "TEST v3 typed", "commands_v3_speech.csv": "TEST v3 speech",
    "commands_v4.jsonl": "dev v4 typed", "commands_v4_speech.jsonl": "dev v4 speech",
    "commands_v5.jsonl": "TEST v5 typed", "commands_v5_speech.jsonl": "TEST v5 speech",
}
ANSWERS = ROOT / "eval" / "results" / "system2_answers.json"


def unscorable_fields(row: dict) -> set[str]:
    """Gold fields that can't be compared with what the app heard: labellers disagreed, or (speech sets) the
    gold was written in a script the transcript doesn't use, e.g. gold "एक्सेल" for a transcript "Excel"."""
    skip = set(row.get("disputed", []))
    if "typed" in row:
        heard_ascii = row["text"].isascii()
        skip |= {f for f, v in row.get("slots", {}).items() if heard_ascii and not v.isascii()}
    return skip


def load(name: str) -> list[dict]:
    """Rows as {text, lang, action, control, slots|None, skip}."""
    path = ROOT / "eval" / name
    if not path.exists():
        return []
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        return [{"text": r["text"], "lang": r["lang"], "action": r["action"], "control": r.get("control"),
                 "slots": r.get("slots", {}), "skip": unscorable_fields(r)} for r in rows]
    with path.open(encoding="utf-8") as f:
        return [{"text": r["command"], "lang": r["lang"], "action": r["action"], "control": r.get("control") or None,
                 "slots": None, "skip": set()} for r in csv.DictReader(f)]


class MissingAnswer(Exception):
    pass


class SavedSystem2:
    """Stands in for the LLM in the real pipeline: answers from the saved file, optionally asking the real
    System 2 for commands it hasn't seen (and saving those). Thread-safe, so rows can run in parallel."""

    def __init__(self, settings, allow_calls: bool):
        from bolos.llm import make_llm

        self.real = make_llm(settings)
        if self.real is None:
            sys.exit("No System 2 configured (codex CLI or an LLM key)")
        # Answers are only reused for the same backend, model, effort and prompts.
        from bolos import llm as prompts

        version = hashlib.sha256(json.dumps([prompts.FIELD_RULES, prompts.EXAMPLES, prompts.CONTEXT,
                                             a.ACTION_DESCRIPTIONS], ensure_ascii=False).encode()).hexdigest()[:8]
        self.backend = (f"{type(self.real).__name__}:{getattr(self.real, 'model', '')}:"
                        f"{getattr(self.real, 'effort', '')}:{version}")
        self.allow_calls = allow_calls
        self.saved = json.loads(ANSWERS.read_text(encoding="utf-8")) if ANSWERS.exists() else {}
        self.called = 0
        self._lock = threading.Lock()

    def answer(self, key: str, ask):
        from bolos.llm import SlotError

        with self._lock:
            answer = self.saved.setdefault(self.backend, {}).get(key)
        if answer is None:
            if not self.allow_calls:
                raise MissingAnswer(key)
            try:
                answer = ask().model_dump(exclude_none=True)
            except SlotError as e:
                answer = {"slot_error": str(e)[:200]}
            except Exception as e:  # outage: report as missing, keep scoring
                print(f"System 2 failed ({type(e).__name__}: {str(e)[:120]}); counted as missing")
                raise MissingAnswer(key) from e
            with self._lock:
                self.saved[self.backend][key] = answer
                self.called += 1
                ANSWERS.parent.mkdir(exist_ok=True)
                ANSWERS.write_text(json.dumps(self.saved, ensure_ascii=False, indent=0), encoding="utf-8")
        if "slot_error" in answer:
            raise SlotError(answer["slot_error"])
        return a.parse_action(answer)


class RowSystem2:
    """One row's view of System 2: counts what the pipeline asked for this command."""

    def __init__(self, shared: SavedSystem2):
        self.shared = shared
        self.asked = self.missing = 0

    def _answer(self, key, ask):
        self.asked += 1
        try:
            return self.shared.answer(key, ask)
        except MissingAnswer:
            self.missing += 1
            raise

    def resolve(self, command: str):
        return self._answer(f"resolve|{command}", lambda: self.shared.real.resolve(command))

    def fill(self, action: str, command: str, fields: list[str]):
        return self._answer(f"fill|{action}|{','.join(fields)}|{command}",
                            lambda: self.shared.real.fill(action, command, fields))


def norm(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (value or "").casefold()).split())


# Not "na": in Hindi it is mostly a softener ("chalo na"), not a "no".
NEGATIONS = {"nahi", "nahin", "mat", "not", "no", "dont", "don", "illa", "illai", "beda", "venda", "vendam",
             "ledu", "vaddu", "nako", "नहीं", "मत", "ನಹಿ", "ಬೇಡ", "இல்லை", "వద్దు"}


def loosely_equal(field: str, pred: str | None, gold: str | None) -> bool:
    p, g = norm(pred), norm(gold)
    if p == g:
        return True
    if not p or not g:
        return False
    pw, gw = {fold(w) for w in p.split()}, {fold(w) for w in g.split()}
    if field == "text" and any(w in NEGATIONS or w.isdigit() for w in pw ^ gw):
        return False  # "main late nahi hoon" or "6 baje" is a different message, however similar
    if field in ("query", "text"):  # same meaning: most words shared, either way round
        return len(pw & gw) / len(pw | gw) >= 0.6 or (gw <= pw and len(pw) <= len(gw) + 2)
    return difflib.SequenceMatcher(None, p.replace(" ", ""), g.replace(" ", "")).ratio() >= 0.8 or gw <= pw


def judge(row: dict, action: a.Action | None) -> dict:
    pred = action.action if action else "unknown"
    ok = pred == row["action"] and (pred != "media_control" or action.control == row["control"])
    out = {"action_ok": ok}
    if row["slots"] is not None:
        fields = [f for f in a.SLOT_FIELDS.get(row["action"], []) if f not in row["skip"]]
        values = action.model_dump() if action else {}
        exact = ok and all(norm(values.get(f)) == norm(row["slots"].get(f)) for f in fields)
        loose = ok and all(loosely_equal(f, values.get(f), row["slots"].get(f)) for f in fields)
        out.update(exact=exact, loose=loose)
    return out


def describe(action: a.Action | None) -> str:
    return action.model_dump_json(exclude_none=True) if action else "(not understood)"


def run(name: str, rows: list[dict], parser, system2: SavedSystem2 | None, errors: bool, workers: int) -> dict:
    def one(row: dict) -> dict:
        s2 = RowSystem2(system2) if system2 else None
        start = time.perf_counter()
        understood = Assistant(parser, s2, runner=None, voice=None).understand(row["text"])
        out = {"ms": (time.perf_counter() - start) * 1000, "asked": bool(s2 and s2.asked),
               "missing": bool(s2 and s2.missing), "understood": understood,
               "s1_acted": understood is not None and understood.source == "system1"}
        out.update(judge(row, understood.action if understood else None))
        if parser is not None and system2 is None:  # System 1's best guess, even when it would defer
            guess = parser.parse(row["text"])
            out["raw_ok"] = guess.action == row["action"] and (
                guess.action != "media_control" or guess.control == row["control"])
        return out

    with ThreadPoolExecutor(max_workers=workers if system2 else 1) as pool:
        outs = list(pool.map(one, rows))
    results = [o for o in outs if not o["missing"]]
    if errors:
        for row, o in zip(rows, outs, strict=True):
            if not o["missing"] and not o.get("loose", o["action_ok"]):
                gold = row["action"] + (f"/{row['control']}" if row["control"] else "")
                gold += f" {json.dumps(row['slots'], ensure_ascii=False)}" if row["slots"] else ""
                print(f"    [{name}] {row['text']}\n        got  "
                      f"{describe(o['understood'].action if o['understood'] else None)}\n        gold {gold}")
    n = len(results)
    acted = [o for o in results if o["s1_acted"]]
    summary = {"n": n, "acc": sum(o["action_ok"] for o in results) / max(n, 1),
               "s1_acts": len(acted) / max(n, 1),
               "s1_acted_acc": sum(o["action_ok"] for o in acted) / max(len(acted), 1),
               "s2_asked": sum(o["asked"] for o in outs) / max(len(outs), 1),
               "missing": len(outs) - n, "ms": statistics.median(o["ms"] for o in outs) if outs else 0.0}
    if results and "raw_ok" in results[0]:
        summary["raw"] = sum(o["raw_ok"] for o in results) / n
    if rows and rows[0]["slots"] is not None:
        summary["exact"] = sum(o["exact"] for o in results) / max(n, 1)
        summary["loose"] = sum(o["loose"] for o in results) / max(n, 1)
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sets", nargs="+", default=list(SETS))
    p.add_argument("--system2", choices=["cached", "call"], help="also score with System 2 (Codex/LLM)")
    p.add_argument("--only", choices=["system1", "combined", "system2"], nargs="+")
    p.add_argument("--errors", action="store_true", help="print commands that went wrong")
    p.add_argument("--json", type=Path)
    p.add_argument("--workers", type=int, default=6, help="System 2 calls in parallel")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    from bolos.decide import FastParser

    settings = load_settings()
    parser = FastParser(settings.fast_model, settings.slot_model)
    parser.parse("warm up")
    system2 = SavedSystem2(settings, args.system2 == "call") if args.system2 else None
    systems = {"system1": (parser, None)}
    if system2:
        systems |= {"combined": (parser, system2), "system2": (None, system2)}
    if args.only:
        systems = {k: v for k, v in systems.items() if k in args.only}

    report = {}
    print("acc = action right (unsure System 1 alone counts as wrong); raw = System 1's best guess; "
          "exact/loose = action and every field right (v4); S1 acts = share System 1 does alone")
    for set_name in args.sets:
        rows = load(set_name)
        if not rows:
            continue
        for system, (prs, s2) in systems.items():
            r = run(set_name, rows, prs, s2, args.errors, args.workers)
            report[f"{set_name}|{system}"] = r
            fields = ""
            if "exact" in r:  # speech: gold fields come from the typed text, so only loose matching is fair
                fields = f" loose {r['loose']:.3f}" if "speech" in set_name else \
                    f" exact {r['exact']:.3f} loose {r['loose']:.3f}"
            extra = f" S2 asked {r['s2_asked']:.1%}" if system != "system1" else \
                f" raw {r['raw']:.3f} acted-acc {r['s1_acted_acc']:.3f}"
            gap = f" ({r['missing']} answers missing)" if r["missing"] else ""
            print(f"{SETS.get(set_name, set_name):16s} {system:9s} n={r['n']:3d} acc {r['acc']:.3f}{fields}"
                  f"  S1 acts {r['s1_acts']:.1%}{extra}  {r['ms']:.2f} ms{gap}")
    if system2 and system2.called:
        print(f"New System 2 calls: {system2.called} (saved to {ANSWERS.relative_to(ROOT)})")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
