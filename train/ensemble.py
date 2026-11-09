"""Merge trained System 1 models into one ensemble model file, then re-calibrate it.

    python train/ensemble.py --name system1 bag-rlcd-s1 bag-rlcd-s2 bag-rlcd-s3

Each member computes linear heads on its mean n-gram vector, so the average of the members' logits is
itself one model of the same kind: embedding tables side by side, heads stacked and divided by the
member count. The runtime needs no ensemble code. Members must share features, classes (actions, controls,
languages) and calibration split. The language head is merged like the others when every member has one.
"""

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "train"))

from train_fast import CONTROL_NAMES, HEAD_KEYS, LANGUAGE_NAMES, MODEL_ACTIONS, calibrate, split  # noqa: E402

from bolos.fast.model import MODELS_DIR, FastModel  # noqa: E402

SHARED = ("data_sha256", "buckets", "char_n", "word_bigrams", "folded", "actions", "controls", "languages",
          "split_seed", "cal_fraction")


def merge(parts) -> dict[str, np.ndarray]:
    """One model whose logits (every head) are the mean of the members' logits."""
    n = len(parts)
    merged = {"emb": np.concatenate([x["emb"] for x in parts], axis=1)}
    for w, b in HEAD_KEYS:
        present = [w in x for x in parts]  # .npz files and dicts alike
        if not any(present):
            continue  # e.g. models from before the language head
        if not all(present):
            raise ValueError(f"only some members have the {w} head")
        merged[w] = np.concatenate([x[w] for x in parts], axis=0) / n
        merged[b] = np.mean([x[b] for x in parts], axis=0)
    return merged


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("members", nargs="+")
    p.add_argument("--data", type=Path, default=ROOT / "train" / "data" / "dataset.jsonl")
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR, help="where the members are and the result goes")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    parts = [np.load(args.models_dir / f"{m}.npz") for m in args.members]
    configs = [json.loads(str(x["config"])) for x in parts]
    for key in SHARED:
        values = {json.dumps(c.get(key)) for c in configs}
        if len(values) != 1:
            sys.exit(f"members differ in {key}: {values}")
    # Calibration labels rows by this train_fast.py's class lists, so the members must use the same ones.
    for key, names in (("actions", MODEL_ACTIONS), ("controls", CONTROL_NAMES), ("languages", LANGUAGE_NAMES)):
        if configs[0].get(key) != names:
            sys.exit(f"the members' {key} {configs[0].get(key)} are not {names}; retrain them with this train_fast.py")
    try:
        merged = merge(parts)
    except ValueError as e:
        sys.exit(str(e))
    cfg = {k: configs[0].get(k) for k in SHARED} | {
        "kind": "ensemble", "members": args.members, "temperature": {"action": 1.0, "control": 1.0, "language": 1.0},
        "thresholds": {a: 1.0 for a in configs[0]["actions"]}}
    # Calibrating on a different dataset than the members saw would reuse their training rows as "held-out"
    # rows, making temperatures too sharp and thresholds too low.
    if configs[0].get("data_sha256") not in (None, hashlib.sha256(args.data.read_bytes()).hexdigest()):
        sys.exit(f"{args.data} changed since the members were trained; retrain them")
    rows = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
    train, cal = split(rows, cfg["cal_fraction"], cfg["split_seed"])
    if (len(train), len(cal)) != (configs[0].get("train_rows"), configs[0].get("cal_rows")):
        sys.exit(f"{args.data} doesn't give the members' train/held-out split; retrain them")
    # Written uncalibrated first (thresholds 1.0: never acts alone) so the runtime class computes the logits.
    out = args.models_dir / f"{args.name}.npz"
    np.savez_compressed(out, config=np.array(json.dumps(cfg)), **merged)
    model = FastModel(out)

    def logits(texts):
        heads = list(zip(*(model.logits(t) for t in texts), strict=True))
        return tuple(torch.tensor(np.stack(h)) for h in heads)

    fit = calibrate(logits, cal, random.Random(0))
    cfg.update(temperature=fit["temperature"], thresholds=fit["thresholds"],
               control_threshold=fit["control_threshold"], language_thresholds=fit["language_thresholds"],
               train_rows=len(train), cal_rows=len(cal))
    np.savez_compressed(out, config=np.array(json.dumps(cfg)), **merged)
    print(json.dumps({"name": args.name, "members": args.members, **fit["report"],
                      "temperature": fit["temperature"],
                      "language_thresholds": {k: round(v, 3) for k, v in fit["language_thresholds"].items()},
                      "size_mb": round(out.stat().st_size / 1e6, 2)}, indent=1))


if __name__ == "__main__":
    main()
