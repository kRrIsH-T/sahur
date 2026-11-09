"""Waveform augmentation and synthetic noise for wake-word training (numpy, float32 at 16 kHz).

TTS clips are studio-clean; a laptop microphone is not. Every training window goes through a random chain
that imitates the distance between them: speed change (tempo and pitch together, like Kaldi's speed
perturbation), level, synthetic room reverb, microphone/laptop frequency colouring, background noise at
0-20 dB SNR (white, pink, brown, fan hum, babble from other voices, music-like tones), and clipping.
Vocal-tract (formant) changes without tempo change are done in the feature step by warping the mel filters
(train.py). All randomness comes from the caller's numpy Generator, so a window is reproducible.
"""

import numpy as np

SR = 16000


def resample(x: np.ndarray, factor: float) -> np.ndarray:
    """Play `factor` times faster (shorter, higher). Linear interpolation is enough for 0.85-1.15."""
    n = max(1, int(round(len(x) / factor)))
    return np.interp(np.arange(n) * factor, np.arange(len(x)), x).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2) + 1e-12))


def fft_size(n: int) -> int:
    """A power of two >= n: numpy's FFT is many times slower on lengths with large prime factors."""
    return 1 << max(0, n - 1).bit_length()


def shaped_noise(n: int, rng: np.random.Generator, exponent: float) -> np.ndarray:
    """Noise with power spectrum ~ 1/f^exponent (0 white, 1 pink, 2 brown)."""
    size = fft_size(n + 1)
    spec = np.fft.rfft(rng.standard_normal(size))
    f = np.arange(len(spec), dtype=np.float64)
    f[0] = 1.0
    out = np.fft.irfft(spec / f ** (exponent / 2), n=size)[:n]
    return (out / (rms(out) + 1e-9)).astype(np.float32)


def fan_hum(n: int, rng: np.random.Generator) -> np.ndarray:
    """Mains/fan hum: harmonics of a low fundamental plus slowly modulated low-passed air noise."""
    t = np.arange(n) / SR
    f0 = rng.uniform(45, 125)
    hum = sum(rng.uniform(0.1, 1.0) / k * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6.28)) for k in range(1, 7))
    air = shaped_noise(n, rng, rng.uniform(1.0, 2.0))
    mod = 1 + 0.3 * np.sin(2 * np.pi * rng.uniform(0.2, 3) * t)
    out = rng.uniform(0.2, 1.0) * hum / (rms(hum) + 1e-9) + air * mod
    return (out / rms(out)).astype(np.float32)


def music(n: int, rng: np.random.Generator) -> np.ndarray:
    """Music-like tones: a sequence of chords of harmonic notes with attack/decay, sometimes a beat."""
    out = np.zeros(n, np.float32)
    pos = 0
    while pos < n:
        dur = int(rng.uniform(0.12, 0.6) * SR)
        t = np.arange(dur) / SR
        env = np.minimum(1.0, t / 0.01) * np.exp(-t * rng.uniform(1, 8))
        for _ in range(rng.integers(1, 4)):
            f0 = 110 * 2 ** (rng.integers(0, 36) / 12)
            note = sum(0.6 ** k * np.sin(2 * np.pi * f0 * (k + 1) * t) for k in range(rng.integers(1, 6)))
            seg = (note * env).astype(np.float32)
            out[pos:pos + dur] += seg[:n - pos]
        if rng.random() < 0.4:  # percussion: a short noise burst
            k = min(int(0.05 * SR), n - pos)
            out[pos:pos + k] += rng.standard_normal(k).astype(np.float32) * np.exp(-np.arange(k) / 200) * 0.5
        pos += dur
    return (out / (rms(out) + 1e-9)).astype(np.float32)


def babble(n: int, rng: np.random.Generator, speech: list[np.ndarray]) -> np.ndarray:
    """Several other voices talking at once (crowd, TV)."""
    out = np.zeros(n, np.float32)
    for _ in range(rng.integers(3, 7)):
        clip = speech[rng.integers(len(speech))]
        if len(clip) <= n:
            seg = np.pad(clip, (0, n - len(clip)))
        else:
            start = rng.integers(0, len(clip) - n)
            seg = clip[start:start + n]
        out += seg * rng.uniform(0.3, 1.0) / (rms(seg) + 1e-9)
    return (out / (rms(out) + 1e-9)).astype(np.float32)


def noise(n: int, rng: np.random.Generator, speech: list[np.ndarray]) -> np.ndarray:
    """One of the background types, unit RMS."""
    kind = rng.choice(["white", "pink", "brown", "fan", "babble", "music"], p=[0.12, 0.18, 0.12, 0.18, 0.25, 0.15])
    if kind == "babble" and speech:
        return babble(n, rng, speech)
    if kind == "fan":
        return fan_hum(n, rng)
    if kind == "music":
        return music(n, rng)
    return shaped_noise(n, rng, {"white": 0.0, "pink": 1.0, "brown": 2.0}.get(kind, 1.0))


def fft_filter(x: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """Apply a zero-phase magnitude response given on the rfft grid of fft_size(len(x))."""
    size = fft_size(len(x))
    return np.fft.irfft(np.fft.rfft(x, size) * gain, n=size)[: len(x)].astype(np.float32)


def mic_colour(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Band-limit and tilt the spectrum like a cheap laptop mic or headset: high-pass, low-pass, a few
    smooth peaks and dips."""
    f = np.fft.rfftfreq(fft_size(len(x)), 1 / SR)
    hp, lp = rng.uniform(60, 400), rng.uniform(3200, 7800)
    gain = 1 / np.sqrt(1 + (hp / np.maximum(f, 1)) ** (2 * rng.integers(1, 4)))
    gain *= 1 / np.sqrt(1 + (f / lp) ** (2 * rng.integers(2, 6)))
    for _ in range(rng.integers(1, 4)):
        fc, width, db = rng.uniform(200, 6000), rng.uniform(0.2, 1.0), rng.uniform(-8, 8)
        gain *= 10 ** (db / 20 * np.exp(-0.5 * (np.log2(np.maximum(f, 1) / fc) / width) ** 2))
    return fft_filter(x, gain)


def reverb(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Convolve with a synthetic room impulse response: direct path, sparse early reflections and an
    exponentially decaying diffuse tail (RT60 0.15-0.9 s)."""
    rt60 = rng.uniform(0.15, 0.9)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    tail = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    tail[: int(0.003 * SR)] = 0
    ir = tail * 10 ** (-rng.uniform(0, 12) / 20) / (rms(tail) * np.sqrt(n) + 1e-9)
    ir[0] = 1.0
    for _ in range(rng.integers(2, 7)):
        ir[rng.integers(int(0.002 * SR), int(0.03 * SR))] += rng.uniform(-0.6, 0.6)
    m = len(x) + n - 1
    size = 1 << (m - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]
    return (y * rms(x) / (rms(y) + 1e-9)).astype(np.float32)


def corrupt(x: np.ndarray, rng: np.random.Generator, speech: list[np.ndarray], strength: float = 1.0) -> np.ndarray:
    """The room/mic/noise chain on a clean float waveform (same length out). strength < 1 makes milder audio."""
    x = x.astype(np.float32)
    if rng.random() < 0.5 * strength:
        x = reverb(x, rng)
    if rng.random() < 0.6:
        x = mic_colour(x, rng)
    level = 10 ** (rng.uniform(-35, -8) / 20)  # speech RMS from very quiet to loud, in dBFS
    x = x * level / (rms(x) + 1e-9)
    if rng.random() < 0.85 * strength:
        snr = rng.uniform(0, 20) if rng.random() < 0.7 else rng.uniform(20, 35)
        nz = noise(len(x), rng, speech)
        if rng.random() < 0.5:
            nz = mic_colour(nz, rng)
        x = x + nz * level * 10 ** (-snr / 20) / (rms(nz) + 1e-9)
    # A microphone always has some self-noise; real input is never digital silence.
    x = x + rng.standard_normal(len(x)).astype(np.float32) * 10 ** (rng.uniform(-80, -55) / 20)
    if rng.random() < 0.08 * strength:
        x = np.clip(x * 10 ** (rng.uniform(6, 18) / 20), -1, 1)
    return np.clip(x, -1, 1).astype(np.float32)


def background(n: int, rng: np.random.Generator, speech: list[np.ndarray]) -> np.ndarray:
    """A window with no speech: silence (mic self-noise) or a noise/music background at a random level."""
    if rng.random() < 0.15:
        return (rng.standard_normal(n) * 10 ** (rng.uniform(-80, -50) / 20)).astype(np.float32)
    x = noise(n, rng, speech)
    if rng.random() < 0.5:
        x = mic_colour(x, rng)
    x = x * 10 ** (rng.uniform(-60, -12) / 20) / (rms(x) + 1e-9)
    return np.clip(x, -1, 1).astype(np.float32)
