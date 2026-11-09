"""Wake word "Hey Bol OS": a small keyword-spotting model trained from scratch, run in numpy on the mic stream.

    uv run python -m bolos.wake live      # print live scores from the microphone (Ctrl+C to stop)
    uv run python -m bolos.wake enroll    # say the phrase 5 times; adapts the threshold to your voice and mic
    uv run python -m bolos.wake bench     # CPU cost on 60 s of audio

Pipeline: 16 kHz int16 samples -> 25 ms Hann frames every 10 ms -> 40 log-mel energies (computed incrementally
as samples arrive) -> every 8 frames (80 ms) a 1D CNN reads the last 160 frames (1.6 s) and outputs the
probability that the phrase has just ended. A trigger fires when the score reaches the threshold, then the
detector stays quiet for a refractory period and until the score drops again, so one phrase is one trigger.

Evaluation happens at fixed frame counts, so the result does not depend on how the audio is chunked. The
model and its threshold come from train/wake/train.py (bolos/fast/models/wake.npz); a personal threshold from
`enroll` is kept in wake_profile.json next to preferences.json. Training details and benchmark numbers are in
train/wake/README.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
FRAME = 400  # 25 ms
HOP = 160  # 10 ms
NFFT = 512
N_MELS = 40
FMIN, FMAX = 60.0, 7600.0
LOG_FLOOR = 1e-6
MODEL_PATH = Path(__file__).resolve().parent / "fast" / "models" / "wake.npz"
PROFILE_NAME = "wake_profile.json"


def mel_filterbank(warp: float = 1.0) -> np.ndarray:
    """(N_MELS, NFFT//2+1) triangular HTK-mel filters. `warp` scales the filter frequencies (training uses it
    to imitate other vocal tract lengths); the runtime always uses 1.0."""
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = mel_to_hz(np.linspace(hz_to_mel(FMIN), hz_to_mel(FMAX), N_MELS + 2)) * warp
    edges = np.clip(edges, 0.0, SAMPLE_RATE / 2)
    bins = np.arange(NFFT // 2 + 1) * SAMPLE_RATE / NFFT
    lo, mid, hi = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    up = (bins[None] - lo) / np.maximum(mid - lo, 1e-6)
    down = (hi - bins[None]) / np.maximum(hi - mid, 1e-6)
    return np.maximum(0.0, np.minimum(up, down)).astype(np.float32)


_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / FRAME)).astype(np.float32)  # periodic Hann
_MEL = mel_filterbank()


def to_float(samples: np.ndarray) -> np.ndarray:
    """Mono float32 in [-1, 1] from int16 (or float) samples of any shape (n,) or (n, 1)."""
    x = np.asarray(samples).reshape(-1)
    if x.dtype == np.int16:
        return x.astype(np.float32) / 32768.0
    return x.astype(np.float32)


def log_mel_frames(frames: np.ndarray, mel: np.ndarray = _MEL) -> np.ndarray:
    """(n, FRAME) float samples -> (n, N_MELS) log-mel energies."""
    spec = np.fft.rfft(frames * _WINDOW, n=NFFT, axis=1)
    power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
    return np.log(power @ mel.T + LOG_FLOOR).astype(np.float32)


def log_mel(samples: np.ndarray, mel: np.ndarray = _MEL) -> np.ndarray:
    """Reference (whole-clip) features: frame i covers samples [i*HOP, i*HOP+FRAME)."""
    x = to_float(samples)
    n = 0 if len(x) < FRAME else 1 + (len(x) - FRAME) // HOP
    if n == 0:
        return np.zeros((0, N_MELS), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME)[::HOP][:n]
    return log_mel_frames(frames, mel)


class StreamingLogMel:
    """The same features as log_mel, computed as audio arrives: keeps the samples not yet framed."""

    def __init__(self):
        self.pending = np.zeros(0, np.float32)

    def reset(self) -> None:
        self.pending = np.zeros(0, np.float32)

    def push(self, samples: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.pending, to_float(samples)]) if len(self.pending) else to_float(samples)
        n = 0 if len(x) < FRAME else 1 + (len(x) - FRAME) // HOP
        if n == 0:
            self.pending = x
            return np.zeros((0, N_MELS), np.float32)
        frames = np.lib.stride_tricks.sliding_window_view(x, FRAME)[::HOP][:n]
        out = log_mel_frames(frames)
        self.pending = x[n * HOP:].copy()
        return out


class WakeNet:
    """numpy forward pass of the 1D CNN exported by train/wake/train.py (batch norm folded into the convs)."""

    def __init__(self, arrays: dict[str, np.ndarray]):
        self.mean = arrays["norm_mean"].astype(np.float32)
        self.inv_std = (1.0 / arrays["norm_std"]).astype(np.float32)
        self.convs = []
        i = 0
        while f"conv{i}.w" in arrays:
            w = arrays[f"conv{i}.w"].astype(np.float32)  # (out, in, k)
            stride, dilation = (int(v) for v in arrays[f"conv{i}.sd"])
            self.convs.append((w.reshape(w.shape[0], -1).T.copy(), arrays[f"conv{i}.b"].astype(np.float32),
                               w.shape[2], stride, dilation))
            i += 1
        self.fc = [(arrays[f"fc{j}.w"].astype(np.float32).T.copy(), arrays[f"fc{j}.b"].astype(np.float32))
                   for j in range(2)]

    def embed(self, feats: np.ndarray) -> np.ndarray:
        """(T, N_MELS) log-mel window -> pooled conv features (the input of the classifier head)."""
        x = ((feats - self.mean) * self.inv_std).T  # (C, T)
        for wt, b, k, stride, dilation in self.convs:
            span = (k - 1) * dilation + 1
            cols = np.lib.stride_tricks.sliding_window_view(x, span, axis=1)[:, ::stride, ::dilation]  # (C, T', k)
            cols = cols.transpose(1, 0, 2).reshape(cols.shape[1], -1)
            x = np.maximum(cols @ wt + b, 0.0).T  # (out, T')
        return np.concatenate([x.mean(axis=1), x.max(axis=1)])

    def __call__(self, feats: np.ndarray) -> float:
        h = self.embed(feats)
        (w1, b1), (w2, b2) = self.fc
        h = np.maximum(h @ w1 + b1, 0.0)
        logit = float((h @ w2 + b2)[0])
        return 1.0 / (1.0 + np.exp(-np.clip(logit, -60.0, 60.0)))


class WakeDetector:
    """Streaming detector. feed() int16 16 kHz audio of any length; True once per spoken wake phrase."""

    def __init__(self, net: WakeNet, threshold: float, window: int = 160, every: int = 8,
                 refractory: float = 1.5, model_id: str = ""):
        self.net = net
        self.threshold = float(threshold)
        self.model_threshold = float(threshold)
        self.window = window
        self.every = every
        self.refractory_frames = int(round(refractory * SAMPLE_RATE / HOP))
        self.model_id = model_id
        self.curve: list[tuple[float, float]] = []  # (threshold, false accepts per hour) on held-out speech
        self.features = StreamingLogMel()
        self.reset()

    @classmethod
    def load(cls, path: str | Path = MODEL_PATH, profile: str | Path | None = None,
             personal: bool = True) -> WakeDetector | None:
        """The detector from a wake.npz, or None if the file is missing. A personal threshold from `enroll`
        (wake_profile.json next to preferences.json, or `profile`) is applied when it was made for this model
        and `personal` is true."""
        path = Path(path)
        if not path.exists():
            return None
        raw = path.read_bytes()
        with np.load(path) as z:
            arrays = {k: z[k] for k in z.files}
        meta = json.loads(str(arrays["meta"]))
        det = cls(WakeNet(arrays), meta["threshold"], window=meta["window"], every=meta["every"],
                  refractory=meta["refractory"], model_id=hashlib.sha1(raw).hexdigest()[:16])
        det.curve = [tuple(p) for p in meta.get("fa_curve", [])]
        prof = load_profile(Path(profile) if profile else default_profile_path()) if personal else None
        if prof and prof.get("model_id") == det.model_id:
            det.threshold = float(prof["threshold"])
        return det

    def reset(self) -> None:
        """Forget all audio (call after the app was deaf, e.g. while it was speaking)."""
        self.features.reset()
        self.ring = np.full((self.window, N_MELS), np.log(LOG_FLOOR), np.float32)
        self.frames = 0
        self.score = 0.0
        self.quiet_until = 0  # frame count before which no trigger fires
        self.armed = True

    def feed(self, samples: np.ndarray) -> bool:
        """Add audio. True if the wake phrase ended in it (at most once per phrase)."""
        new = self.features.push(samples)
        fired = False
        pos = 0
        while pos < len(new):
            # Advance to the next evaluation point at most, so chunk sizes never change which windows are scored.
            take = min(self.every - self.frames % self.every, len(new) - pos)
            self.ring = np.concatenate([self.ring[take:], new[pos:pos + take]])
            self.frames += take
            pos += take
            if self.frames % self.every == 0:
                fired |= self._evaluate()
        return fired

    def _evaluate(self) -> bool:
        self.score = self.net(self.ring)
        if self.score < self.threshold:
            self.armed = True
            return False
        if not self.armed or self.frames < self.quiet_until:
            return False
        self.armed = False
        self.quiet_until = self.frames + self.refractory_frames
        return True


# ---- personal profile ---------------------------------------------------------------------------------------

def default_profile_path() -> Path:
    """wake_profile.json next to preferences.json (PREFERENCES_PATH, from the environment or .env)."""
    from bolos.config import ROOT  # importing bolos.config loads .env, which may set PREFERENCES_PATH

    prefs = os.environ.get("PREFERENCES_PATH")
    return (Path(prefs).parent if prefs else ROOT) / PROFILE_NAME


def load_profile(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def personal_threshold(det: WakeDetector, peaks: list[float], max_fa_per_hour: float = 1.0) -> float:
    """Threshold for one user from the peak scores of their enrollment takes.

    Aim just under their second-weakest take (one bad take should not drag it down), never above the model's
    own threshold, and never so low that the held-out false-accept rate exceeds `max_fa_per_hour`."""
    target = 0.8 * sorted(peaks)[1] if len(peaks) > 1 else 0.8 * peaks[0]
    floor = min((t for t, fa in det.curve if fa <= max_fa_per_hour), default=det.model_threshold)
    return float(min(det.model_threshold, max(target, floor)))


def peak_score(det: WakeDetector, samples: np.ndarray, chunk: int = 1280) -> float:
    det.reset()
    best = 0.0
    for i in range(0, len(samples), chunk):
        det.feed(samples[i:i + chunk])
        best = max(best, det.score)
    det.reset()
    return best


# ---- command line -------------------------------------------------------------------------------------------

def _require_model() -> WakeDetector:
    det = WakeDetector.load(MODEL_PATH)
    if det is None:
        sys.exit(f"no wake model at {MODEL_PATH}; train one with train/wake/train.py")
    return det


def _live(det: WakeDetector, seconds: float = 0.0) -> None:
    """Print the live score (and whole-process CPU) from the default microphone; stop after `seconds` if > 0."""
    import sounddevice as sd

    print(f"threshold {det.threshold:.3f} (model {det.model_threshold:.3f}); say 'Hey Bol OS'. Ctrl+C stops.")
    print("cpu = whole process (microphone included) as % of one core")
    wall0, cpu0 = time.perf_counter(), time.process_time()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=1280) as stream:
        try:
            while not seconds or time.perf_counter() - wall0 < seconds:
                data, _ = stream.read(1280)
                fired = det.feed(data)
                bar = "#" * int(det.score * 40)
                cpu = 100 * (time.process_time() - cpu0) / max(1e-3, time.perf_counter() - wall0)
                print(f"\r{det.score:5.3f} |{bar:<40}| cpu {cpu:4.1f}% {'WAKE!' if fired else '     '}", end="",
                      flush=True)
                if fired:
                    print()
        except KeyboardInterrupt:
            pass
    print()


def _record(seconds: float) -> np.ndarray:
    import sounddevice as sd

    data = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return data[:, 0]


def _enroll(det: WakeDetector, takes: int = 5) -> None:
    print("Say 'Hey Bol OS' once after each beep-free prompt, as you normally would.")
    peaks = []
    while len(peaks) < takes:
        input(f"[{len(peaks) + 1}/{takes}] press Enter, then say the phrase within 3 seconds...")
        audio = _record(3.0)
        level = float(np.sqrt(np.mean((audio.astype(np.float32) / 32768) ** 2)))
        if level < 0.003:
            print("  that was very quiet; check the microphone and try again")
            continue
        peak = peak_score(det, audio)
        print(f"  score {peak:.3f}")
        peaks.append(peak)
    thr = personal_threshold(det, peaks)
    path = default_profile_path()
    path.write_text(json.dumps({"model_id": det.model_id, "threshold": thr, "peaks": peaks,
                                "time": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2), encoding="utf-8")
    print(f"personal threshold {thr:.3f} (model default {det.model_threshold:.3f}) saved to {path}")
    if min(peaks) < thr:
        print("some takes scored below it; speak a little closer to the microphone, or enroll again")


def _bench(det: WakeDetector, seconds: int = 60) -> None:
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(seconds * SAMPLE_RATE) * 1000).astype(np.int16)
    det.reset()
    t0 = time.process_time()
    for i in range(0, len(audio), 1280):
        det.feed(audio[i:i + 1280])
    used = time.process_time() - t0
    print(f"{seconds} s of audio in {used:.2f} s CPU: {100 * used / seconds:.2f}% of one core")


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m bolos.wake", description="Hey Bol OS wake word")
    ap.add_argument("command", choices=["live", "enroll", "bench"])
    ap.add_argument("--seconds", type=float, default=0.0, help="live: stop after this many seconds")
    args = ap.parse_args(argv)
    det = _require_model()
    if args.command == "live":
        _live(det, args.seconds)
    else:
        {"enroll": _enroll, "bench": _bench}[args.command](det)


if __name__ == "__main__":
    main()
