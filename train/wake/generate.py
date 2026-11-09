"""Synthesise the wake-word corpus with Timbre (cached, resumable, within a request budget).

    uv run python train/wake/generate.py --dry-run     # print the plan and how many requests it needs
    uv run python train/wake/generate.py               # synthesise what is missing

Voices are split into train / val / test as whole voices, so the reported recall and false-accept rate are
for voices the model never heard. The plan is deterministic (seeded): re-running it asks only for clips
that are not on disk yet.
"""

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
if not __package__:  # run as a script: drop this folder from the path so train/wake/train.py cannot shadow the
    # "train" package, and import the siblings as train.wake.*
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT / "train" / "wake"]
    sys.path.insert(0, str(ROOT))

from train.wake.texts import LANGS, NEAR_MISSES, PHRASES, SENTENCES  # noqa: E402
from train.wake.tts import Clip, Synth, failed_keys  # noqa: E402

V3 = "timbre-v3.0"
V25 = "timbre-v2.5"
SPEEDS = [0.85, 1.0, 1.15]  # Timbre's range; training widens it with resampling

# Timbre v3.0: every voice speaks all 10 languages.
V3_VOICES = {
    "train": ["Shivani", "Lakshmi", "Kalpana", "Chetana", "Vasudha", "Avinash", "Shubham", "Yashraj"],
    "val": ["Riyaa", "Prakash"],
    "test": ["Tarika", "Harshad", "Devansh"],
}
# Timbre v2.5: one preferred language per voice.
V25_LANG = {
    "Kaveri": "en-IN", "Trupti": "en-IN", "Devika": "en-IN", "Pranav": "en-IN", "Shlok": "en-IN", "Girish": "en-IN",
    "Nalini": "hi-IN", "Bhavna": "hi-IN", "Yashvi": "hi-IN", "Urmila": "hi-IN", "Jwala": "hi-IN", "Chitra": "hi-IN",
    "Ambuja": "hi-IN", "Deepak": "hi-IN", "Roopesh": "hi-IN", "Vikrant": "hi-IN", "Hemraj": "hi-IN",
    "Jalaj": "hi-IN", "Omkar": "hi-IN", "Poorvi": "hi-en",
    "Asmita": "ta-IN", "Trisha": "ta-IN", "Brinda": "ta-IN", "Vedika": "ta-IN", "Noopur": "ta-IN",
    "Suhana": "te-IN", "Lehara": "te-IN", "Lavanya": "te-IN", "Yukti": "te-IN", "Varuni": "te-IN",
    "Saanvi": "kn-IN", "Kavin": "kn-IN", "Reshma": "ml-IN", "Riyaan": "ml-IN", "Zahira": "mr-IN", "Ishaan": "mr-IN",
    "Kirra": "bn-IN", "Dhruva": "bn-IN", "Falak": "gu-IN", "Veera": "gu-IN", "Mehuli": "pa-IN", "Zayan": "pa-IN",
}
V25_SPLIT = {
    **{v: "test" for v in ["Bhavna", "Pranav", "Trisha", "Lavanya", "Kavin", "Riyaan", "Ishaan", "Kirra", "Veera",
                           "Zayan"]},
    **{v: "val" for v in ["Deepak", "Shlok", "Noopur", "Yukti", "Omkar"]},
}
SPLITS = ("train", "val", "test")
PARAGRAPHS = {"train": 520, "val": 120, "test": 420}
NEAR_PER_SPLIT = {"train": 3, "val": 1, "test": 2}


def v25_split(voice: str) -> str:
    return V25_SPLIT.get(voice, "train")


def stable(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


def phrase_lang(lang: str) -> str:
    """Phrase spelling table to use for a voice language ("hi-en" is Hinglish: Latin spelling)."""
    return "en-IN" if lang == "hi-en" else lang


def positives() -> list[Clip]:
    clips = []
    for split, voices in V3_VOICES.items():
        for voice in voices:
            for lang in LANGS:
                for speed, text in zip(SPEEDS, PHRASES[lang], strict=True):
                    clips.append(Clip(V3, voice, lang, text, speed, "positive", split))
    rng = random.Random(1)
    for voice, lang in V25_LANG.items():
        split = v25_split(voice)
        for speed, text in zip(SPEEDS, PHRASES[phrase_lang(lang)], strict=True):
            clips.append(Clip(V25, voice, lang, text, speed, "positive", split))
        if lang not in ("en-IN", "hi-en"):
            clips.append(Clip(V25, voice, "en-IN", rng.choice(PHRASES["en-IN"]), rng.choice(SPEEDS), "positive", split))
    return clips


def voices_for(lang: str, split: str) -> list[tuple[str, str, str]]:
    """(model, voice, language setting) that can speak `lang` well, from one split."""
    out = [(V3, v, lang) for v in V3_VOICES[split]]
    for voice, pref in V25_LANG.items():
        if v25_split(voice) != split:
            continue
        if pref == lang or (lang == "en-IN" and pref in ("en-IN", "hi-en", "hi-IN")):
            out.append((V25, voice, "en-IN" if lang == "en-IN" and pref != "hi-en" else pref))
    return out


def near_misses() -> list[Clip]:
    rng = random.Random(2)
    clips = []
    for lang, text in NEAR_MISSES:
        for split, n in NEAR_PER_SPLIT.items():
            pool = voices_for(lang, split)
            for model, voice, setting in rng.sample(pool, min(n, len(pool))):
                clips.append(Clip(model, voice, setting, text, rng.choice(SPEEDS), "near", split))
    return clips


def dataset_commands() -> dict[str, list[str]]:
    """Bol OS training commands (never the frozen test sets), bucketed by script: Latin vs Devanagari etc."""
    buckets: dict[str, list[str]] = {"latin": [], "deva": []}
    path = ROOT / "train" / "data" / "dataset.jsonl"
    if not path.exists():
        return buckets
    for line in path.read_text(encoding="utf-8").splitlines():
        text = json.loads(line)["text"].strip()
        if len(text) < 6:
            continue
        if all(ord(c) < 0x250 for c in text):
            buckets["latin"].append(text)
        elif any(0x900 <= ord(c) < 0x980 for c in text):
            buckets["deva"].append(text)
    return buckets


def sentence_pool(bucket: str, split: str, commands: dict[str, list[str]]) -> list[str]:
    """Sentences for one language bucket. Test voices get a disjoint third of the sentences."""
    def keep(i: int, text: str) -> bool:
        return (i % 3 == 2) == (split == "test")

    if bucket == "latin":
        return [t for t in commands["latin"] if (stable(t) % 3 == 2) == (split == "test")]
    pool = [t for i, t in enumerate(SENTENCES[bucket]) if keep(i, t)]
    if bucket == "hi-IN":
        pool += [t for t in commands["deva"] if (stable(t) % 3 == 2) == (split == "test")][:150]
    return pool


def paragraphs() -> list[Clip]:
    rng = random.Random(3)
    commands = dataset_commands()
    buckets = ["latin"] * 4 + LANGS + ["en-IN", "hi-IN"]  # romanised commands are what Bol OS hears most
    clips, seen = [], set()
    for split, count in PARAGRAPHS.items():
        made = 0
        while made < count:
            bucket = rng.choice(buckets)
            pool = sentence_pool(bucket, split, commands)
            text = " ".join(t if t[-1] in ".?!।" else t + "." for t in rng.sample(pool, rng.randint(4, 6)))
            if text in seen:
                continue
            seen.add(text)
            lang = rng.choice(["hi-IN", "en-IN"]) if bucket == "latin" else bucket
            model, voice, setting = rng.choice(voices_for(lang, split))
            if bucket == "latin" and model == V25 and V25_LANG[voice] == "hi-en":
                setting = "hi-en"
            clips.append(Clip(model, voice, setting, text, rng.choice(SPEEDS), "speech", split))
            made += 1
    return clips


def plan() -> list[Clip]:
    return positives() + near_misses() + paragraphs()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    ap.add_argument("--budget", type=int, default=2300, help="max Timbre requests in this run")
    args = ap.parse_args()
    clips = plan()
    failed = failed_keys()
    missing = [c for c in clips if not c.path.exists() and c.key not in failed]
    print(Counter((c.kind, c.split) for c in clips))
    print(f"{len(clips)} clips planned, {len(missing)} to synthesise, {len(failed)} known failures")
    if args.dry_run:
        return
    synth = Synth(budget=args.budget)
    start = time.monotonic()
    for i, clip in enumerate(missing, 1):
        if not synth.ensure(clip) and synth.requests >= synth.budget:
            print("budget spent")
            break
        if i % 25 == 0:
            rate = synth.requests / (time.monotonic() - start)
            print(f"{i}/{len(missing)} done, {synth.requests} requests, {rate:.2f}/s", flush=True)
    print(f"finished: {synth.requests} requests")


if __name__ == "__main__":
    main()
