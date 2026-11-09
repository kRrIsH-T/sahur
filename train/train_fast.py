"""Train the System 1 decision model from scratch (random init) on train/data/dataset.jsonl.

    python train/train_fast.py --name bag-ce
    python train/train_fast.py --name bag-rlcd --loss rlcd
    python train/train_fast.py --name linear-ce --kind linear

Model: hashed word + character n-gram bag (fastText-style), mean-pooled, with three linear softmax heads:
- action: the executable actions (incl. the form actions explain_form, fill_form, check_site) plus "multi" (a
  compound command with two or more steps, rows from train/data/r3);
- media control (pause, next, ...; trained only on media_control rows);
- language (hi, en, kn, ta, te, mr, ml, bn, gu, pa, or "none" for language-neutral commands; trained only on
  rows that carry a "lang" label, the other rows are masked out of this head's loss).

Losses:
- ce:   soft cross-entropy with label smoothing.
- rlcd: Laya's objective (laya/train.py): soft cross-entropy plus a group-relative policy-gradient term
        over S noisy logit samples, rewarded by a strictly proper score (log + 0.75 * spherical).

After training, a held-out calibration split (augmented, to look like noisy speech) fits one temperature
per head and per-action (and per-language) abstention thresholds: the lowest confidence at which the
conservative error estimate (errors + 1) / (accepted + 1) stays under the target error. The split is
stratified by action and language, so every language has held-out rows to calibrate on.
"""

import argparse
import hashlib
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

from augment import augment  # noqa: E402
from check_rows import ACTIONS  # noqa: E402

from bolos.actions import CONTROLS  # noqa: E402
from bolos.fast.features import feature_ids  # noqa: E402
from bolos.fast.model import LANGUAGES, MODELS_DIR, MULTI  # noqa: E402

# The executable actions (bolos.actions' plus the form actions, see check_rows.ACTIONS) and "multi" (ACTIONS may
# list it already once the pipeline knows compounds).
MODEL_ACTIONS = ACTIONS if MULTI in ACTIONS else [*ACTIONS, MULTI]
CONTROL_NAMES = list(CONTROLS)
LANGUAGE_NAMES = list(LANGUAGES)
# Typing or clicking in the wrong place can't be taken back and isn't confirmed first, so System 1 must be
# surer before doing them alone. A message is read back and needs a spoken "haan", which catches a wrong
# guess, so it gets the normal target.
TARGET_ERROR = {"type_text": 0.03, "click": 0.03}
DEFAULT_TARGET_ERROR = 0.06
CONTROL_TARGET_ERROR = 0.06  # a wrong pause/next is harmless and said again; below it, System 2 decides
# A reply in the wrong language is jarring but harmless; below the threshold the reply falls back to the
# script, the previous command's language, then USER_LANGUAGE.
LANGUAGE_TARGET_ERROR = 0.05
CAL_AUG_COPIES = 3
HEAD_KEYS = (("wa", "ba"), ("wp", "bp"), ("wl", "bl"))  # action, control, language weights in the .npz


class BagModel(nn.Module):
    SIZES = (len(MODEL_ACTIONS), len(CONTROL_NAMES), len(LANGUAGE_NAMES))

    def __init__(self, buckets: int, dim: int, kind: str):
        super().__init__()
        self.kind = kind
        width = dim if kind == "bag" else sum(self.SIZES)
        self.emb = nn.EmbeddingBag(buckets, width, mode="mean")
        nn.init.uniform_(self.emb.weight, -1 / width, 1 / width)
        if kind == "bag":
            self.heads = nn.ModuleList(nn.Linear(dim, k) for k in self.SIZES)
            for head in self.heads:
                nn.init.zeros_(head.weight)
                nn.init.zeros_(head.bias)
        else:  # logistic regression on the mean feature vector: heads are fixed slices
            self.biases = nn.ParameterList(nn.Parameter(torch.zeros(k)) for k in self.SIZES)

    def forward(self, ids, offsets):
        """(action, control, language) logits."""
        h = self.emb(ids, offsets)
        if self.kind == "bag":
            return tuple(head(h) for head in self.heads)
        parts = torch.split(h, list(self.SIZES), dim=-1)
        return tuple(part + b for part, b in zip(parts, self.biases, strict=True))

    def export(self) -> dict[str, np.ndarray]:
        emb = self.emb.weight.detach().numpy()
        out = {"emb": emb.astype(np.float16)}
        if self.kind == "bag":
            for (w, b), head in zip(HEAD_KEYS, self.heads, strict=True):
                out[w], out[b] = head.weight.detach().numpy().T, head.bias.detach().numpy()
            return out
        eye = np.eye(emb.shape[1], dtype=np.float32)
        start = 0
        for (w, b), k, bias in zip(HEAD_KEYS, self.SIZES, self.biases, strict=True):
            out[w], out[b] = eye[:, start:start + k], bias.detach().numpy()
            start += k
        return out


def soft_targets(labels: torch.Tensor, k: int, smoothing: float) -> torch.Tensor:
    t = torch.full((len(labels), k), smoothing / k)
    t[torch.arange(len(labels)), labels] += 1 - smoothing
    return t


def soft_ce(logits, t):
    return -(t * logits.log_softmax(-1)).sum(-1).mean()


def rlcd(logits, t, sigma: float, samples: int = 4, w_sph: float = 0.75):
    """laya/train.py:493-523, for a fixed-class head."""
    eps = torch.randn(samples, *logits.shape) * sigma
    eps = eps - eps.mean(-1, keepdim=True)
    z = logits.detach() + eps
    with torch.no_grad():
        q = z.softmax(-1)
        reward = (t * q.clamp_min(1e-12).log().clamp_min(-9.21)).sum(-1) \
            + w_sph * (t * q).sum(-1) / q.norm(dim=-1).clamp_min(1e-9)
        adv = reward - reward.mean(0, keepdim=True)
        adv = adv / (adv.std() + 1e-6)
    log_pi = -((z - logits) ** 2).sum(-1) / (2 * sigma ** 2)
    return -(adv * log_pi).mean() + soft_ce(logits, t)


def batch_tensors(texts, cfg, feature_dropout: float, rng: random.Random):
    ids, offsets = [], []
    for text in texts:
        f = feature_ids(text, cfg["buckets"], tuple(cfg["char_n"]), cfg["word_bigrams"], cfg["folded"])
        if feature_dropout:
            keep = [i for i in f.tolist() if rng.random() >= feature_dropout] or f.tolist()[:1]
            f = np.asarray(keep)
        offsets.append(sum(len(x) for x in ids))
        ids.append(f)
    return torch.from_numpy(np.concatenate(ids)), torch.tensor(offsets)


def predict(model, texts, cfg):
    """(action, control, language) logits for many texts."""
    model.eval()
    with torch.no_grad():
        outs = [model(*batch_tensors(texts[i:i + 512], cfg, 0.0, random.Random(0)))
                for i in range(0, len(texts), 512)]
    model.train()
    return tuple(torch.cat(head) for head in zip(*outs, strict=True))


def fit_temperature(logits: torch.Tensor, labels: torch.Tensor) -> float:
    if len(labels) < 10:
        return 1.0
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = nn.functional.cross_entropy(logits / log_t.exp(), labels)
        loss.backward()
        return loss

    opt.step(closure)
    return float(log_t.detach().exp().clamp(0.5, 5.0))


def lowest_safe_threshold(conf: np.ndarray, correct: np.ndarray, target: float) -> float:
    """Lowest confidence at which the conservative error estimate stays under target, or 1.0 (never).

    Calibration rows appear CAL_AUG_COPIES times (augmented), so each copy counts 1/CAL_AUG_COPIES of a
    row: the estimate must not get more confident just because a row was copied."""
    c, ok = np.round(conf, 4), correct
    order = np.argsort(-c)
    c, ok = c[order], ok[order]
    best = 1.0
    for i in range(len(c)):
        if i + 1 < len(c) and c[i + 1] == c[i]:
            continue  # accept whole ties at once
        accepted = (i + 1) / CAL_AUG_COPIES
        errors = int((~ok[: i + 1]).sum()) / CAL_AUG_COPIES
        if (errors + 1) / (accepted + 1) <= target:
            best = float(c[i])
    return best


def fit_thresholds(conf: np.ndarray, pred: np.ndarray, gold: np.ndarray, names: list[str],
                   target: dict[str, float], default: float) -> dict[str, float]:
    """Per class: the lowest confidence at which that class's predictions are right often enough."""
    return {name: lowest_safe_threshold(conf[pred == k], gold[pred == k] == k, target.get(name, default))
            for k, name in enumerate(names)}


def fit_language_thresholds(conf: np.ndarray, pred: np.ndarray, gold: np.ndarray) -> tuple[dict[str, float], float]:
    """Per-language thresholds, and the pooled one over every language.

    With ~50 held-out rows per language, a language the model predicts rarely (often "none") can't show the
    target error on its own: even with no mistakes, (0 + 1) / (n + 1) <= 5% needs 19 accepted rows. Such a
    language uses the pooled threshold, which is certified on all languages together, provided its own
    predictions above that threshold contain no error. A language never predicted on held-out rows stays
    never-sure, and one with enough rows keeps its own threshold (higher or lower than the pooled one)."""
    pooled = lowest_safe_threshold(conf, pred == gold, LANGUAGE_TARGET_ERROR)
    thresholds = fit_thresholds(conf, pred, gold, LANGUAGE_NAMES, {}, LANGUAGE_TARGET_ERROR)
    enough = 1 / LANGUAGE_TARGET_ERROR - 1
    for k, name in enumerate(LANGUAGE_NAMES):
        mine = pred == k
        if thresholds[name] < 1.0 or pooled >= 1.0 or not mine.any() or mine.sum() / CAL_AUG_COPIES >= enough:
            continue
        above = mine & (conf >= pooled)
        if (gold[above] == k).all():
            thresholds[name] = pooled
    return thresholds, pooled


def accepted_report(conf: np.ndarray, pred: np.ndarray, gold: np.ndarray, thresholds: dict[str, float],
                    names: list[str]) -> dict[str, float | None]:
    """Accuracy, coverage, and accuracy on the predictions the thresholds accept (augmented held-out rows)."""
    if not len(gold):
        return {"acc": None, "coverage": None, "acc_when_accepted": None}
    th = np.array([thresholds[names[k]] for k in pred])
    accepted = (conf >= th) & (th < 1.0)
    return {"acc": round(float((pred == gold).mean()), 4), "coverage": round(float(accepted.mean()), 3),
            "acc_when_accepted": round(float((pred[accepted] == gold[accepted]).mean()), 4) if accepted.any() else None}


def split(rows: list[dict], cal_fraction: float, seed: int) -> tuple[list[dict], list[dict]]:
    """Calibration split stratified by action and language, fixed before training."""
    rng = random.Random(seed)
    by_group: dict[tuple[str, str], list] = {}
    for r in rows:
        by_group.setdefault((r["action"], r.get("lang") or ""), []).append(r)
    train, cal = [], []
    for name in sorted(by_group):
        group = sorted(by_group[name], key=lambda r: r["text"])
        rng.shuffle(group)
        n = max(1, int(len(group) * cal_fraction))
        cal += group[:n]
        train += group[n:]
    return train, cal


def calibrate(logits_fn, cal: list[dict], rng: random.Random) -> dict:
    """Temperatures and thresholds from held-out rows, augmented so confidence reflects noisy speech rather
    than clean text. logits_fn(texts) -> (action, control, language) logits as tensors."""
    cal_rows = [r for r in cal for _ in range(CAL_AUG_COPIES)]
    texts = [augment(r["text"], rng) for r in cal_rows]
    la, lp, ll = logits_fn(texts)
    ya = torch.tensor([MODEL_ACTIONS.index(r["action"]) for r in cal_rows])
    t_action = fit_temperature(la, ya)
    ctl_mask = torch.tensor([r.get("control") is not None for r in cal_rows])
    yp = torch.tensor([CONTROL_NAMES.index(r["control"]) for r in cal_rows if r.get("control") is not None])
    t_control = fit_temperature(lp[ctl_mask], yp)
    probs = (la / t_action).softmax(-1).numpy()
    pred, conf, gold = probs.argmax(-1), probs.max(-1), ya.numpy()
    thresholds = fit_thresholds(conf, pred, gold, MODEL_ACTIONS, TARGET_ERROR, DEFAULT_TARGET_ERROR)
    ctl_probs = (lp[ctl_mask] / t_control).softmax(-1).numpy()
    control_threshold = lowest_safe_threshold(ctl_probs.max(-1), ctl_probs.argmax(-1) == yp.numpy(),
                                              CONTROL_TARGET_ERROR)

    lang_mask = torch.tensor([bool(r.get("lang")) for r in cal_rows])
    yl = torch.tensor([LANGUAGE_NAMES.index(r["lang"]) for r in cal_rows if r.get("lang")], dtype=torch.long)
    t_language = fit_temperature(ll[lang_mask], yl)
    lang_probs = (ll[lang_mask] / t_language).softmax(-1).numpy()
    lang_pred, lang_conf, lang_gold = lang_probs.argmax(-1), lang_probs.max(-1), yl.numpy()
    language_thresholds, pooled = fit_language_thresholds(lang_conf, lang_pred, lang_gold)
    is_multi = gold == MODEL_ACTIONS.index(MULTI)
    by_language = {name: accepted_report(lang_conf[lang_gold == k], lang_pred[lang_gold == k],
                                         lang_gold[lang_gold == k], language_thresholds, LANGUAGE_NAMES)
                   for k, name in enumerate(LANGUAGE_NAMES) if (lang_gold == k).any()}
    action_report = accepted_report(conf, pred, gold, thresholds, MODEL_ACTIONS)
    return {
        "temperature": {"action": t_action, "control": t_control, "language": t_language},
        "thresholds": thresholds, "control_threshold": control_threshold,
        "language_thresholds": language_thresholds,
        "report": {"cal_acc_augmented": action_report["acc"], "coverage": action_report["coverage"],
                   "acc_when_accepted": action_report["acc_when_accepted"],
                   "multi_rows": accepted_report(conf[is_multi], pred[is_multi], gold[is_multi], thresholds,
                                                 MODEL_ACTIONS),
                   "action_by_gold": {name: accepted_report(conf[gold == k], pred[gold == k], gold[gold == k],
                                                            thresholds, MODEL_ACTIONS)
                                      for k, name in enumerate(MODEL_ACTIONS) if (gold == k).any()},
                   "language": {**accepted_report(lang_conf, lang_pred, lang_gold, language_thresholds,
                                                  LANGUAGE_NAMES),
                                "cal_rows": int(lang_mask.sum()) // CAL_AUG_COPIES,
                                "pooled_threshold": round(pooled, 4)},
                   "language_by_gold": by_language},
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--kind", choices=["bag", "linear"], default="bag")
    p.add_argument("--loss", choices=["ce", "rlcd"], default="ce")
    p.add_argument("--dim", type=int, default=48)
    p.add_argument("--buckets", type=int, default=1 << 17)
    p.add_argument("--char-min", type=int, default=2)
    p.add_argument("--char-max", type=int, default=5)
    p.add_argument("--no-bigrams", action="store_true")
    p.add_argument("--no-fold", action="store_true")
    p.add_argument("--epochs", type=int, default=25)
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--lr", type=float, default=0.01)
    p.add_argument("--smoothing", type=float, default=0.05)
    p.add_argument("--aug-prob", type=float, default=0.7)
    p.add_argument("--feature-dropout", type=float, default=0.1)
    p.add_argument("--cal-fraction", type=float, default=0.12)
    p.add_argument("--seed", type=int, default=13, help="initialisation, shuffling and augmentation")
    p.add_argument("--split-seed", type=int, default=0,
                   help="calibration split; keep it fixed so ensemble members share held-out rows")
    p.add_argument("--data", type=Path, default=ROOT / "train" / "data" / "dataset.jsonl")
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR, help="where <name>.npz is written")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    rows = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
    train, cal = split(rows, args.cal_fraction, args.split_seed)

    data_sha256 = hashlib.sha256(args.data.read_bytes()).hexdigest()  # ensemble.py checks members share it
    cfg = {"data_sha256": data_sha256, "buckets": args.buckets, "char_n": [args.char_min, args.char_max],
           "word_bigrams": not args.no_bigrams,
           "folded": not args.no_fold, "actions": MODEL_ACTIONS, "controls": CONTROL_NAMES,
           "languages": LANGUAGE_NAMES, "kind": args.kind, "loss": args.loss, "dim": args.dim, "seed": args.seed}
    model = BagModel(args.buckets, args.dim, args.kind)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    steps = args.epochs * math.ceil(len(train) / args.batch)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: max(0.02, 1 - s / steps))

    y_action = {id(r): MODEL_ACTIONS.index(r["action"]) for r in rows}
    y_control = {id(r): CONTROL_NAMES.index(r["control"]) if r.get("control") else -1 for r in rows}
    y_lang = {id(r): LANGUAGE_NAMES.index(r["lang"]) if r.get("lang") else -1 for r in rows}
    print(f"{len(rows)} rows: {sum(r['action'] == MULTI for r in rows)} multi, "
          f"{sum(v >= 0 for v in y_lang.values())} with a language label")

    def head_loss(logits, labels, k: int, sigma: float):
        """One head's loss on the rows that have its label (-1: none); 0 when no row in the batch has one."""
        mask = labels >= 0
        if not mask.any():
            return torch.zeros(())
        t = soft_targets(labels[mask], k, args.smoothing)
        return rlcd(logits[mask], t, sigma) if args.loss == "rlcd" else soft_ce(logits[mask], t)

    start = time.perf_counter()
    for epoch in range(args.epochs):
        sigma = 0.4 + (0.1 - 0.4) * epoch / max(args.epochs - 1, 1)
        rng.shuffle(train)
        total = 0.0
        for i in range(0, len(train), args.batch):
            chunk = train[i:i + args.batch]
            texts = [augment(r["text"], rng) if rng.random() < args.aug_prob else r["text"] for r in chunk]
            ids, offsets = batch_tensors(texts, cfg, args.feature_dropout, rng)
            la, lp, ll = model(ids, offsets)
            ya = torch.tensor([y_action[id(r)] for r in chunk])
            yp = torch.tensor([y_control[id(r)] for r in chunk])
            yl = torch.tensor([y_lang[id(r)] for r in chunk])
            loss = (head_loss(la, ya, len(MODEL_ACTIONS), sigma) + head_loss(lp, yp, len(CONTROL_NAMES), sigma)
                    + head_loss(ll, yl, len(LANGUAGE_NAMES), sigma))
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            total += loss.item() * len(chunk)
        la, _, ll = predict(model, [r["text"] for r in cal], cfg)
        acc = (la.argmax(-1) == torch.tensor([y_action[id(r)] for r in cal])).float().mean()
        yl = torch.tensor([y_lang[id(r)] for r in cal])
        lang = f" lang acc {(ll.argmax(-1) == yl)[yl >= 0].float().mean():.4f}" if (yl >= 0).any() else ""
        print(f"epoch {epoch + 1:2d} loss {total / len(train):.4f} cal acc {acc:.4f}{lang}")

    cal_fit = calibrate(lambda texts: predict(model, texts, cfg), cal, rng)
    cfg.update(temperature=cal_fit["temperature"], thresholds=cal_fit["thresholds"],
               control_threshold=cal_fit["control_threshold"], language_thresholds=cal_fit["language_thresholds"],
               split_seed=args.split_seed, cal_fraction=args.cal_fraction,
               train_rows=len(train), cal_rows=len(cal), train_seconds=round(time.perf_counter() - start, 1))

    args.models_dir.mkdir(parents=True, exist_ok=True)
    out = args.models_dir / f"{args.name}.npz"
    np.savez_compressed(out, config=np.array(json.dumps(cfg)), **model.export())
    print(json.dumps({
        "name": args.name, **cal_fit["report"], "temperature": cfg["temperature"],
        "control_threshold": round(cal_fit["control_threshold"], 3),
        "thresholds": {k: round(v, 3) for k, v in cal_fit["thresholds"].items()},
        "language_thresholds": {k: round(v, 3) for k, v in cal_fit["language_thresholds"].items()},
        "size_mb": round(out.stat().st_size / 1e6, 2), "train_seconds": cfg["train_seconds"],
    }, indent=1))
    split_counts = Counter(r["action"] for r in cal)
    print(f"Saved {out}  (cal split: {dict(split_counts)}; languages: {dict(Counter(r.get('lang') for r in cal))})")


if __name__ == "__main__":
    main()
