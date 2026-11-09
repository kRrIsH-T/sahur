"""Text -> hashed feature ids. Shared by training and runtime, so both see exactly the same features."""

import re
import unicodedata
import zlib

import numpy as np

_KEEP = re.compile(r"[^\wऀ-෿\s]+")  # drop punctuation, keep Indic vowel signs and viramas
_INDIC_PUNCT = re.compile("[।॥॰]")  # danda, double danda, abbreviation sign
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    text = text.replace("‌", "").replace("‍", "")  # zero-width (non-)joiners from transliteration
    text = _INDIC_PUNCT.sub(" ", text)
    return _SPACE.sub(" ", _KEEP.sub(" ", text)).strip()


_FOLDS = [("aa", "a"), ("ee", "i"), ("oo", "u"), ("sh", "s"), ("ph", "f"), ("wh", "v"), ("w", "v"), ("z", "j"),
          ("q", "k"), ("y", "i")]
_ASPIRATE = re.compile(r"([kgcjtdpb])h")
_DOUBLED = re.compile(r"(.)\1+")


def fold(word: str) -> str:
    """One spelling for the many ways a romanised word is written: kholo/kolo, maadu/madu, paattu/patu."""
    if not word.isascii():
        return word
    for a, b in _FOLDS:
        word = word.replace(a, b)
    return _DOUBLED.sub(r"\1", _ASPIRATE.sub(r"\1", word))


def _hash(feature: str, buckets: int) -> int:
    # crc32, not hash(): Python salts str hashes per process, which would scramble a saved model.
    return zlib.crc32(feature.encode("utf-8")) % buckets


def feature_ids(text: str, buckets: int, char_n: tuple[int, int] = (2, 5), word_bigrams: bool = True,
                folded: bool = True) -> np.ndarray:
    words = normalize(text).split()
    feats = [f"w:{w}" for w in words]
    if word_bigrams:
        feats += [f"b:{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    lo, hi = char_n
    variants = [("c", words)]
    if folded:
        folded_words = [fold(w) for w in words]
        feats += [f"fw:{w}" for w in folded_words]
        variants.append(("fc", folded_words))
    for prefix, ws in variants:
        for w in ws:
            padded = f"<{w}>"
            for n in range(lo, hi + 1):
                feats += [f"{prefix}:{padded[i:i + n]}" for i in range(len(padded) - n + 1)]
    if not feats:
        feats = ["<empty>"]
    return np.fromiter((_hash(f, buckets) for f in feats), dtype=np.int64, count=len(feats))
