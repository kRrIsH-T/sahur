"""Train System 1's slot tagger from scratch on train/data/dataset.jsonl.

    python train/train_slots.py --name slots

Rows carry slot values copied from the text (train/SLOTS.md); they become BIO tags over tokens. The model
(see bolos/fast/slots.py) is trained with the tags each action can't have masked out, as at runtime, on
token-level augmentations that keep tags aligned (respellings, typos, dropped and added fillers, a name
split in two the way speech recognition does, Prisma casing).

Compound commands (action "multi") are skipped: their parts are tagged after the pipeline splits them, as
single commands. They are dropped after the split, so the held-out rows stay the action model's.

Calibration uses the same held-out split as the action model: one temperature, then per slot the lowest
confidence at which an extracted value is exactly right often enough, and the same for "this optional
slot is really not there".
"""

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "train"))

from augment import ADD_FILLERS, FILLERS, _swap_spelling, _typo, _vary_doubles  # noqa: E402
from check_rows import ACTIONS as ACTION_NAMES  # noqa: E402  (bolos.actions' plus the form actions)
from train_fast import CAL_AUG_COPIES, fit_temperature, lowest_safe_threshold, split  # noqa: E402

from bolos.fast.model import MODELS_DIR, MULTI  # noqa: E402
from bolos.fast.slots import ACTION_SLOTS, TAGS, SlotModel, allowed_tags  # noqa: E402
from bolos.fast.tokens import nfc, token_features, tokenize  # noqa: E402

# Wrong words in a message or contact are worse than a search for slightly the wrong thing; a slightly off
# search or song query is visible and cheap to repeat, while asking System 2 costs ~10 s.
TARGET_ERROR = {"text": 0.03, "contact": 0.03, "query": 0.10}
DEFAULT_TARGET_ERROR = 0.05
TAG_ID = {t: i for i, t in enumerate(TAGS)}


def to_example(row: dict) -> tuple[list[str], list[int], str] | None:
    """Tokens and BIO tag ids, or None if a slot doesn't fall on token boundaries."""
    text = nfc(row["text"])
    spans = tokenize(text)
    tags = ["O"] * len(spans)
    taken: list[tuple[int, int]] = []
    for name, value in (row.get("slots") or {}).items():
        value = nfc(value)
        start = text.find(value)
        while start >= 0 and any(start < e and start + len(value) > s for s, e in taken):
            start = text.find(value, start + 1)  # same words twice: use the copy not already a slot
        if start < 0:
            return None
        end = start + len(value)
        idx = [i for i, (s, e) in enumerate(spans) if s >= start and e <= end]
        if not idx or spans[idx[0]][0] != start or spans[idx[-1]][1] != end:
            return None
        taken.append((start, end))
        tags[idx[0]] = f"B-{name}"
        for i in idx[1:]:
            tags[i] = f"I-{name}"
    return [text[s:e] for s, e in spans], [TAG_ID[t] for t in tags], row["action"]


def examples(rows: list[dict], skipped: Counter) -> list[tuple[list[str], list[int], str]]:
    """The rows the tagger learns from; counts the others in `skipped` by why (compound rows, misaligned slots)."""
    out = []
    for r in rows:
        if r["action"] == MULTI:
            skipped["multi (skipped)"] += 1
        elif (ex := to_example(r)) is None:
            skipped[r["action"]] += 1
        elif ex[0]:
            out.append(ex)
    return out


def augment_tokens(tokens: list[str], tags: list[int], rng: random.Random) -> tuple[list[str], list[int]]:
    out_t, out_y = [], []
    for tok, y in zip(tokens, tags, strict=True):
        if y == 0 and tok.casefold() in FILLERS and rng.random() < 0.3:
            continue
        r = rng.random()
        if tok.isascii() and tok.isalpha():
            if r < 0.15:
                tok = _swap_spelling(tok, rng)
            elif r < 0.22:
                tok = _vary_doubles(tok, rng)
            elif r < 0.26:
                tok = _typo(tok, rng)
            elif r < 0.29 and y != 0 and len(tok) >= 6:  # "Radiohead" heard as "radio head"
                cut = rng.randrange(3, len(tok) - 2)
                out_t += [tok[:cut], tok[cut:]]
                out_y += [y, y if TAGS[y].startswith("I-") else TAG_ID["I-" + TAGS[y][2:]]]
                continue
        out_t.append(tok)
        out_y.append(y)
    if rng.random() < 0.15:
        i = rng.randrange(len(out_t) + 1)
        # Never inside a span: the filler would split it.
        if i == len(out_t) or not TAGS[out_y[i]].startswith("I-"):
            for f in reversed(rng.choice(ADD_FILLERS).split()):
                out_t.insert(i, f)
                out_y.insert(i, 0)
    if not out_t:
        return tokens, tags
    style = rng.random()
    if style < 0.35:
        out_t[0] = out_t[0][:1].upper() + out_t[0][1:]
        if out_t[-1] not in ".?!":
            out_t.append(rng.choice([".", ".", "?"]))
            out_y.append(0)
    elif style < 0.5:
        out_t = [t.lower() for t in out_t]
    return out_t, out_y


class Tagger(nn.Module):
    def __init__(self, buckets: int, dim: int, hidden: int):
        super().__init__()
        self.emb = nn.EmbeddingBag(buckets, dim, mode="mean")
        nn.init.normal_(self.emb.weight, std=0.1)
        self.action_emb = nn.Embedding(len(ACTION_NAMES), dim)
        nn.init.normal_(self.action_emb.weight, std=0.1)
        self.gru = nn.GRU(dim, hidden, batch_first=True, bidirectional=True)
        self.out = nn.Linear(2 * hidden, len(TAGS))
        self.drop = nn.Dropout(0.1)

    def forward(self, ids, offsets, lengths, actions):
        tok = self.emb(ids, offsets)  # (total tokens, dim)
        rows = torch.split(tok, lengths.tolist())
        x = nn.utils.rnn.pad_sequence(rows, batch_first=True) + self.action_emb(actions)[:, None, :]
        packed = nn.utils.rnn.pack_padded_sequence(self.drop(x), lengths, batch_first=True, enforce_sorted=False)
        h, _ = self.gru(packed)
        h, _ = nn.utils.rnn.pad_packed_sequence(h, batch_first=True)
        return self.out(self.drop(h))

    def export(self) -> dict[str, np.ndarray]:
        g = {k: v.detach().numpy() for k, v in self.gru.state_dict().items()}
        out = {"emb": self.emb.weight.detach().numpy().astype(np.float16),
               "action_emb": self.action_emb.weight.detach().numpy(),
               "w_out": self.out.weight.detach().numpy().T, "b_out": self.out.bias.detach().numpy()}
        for side, suffix in (("fwd", "l0"), ("bwd", "l0_reverse")):
            for k in ("weight_ih", "weight_hh", "bias_ih", "bias_hh"):
                short = k.replace("weight", "w").replace("bias", "b")
                out[f"{side}_{short}"] = g[f"{k}_{suffix}"]
        return out


MASKS = torch.tensor(np.stack([allowed_tags(a) for a in ACTION_NAMES]))


def batch(examples, cfg):
    ids, offsets, lengths, n = [], [], [], 0
    for tokens, _, _ in examples:
        for f in token_features(tokens, cfg["buckets"], tuple(cfg["char_n"])):
            offsets.append(n)
            ids.append(f)
            n += len(f)
        lengths.append(len(tokens))
    actions = torch.tensor([ACTION_NAMES.index(a) for _, _, a in examples])
    return torch.from_numpy(np.concatenate(ids)), torch.tensor(offsets), torch.tensor(lengths), actions


def masked_logits(model, examples, cfg):
    ids, offsets, lengths, actions = batch(examples, cfg)
    z = model(ids, offsets, lengths, actions)
    return z.masked_fill(~MASKS[actions][:, None, :], -1e4), lengths


def spans_of(tags: list[str], tokens: list[str]) -> dict[str, str]:
    out, i = {}, 0
    while i < len(tags):
        if tags[i].startswith("B-"):
            name, j = tags[i][2:], i + 1
            while j < len(tags) and tags[j] == f"I-{name}":
                j += 1
            out.setdefault(name, " ".join(tokens[i:j]))
            i = j
        else:
            i += 1
    return out


def calibrate(slot_model: SlotModel, cal: list[tuple], rng: random.Random) -> dict:
    """Per slot: thresholds for "this value is right" and "this slot is really absent"."""
    found: dict[str, list[tuple[float, bool]]] = {}
    absent: dict[str, list[tuple[float, bool]]] = {}
    exact = total = 0
    for tokens, tags, action in cal:
        if not ACTION_SLOTS.get(action):
            continue  # no slots to get right: would only inflate the "every slot right" share
        for _ in range(CAL_AUG_COPIES):
            t, y = augment_tokens(tokens, tags, rng)
            gold = spans_of([TAGS[i] for i in y], t)
            ex = slot_model.extract(" ".join(t), action)
            pred = {k: s.value for k, s in ex.slots.items()}
            total += 1
            exact += pred == gold
            for name, slot in ex.slots.items():
                found.setdefault(name, []).append((slot.confidence, gold.get(name) == slot.value))
            for name, conf in ex.absent.items():
                absent.setdefault(name, []).append((conf, name not in gold))
    thresholds = {}
    for prefix, groups in (("", found), ("no_", absent)):
        for name, pairs in groups.items():
            conf = np.array([c for c, _ in pairs])
            ok = np.array([o for _, o in pairs])
            thresholds[prefix + name] = lowest_safe_threshold(conf, ok, TARGET_ERROR.get(name, DEFAULT_TARGET_ERROR))
    report = {"cal_slot_rows_exact": round(exact / max(total, 1), 4)}
    for name, pairs in found.items():
        th = thresholds[name]
        acc = [o for c, o in pairs if c >= th]
        report[name] = {"acc": round(float(np.mean([o for _, o in pairs])), 4),
                        "coverage": round(len(acc) / len(pairs), 3),
                        "acc_when_sure": round(float(np.mean(acc)), 4) if acc else None}
    return {"thresholds": thresholds, "report": report}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="slots")
    p.add_argument("--dim", type=int, default=64)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--buckets", type=int, default=1 << 16)
    p.add_argument("--char-min", type=int, default=2)
    p.add_argument("--char-max", type=int, default=4)
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=0.003)
    p.add_argument("--aug-prob", type=float, default=0.7)
    p.add_argument("--cal-fraction", type=float, default=0.12)
    p.add_argument("--seed", type=int, default=13)
    p.add_argument("--split-seed", type=int, default=0)
    p.add_argument("--data", type=Path, default=ROOT / "train" / "data" / "dataset.jsonl")
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR, help="where <name>.npz is written")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)
    rows = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
    train_rows, cal_rows = split(rows, args.cal_fraction, args.split_seed)
    unaligned = Counter()
    train, cal = examples(train_rows, unaligned), examples(cal_rows, unaligned)
    print(f"{len(train)} train, {len(cal)} cal examples; not used: {dict(unaligned)}")

    cfg = {"buckets": args.buckets, "char_n": [args.char_min, args.char_max], "dim": args.dim,
           "hidden": args.hidden, "actions": ACTION_NAMES, "tags": TAGS, "seed": args.seed,
           "split_seed": args.split_seed, "cal_fraction": args.cal_fraction}
    model = Tagger(args.buckets, args.dim, args.hidden)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    steps = args.epochs * math.ceil(len(train) / args.batch)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: max(0.03, 1 - s / steps))
    loss_fn = nn.CrossEntropyLoss(ignore_index=-100)
    start = time.perf_counter()
    for epoch in range(args.epochs):
        model.train()
        rng.shuffle(train)
        total = 0.0
        for i in range(0, len(train), args.batch):
            chunk = []
            for tokens, tags, action in train[i:i + args.batch]:
                if rng.random() < args.aug_prob:
                    tokens, tags = augment_tokens(tokens, tags, rng)
                chunk.append((tokens, tags, action))
            z, lengths = masked_logits(model, chunk, cfg)
            y = torch.full(z.shape[:2], -100, dtype=torch.long)
            for k, (_, tags, _) in enumerate(chunk):
                y[k, :len(tags)] = torch.tensor(tags)
            loss = loss_fn(z.reshape(-1, len(TAGS)), y.reshape(-1))
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            total += loss.item() * len(chunk)
        model.eval()
        with torch.no_grad():
            z, _ = masked_logits(model, cal, cfg)
        pred = z.argmax(-1)
        exact = np.mean([pred[k, :len(t)].tolist() == t for k, (_, t, act) in enumerate(cal) if ACTION_SLOTS.get(act)])
        print(f"epoch {epoch + 1:2d} loss {total / len(train):.4f} cal rows all tags right {exact:.4f}")

    model.eval()
    with torch.no_grad():
        z, lengths = masked_logits(model, cal, cfg)
        y = torch.full(z.shape[:2], -100, dtype=torch.long)
        for k, (_, tags, _) in enumerate(cal):
            y[k, :len(tags)] = torch.tensor(tags)
        keep = y.reshape(-1) >= 0
        temperature = fit_temperature(z.reshape(-1, len(TAGS))[keep], y.reshape(-1)[keep])
    cfg.update(temperature=temperature, train_rows=len(train), cal_rows=len(cal),
               train_seconds=round(time.perf_counter() - start, 1))
    args.models_dir.mkdir(parents=True, exist_ok=True)
    out = args.models_dir / f"{args.name}.npz"
    weights = model.export()
    np.savez_compressed(out, config=np.array(json.dumps(cfg)), **weights)
    fit = calibrate(SlotModel(out), cal, random.Random(0))
    cfg["thresholds"] = fit["thresholds"]
    np.savez_compressed(out, config=np.array(json.dumps(cfg)), **weights)
    print(json.dumps({"name": args.name, "temperature": round(temperature, 3),
                      "thresholds": {k: round(v, 3) for k, v in fit["thresholds"].items()},
                      **fit["report"], "size_mb": round(out.stat().st_size / 1e6, 2),
                      "train_seconds": cfg["train_seconds"]}, indent=1))


if __name__ == "__main__":
    main()
