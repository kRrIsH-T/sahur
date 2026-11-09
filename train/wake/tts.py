"""Disk-cached Timbre synthesis for wake-word training data.

Every clip is keyed by (model, voice, language, text, speed) and saved under train/wake/audio/ as 16 kHz mono
WAV, so a clip is paid for once: re-running a generator only synthesises what is missing. Requests hold
bolos.speech.timbre_turn() for their whole duration (Timbre allows one active request per API key, and the
app and other generators may be running at the same time).
"""

import hashlib
import io
import json
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bolos.config import load_settings  # noqa: E402
from bolos.speech import TTS_URL, SpeechError, timbre_turn  # noqa: E402

AUDIO_DIR = Path(__file__).resolve().parent / "audio"
MANIFEST = AUDIO_DIR / "manifest.jsonl"
FAILED = AUDIO_DIR / "failed.jsonl"
SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Clip:
    """One synthesis request. kind: "positive", "near", "speech"; split: "train", "val", "test" (by voice)."""

    model: str
    voice: str
    lang: str
    text: str
    speed: float
    kind: str
    split: str

    @property
    def key(self) -> str:
        raw = json.dumps([self.model, self.voice, self.lang, self.text, round(self.speed, 3)], ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    @property
    def path(self) -> Path:
        return AUDIO_DIR / f"{self.key}.wav"


def read_wav(path: Path) -> np.ndarray:
    """int16 mono samples of a 16 kHz WAV written by this module."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono 16-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def load_manifest() -> list[dict]:
    """Rows for every cached clip (the Clip fields plus "file" and "seconds")."""
    if not MANIFEST.exists():
        return []
    rows = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["file"]] = row
    return [r for r in rows.values() if (AUDIO_DIR / r["file"]).exists()]


def failed_keys() -> set[str]:
    if not FAILED.exists():
        return set()
    return {json.loads(line)["key"] for line in FAILED.read_text(encoding="utf-8").splitlines() if line.strip()}


class Synth:
    """Synthesises Clips that are not cached yet. Counts real requests so a run can stop at a budget."""

    def __init__(self, budget: int):
        self.key = load_settings().gnani_api_key
        if not self.key or self.key.startswith("your-"):
            raise SystemExit("GNANI_API_KEY is not set")
        self.budget = budget
        self.requests = 0
        self.session = requests.Session()
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    def ensure(self, clip: Clip) -> bool:
        """True when the clip is on disk (cached or just made). False if it failed or the budget is spent."""
        if clip.path.exists():
            return True
        if self.requests >= self.budget:
            return False
        self.requests += 1
        body = {
            "text": clip.text, "voice": clip.voice, "model": clip.model, "language": clip.lang,
            "speed": clip.speed,
            "audio_config": {"sample_rate": SAMPLE_RATE, "num_channels": 1, "sample_width": 2,
                             "encoding": "linear_pcm", "container": "wav"},
        }
        error = None
        for attempt in range(3):
            try:
                with timbre_turn(timeout=600):
                    r = self.session.post(TTS_URL, headers={"X-API-Key-ID": self.key}, json=body, timeout=60)
            except (requests.RequestException, SpeechError) as e:
                error = repr(e)
                time.sleep(2 + 3 * attempt)
                continue
            if r.status_code == 200:
                return self._save(clip, r.content)
            error = f"{r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 404, 422):
                break  # the request itself is wrong; retrying cannot help
            time.sleep(2 + 3 * attempt)
        with FAILED.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": clip.key, **asdict(clip), "error": error}, ensure_ascii=False) + "\n")
        print(f"  failed {clip.voice} {clip.lang} {clip.text[:40]!r}: {error}", flush=True)
        return False

    def _save(self, clip: Clip, data: bytes) -> bool:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width != 2 or channels != 1 or rate != SAMPLE_RATE:
            raise SpeechError(f"unexpected audio format {rate} Hz x{channels} {8 * width}-bit")
        samples = np.frombuffer(frames, dtype=np.int16)
        tmp = clip.path.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(samples.tobytes())
        tmp.replace(clip.path)
        row = {**asdict(clip), "file": clip.path.name, "seconds": round(len(samples) / SAMPLE_RATE, 3)}
        with MANIFEST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
