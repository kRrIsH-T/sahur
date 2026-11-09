"""Tokens with their place in the original text, and per-token hashed features for the slot tagger.

Shared by training and runtime. A slot is read back as the original text between its first and last
token, so "Spotify pe Radiohead chalao." gives query "Radiohead" exactly as heard.
"""

import re
import unicodedata

import numpy as np

from bolos.fast.features import _hash, fold, normalize

# Runs of letters, digits, Indic letters with their vowel signs and viramas, and joiners inside a word;
# any other non-space character (including the danda) is a token of its own.
_WORD = "\\w\u0900-\u0963\u0966-\u0dff\u200c\u200d"
_TOKEN = re.compile(f"[{_WORD}]+|[^\\s{_WORD}]")


# Case endings that Kannada, Tamil, Telugu, Malayalam, Marathi and Bengali join to the word ("Ravige",
# "Sureshku", "அம்மாவுக்கு", "ಅಮ್ಮನಿಗೆ", "राहुलला", "মাকে", "ஸ்பாட்டிஃபைல"). The word is cut before every
# ending that matches, since the stem is ambiguous ("Ravi-ge" but "Suresh-ige"), so a slot can be just the
# name. Cutting a word that merely ends this way is harmless: a slot can still cover all the pieces.
_SUFFIXES = [
    "vukku", "ukku", "kku", "ku", "nige", "ige", "ge", "kke", "ke", "kitta", "ki", "nalli", "alli", "la",
    "ನಿಗೆ", "ಿಗೆ", "ಂಗೆ", "ಗೆ", "ಕ್ಕೆ", "ನಲ್ಲಿ", "ಲ್ಲಿ",
    "வுக்கு", "ுக்கு", "க்கு", "கு", "கிட்ட", "ல", "ில்",
    "కి", "కు", "లో",
    "യിൽ", "ിൽ", "ൽ", "വിന്", "ിന്", "ക്ക്", "യ്ക്ക്",
    "কে", "তে", "ের", "ে",
    "ला", "ंना", "ना", "ची", "चा", "चे",
]


def _cuts(word: str) -> list[int]:
    """Offsets inside word where a case ending starts."""
    lower = word.casefold()
    min_stem = 3 if word.isascii() else 2
    return sorted({len(word) - len(s) for s in _SUFFIXES if lower.endswith(s) and len(word) - len(s) >= min_stem})


def tokenize(text: str) -> list[tuple[int, int]]:
    """(start, end) of each token in text (which should already be NFC)."""
    spans = []
    for m in _TOKEN.finditer(text):
        start, end = m.span()
        bounds = [start, *(start + c for c in _cuts(m.group())), end]
        spans += list(zip(bounds, bounds[1:], strict=False))
    return spans


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _shape(tok: str) -> str:
    if not tok[0].isalnum() and not "ऀ" <= tok[0] <= "෿":
        return "punct"
    if tok.isdigit():
        return "digit"
    if not tok.isascii():
        return "indic"
    if tok.isupper() and len(tok) > 1:
        return "caps"
    return "title" if tok[0].isupper() else "lower"


def token_features(tokens: list[str], buckets: int, char_n: tuple[int, int] = (2, 4)) -> list[np.ndarray]:
    """Hashed feature ids for each token: the word, its folded spelling, character n-grams, shape, and
    position. Context comes from the tagger's recurrent layer, not from here."""
    out = []
    last = len(tokens) - 1
    for i, raw in enumerate(tokens):
        w = normalize(raw) or raw
        fw = fold(w)
        feats = [f"w:{w}", f"fw:{fw}", f"s:{_shape(raw)}", f"len:{min(len(w), 8)}"]
        if i == 0:
            feats.append("first")
        if i == last:
            feats.append("last")
        padded = f"<{fw}>"
        lo, hi = char_n
        for n in range(lo, hi + 1):
            feats += [f"c:{padded[j:j + n]}" for j in range(len(padded) - n + 1)]
        out.append(np.fromiter((_hash(f, buckets) for f in feats), dtype=np.int64, count=len(feats)))
    return out
