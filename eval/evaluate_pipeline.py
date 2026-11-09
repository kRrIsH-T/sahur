"""Offline plan evaluation: no Windows execution, microphone, speech service or System 2 calls.

    python eval/evaluate_pipeline.py --data eval/commands_v6.jsonl --output eval/results/pipeline_v6.json

This measures the connected local planner, including splitting, context, form routing and abstention.
It is separate from the earlier action-model-only evaluate.py results.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bolos import actions as a
from bolos.compound import chain
from bolos.decide import FastParser
from bolos.pipeline import Assistant
from bolos.responses import script_language


class NoEffects:
    def execute(self, action):
        raise AssertionError("Plan evaluation must never execute an action")

    def speak(self, *args):
        raise AssertionError("Offline planning must never request speech")

    def listen(self):
        raise AssertionError("Offline planning must never request a microphone")


def expected_steps(row):
    raw = row.get("steps", []) if row["action"] == "multi" else [row]
    return chain([a.parse_action({"action": step["action"], **step.get("slots", {}),
                                  **({"control": step["control"]} if "control" in step else {})}) for step in raw])


def _canonical(steps):
    # Case and whitespace are harmless; numbers, words, script and step order remain significant.
    return [{key: " ".join(value.casefold().split()) if isinstance(value, str) else value
             for key, value in step.model_dump(exclude_none=True).items()} for step in steps]


def evaluate(parser, rows):
    effects = NoEffects()
    assistant = Assistant(parser, None, effects, effects, learning=False)
    results = []
    for row in rows:
        text = row["text"]
        assistant.language = script_language(text)  # independent commands: no previous-language carry
        start = time.perf_counter()
        understood = assistant._plan(text)
        elapsed = (time.perf_counter() - start) * 1000
        predicted = [step.action for step in understood]
        expected = expected_steps(row)
        # An unsupported/chatter command is correctly refused; cancel is an explicit no-op plan.
        if len(expected) == 1 and expected[0].action == "unknown":
            expected = []
        same_actions = [s.action for s in predicted] == [s.action for s in expected]
        results.append({"id": row.get("id"), "text": text, "lang": row.get("lang"),
                        "expected": _canonical(expected), "predicted": _canonical(predicted),
                        "planned": bool(predicted), "actions_correct": same_actions,
                        "exact": _canonical(predicted) == _canonical(expected),
                        "predicted_language": assistant.language,
                        "language_correct": assistant.language == row.get("lang"), "ms": round(elapsed, 2)})

    def summary(items):
        n = len(items)
        return {"n": n, "planned": sum(r["planned"] for r in items),
                "actions_correct": sum(r["actions_correct"] for r in items),
                "exact": sum(r["exact"] for r in items),
                "language_correct": sum(r["language_correct"] for r in items),
                "mean_ms": round(sum(r["ms"] for r in items) / max(n, 1), 2)}

    return {"summary": summary(results),
            "languages": {lang: summary([r for r in results if r["lang"] == lang])
                          for lang in sorted({r["lang"] for r in results if r["lang"]})},
            "results": results,
            "limits": "Synthetic command understanding only; exact copied fields; no execution, LLM or speech."}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data", type=Path, required=True)
    cli.add_argument("--model", type=Path, default=ROOT / "bolos/fast/models/system1.npz")
    cli.add_argument("--slots", type=Path, default=ROOT / "bolos/fast/models/slots.npz")
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    rows = [json.loads(line) for line in args.data.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    report = evaluate(FastParser(args.model, args.slots), rows)
    report["provenance"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (args.data, args.model, args.slots)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "languages": report["languages"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
