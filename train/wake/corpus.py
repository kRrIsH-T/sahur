"""Pack the synthesised clips into one int16 array per split, with where the speech starts and ends.

    uv run python train/wake/corpus.py                 # all splits
    uv run python train/wake/corpus.py train val       # only these (e.g. while test clips are still coming)

Writes train/wake/work/corpus_<split>.npy (all clips of the split back to back) and corpus_<split>.json
(one entry per clip: offset, length, kind, voice, lang, text, and the voiced start/end in samples). Training
workers memory-map the arrays, so several processes share one copy of the audio.
"""

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
if not __package__:  # run as a script: drop this folder from the path so train/wake/train.py cannot shadow the
    # "train" package, and import the siblings as train.wake.*
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT / "train" / "wake"]
    sys.path.insert(0, str(ROOT))

from train.wake.generate import plan  # noqa: E402
from train.wake.tts import read_wav  # noqa: E402

WORK = HERE / "work"
SR = 16000


def voiced_bounds(x: np.ndarray, rel_db: float = -35.0) -> tuple[int, int]:
    """First and last sample of the region louder than `rel_db` below the loudest 10 ms frame."""
    n = len(x) // 160
    if n == 0:
        return 0, len(x)
    frames = x[: n * 160].astype(np.float64).reshape(n, 160)
    db = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-3)
    loud = np.where(db > db.max() + rel_db)[0]
    return int(loud[0] * 160), int(min(len(x), (loud[-1] + 1) * 160))


def build(splits: tuple[str, ...] = ("train", "val", "test")) -> dict[str, int]:
    WORK.mkdir(exist_ok=True)
    by_split: dict[str, list] = {s: [] for s in splits}
    for clip in plan():
        if clip.split in by_split and clip.path.exists():
            by_split[clip.split].append(clip)
    counts = {}
    for split, clips in by_split.items():
        index, chunks, off = [], [], 0
        for clip in clips:
            x = read_wav(clip.path)
            start, end = voiced_bounds(x)
            if end - start < 0.15 * SR:
                continue  # empty or broken synthesis
            index.append({"off": off, "len": len(x), "kind": clip.kind, "voice": clip.voice, "model": clip.model,
                          "lang": clip.lang, "text": clip.text, "speed": clip.speed, "start": start, "end": end})
            chunks.append(x)
            off += len(x)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
        np.save(WORK / f"corpus_{split}.npy", audio)
        (WORK / f"corpus_{split}.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        counts[split] = len(index)
        kinds = {k: sum(1 for r in index if r["kind"] == k) for k in ("positive", "near", "speech")}
        hours = {k: round(sum(r["len"] for r in index if r["kind"] == k) / SR / 3600, 3) for k in kinds}
        print(f"{split}: {kinds} hours {hours}")
    return counts


def load(split: str) -> tuple[np.ndarray, list[dict]]:
    """(memory-mapped int16 audio, index) for a split."""
    audio = np.load(WORK / f"corpus_{split}.npy", mmap_mode="r")
    index = json.loads((WORK / f"corpus_{split}.json").read_text(encoding="utf-8"))
    return audio, index


def clip_audio(audio: np.ndarray, row: dict) -> np.ndarray:
    return np.asarray(audio[row["off"]: row["off"] + row["len"]], dtype=np.float32) / 32768.0


if __name__ == "__main__":
    build(tuple(sys.argv[1:]) or ("train", "val", "test"))
