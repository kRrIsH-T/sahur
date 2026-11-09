"""The System 1 action model at runtime: hashed n-gram bag -> calibrated softmax heads, in numpy.

No PyTorch at runtime. A model file is a .npz with the embedding table, the heads (action; media control
for media_control commands; and, from round 3, the language the command is spoken in), the calibration
temperatures, abstention thresholds and the feature config, written by train/train_fast.py.

Older model files have no language head and no "multi" action: they still load, and then never report a
language (Prediction.language is None) or a compound command.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bolos.fast.features import feature_ids
from bolos.fast.slots import NEVER

MODELS_DIR = Path(__file__).resolve().parent / "models"
# The 12th action class: a command with two or more steps ("Chrome kholo aur weather batao"). It is not an
# executable action; the pipeline splits the command and parses each part.
MULTI = "multi"
# The language head's classes: the 10 languages Prisma and Timbre support, plus "none" for commands that
# belong to no language ("Spotify", "next").
LANGUAGES = ("hi", "en", "kn", "ta", "te", "mr", "ml", "bn", "gu", "pa", "none")


@dataclass(frozen=True)
class Prediction:
    action: str
    action_confidence: float
    control: str
    control_confidence: float
    action_probs: np.ndarray
    language: str | None = None  # one of LANGUAGES (may be "none"); None when the model has no language head
    language_confidence: float = 0.0
    language_probs: np.ndarray | None = None  # over FastModel.languages


def _softmax(z: np.ndarray) -> np.ndarray:
    e = np.exp(z - z.max())
    return e / e.sum()


class FastModel:
    def __init__(self, path: Path):
        data = np.load(path, allow_pickle=False)
        self.config = json.loads(str(data["config"]))
        self.emb = data["emb"].astype(np.float32)
        self.wa, self.ba = data["wa"].astype(np.float32), data["ba"].astype(np.float32)
        self.wp, self.bp = data["wp"].astype(np.float32), data["bp"].astype(np.float32)
        self.actions: list[str] = self.config["actions"]
        self.controls: list[str] = self.config["controls"]
        temperature = self.config["temperature"]
        self.t_action = temperature["action"]
        self.t_control = temperature["control"]
        self.thresholds: dict[str, float] = self.config["thresholds"]
        self.control_threshold: float = self.config.get("control_threshold", 1.0)
        has_language = "wl" in data.files and bool(self.config.get("languages"))
        self.languages: list[str] = list(self.config["languages"]) if has_language else []
        width = self.emb.shape[1]
        self.wl = data["wl"].astype(np.float32) if has_language else np.zeros((width, 0), np.float32)
        self.bl = data["bl"].astype(np.float32) if has_language else np.zeros(0, np.float32)
        self.t_language: float = temperature.get("language", 1.0)
        self.language_thresholds: dict[str, float] = self.config.get("language_thresholds", {})

    def logits(self, text: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Uncalibrated action, control and language logits (the last is empty without a language head)."""
        c = self.config
        ids = feature_ids(text, c["buckets"], tuple(c["char_n"]), c["word_bigrams"], c["folded"])
        h = self.emb[ids].mean(axis=0)
        return h @ self.wa + self.ba, h @ self.wp + self.bp, h @ self.wl + self.bl

    def predict(self, text: str) -> Prediction:
        la, lc, ll = self.logits(text)
        pa = _softmax(la / self.t_action)
        pc = _softmax(lc / self.t_control)
        a, c = int(pa.argmax()), int(pc.argmax())
        if not self.languages:
            return Prediction(self.actions[a], float(pa[a]), self.controls[c], float(pc[c]), pa)
        pl = _softmax(ll / self.t_language)
        k = int(pl.argmax())
        return Prediction(self.actions[a], float(pa[a]), self.controls[c], float(pc[c]), pa,
                          self.languages[k], float(pl[k]), pl)

    def accepts(self, pred: Prediction) -> bool:
        """Whether System 1 is sure enough to act alone (per-action thresholds fitted on held-out data)."""
        threshold = self.thresholds.get(pred.action, NEVER)
        if threshold >= NEVER or pred.action_confidence < threshold:
            return False
        if pred.action != "media_control":
            return True
        return self.control_threshold < NEVER and pred.control_confidence >= self.control_threshold

    def accepts_language(self, pred: Prediction) -> bool:
        """Whether the language guess is sure (per-language thresholds fitted for <= 5% error when sure)."""
        if pred.language is None:
            return False
        threshold = self.language_thresholds.get(pred.language, NEVER)
        return threshold < NEVER and pred.language_confidence >= threshold
