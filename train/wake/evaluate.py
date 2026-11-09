"""Streaming evaluation of a wake model on held-out voices, threshold choice and installation.

    uv run python train/wake/evaluate.py                       # evaluate work/wake_candidate.npz
    uv run python train/wake/evaluate.py --install             # ... and install it as bolos/fast/models/wake.npz

Everything runs through the real runtime (bolos.wake.WakeDetector, fed 80 ms chunks), on long streams:
- positive streams: every "Hey Bol OS" clip of a split, in three conditions (clean; noisy room/mic/noise;
  noisy with the speaker talking just before and right after), separated by background noise. A hit is a
  trigger between 0.3 s before and 1.0 s after the phrase ends; latency is trigger time minus phrase end.
- negative stream: every near miss and every paragraph of ordinary speech of the split, half clean, half
  through the noise chain, with gaps and stretches of noise, music, babble and silence; `--passes` repeats it
  with new noise.
The threshold is chosen on the val voices (highest recall with false accepts/hour <= --target-fa), then
reported on the test voices, which were used for nothing else.
"""

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
if not __package__:  # run as a script: drop this folder from the path so train/wake/train.py cannot shadow the
    # "train" package, and import the siblings as train.wake.*
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT / "train" / "wake"]
    sys.path.insert(0, str(ROOT))

from bolos.wake import FRAME, HOP, MODEL_PATH, WakeDetector  # noqa: E402
from train.wake import augment  # noqa: E402
from train.wake.corpus import clip_audio  # noqa: E402

SR = 16000
WORK = HERE / "work"
FIXTURES = HERE / "fixtures"
# Finer near the top, where a well-trained model separates (label smoothing keeps positives near 0.98).
THRESHOLDS = np.round(np.concatenate([np.arange(0.05, 0.9, 0.01), np.arange(0.9, 0.9991, 0.0025)]), 4)


class Recorder(WakeDetector):
    """WakeDetector that also records (frame count, score) at every evaluation."""

    def reset(self) -> None:
        super().reset()
        self.trace: list[tuple[int, float]] = []

    def _evaluate(self) -> bool:
        fired = super()._evaluate()
        self.trace.append((self.frames, self.score))
        return fired


def score_stream(det: Recorder, audio: np.ndarray, chunk: int = 1280) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """(sample time of each evaluation, score, sample times of real triggers at det.threshold)."""
    det.reset()
    pcm = np.clip(audio * 32768, -32768, 32767).astype(np.int16)
    fired = []
    for i in range(0, len(pcm), chunk):
        if det.feed(pcm[i:i + chunk]):
            fired.append((det.frames - 1) * HOP + FRAME)
    frames = np.array([f for f, _ in det.trace])
    return (frames - 1) * HOP + FRAME, np.array([s for _, s in det.trace]), fired


def simulate(times: np.ndarray, scores: np.ndarray, thr: float, refractory: int) -> np.ndarray:
    """Trigger times the detector logic produces at threshold `thr` (same rules as WakeDetector._evaluate)."""
    out, armed, quiet = [], True, -1
    for t, s in zip(times, scores, strict=True):
        if s < thr:
            armed = True
        elif armed and t >= quiet:
            armed = False
            quiet = t + refractory
            out.append(t)
    return np.array(out, dtype=np.int64)


def positive_stream(split: str, seed: int) -> tuple[np.ndarray, list[tuple[int, str]]]:
    """All positive clips in three conditions; returns audio and (phrase end sample, condition) per episode."""
    from train.wake.windows import Sampler

    smp = Sampler(split)
    rng = np.random.default_rng(seed)
    pieces, ends, pos = [], [], 0
    for row in smp.pos:
        for cond in ("clean", "noisy", "context"):
            x = smp.clip(row)
            word = x[max(0, row["start"] - 1600): row["end"] + 800]
            end_in_word = row["end"] - max(0, row["start"] - 1600)
            before = smp._context(rng, int(rng.uniform(1.0, 2.0) * SR), row["voice"]) if cond == "context" else None
            after = smp._context(rng, int(rng.uniform(1.0, 2.0) * SR), row["voice"]) if cond == "context" else None
            lead = np.zeros(int(1.2 * SR), np.float32)
            gap1 = np.zeros(int(rng.uniform(0.15, 0.5) * SR), np.float32)
            gap2 = np.zeros(int(rng.uniform(0.05, 0.3) * SR), np.float32)
            tail = np.zeros(int(1.8 * SR), np.float32)
            head = [lead] + ([before, gap1] if before is not None else [])
            parts = head + [word] + ([gap2, after] if after is not None else []) + [tail]
            end = sum(len(p) for p in head) + end_in_word
            ep = np.concatenate(parts)
            if cond == "clean":
                ep = ep * 10 ** (rng.uniform(-30, -12) / 20) / (augment.rms(word) + 1e-9)
                ep = ep + rng.standard_normal(len(ep)).astype(np.float32) * 10 ** (-70 / 20)
            else:
                ep = augment.corrupt(ep, rng, smp.babble_pool(rng))
            pieces.append(ep.astype(np.float32))
            ends.append((pos + end, cond, row["lang"]))
            pos += len(ep)
    return np.concatenate(pieces), ends


def negative_stream(split: str, seed: int) -> tuple[np.ndarray, float]:
    """Near misses and ordinary speech of the split, half clean and half noisy; returns audio and hours."""
    from train.wake.windows import Sampler

    smp = Sampler(split)
    rng = np.random.default_rng(seed)
    rows = smp.near + smp.speech
    pieces = []
    for i in rng.permutation(len(rows)):
        if rng.random() < 0.15:  # now and then a stretch of silence, noise or music with nobody talking
            pieces.append(augment.background(int(rng.uniform(3, 15) * SR), rng, smp.babble_pool(rng)))
        x = clip_audio(smp.audio, rows[i])
        x = np.concatenate([np.zeros(int(rng.uniform(0.2, 1.2) * SR), np.float32), x])
        if rng.random() < 0.5:
            x = x * 10 ** (rng.uniform(-30, -12) / 20) / (augment.rms(x) + 1e-9)
            x = x + rng.standard_normal(len(x)).astype(np.float32) * 10 ** (-70 / 20)
        else:
            x = augment.corrupt(x, rng, smp.babble_pool(rng))
        pieces.append(x.astype(np.float32))
    audio = np.concatenate(pieces)
    return audio, len(audio) / SR / 3600


def evaluate_split(path: Path, split: str, passes: int, seed: int = 7) -> dict:
    det = Recorder.load(path, personal=False)
    refractory = det.refractory_frames * HOP
    pos_audio, ends = positive_stream(split, seed)
    pt, ps, _ = score_stream(det, pos_audio)
    neg = []
    hours = 0.0
    for p in range(passes):
        audio, h = negative_stream(split, seed + 100 + p)
        nt, ns, _ = score_stream(det, audio)
        neg.append((nt, ns))
        hours += h
    return {"pos": (pt, ps, ends), "neg": neg, "hours": hours, "refractory": refractory}


def metrics(ev: dict, thr: float) -> dict:
    pt, ps, ends = ev["pos"]
    trig = simulate(pt, ps, thr, ev["refractory"])
    hits, lat, by_cond, by_lang = 0, [], {}, {}
    for end, cond, lang in ends:
        ok = trig[(trig >= end - 0.3 * SR) & (trig <= end + 1.0 * SR)]
        for c in (by_cond.setdefault(cond, [0, 0]), by_lang.setdefault(lang, [0, 0])):
            c[1] += 1
            c[0] += bool(len(ok))
        if len(ok):
            hits += 1
            lat.append((ok[0] - end) / SR)
    # Triggers on positive streams that are not near any phrase are false accepts too.
    stray = sum(1 for t in trig if not any(end - 0.3 * SR <= t <= end + 1.0 * SR for end, _, _ in ends))
    fa = sum(len(simulate(nt, ns, thr, ev["refractory"])) for nt, ns in ev["neg"])
    return {"threshold": float(thr), "recall": hits / len(ends),
            "recall_by_condition": {k: round(v[0] / v[1], 4) for k, v in by_cond.items()},
            "recall_by_language": {k: round(v[0] / v[1], 4) for k, v in sorted(by_lang.items())},
            "stray_triggers_in_positive_streams": stray,
            "false_accepts": fa, "hours": ev["hours"], "fa_per_hour": fa / ev["hours"],
            "latency_median_s": float(np.median(lat)) if lat else None,
            "latency_p90_s": float(np.quantile(lat, 0.9)) if lat else None, "episodes": len(ends)}


def choose_threshold(ev: dict, target_fa: float) -> tuple[float, list[tuple[float, float]]]:
    curve = []
    best = None
    for thr in THRESHOLDS:
        m = metrics(ev, thr)
        curve.append((float(thr), round(m["fa_per_hour"], 3)))
        if m["fa_per_hour"] <= target_fa and (best is None or m["recall"] > best[1]):
            best = (float(thr), m["recall"])
    return (best[0] if best else float(THRESHOLDS[-1])), curve


def write_fixtures(seed: int = 11) -> None:
    """Small held-out (test-voice) clips for tests/test_wake.py: three phrases with speech around them in
    noise, and ~30 s of near misses and speech. Chosen by rule (first test clip per language), not by score."""
    from bolos.audio import to_wav
    from train.wake.windows import Sampler

    smp = Sampler("test")
    rng = np.random.default_rng(seed)
    FIXTURES.mkdir(exist_ok=True)
    info = {"positives": [], "negative": "negative.wav"}
    for lang in ("hi-IN", "ta-IN", "kn-IN"):
        row = next(r for r in smp.pos if r["lang"] == lang and r["model"] == "timbre-v3.0")
        x = smp.clip(row)
        word = x[max(0, row["start"] - 1600): row["end"] + 800]
        before = smp._context(rng, int(1.5 * SR), row["voice"])
        after = smp._context(rng, int(1.5 * SR), row["voice"])
        head = [np.zeros(int(1.0 * SR), np.float32), before, np.zeros(int(0.3 * SR), np.float32)]
        end = sum(len(p) for p in head) + row["end"] - max(0, row["start"] - 1600)
        ep = np.concatenate(head + [word, np.zeros(int(0.15 * SR), np.float32), after,
                                    np.zeros(int(1.0 * SR), np.float32)])
        ep = augment.corrupt(ep, rng, smp.babble_pool(rng), strength=0.5)
        name = f"positive_{lang[:2]}.wav"
        (FIXTURES / name).write_bytes(to_wav(np.clip(ep * 32768, -32768, 32767).astype(np.int16)))
        info["positives"].append({"file": name, "phrase_end": round(end / SR, 3), "voice": row["voice"],
                                  "text": row["text"]})
    parts = []
    for row in smp.near[:12] + smp.speech[:2]:
        parts += [np.zeros(int(0.5 * SR), np.float32), clip_audio(smp.audio, row)]
    neg = augment.corrupt(np.concatenate(parts), rng, smp.babble_pool(rng), strength=0.5)
    (FIXTURES / "negative.wav").write_bytes(to_wav(np.clip(neg * 32768, -32768, 32767).astype(np.int16)))
    (FIXTURES / "fixtures.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")


def installation_failures(report: dict, min_recall: float = 0.8, max_fa: float = 0.5) -> list[str]:
    """Deployment gate at the validation-chosen threshold, without tuning on the test set.

    False accepts/hour is an observed synthetic rate. Short recordings do not establish that rate with
    statistical confidence, and these checks do not establish real-microphone performance.
    """
    failures = []
    for split in ("val", "test"):
        result = report.get(split, {})
        recall, fa = result.get("recall", float("nan")), result.get("fa_per_hour", float("nan"))
        if not math.isfinite(recall) or not min_recall <= recall <= 1:
            failures.append(f"{split}: recall must be at least {min_recall:.2f}")
        if not math.isfinite(fa) or not 0 <= fa <= max_fa:
            failures.append(f"{split}: observed false accepts/hour must be at most {max_fa:.2f}")
        hours = result.get("hours", 0)
        if result.get("episodes", 0) <= 0 or not math.isfinite(hours) or hours <= 0:
            failures.append(f"{split}: positive episodes and negative audio are required")
    return failures


def install(path: Path, report: dict, curve: list, min_recall: float = 0.8, max_fa: float = 0.5) -> None:
    """Check the held-out result before creating fixtures or replacing any installed model."""
    failures = installation_failures(report, min_recall, max_fa)
    if failures:
        raise ValueError("Wake model was not installed: " + "; ".join(failures))
    with np.load(path) as z:
        arrays = {key: z[key] for key in z.files}
    meta = json.loads(str(arrays["meta"]))
    meta.update({"threshold": report["threshold"], "fa_curve": curve,
                 "val": report["val"], "test": report["test"]})
    arrays["meta"] = np.array(json.dumps(meta))
    write_fixtures()
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=MODEL_PATH.parent, suffix=".npz", delete=False) as output:
            temporary = Path(output.name)
            np.savez(output, **arrays)
        os.replace(temporary, MODEL_PATH)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", type=Path, default=WORK / "wake_candidate.npz")
    ap.add_argument("--target-fa", type=float, default=0.3, help="max false accepts per hour on val voices")
    ap.add_argument("--passes", type=int, default=2, help="negative stream passes (new noise each)")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--min-recall", type=float, default=0.8, help="minimum val and test recall for installation")
    ap.add_argument("--max-fa", type=float, default=0.5, help="maximum observed val and test false accepts/hour")
    args = ap.parse_args()

    val = evaluate_split(args.model, "val", args.passes)
    thr, curve = choose_threshold(val, args.target_fa)
    val_m = metrics(val, thr)
    print(f"val: threshold {thr:.2f} -> {json.dumps(val_m)}", flush=True)
    test = evaluate_split(args.model, "test", args.passes)
    test_m = metrics(test, thr)
    print(f"test: {json.dumps(test_m)}")
    # The test set is scored exactly once at the validation-chosen threshold. Do not print a test sweep
    # that invites selecting a different threshold after seeing held-out answers.
    report = {"model": str(args.model), "threshold": thr, "val": val_m, "test": test_m,
              "val_curve": curve,
              "evidence_limit": "Synthetic held-out voices only; observed FA/hour has limited recording exposure. "
                                "Real microphone performance has not been established."}
    failures = installation_failures(report, args.min_recall, args.max_fa)
    report["install_gate"] = {"min_recall": args.min_recall, "max_fa_per_hour": args.max_fa,
                              "eligible": not failures, "failures": failures}
    WORK.mkdir(exist_ok=True)
    (WORK / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    if args.install:
        try:
            install(args.model, report, curve, args.min_recall, args.max_fa)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print(f"installed {MODEL_PATH} with threshold {thr:.2f}")


if __name__ == "__main__":
    main()
