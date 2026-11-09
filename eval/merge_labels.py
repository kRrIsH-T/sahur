"""Merge blind labels into a test set: keep a command only if every labeller chose the same action.

    python eval/merge_labels.py eval/drafts/v4_texts.jsonl eval/drafts/v4_label_*.jsonl --out eval/commands_v4.jsonl

A slot's gold value is the one most labellers gave (compared without case and punctuation); a slot with
no majority is left unscored (listed in "disputed"). The writer's own labels are not used, only the blind
labellers'.
"""

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path


def norm(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("texts", type=Path)
    p.add_argument("labels", nargs="+")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    texts = [json.loads(line) for line in args.texts.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    files = [Path(f) for pattern in args.labels for f in sorted(glob.glob(pattern))]
    labels = [{r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8-sig").splitlines()) if r}
              for f in files]
    kept, dropped = [], Counter()
    for row in texts:
        votes = [lab.get(row["id"]) for lab in labels]
        if any(v is None or v["text"] != row["text"] for v in votes):
            dropped["missing"] += 1
            continue
        actions = {(v["action"], v.get("control")) for v in votes}
        if len(actions) != 1:
            dropped["actions_differ"] += 1
            continue
        action, control = actions.pop()
        slots, disputed = {}, []
        for name in sorted({k for v in votes for k in v.get("slots", {})}):
            values = [v.get("slots", {}).get(name) for v in votes]
            counts = Counter(norm(x) if x else None for x in values)
            best, n = counts.most_common(1)[0]
            if n * 2 > len(votes):
                if best is not None:
                    slots[name] = next(x for x in values if x and norm(x) == best)
            else:
                disputed.append(name)
        kept.append({"id": row["id"], "lang": row["lang"], "text": row["text"], "action": action,
                     **({"control": control} if control else {}), "slots": slots,
                     **({"disputed": disputed} if disputed else {})})
    with args.out.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"kept {len(kept)}/{len(texts)} -> {args.out}; dropped: {dict(dropped)}")
    print("actions:", dict(Counter(r["action"] for r in kept)))
    print("langs:", dict(Counter(r["lang"] for r in kept)))
    print("disputed slots:", sum(len(r.get("disputed", [])) for r in kept))


if __name__ == "__main__":
    main()
