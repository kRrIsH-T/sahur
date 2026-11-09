"""Never replace a wake model that fails its held-out installation gate."""

import json

import numpy as np
import pytest

from train.wake import evaluate as evaluation


def result(recall=0.9, false_accepts_per_hour=0.0):
    return {"recall": recall, "fa_per_hour": false_accepts_per_hour, "hours": 1.2, "episodes": 120}


def report():
    return {"threshold": 0.8, "val": result(), "test": result()}


@pytest.mark.parametrize("split,field,value", [
    ("val", "recall", 0.1),
    ("test", "recall", 0.79),
    ("test", "fa_per_hour", 0.51),
    ("val", "fa_per_hour", float("nan")),
    ("test", "recall", float("inf")),
    ("test", "hours", 0),
    ("test", "hours", float("nan")),
    ("val", "episodes", 0),
])
def test_failed_candidate_preserves_existing_model_and_fixtures(tmp_path, monkeypatch, split, field, value):
    installed = tmp_path / "wake.npz"
    installed.write_bytes(b"existing model")
    monkeypatch.setattr(evaluation, "MODEL_PATH", installed)

    def forbidden():
        raise AssertionError("failed candidate changed fixtures")

    monkeypatch.setattr(evaluation, "write_fixtures", forbidden)
    scores = report()
    scores[split][field] = value
    with pytest.raises(ValueError, match="not installed"):
        evaluation.install(tmp_path / "not-even-read.npz", scores, [])
    assert installed.read_bytes() == b"existing model"


def test_passing_candidate_installs_with_only_validation_chosen_threshold(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.npz"
    installed = tmp_path / "models" / "wake.npz"
    np.savez(candidate, meta=np.array(json.dumps({"threshold": 0.5, "epoch": 7})), weight=np.array([1.0]))
    monkeypatch.setattr(evaluation, "MODEL_PATH", installed)
    fixtures = []
    monkeypatch.setattr(evaluation, "write_fixtures", lambda: fixtures.append(True))
    evaluation.install(candidate, report(), [(0.8, 0.0)])
    with np.load(installed) as model:
        meta = json.loads(str(model["meta"]))
        assert meta["threshold"] == 0.8
        assert meta["epoch"] == 7
        assert meta["test"] == result()
        assert model["weight"].tolist() == [1.0]
    assert fixtures == [True]
    assert list(installed.parent.iterdir()) == [installed]


def test_cli_saves_failed_report_and_scores_test_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(evaluation, "WORK", tmp_path)
    monkeypatch.setattr(evaluation, "evaluate_split", lambda model, split, passes: split)
    monkeypatch.setattr(evaluation, "choose_threshold", lambda ev, target: (0.8, [(0.8, 0.0)]))

    def metrics(split, threshold):
        calls.append((split, threshold))
        return result(0.3) if split == "test" else result()

    monkeypatch.setattr(evaluation, "metrics", metrics)
    monkeypatch.setattr("sys.argv", ["wake-eval", "--install"])
    with pytest.raises(SystemExit, match="not installed"):
        evaluation.main()
    assert calls == [("val", 0.8), ("test", 0.8)]
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert not saved["install_gate"]["eligible"]
    assert "test_sweep" not in saved
