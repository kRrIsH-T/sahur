"""Wake word runtime: features, streaming, loading, and the shipped model on held-out voices."""

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from bolos import wake
from bolos.wake import (
    FRAME,
    HOP,
    MODEL_PATH,
    N_MELS,
    StreamingLogMel,
    WakeDetector,
    WakeNet,
    log_mel,
    personal_threshold,
)

FIXTURES = Path(__file__).resolve().parent.parent / "train" / "wake" / "fixtures"


def reference_log_mel(x: np.ndarray) -> np.ndarray:
    """Straightforward per-frame implementation written independently of bolos.wake."""
    x = x.astype(np.float64) / 32768.0
    n_frames = 1 + (len(x) - 400) // 160
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)

    def mel(f):
        return 2595 * np.log10(1 + f / 700)

    def hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    edges = hz(np.linspace(mel(60), mel(7600), 42))
    freqs = np.arange(257) * 16000 / 512
    bank = np.zeros((40, 257))
    for i in range(40):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        for j, f in enumerate(freqs):
            if lo < f <= mid:
                bank[i, j] = (f - lo) / (mid - lo)
            elif mid < f < hi:
                bank[i, j] = (hi - f) / (hi - mid)
    out = np.zeros((n_frames, 40))
    for t in range(n_frames):
        spec = np.fft.rfft(x[t * 160: t * 160 + 400] * window, 512)
        out[t] = np.log(bank @ np.abs(spec) ** 2 + 1e-6)
    return out


def noise_pcm(seconds: float, seed: int = 0, scale: float = 2000) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * 16000)) * scale).astype(np.int16)


def random_detector(threshold: float = 0.5) -> WakeDetector:
    """A detector with random weights and the shipped architecture, so streaming tests need no model file."""
    rng = np.random.default_rng(3)
    arrays = {"norm_mean": np.full(N_MELS, -8.0, np.float32), "norm_std": np.full(N_MELS, 3.0, np.float32)}
    convs = [(40, 16, 5, 2, 1), (16, 16, 3, 1, 2), (16, 24, 3, 2, 1)]
    for i, (cin, cout, k, s, d) in enumerate(convs):
        arrays[f"conv{i}.w"] = (rng.standard_normal((cout, cin, k)) / np.sqrt(cin * k)).astype(np.float32)
        arrays[f"conv{i}.b"] = np.zeros(cout, np.float32)
        arrays[f"conv{i}.sd"] = np.array([s, d], np.int32)
    arrays["fc0.w"] = (rng.standard_normal((8, 48)) / 7).astype(np.float32)
    arrays["fc0.b"] = np.zeros(8, np.float32)
    arrays["fc1.w"] = rng.standard_normal((1, 8)).astype(np.float32)
    arrays["fc1.b"] = np.zeros(1, np.float32)
    return WakeDetector(WakeNet(arrays), threshold)


def run(det: WakeDetector, pcm: np.ndarray, sizes) -> tuple[list[int], list[float]]:
    """Feed pcm in chunks cycling through `sizes`; return trigger frame counts and every evaluated score."""
    det.reset()
    triggers, scores, i, k = [], [], 0, 0
    evaluate = det._evaluate

    def recording_evaluate():
        fired = evaluate()
        scores.append(det.score)
        if fired:
            triggers.append(det.frames)
        return fired

    det._evaluate = recording_evaluate
    try:
        while i < len(pcm):
            n = sizes[k % len(sizes)]
            det.feed(pcm[i:i + n])
            i += n
            k += 1
    finally:
        del det._evaluate
    return triggers, scores


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


# ---- features -----------------------------------------------------------------------------------------------

def test_log_mel_matches_reference_implementation():
    pcm = noise_pcm(1.0) + (3000 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.int16)
    ours = log_mel(pcm)
    ref = reference_log_mel(pcm)
    assert ours.shape == ref.shape == (1 + (16000 - FRAME) // HOP, 40)
    np.testing.assert_allclose(ours, ref, atol=2e-3)


def test_pure_tone_lands_in_the_right_mel_band():
    tone = (8000 * np.sin(2 * np.pi * 1000 * np.arange(8000) / 16000)).astype(np.int16)
    peak = int(np.argmax(log_mel(tone).mean(axis=0)))
    centers = 700 * (10 ** (np.linspace(2595 * np.log10(1 + 60 / 700), 2595 * np.log10(1 + 7600 / 700), 42)[1:-1]
                            / 2595) - 1)
    assert abs(centers[peak] - 1000) < 80


def test_streaming_features_equal_whole_clip_features_for_odd_chunks():
    pcm = noise_pcm(2.3, seed=1)
    stream = StreamingLogMel()
    rng = np.random.default_rng(0)
    parts, i = [], 0
    while i < len(pcm):
        n = int(rng.integers(1, 2000))
        parts.append(stream.push(pcm[i:i + n]))
        i += n
    np.testing.assert_allclose(np.concatenate(parts), log_mel(pcm), atol=1e-4)


def test_accepts_float_and_column_input():
    pcm = noise_pcm(0.5, seed=2)
    np.testing.assert_allclose(log_mel(pcm.reshape(-1, 1)), log_mel(pcm))
    np.testing.assert_allclose(log_mel(pcm.astype(np.float32) / 32768), log_mel(pcm), atol=1e-5)


# ---- detector mechanics (random weights) ----------------------------------------------------------------------

@pytest.mark.parametrize("sizes", [[1280], [1], [7, 333, 4096], [160], [16000 * 5]])
def test_streaming_gives_the_same_scores_for_any_chunking(sizes):
    pcm = noise_pcm(5.0, seed=4)
    det = random_detector()
    triggers, scores = run(det, pcm, [1280])
    triggers2, scores2 = run(det, pcm, sizes)
    assert triggers2 == triggers
    np.testing.assert_allclose(scores2, scores, atol=1e-5)  # batched FFTs differ only in rounding
    assert len(scores) == (1 + (len(pcm) - FRAME) // HOP) // det.every


def test_triggers_once_then_rearms_after_refractory():
    det = random_detector(threshold=0.0)  # every evaluation is "above threshold"
    assert det.feed(noise_pcm(0.2)) is True
    assert not det.feed(noise_pcm(1.0))  # still the same "phrase": score never dropped
    det.threshold = 2.0
    det.feed(noise_pcm(0.1))  # score drops below: re-armed
    det.threshold = 0.0
    assert det.feed(noise_pcm(1.0, seed=5)) is True  # refractory (1.5 s) has passed since the first trigger
    det.reset()
    assert det.score == 0.0 and det.frames == 0


def test_load_returns_none_for_missing_file(tmp_path):
    assert WakeDetector.load(tmp_path / "nope.npz") is None


def test_personal_threshold_is_bounded():
    det = random_detector(threshold=0.6)
    det.model_threshold = 0.6
    det.curve = [(0.3, 5.0), (0.4, 0.8), (0.5, 0.2), (0.6, 0.1)]
    assert personal_threshold(det, [0.95, 0.97, 0.9, 0.99, 0.96]) == pytest.approx(0.6)  # never above the model's
    assert personal_threshold(det, [0.55, 0.6, 0.62, 0.7, 0.65]) == pytest.approx(0.8 * 0.6)
    assert personal_threshold(det, [0.1, 0.2, 0.15, 0.3, 0.2]) == pytest.approx(0.4)  # FA/h floor


# ---- the shipped model ---------------------------------------------------------------------------------------

needs_model = pytest.mark.skipif(not MODEL_PATH.exists(), reason="no trained wake model installed")


@needs_model
def test_shipped_model_loads_with_its_threshold(tmp_path):
    det = WakeDetector.load(MODEL_PATH, personal=False)
    assert 0.0 < det.threshold < 1.0
    assert det.window == 160 and det.every == 8
    meta = json.loads(str(np.load(MODEL_PATH)["meta"]))
    assert meta["test"]["recall"] > 0.8


@needs_model
def test_personal_profile_is_applied_only_for_the_same_model(tmp_path):
    det = WakeDetector.load(MODEL_PATH, personal=False)
    profile = tmp_path / "wake_profile.json"
    profile.write_text(json.dumps({"model_id": det.model_id, "threshold": 0.123}))
    assert WakeDetector.load(MODEL_PATH, profile=profile).threshold == pytest.approx(0.123)
    profile.write_text(json.dumps({"model_id": "other", "threshold": 0.123}))
    assert WakeDetector.load(MODEL_PATH, profile=profile).threshold == pytest.approx(det.threshold)


@needs_model
def test_triggers_on_held_out_voice_and_not_on_negatives():
    info = json.loads((FIXTURES / "fixtures.json").read_text(encoding="utf-8"))
    det = WakeDetector.load(MODEL_PATH, personal=False)
    for item in info["positives"]:
        pcm = read_wav(FIXTURES / item["file"])
        triggers, _ = run(det, pcm, [1280])
        assert len(triggers) == 1, item
        t = ((triggers[0] - 1) * HOP + FRAME) / 16000
        assert item["phrase_end"] - 0.3 <= t <= item["phrase_end"] + 1.0
        assert run(det, pcm, [37, 1001, 4800])[0] == triggers  # odd chunk sizes: the same decision
    negative = read_wav(FIXTURES / info["negative"])
    assert run(det, negative, [1280])[0] == []


@needs_model
def test_cpu_cost_is_small():
    import time

    det = WakeDetector.load(MODEL_PATH, personal=False)
    pcm = noise_pcm(10.0, seed=9)
    start = time.process_time()
    run(det, pcm, [1280])
    assert (time.process_time() - start) / 10.0 < 0.05  # under 5% of one core even on a slow test machine


def test_module_exposes_cli():
    assert callable(wake.main)
