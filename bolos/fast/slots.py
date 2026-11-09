"""System 1's slot tagger at runtime, in numpy: which words of the command are the app, the song, the
contact, the message...

Each token is a mean of hashed feature embeddings plus an embedding of the action System 1 chose; a
bidirectional GRU reads the sentence; a linear layer scores BIO tags. Decoding only allows valid BIO
sequences and the slots the action takes, and each slot comes with a confidence: the lowest probability
of the tags that make it up (and of the tags just outside it, so a span cut short or run long is unsure).
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bolos.fast.tokens import nfc, token_features, tokenize

# A threshold of 1.0 means calibration found no safe cutoff: never sure. (A float32 probability can round
# to exactly 1.0, so "confidence >= 1.0" alone would still accept it.)
NEVER = 1.0
SLOT_TYPES = ["app", "site", "query", "contact", "text", "target"]
TAGS = ["O"] + [f"{p}-{s}" for s in SLOT_TYPES for p in ("B", "I")]
# Which slots each action can have (others are masked out when decoding).
ACTION_SLOTS = {
    "open_app": {"app"}, "open_website": {"site"}, "web_search": {"query", "site"},
    "play_media": {"query", "app"}, "send_message": {"contact", "text"}, "type_text": {"text"},
    "click": {"target"},
}


@dataclass(frozen=True)
class Slot:
    value: str
    confidence: float


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _gru(xs: np.ndarray, w_ih, w_hh, b_ih, b_hh) -> np.ndarray:
    """torch.nn.GRU (one layer, one direction) over a (T, I) sequence."""
    hidden = w_hh.shape[1]
    h = np.zeros(hidden, dtype=np.float32)
    gi_all = xs @ w_ih.T + b_ih
    out = np.empty((len(xs), hidden), dtype=np.float32)
    for t in range(len(xs)):
        gi, gh = gi_all[t], w_hh @ h + b_hh
        r = _sigmoid(gi[:hidden] + gh[:hidden])
        z = _sigmoid(gi[hidden:2 * hidden] + gh[hidden:2 * hidden])
        n = np.tanh(gi[2 * hidden:] + r * gh[2 * hidden:])
        h = (1 - z) * n + z * h
        out[t] = h
    return out


def allowed_tags(action: str) -> np.ndarray:
    slots = ACTION_SLOTS.get(action, set())
    return np.array([t == "O" or t[2:] in slots for t in TAGS])


def _valid_transition() -> np.ndarray:
    """valid[i, j]: tag j may follow tag i (I-x only after B-x or I-x)."""
    valid = np.ones((len(TAGS), len(TAGS)), dtype=bool)
    for j, tag in enumerate(TAGS):
        if tag.startswith("I-"):
            valid[:, j] = [t[2:] == tag[2:] and t != "O" for t in TAGS]
    return valid


VALID = _valid_transition()
START_VALID = np.array([not t.startswith("I-") for t in TAGS])


def decode(log_probs: np.ndarray, allowed: np.ndarray) -> list[int]:
    """Most probable valid tag sequence (Viterbi over hard BIO constraints)."""
    neg = -1e9
    emit = np.where(allowed, log_probs, neg)
    trans = np.where(VALID, 0.0, neg)
    score = np.where(START_VALID, emit[0], neg)
    back = []
    for t in range(1, len(emit)):
        cand = score[:, None] + trans
        back.append(cand.argmax(0))
        score = cand.max(0) + emit[t]
    path = [int(score.argmax())]
    for b in reversed(back):
        path.append(int(b[path[-1]]))
    return path[::-1]


class SlotModel:
    def __init__(self, path: Path):
        data = np.load(path, allow_pickle=False)
        self.config = json.loads(str(data["config"]))
        self.emb = data["emb"].astype(np.float32)
        self.action_emb = data["action_emb"].astype(np.float32)
        self.fwd = [data[f"fwd_{k}"].astype(np.float32) for k in ("w_ih", "w_hh", "b_ih", "b_hh")]
        self.bwd = [data[f"bwd_{k}"].astype(np.float32) for k in ("w_ih", "w_hh", "b_ih", "b_hh")]
        self.w_out, self.b_out = data["w_out"].astype(np.float32), data["b_out"].astype(np.float32)
        self.actions: list[str] = self.config["actions"]
        self.temperature: float = self.config.get("temperature", 1.0)
        self.thresholds: dict[str, float] = self.config.get("thresholds", {})

    def tag_probs(self, tokens: list[str], action: str) -> np.ndarray:
        c = self.config
        feats = token_features(tokens, c["buckets"], tuple(c["char_n"]))
        x = np.stack([self.emb[f].mean(axis=0) for f in feats])
        if action in self.actions:
            x = x + self.action_emb[self.actions.index(action)]
        h = np.concatenate([_gru(x, *self.fwd), _gru(x[::-1], *self.bwd)[::-1]], axis=1)
        z = (h @ self.w_out + self.b_out) / self.temperature
        z = z - z.max(axis=1, keepdims=True)
        p = np.exp(z)
        return p / p.sum(axis=1, keepdims=True)

    def extract(self, text: str, action: str) -> "Extraction":
        """The slots of the given action found in text, each with its confidence, and for slots not
        found, how sure the model is that they really aren't there."""
        text = nfc(text)
        spans = tokenize(text)
        allowed = allowed_tags(action)
        if not spans or not allowed[1:].any():
            return Extraction({}, {})
        tokens = [text[s:e] for s, e in spans]
        probs = self.tag_probs(tokens, action)
        masked = np.where(allowed, probs, 0.0)
        masked = masked / masked.sum(axis=1, keepdims=True).clip(1e-9)
        path = decode(np.log(masked.clip(1e-12)), allowed)
        slots: dict[str, Slot] = {}
        i = 0
        while i < len(path):
            tag = TAGS[path[i]]
            if not tag.startswith("B-"):
                i += 1
                continue
            name, j = tag[2:], i + 1
            while j < len(path) and TAGS[path[j]] == f"I-{name}":
                j += 1
            inside = [masked[k, path[k]] for k in range(i, j)]
            # The token after the span must really not continue it.
            after = [1.0 - masked[j, TAGS.index(f"I-{name}")]] if j < len(path) else []
            conf = float(min(inside + after))
            value = text[spans[i][0]:spans[j - 1][1]]
            if name in slots:  # one slot in two pieces: part of it would be lost, so it is not sure
                keep = slots[name] if slots[name].confidence >= conf else Slot(value, conf)
                slots[name] = Slot(keep.value, 0.0)
            else:
                slots[name] = Slot(value, conf)
            i = j
        absent = {name: float(1.0 - masked[:, TAGS.index(f"B-{name}")].max())
                  for name in ACTION_SLOTS.get(action, ()) if name not in slots}
        return Extraction(slots, absent)

    def sure(self, name: str, confidence: float) -> bool:
        threshold = self.thresholds.get(name, NEVER)
        return threshold < NEVER and confidence >= threshold


@dataclass(frozen=True)
class Extraction:
    slots: dict[str, Slot]
    absent: dict[str, float]  # slot name -> confidence that it is not in the command
