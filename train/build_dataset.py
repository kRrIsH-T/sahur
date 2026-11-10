"""Validate the labelled commands and write a clean, de-duplicated dataset with no test-set overlap.

    python train/build_dataset.py
    python train/build_dataset.py --data-dir <scratch copy of train/data> --corrections none

Reads, in this order (the first copy of a command wins):
1. corrections.jsonl next to preferences.json (PREFERENCES_PATH), newest first: commands the user corrected,
   labelled with what they meant. Real use beats generated data, and a later correction beats an earlier one.
2. train/data/slots/*.jsonl: rows per train/SLOTS.md.
3. train/data/r3/*.jsonl: round 3 rows with a "lang" label, including compound commands (action "multi"
   with "steps"), which feed the action and language heads but not the slot tagger.

Writes train/data/dataset.jsonl. Rows that match any command in any test or dev set (eval/*.csv,
eval/*.jsonl, which includes the frozen test sets v3, v5 and v6 and their speech versions) exactly, nearly
(character 4-gram Jaccard >= 0.8), or as a rewording (spelling-folded word sets with Jaccard >= 0.6) are
dropped, so the test sets stay unseen.
"""

import argparse
import bisect
import csv
import json
import math
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "train"))

from check_rows import LANGS, MULTI, SPAN_SLOTS, problem  # noqa: E402

from bolos.fast.features import fold, normalize  # noqa: E402

NEAR_DUP = 0.8  # character 4-gram Jaccard
NEAR_DUP_WORDS = 0.6  # folded word-bag Jaccard, for rows of 3+ words
# The frozen test sets (the glob over eval/ reads them anyway; named here so a missing one is reported).
TEST_SETS = ("commands_v3.csv", "commands_v3_speech.csv", "commands_v5.jsonl", "commands_v5_speech.jsonl",
             "commands_v6.jsonl", "commands_v6_speech.jsonl")


def shingles(text: str) -> frozenset[str]:
    t = f" {normalize(text)} "
    return frozenset(t[i:i + 4] for i in range(len(t) - 3))


def word_bag(text: str) -> frozenset[str]:
    """Order-free, spelling-folded words: what the model sees, so swaps and respellings still match."""
    return frozenset(fold(w) for w in normalize(text).split())


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / max(len(a | b), 1)


class NearSets:
    """The test-set texts' sets, sorted by size, to ask "is any of them at least this similar?" quickly.

    Jaccard(a, b) <= min(|a|, |b|) / max(|a|, |b|), so only sets whose size is within a factor `threshold`
    of the query's can reach it. Checking just those gives exactly the all-pairs answer, many times faster
    (every training row is compared with every test command)."""

    def __init__(self, sets: list[frozenset]):
        self.sets = sorted(sets, key=len)
        self.sizes = [len(s) for s in self.sets]

    def any_at_least(self, query: frozenset, threshold: float) -> bool:
        lo = bisect.bisect_left(self.sizes, math.ceil(threshold * len(query) - 1e-9))
        hi = bisect.bisect_right(self.sizes, math.floor(len(query) / threshold + 1e-9))
        return any(jaccard(query, s) >= threshold for s in self.sets[lo:hi])


def load_eval_texts(eval_dir: Path) -> list[str]:
    texts = []
    for path in sorted(eval_dir.glob("*.csv")):  # every test set and transcript variant
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                texts += [row["command"], row.get("typed") or ""]
    for path in sorted(eval_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                row = json.loads(line)
                texts += [row.get("text") or "", row.get("typed") or ""]
    return [t for t in texts if t]


def without_eval_overlap(rows: list[dict], eval_texts: list[str], problems: Counter) -> list[dict]:
    eval_norm = {normalize(t) for t in eval_texts}
    eval_bags = {word_bag(t) for t in eval_texts}
    near_shingles, near_bags = NearSets([shingles(t) for t in eval_texts]), NearSets(list(eval_bags))
    clean = []
    for row in rows:
        chars, bag = shingles(row["text"]), word_bag(row["text"])
        if normalize(row["text"]) in eval_norm or bag in eval_bags:
            problems["in_test_set"] += 1
        elif near_shingles.any_at_least(chars, NEAR_DUP):
            problems["near_test_set"] += 1
        elif len(bag) >= 3 and near_bags.any_at_least(bag, NEAR_DUP_WORDS):
            problems["reworded_test_set"] += 1
        else:
            clean.append(row)
    return clean


def default_corrections_path() -> Path:
    from bolos.config import load_settings

    return load_settings().preferences_path.with_name("corrections.jsonl")


def read_jsonl(path: Path, problems: Counter) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            problems["bad_json"] += 1
    return rows


def sanitize_correction(r: dict) -> dict | None:
    """A correction row as a training row: only the action's span slots whose values occur in the text (the
    runtime writes those already; this guards against hand edits), "incomplete" when a required one is
    missing, and the language only when it is a known code."""
    if not isinstance(r, dict) or not isinstance(r.get("text"), str) or r.get("action") not in SPAN_SLOTS:
        return None
    text = unicodedata.normalize("NFC", r["text"].strip())
    allowed, required = SPAN_SLOTS[r["action"]]
    slots = {k: v.strip() for k, v in (r.get("slots") or {}).items()
             if k in allowed and isinstance(v, str) and v.strip() and v.strip() in text}
    row = {"text": text, "action": r["action"], "slots": slots}
    if r.get("control") and r["action"] == "media_control":
        row["control"] = r["control"]
    if required - set(slots):
        row["incomplete"] = True
    if r.get("lang") in LANGS:
        row["lang"] = r["lang"]
    return row


def training_row(r: dict, source: str) -> dict:
    """The fields training needs, in a fixed order."""
    out = {"text": r["text"].strip(), "action": r["action"], "slots": r.get("slots", {})}
    if r.get("control"):
        out["control"] = r["control"]
    if r.get("lang"):
        out["lang"] = r["lang"]
    if r["action"] == MULTI:
        out["steps"] = [{"action": s["action"], "slots": s.get("slots", {}),
                         **({"control": s["control"]} if s.get("control") else {})} for s in r["steps"]]
        if r.get("joiners"):
            out["joiners"] = r["joiners"]
    return out | {"source": source}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=ROOT / "train" / "data", help="has slots/ and r3/")
    p.add_argument("--out", type=Path, help="default: <data-dir>/dataset.jsonl")
    p.add_argument("--corrections", default=None,
                   help="corrections.jsonl to add (default: next to PREFERENCES_PATH, if present); 'none' to skip")
    p.add_argument("--eval-dir", type=Path, default=ROOT / "eval")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    problems, rows = Counter(), []
    seen: dict[str, dict] = {}  # normalized text -> the row kept for it
    sources: list[tuple[str, list[dict]]] = []
    if args.corrections != "none":
        path = Path(args.corrections) if args.corrections else default_corrections_path()
        if path.exists():
            raw = read_jsonl(path, problems)
            sources.append(("correction", [sanitize_correction(r) for r in reversed(raw)]))  # newest first
            print(f"corrections: {len(raw)} rows from {path}")
        elif args.corrections:
            sys.exit(f"{path} does not exist")
        else:
            print(f"corrections: none ({path} does not exist)")
    for folder, prefix in (("slots", ""), ("r3", "r3/")):
        for path in sorted((args.data_dir / folder).glob("*.jsonl")):
            sources.append((prefix + path.stem, read_jsonl(path, problems)))

    for source, source_rows in sources:
        for r in source_rows:
            if r is None:
                problems["bad_correction"] += 1
            elif reason := problem(r):
                problems[reason.split(":")[0].split("=")[0]] += 1
            elif (kept := seen.get(normalize(r["text"]))) is not None:
                problems["duplicate"] += 1
                # The language belongs to the words, not the labels: an older row without one ("next",
                # "Spotify" in slots/) gains it from its r3 copy, so short neutral commands reach the head.
                if r.get("lang") and "lang" not in kept:
                    kept["lang"] = r["lang"]
                    problems["duplicate_gave_lang"] += 1
            else:
                row = training_row(r, source)
                seen[normalize(r["text"])] = row
                rows.append(row)

    eval_texts = load_eval_texts(args.eval_dir)
    found = [name for name in TEST_SETS if (args.eval_dir / name).exists()]
    print(f"test sets kept out: {', '.join(found)}; not found: {sorted(set(TEST_SETS) - set(found)) or 'none'}")
    clean = without_eval_overlap(rows, eval_texts, problems)

    out = args.out or args.data_dir / "dataset.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for r in clean:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(clean)} rows -> {out}")
    print("dropped:", dict(problems))
    print("actions:", dict(Counter(r["action"] for r in clean)))
    print("controls:", dict(Counter(r["control"] for r in clean if "control" in r)))
    print("languages:", dict(Counter(r.get("lang", "-") for r in clean)))
    print("sources:", dict(Counter("r3" if r["source"].startswith("r3/") else
                                   "correction" if r["source"] == "correction" else "slots" for r in clean)))


if __name__ == "__main__":
    main()
