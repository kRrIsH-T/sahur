"""Training windows: 1.6 s of augmented audio -> (160, 40) log-mel features and a label.

The detector scores the last 1.6 s every 80 ms, so a window is labelled by where it ends:
- "pos":     ends 0.02-0.5 s after "Hey Bol OS" ends (the phrase is in it; the start may be cut for long,
             slow phrases). Label 1.
- "partial": ends 0.25-0.7 s before the phrase ends ("hey bol" without "OS" yet). Label 0, so the model
             waits for the whole phrase.
- "post":    ends 1.1-1.6 s after the phrase ("OS" and what follows). Label 0.
- "near":    a near miss ("hey bolo", "hey boss", "bol na"...) fully inside. Label 0.
- "speech":  any stretch of ordinary speech in the 10 languages. Label 0.
- "noise":   silence, noise or music only. Label 0.
Speech before and after the phrase (as when someone says "Hey Bol OS, Spotify kholo" in one breath) is added
at random, then the whole window goes through augment.corrupt.
"""

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
if not __package__:  # run as a script: drop this folder from the path so train/wake/train.py cannot shadow the
    # "train" package, and import the siblings as train.wake.*
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT / "train" / "wake"]
    sys.path.insert(0, str(ROOT))

from bolos.wake import FRAME, HOP, LOG_FLOOR, log_mel, mel_filterbank  # noqa: E402
from train.wake import augment  # noqa: E402
from train.wake.corpus import clip_audio, load  # noqa: E402

SR = 16000
WINDOW_FRAMES = 160
WIN = (WINDOW_FRAMES - 1) * HOP + FRAME  # samples behind one window of features
PRE = int(0.3 * SR)  # extra audio before the window so reverb and noise start before it
KINDS = ["pos", "partial", "post", "near", "speech", "noise"]
LABEL = {"pos": 1.0}
WARPS = np.round(np.linspace(0.88, 1.12, 13), 3)
_MELS = {float(w): mel_filterbank(float(w)) for w in WARPS}


class Sampler:
    """Draws labelled windows from one split of the corpus. Safe to build in each worker process."""

    def __init__(self, split: str):
        self.audio, index = load(split)
        self.pos = [r for r in index if r["kind"] == "positive"]
        self.near = [r for r in index if r["kind"] == "near"]
        self.speech = [r for r in index if r["kind"] == "speech"]
        self.by_voice: dict[str, list[dict]] = {}
        for r in self.speech:
            self.by_voice.setdefault(r["voice"], []).append(r)
        self._babble: list[np.ndarray] = []

    def clip(self, row: dict) -> np.ndarray:
        return clip_audio(self.audio, row)

    def babble_pool(self, rng: np.random.Generator) -> list[np.ndarray]:
        """A few speech clips of this split for babble noise, refreshed now and then."""
        if not self._babble or rng.random() < 0.02:
            rows = [self.speech[i] for i in rng.choice(len(self.speech), size=min(12, len(self.speech)),
                                                       replace=False)]
            self._babble = [self.clip(r) for r in rows]
        return self._babble

    def _context(self, rng: np.random.Generator, n: int, voice: str | None) -> np.ndarray:
        """n samples of speech, preferably by the same voice (what they say before/after the phrase)."""
        rows = self.by_voice.get(voice) if voice and rng.random() < 0.7 else None
        row = rows[rng.integers(len(rows))] if rows else self.speech[rng.integers(len(self.speech))]
        x = self.clip(row)[row["start"]: row["end"]]
        if len(x) <= n:
            return np.pad(x, (0, n - len(x)))
        s = rng.integers(0, len(x) - n)
        return x[s: s + n]

    def _keyword_window(self, rng: np.random.Generator, row: dict, end_offset: float) -> np.ndarray:
        """Window ending `end_offset` seconds after the (sped-up/slowed) voiced region of `row` ends."""
        x = self.clip(row)
        lo, hi = max(0, row["start"] - int(0.15 * SR)), min(len(x), row["end"] + int(0.1 * SR))
        speed = rng.uniform(0.9, 1.1)
        word = augment.resample(x[lo:hi], speed)
        w_start = int((row["start"] - lo) / speed)
        w_end = int((row["end"] - lo) / speed)
        total = WIN + PRE
        seg = np.zeros(total, np.float32)
        end_pos = total - int(end_offset * SR)  # where the voiced region ends in seg
        place = end_pos - w_end  # where word[0] goes
        a, b = max(0, place), min(total, place + len(word))
        if b > a:
            seg[a:b] += word[a - place: b - place]
        level = augment.rms(word[w_start:w_end])
        if rng.random() < 0.35:  # speech before
            gap = int(rng.uniform(0.1, 0.8) * SR)
            stop = place + w_start - gap
            if stop > 0:
                ctx = self._context(rng, stop, row["voice"])
                seg[:stop] += ctx * level * 10 ** (rng.uniform(-6, 3) / 20) / (augment.rms(ctx) + 1e-9)
        if rng.random() < 0.45:  # speech after, as in "Hey Bol OS, Spotify kholo"
            begin = end_pos + int(rng.uniform(0.03, 0.4) * SR)
            if begin < total:
                ctx = self._context(rng, total - begin, row["voice"])
                seg[begin:] += ctx * level * 10 ** (rng.uniform(-4, 2) / 20) / (augment.rms(ctx) + 1e-9)
        return seg

    def audio_window(self, kind: str, rng: np.random.Generator) -> np.ndarray:
        """WIN + PRE samples of clean audio for one window kind (before corruption)."""
        total = WIN + PRE
        if kind in ("pos", "partial", "post"):
            row = self.pos[rng.integers(len(self.pos))]
            offset = {"pos": rng.uniform(0.02, 0.5), "partial": -rng.uniform(0.25, 0.7),
                      "post": rng.uniform(1.1, 1.6)}[kind]
            return self._keyword_window(rng, row, offset)
        if kind == "near":
            return self._keyword_window(rng, self.near[rng.integers(len(self.near))], rng.uniform(-0.1, 0.6))
        if kind == "speech":
            row = self.speech[rng.integers(len(self.speech))]
            speed = rng.uniform(0.9, 1.1)
            need = int(total * speed) + 2  # cut first, then resample only what the window uses
            if row["len"] > need:
                s = int(rng.integers(0, row["len"] - need))
                row = {**row, "off": row["off"] + s, "len": need}
            x = augment.resample(self.clip(row), speed)[-total:]
            return np.pad(x, (total - len(x), 0))
        return np.zeros(total, np.float32)

    def window(self, kind: str, rng: np.random.Generator, strength: float = 1.0) -> tuple[np.ndarray, float]:
        """(features (160, 40) float32, label) for one random window of `kind`."""
        if kind == "noise":
            audio = augment.background(WIN + PRE, rng, self.babble_pool(rng))
        else:
            audio = augment.corrupt(self.audio_window(kind, rng), rng, self.babble_pool(rng), strength)
        warp = float(rng.choice(WARPS)) if rng.random() < 0.6 else 1.0
        feats = log_mel(audio[-WIN:], _MELS[warp])
        if rng.random() < 0.03:  # the detector's buffer right after start-up or reset: silence floor
            feats[: rng.integers(10, 120)] = np.log(LOG_FLOOR)
        return feats, LABEL.get(kind, 0.0)
