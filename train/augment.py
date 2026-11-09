"""Training-time variants of a command: romanisation spellings, speech-recognition noise, Prisma-style casing.

Each transform is small and label-preserving: it changes how a command is written or heard, never what
it asks for. Word deletion only drops filler words, so "Raju ko message karo" can't lose "message".
"""

import random
import re

# Pairs of interchangeable romanised spellings, applied in either direction.
SPELLING_SWAPS = [
    ("aa", "a"), ("ee", "i"), ("oo", "u"), ("w", "v"), ("sh", "s"), ("ph", "f"), ("z", "j"),
    ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"), ("bh", "b"), ("ch", "c"), ("ai", "e"),
    ("au", "o"), ("ou", "u"), ("y", "i"), ("q", "k"), ("x", "ks"),
]
DOUBLE = re.compile(r"([bcdfgjklmnprstvz])\1")
FILLERS = {"zara", "please", "plz", "na", "yaar", "jara", "ek", "ondu", "konjam", "toh", "to", "bhai",
           "ji", "sir", "madam", "ok", "okay", "abhi", "jaldi", "just", "once", "do", "one", "thing"}
ADD_FILLERS = ["zara", "please", "na", "yaar", "abhi", "ek baar", "jaldi", "konjam", "ondu", "ok"]


def _swap_spelling(word: str, rng: random.Random) -> str:
    a, b = rng.choice(SPELLING_SWAPS)
    if a in word and rng.random() < 0.5:
        return word.replace(a, b, 1)
    if b in word and len(b) > 1:
        return word.replace(b, a, 1)
    return word


def _vary_doubles(word: str, rng: random.Random) -> str:
    if DOUBLE.search(word):
        return DOUBLE.sub(r"\1", word, count=1)
    i = rng.randrange(len(word)) if word else 0
    return word[:i] + word[i] + word[i:] if word and word[i].isalpha() and word[i] not in "aeiou" else word


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 4 or not word.isascii():
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.random()
    if op < 0.4:
        return word[:i] + word[i + 1:]
    if op < 0.7:
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    return word[:i] + rng.choice("aeiou") + word[i + 1:]


def augment(text: str, rng: random.Random) -> str:
    words = text.split()
    if not words:
        return text
    out = []
    for w in words:
        lw = w.lower()
        if lw in FILLERS and rng.random() < 0.3:
            continue  # recognisers often drop short filler words
        r = rng.random()
        if w.isascii() and r < 0.18:
            w = _swap_spelling(w, rng)
        elif w.isascii() and r < 0.26:
            w = _vary_doubles(w, rng)
        elif r < 0.30:
            w = _typo(w, rng)
        out.append(w)
        if rng.random() < 0.02:
            out.append(w)  # stutter / repeated word
    if rng.random() < 0.15:
        out.insert(rng.randrange(len(out) + 1), rng.choice(ADD_FILLERS))
    text = " ".join(out) or text
    style = rng.random()
    if style < 0.35:
        text = text[:1].upper() + text[1:] + "."  # Prisma's sentence style
    elif style < 0.5:
        text = text.lower()
    elif style < 0.55:
        text = text.upper() if text.isascii() else text
    return text
