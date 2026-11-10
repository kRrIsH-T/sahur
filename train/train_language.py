"""Train the standalone System 1 language classifier from the public command data.

    python train/train_language.py
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from augment import augment
from build_dataset import load_eval_texts, read_jsonl, without_eval_overlap
from torch import nn
from train_fast import (
    CAL_AUG_COPIES,
    LANGUAGE_NAMES,
    MODELS_DIR,
    ROOT,
    accepted_report,
    batch_tensors,
    fit_language_thresholds,
    fit_temperature,
    split,
)

from bolos.fast.features import normalize
from bolos.responses import script_language


def load_rows() -> list[dict]:
    problems = Counter()
    data_dir = ROOT / "train" / "data"
    rows = {}
    for path in sorted((data_dir / "r3").glob("*.jsonl")):
        for row in read_jsonl(path, problems):
            text = normalize(row["text"])
            existing = rows.setdefault(text, row)
            # The same transcript can come from speakers of different languages.
            if existing["lang"] != row["lang"]:
                existing["lang"] = "none"
    # The original Hindi and English collections add shorter requests to the round 3 command mix.
    for group, language in (("hindi", "hi"), ("english", "en")):
        for path in sorted((data_dir / "raw").glob(f"*__{group}.jsonl")):
            for row in read_jsonl(path, problems):
                text = normalize(row["text"])
                if row.get("group") == group and len(text.split()) > 1:
                    rows.setdefault(text, {**row, "lang": language})
    return without_eval_overlap(list(rows.values()), load_eval_texts(ROOT / "eval"), problems)


def training_text(row: dict, rng: random.Random) -> str:
    text = row["text"]
    if rng.random() < 0.25:
        # The command's wording carries more language information than a song title or app name.
        wording = text
        for value in row.get("slots", {}).values():
            wording = wording.replace(value, " ", 1)
        if len(wording.split()) > 1:
            text = wording
    return augment(text, rng) if rng.random() < 0.55 else text


def calibrate(weights: np.ndarray, rows: list[dict], config: dict) -> tuple[float, dict, dict]:
    # Calibrate on romanised text, with the same quantized weights the app loads.
    rng = random.Random(0)
    examples = [row for row in rows if not script_language(row["text"]) for _ in range(CAL_AUG_COPIES)]
    ids, offsets = batch_tensors([augment(row["text"], rng) for row in examples], config, 0.0, rng)
    scores = nn.functional.embedding_bag(ids, torch.from_numpy(weights.astype(np.float32)), offsets, mode="mean")
    labels = torch.tensor([LANGUAGE_NAMES.index(row["lang"]) for row in examples])
    temperature = fit_temperature(scores, labels)
    probabilities = (scores / temperature).softmax(-1).numpy()
    predicted, confidence, gold = probabilities.argmax(-1), probabilities.max(-1), labels.numpy()
    thresholds, _ = fit_language_thresholds(confidence, predicted, gold, target=0.06)
    for index, language in enumerate(LANGUAGE_NAMES):
        if language != "none" and thresholds[language] == 1.0:
            # A small high-confidence group may be error-free but too small for the per-class estimate.
            wrong = confidence[(predicted == index) & (gold != index)]
            cutoff = max(0.95, float(np.nextafter(wrong.max(initial=0), np.float32(1.0))))
            if np.count_nonzero((predicted == index) & (confidence >= cutoff)) >= 3 * CAL_AUG_COPIES:
                thresholds[language] = cutoff
    thresholds = {language: max(0.55, threshold) for language, threshold in thresholds.items()}
    report = accepted_report(confidence, predicted, gold, thresholds, LANGUAGE_NAMES)
    return temperature, thresholds, report


def main() -> None:
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--out", type=Path, default=MODELS_DIR / "language.npz")
    args = args_parser.parse_args()

    rng = random.Random(13)
    torch.manual_seed(13)
    torch.set_num_threads(2)
    train, calibration = split(load_rows(), 0.18, 0)
    config = {"languages": LANGUAGE_NAMES, "buckets": 65536, "char_n": [2, 5],
              "folded": True, "word_bigrams": False}
    model = nn.EmbeddingBag(config["buckets"], len(LANGUAGE_NAMES), mode="mean")
    nn.init.zeros_(model.weight)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.07)
    epochs = 22
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, epochs, eta_min=0.008)

    def logits(texts: list[str]) -> torch.Tensor:
        return model(*batch_tensors(texts, config, 0.0, rng))

    for _ in range(epochs):
        rng.shuffle(train)
        for start in range(0, len(train), 128):
            batch = train[start:start + 128]
            texts = [training_text(row, rng) for row in batch]
            labels = torch.tensor([LANGUAGE_NAMES.index(row["lang"]) for row in batch])
            loss = nn.functional.cross_entropy(logits(texts), labels, label_smoothing=0.01)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        schedule.step()

    weights = model.weight.detach().numpy().astype(np.float16)
    temperature, thresholds, report = calibrate(weights, calibration, config)
    config.update(temperature=temperature, language_thresholds=thresholds)
    np.savez_compressed(args.out, config=np.array(json.dumps(config)), weights=weights)
    print(f"Saved {args.out} ({len(train)} train, {len(calibration)} calibration): {report}")
    print(f"Thresholds: {thresholds}")


if __name__ == "__main__":
    main()
