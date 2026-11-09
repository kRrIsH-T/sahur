import json
import subprocess
import sys

import numpy as np
import pytest

from bolos.actions import ACTION_NAMES, CONTROLS
from bolos.decide import FastParser, script_languages
from bolos.fast.features import feature_ids, fold, normalize
from bolos.fast.model import LANGUAGES, MULTI, FastModel
from bolos.fast.slots import TAGS, SlotModel, allowed_tags, decode
from bolos.fast.tokens import tokenize

ACTIONS = ACTION_NAMES
MODEL_ACTIONS = [*ACTION_NAMES, MULTI]  # what round 3 models are trained on
CONTROL_NAMES = list(CONTROLS)


def test_normalize_strips_punctuation_but_keeps_indic_marks():
    assert normalize("Chrome Khol Do.") == "chrome khol do"
    assert normalize("ಸ್ಕ್ರೀನ್ ಅಲ್ಲಿ, ಏನಿದೆ?") == "ಸ್ಕ್ರೀನ್ ಅಲ್ಲಿ ಏನಿದೆ"
    assert normalize("screen‌ನಲ್ಲಿ") == "screenನಲ್ಲಿ"


@pytest.mark.parametrize("a,b", [("kholo", "kolo"), ("maadu", "madu"), ("paattu", "patu"), ("whatsapp", "vatsap"),
                                 ("shuru", "suru"), ("bhejo", "bejo")])
def test_fold_merges_romanisation_variants(a, b):
    assert fold(a) == fold(b)


def test_fold_leaves_native_script_alone():
    assert fold("खोलो") == "खोलो"


def test_feature_ids_are_stable_across_processes():
    # Python's hash() is salted per process; a saved model is only usable if ids never change.
    here = feature_ids("Raju ko message karo", 1 << 17).tolist()
    code = "from bolos.fast.features import feature_ids; print(feature_ids('Raju ko message karo', 1 << 17).tolist())"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert here == json.loads(other)


def test_feature_ids_depend_on_config():
    with_fold = feature_ids("kholo", 1 << 17)
    without = feature_ids("kholo", 1 << 17, folded=False)
    assert len(with_fold) > len(without)
    assert feature_ids("", 1 << 17).size == 1  # an empty transcript still has one feature


def write_model(path, thresholds, favour: str, control: str = "pause", control_threshold: float = 0.0,
                language: str | None = None, language_thresholds: dict[str, float] | None = None,
                language_weights: dict[str, float] | None = None):
    """A tiny action model whose embedding pushes every feature towards one action (and one control).

    Without `language` it is a round 2 file: 11 actions, no language head. With it, a round 3 file: the 12
    actions including multi, and a language head favouring `language` (or weighted by `language_weights`)."""
    buckets, dim = 64, 4
    actions = ACTIONS if language is None else MODEL_ACTIONS
    emb = np.zeros((buckets, dim), dtype=np.float16)
    emb[:, 0] = 1.0
    wa = np.zeros((dim, len(actions)), dtype=np.float32)
    wa[0, actions.index(favour)] = 5.0
    wp = np.zeros((dim, len(CONTROL_NAMES)), dtype=np.float32)
    wp[0, CONTROL_NAMES.index(control)] = 5.0
    config = {"buckets": buckets, "char_n": [2, 3], "word_bigrams": True, "folded": True, "actions": actions,
              "controls": CONTROL_NAMES, "temperature": {"action": 1.0, "control": 1.0}, "thresholds": thresholds,
              "control_threshold": control_threshold}
    heads = {"wa": wa, "ba": np.zeros(len(actions), np.float32),
             "wp": wp, "bp": np.zeros(len(CONTROL_NAMES), np.float32)}
    if language is not None:
        wl = np.zeros((dim, len(LANGUAGES)), dtype=np.float32)
        for name, weight in (language_weights or {language: 5.0}).items():
            wl[0, LANGUAGES.index(name)] = weight
        heads |= {"wl": wl, "bl": np.zeros(len(LANGUAGES), np.float32)}
        config |= {"languages": list(LANGUAGES), "language_thresholds": language_thresholds or {},
                   "temperature": {"action": 1.0, "control": 1.0, "language": 1.0}}
    np.savez(path, config=np.array(json.dumps(config)), emb=emb, **heads)


def write_tagger(path, thresholds: dict[str, float]):
    """A tiny tagger with all-zero weights: every tag is equally likely for every token."""
    buckets, dim, hidden = 512, 4, 2
    config = {"buckets": buckets, "char_n": [2, 4], "actions": ACTIONS, "temperature": 1.0, "thresholds": thresholds}
    gru = {f"{side}_{k}": np.zeros(shape, np.float32) for side in ("fwd", "bwd")
           for k, shape in (("w_ih", (3 * hidden, dim)), ("w_hh", (3 * hidden, hidden)),
                            ("b_ih", (3 * hidden,)), ("b_hh", (3 * hidden,)))}
    np.savez(path, config=np.array(json.dumps(config)), emb=np.zeros((buckets, dim), np.float16),
             action_emb=np.zeros((len(ACTIONS), dim), np.float32), w_out=np.zeros((2 * hidden, len(TAGS)), np.float32),
             b_out=np.zeros(len(TAGS), np.float32), **gru)


def test_fast_model_predicts_and_gates(tmp_path):
    path = tmp_path / "m.npz"
    write_model(path, {a: 0.5 for a in ACTIONS}, favour="web_search")
    model = FastModel(path)
    pred = model.predict("Bangalore weather")
    assert pred.action == "web_search"
    assert abs(pred.action_probs.sum() - 1) < 1e-5
    assert model.accepts(pred)


def test_unsure_control_is_not_accepted(tmp_path):
    path = tmp_path / "m.npz"
    write_model(path, {a: 0.0 for a in ACTIONS}, favour="media_control", control="next", control_threshold=0.999)
    model = FastModel(path)
    pred = model.predict("agla gaana")
    assert (pred.action, pred.control) == ("media_control", "next")
    assert not model.accepts(pred)


def test_ensemble_merge_equals_mean_of_member_logits(tmp_path):
    sys.path.insert(0, "train")
    from ensemble import merge

    rng = np.random.default_rng(0)
    paths = []
    for i in range(3):
        path = tmp_path / f"m{i}.npz"
        write_model(path, {a: 0.5 for a in ACTIONS}, favour=ACTIONS[i])
        data = dict(np.load(path))
        data["emb"] = rng.normal(size=data["emb"].shape).astype(np.float16)
        data["ba"] = rng.normal(size=data["ba"].shape).astype(np.float32)
        np.savez(path, **data)
        paths.append(path)
    members = [FastModel(p) for p in paths]
    merged_path = tmp_path / "merged.npz"
    config = np.load(paths[0])["config"]
    np.savez(merged_path, config=config, **merge([np.load(p) for p in paths]))
    merged = FastModel(merged_path)
    for text in ["chrome kholo", "Raju ko message karo", "ಸ್ಕ್ರೀನ್ ಓದು"]:
        expected = np.mean([m.logits(text)[0] for m in members], axis=0)
        assert np.allclose(merged.logits(text)[0], expected, atol=1e-4)


def test_tokenize_keeps_offsets_and_splits_punctuation():
    text = "Spotify pe Radiohead chalao."
    assert [text[s:e] for s, e in tokenize(text)] == ["Spotify", "pe", "Radiohead", "chalao", "."]
    hindi = "मम्मी को बोलो"
    assert [hindi[s:e] for s, e in tokenize(hindi)] == ["मम्मी", "को", "बोलो"]  # viramas stay inside words


def test_decode_obeys_bio_and_action_masks():
    lp = np.log(np.full((3, len(TAGS)), 1e-3))
    lp[0, TAGS.index("I-query")] = 0.0  # best tag is invalid at the start
    lp[1, TAGS.index("I-query")] = 0.0
    lp[2, TAGS.index("B-text")] = 0.0  # best tag not allowed for web_search
    path = [TAGS[i] for i in decode(lp, allowed_tags("web_search"))]
    assert path[0] in ("O", "B-query", "B-site") and "text" not in " ".join(path)
    for prev, tag in zip(path, path[1:], strict=False):
        assert not tag.startswith("I-") or prev[2:] == tag[2:]


def test_tagger_runtime_shapes_and_extraction(tmp_path):
    # With zero GRU weights the scores are just the output bias: tags are uniform, so nothing is sure.
    path = tmp_path / "s.npz"
    write_tagger(path, {"query": 0.5})
    model = SlotModel(path)
    probs = model.tag_probs(["Radiohead", "chalao"], "play_media")
    assert probs.shape == (2, len(TAGS)) and np.allclose(probs.sum(1), 1)
    found = model.extract("Radiohead chalao", "play_media")
    assert set(found.slots) | set(found.absent) == {"query", "app"}
    assert all(not model.sure(name, s.confidence) for name, s in found.slots.items())


@pytest.mark.parametrize("text", ["", "?", " ... ", "।"])
def test_input_without_words_is_a_certain_unknown(tmp_path, text):
    # Never a guessed action, and never worth a System 2 request.
    write_model(tmp_path / "m.npz", {a: 0.0 for a in ACTIONS}, favour="web_search")
    write_tagger(tmp_path / "s.npz", {})
    parse = FastParser(tmp_path / "m.npz", tmp_path / "s.npz").parse(text)
    assert (parse.action, parse.sure) == ("unknown", True)


def test_parser_marks_missing_required_fields_unsure(tmp_path):
    write_model(tmp_path / "m.npz", {a: 0.0 for a in ACTIONS}, favour="send_message")
    write_tagger(tmp_path / "s.npz", {})
    parse = FastParser(tmp_path / "m.npz", tmp_path / "s.npz").parse("Raju ko bolo")
    assert parse.action == "send_message" and parse.sure
    assert {"contact", "text"} <= parse.unsure and not parse.complete


def test_parser_returns_control_for_media(tmp_path):
    write_model(tmp_path / "m.npz", {a: 0.0 for a in ACTIONS}, favour="media_control", control="volume_up")
    write_tagger(tmp_path / "s.npz", {})
    parse = FastParser(tmp_path / "m.npz", tmp_path / "s.npz").parse("awaaz badhao")
    assert (parse.action, parse.control, parse.complete) == ("media_control", "volume_up", True)


# Round 3: the "multi" action class and the language head (F4, F8)

ALL_SURE = {name: 0.0 for name in LANGUAGES}
NEVER_SURE = {name: 1.0 for name in LANGUAGES}


def test_round2_model_without_language_head_still_loads(tmp_path):
    write_model(tmp_path / "m.npz", {a: 0.0 for a in ACTIONS}, favour="open_app")
    model = FastModel(tmp_path / "m.npz")
    pred = model.predict("chrome kholo")
    assert (pred.action, pred.language, pred.language_probs) == ("open_app", None, None)
    assert model.languages == [] and MULTI not in model.actions
    assert not model.accepts_language(pred)
    assert model.logits("chrome kholo")[2].shape == (0,)


def test_language_head_predicts_and_gates(tmp_path):
    def model_with(thresholds):
        write_model(tmp_path / "m.npz", {a: 0.0 for a in MODEL_ACTIONS}, favour="open_app", language="ta",
                    language_thresholds=thresholds)
        model = FastModel(tmp_path / "m.npz")
        return model, model.predict("chrome thira")

    model, pred = model_with({"ta": 0.5})
    assert pred.language == "ta" and pred.language_confidence > 0.9
    assert abs(pred.language_probs.sum() - 1) < 1e-5
    assert model.accepts_language(pred)
    for thresholds in ({"ta": 0.999}, {"ta": 1.0}, {}):  # too unsure; no safe cutoff found; no threshold at all
        model, pred = model_with(thresholds)
        assert pred.language == "ta" and not model.accepts_language(pred)


def parser(tmp_path, favour="open_app", **kwargs) -> FastParser:
    """A round 3 parser when given language kwargs, else a round 2 one."""
    actions = MODEL_ACTIONS if kwargs else ACTIONS
    write_model(tmp_path / "m.npz", {a: 0.0 for a in actions}, favour=favour, **kwargs)
    write_tagger(tmp_path / "s.npz", {})
    return FastParser(tmp_path / "m.npz", tmp_path / "s.npz")


def test_parse_reports_sure_language_only(tmp_path):
    assert parser(tmp_path, language="kn", language_thresholds=ALL_SURE).parse("Chrome tegi").language == "kn"
    unsure = parser(tmp_path, language="kn", language_thresholds={"kn": 0.999})
    assert unsure.parse("Chrome tegi").language is None
    # Sure it's language-neutral ("Spotify"): no language, so the reply keeps the previous one.
    assert parser(tmp_path, language="none", language_thresholds=ALL_SURE).parse("Spotify").language is None


def test_parse_carries_the_action_confidence(tmp_path):
    # send_message has required slots the (uniform) tagger reports absent: their confidences must not leak in.
    p = parser(tmp_path, favour="send_message", language="hi", language_thresholds=ALL_SURE)
    parse = p.parse("Raju ko bolo")
    assert parse.confidence == pytest.approx(p.model.predict("Raju ko bolo").action_confidence)
    assert parse.confidence > 0.9 and parse.language == "hi"


def test_parse_of_old_model_has_no_language_for_latin_text(tmp_path):
    parse = parser(tmp_path).parse("Chrome kholo")
    assert (parse.action, parse.language) == ("open_app", None)


def test_multi_is_a_sure_parse_without_slots(tmp_path):
    p = parser(tmp_path, favour=MULTI, language="hi", language_thresholds=ALL_SURE)
    parse = p.parse("Chrome kholo aur Bangalore ka weather search karo")
    assert (parse.action, parse.sure, parse.slots, parse.unsure, parse.control) == (MULTI, True, {}, frozenset(), None)
    assert parse.complete and parse.language == "hi" and 0.9 < parse.confidence <= 1.0
    p.model.thresholds[MULTI] = 0.999
    assert not p.parse("Chrome kholo aur weather batao").sure


@pytest.mark.parametrize("text,expected", [
    ("Chrome kholo", ()),
    ("", ()),
    ("ಸ್ಕ್ರೀನ್ ಓದು", ("kn",)),
    ("Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari", ("kn",)),  # code-mixed: the native script decides
    ("Notepad திற", ("ta",)),
    ("Chrome తెరువు", ("te",)),
    ("Chrome തുറക്കൂ", ("ml",)),
    ("Chrome খোলো", ("bn",)),
    ("Chrome ખોલો", ("gu",)),
    ("Chrome ਖੋਲ੍ਹੋ", ("pa",)),
    ("Chrome खोलो", ("hi", "mr")),
    ("ಹೌದು ok அ", ("kn",)),  # the script with most characters wins
])
def test_script_languages(text, expected):
    assert script_languages(text) == expected


def test_native_script_decides_language_before_the_model(tmp_path):
    # The model is sure it's Hindi, but Tamil letters are certain evidence.
    p = parser(tmp_path, language="hi", language_thresholds=ALL_SURE)
    assert p.parse("Notepad திற").language == "ta"
    # Old models still get a language from the script.
    old = parser(tmp_path)
    assert old.parse("ಸ್ಕ್ರೀನ್ ಓದು").language == "kn"
    assert old.parse("नोटपैड खोलो").language == "hi"  # Devanagari without a language head: Hindi


def test_devanagari_is_hindi_or_marathi_by_the_model(tmp_path):
    # Even an unsure model picks between the two the script allows, and never a third language.
    p = parser(tmp_path, language="mr", language_weights={"en": 3.0, "mr": 1.0, "hi": 0.5},
               language_thresholds=NEVER_SURE)
    assert p.parse("नोटपॅड उघड").language == "mr"
    p = parser(tmp_path, language="hi", language_weights={"en": 3.0, "hi": 1.0, "mr": 0.5},
               language_thresholds=NEVER_SURE)
    assert p.parse("नोटपैड खोलो").language == "hi"


def test_ensemble_merges_language_head(tmp_path):
    sys.path.insert(0, "train")
    from ensemble import merge

    rng = np.random.default_rng(1)
    paths = []
    for i, lang in enumerate(["hi", "kn", "ta"]):
        path = tmp_path / f"m{i}.npz"
        write_model(path, {a: 0.5 for a in MODEL_ACTIONS}, favour=MODEL_ACTIONS[i], language=lang)
        data = dict(np.load(path))
        data["emb"] = rng.normal(size=data["emb"].shape).astype(np.float16)
        data["wl"] = rng.normal(size=data["wl"].shape).astype(np.float32)
        data["bl"] = rng.normal(size=data["bl"].shape).astype(np.float32)
        np.savez(path, **data)
        paths.append(path)
    members = [FastModel(p) for p in paths]
    merged_path = tmp_path / "merged.npz"
    np.savez(merged_path, config=np.load(paths[0])["config"], **merge([np.load(p) for p in paths]))
    merged = FastModel(merged_path)
    assert merged.languages == list(LANGUAGES) and merged.actions[-1] == MULTI
    for text in ["chrome kholo aur weather batao", "ಸ್ಕ್ರೀನ್ ಓದು"]:
        for head in range(3):
            expected = np.mean([m.logits(text)[head] for m in members], axis=0)
            assert np.allclose(merged.logits(text)[head], expected, atol=1e-4)


def test_ensemble_refuses_members_with_and_without_language_head(tmp_path):
    sys.path.insert(0, "train")
    from ensemble import merge

    write_model(tmp_path / "old.npz", {a: 0.5 for a in ACTIONS}, favour="open_app")
    write_model(tmp_path / "new.npz", {a: 0.5 for a in MODEL_ACTIONS}, favour="open_app", language="hi")
    with pytest.raises(ValueError):
        merge([np.load(tmp_path / "new.npz"), np.load(tmp_path / "old.npz")])
    assert "wl" not in merge([np.load(tmp_path / "old.npz")] * 2)  # two old members merge as before


def test_check_rows_validates_round3_fields():
    sys.path.insert(0, "train")
    from check_rows import problem

    single = {"text": "Chrome thira", "action": "open_app", "slots": {"app": "Chrome"}, "lang": "ta"}
    assert problem(single) is None
    assert problem(single | {"lang": "hi-IN"}).startswith("lang")
    assert problem(single | {"lang": "none"}) is None
    first = {"action": "open_app", "slots": {"app": "Chrome"}}
    multi = {"text": "Chrome kholo aur Bangalore ka weather search karo", "action": "multi", "slots": {},
             "lang": "hi", "joiners": ["aur"],
             "steps": [first, {"action": "web_search", "slots": {"query": "Bangalore ka weather"}}]}
    assert problem(multi) is None
    assert problem(multi | {"slots": {"app": "Chrome"}}).startswith("multi rows have no slots")
    assert "2-" in problem(multi | {"steps": [first]})
    assert "2-" in problem(multi | {"steps": None})
    off_text = {"action": "web_search", "slots": {"query": "Delhi"}}
    assert "not an exact piece" in problem(multi | {"steps": [first, off_text]})
    assert "needs" in problem(multi | {"steps": [first, {"action": "web_search", "slots": {}}]})
    assert "can't be a step" in problem(multi | {"steps": [first, {"action": "multi", "slots": {}}]})
    media = {"action": "media_control", "slots": {}, "control": "pause"}
    assert problem(multi | {"steps": [first, media]}) is None
    assert "control" in problem(multi | {"steps": [first, media | {"control": "stop"}]})
    assert "joiner" in problem(multi | {"joiners": ["mattu"]})
    assert problem(single | {"steps": multi["steps"]}).startswith("steps and joiners")
    # Two steps may name the same words ("YouTube khol ke Arijit ke gaane chalao": both on YouTube).
    shared = {"text": "YouTube khol ke Arijit ke gaane chalao", "action": "multi", "slots": {}, "lang": "hi",
              "joiners": ["ke"], "steps": [{"action": "open_website", "slots": {"site": "YouTube"}},
                                           {"action": "play_media", "slots": {"query": "Arijit", "app": "YouTube"}}]}
    assert problem(shared) is None


def test_form_actions_are_slotless_training_classes():
    sys.path.insert(0, "train")
    import train_fast
    from check_rows import ACTIONS, FORM_ACTIONS, problem

    assert ACTIONS[:len(ACTION_NAMES)] == ACTION_NAMES  # the schema's order, which saved models follow
    for action in FORM_ACTIONS:
        assert action in ACTIONS and action in train_fast.MODEL_ACTIONS
        row = {"text": "is form ko samjhao", "lang": "hi", "action": action, "slots": {}}
        assert problem(row) is None
        assert "does not take" in problem(row | {"slots": {"query": "form"}})
    assert train_fast.MODEL_ACTIONS[-1] == MULTI and len(set(train_fast.MODEL_ACTIONS)) == len(ACTIONS) + 1
    step = {"action": "explain_form", "slots": {}}
    multi = {"text": "Chrome kholo aur form samjhao", "action": "multi", "slots": {}, "lang": "hi",
             "steps": [{"action": "open_app", "slots": {"app": "Chrome"}}, step]}
    assert problem(multi) is None


def test_corrections_become_clean_training_rows():
    sys.path.insert(0, "train")
    from build_dataset import sanitize_correction, training_row
    from check_rows import problem

    row = sanitize_correction({"text": "Radiohead chalao", "action": "play_media",
                               "slots": {"query": "Radiohead", "app": "Spotify"}, "lang": "hi",
                               "source": "correction", "time": "2026-11-04T10:00:00+05:30"})
    assert row == {"text": "Radiohead chalao", "action": "play_media", "slots": {"query": "Radiohead"}, "lang": "hi"}
    row = sanitize_correction({"text": "mera editor kholo", "action": "open_app", "slots": {"app": "Notepad"},
                               "lang": "xx"})
    assert row == {"text": "mera editor kholo", "action": "open_app", "slots": {}, "incomplete": True}
    assert problem(row) is None
    assert sanitize_correction({"text": "x", "action": "teleport"}) is None
    message = sanitize_correction({"text": "Raju ko bolo ki aa raha hoon", "action": "send_message",
                                   "slots": {"contact": "Raju", "text": "aa raha hoon", "app": "whatsapp"}})
    assert problem(message) is None and "app" not in message["slots"]
    assert training_row(row, "correction") == {"text": "mera editor kholo", "action": "open_app", "slots": {},
                                               "source": "correction"}


def test_build_dataset_merges_r3_and_corrections_and_keeps_v6_out(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, "train")
    import build_dataset

    def write_jsonl(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    data, evals = tmp_path / "data", tmp_path / "eval"
    write_jsonl(data / "slots" / "a.jsonl", [
        {"text": "Notepad kholo", "action": "open_app", "slots": {"app": "Notepad"}},
        {"text": "agla wala", "action": "media_control", "slots": {}, "control": "previous"},
    ])
    multi = {"text": "Chrome kholo aur Bangalore ka weather search karo", "lang": "hi", "action": "multi",
             "slots": {}, "joiners": ["aur"], "steps": [{"action": "open_app", "slots": {"app": "Chrome"}},
                                                        {"action": "web_search",
                                                         "slots": {"query": "Bangalore ka weather"}}]}
    write_jsonl(data / "r3" / "hi.jsonl", [
        multi,
        {"text": "Calculator tegi", "lang": "kn", "action": "open_app", "slots": {"app": "Calculator"}},
        {"text": "Mysore Palace bagge heLu", "lang": "kn", "action": "web_search", "slots": {"query": "Mysore"}},
        {"text": "Paint thira", "lang": "xx", "action": "open_app", "slots": {"app": "Paint"}},  # bad lang
    ])
    write_jsonl(data / "r3" / "forms_hi.jsonl", [
        {"text": "ration card form bharo", "lang": "hi", "action": "fill_form", "slots": {}},
        {"text": "yeh site asli hai kya", "lang": "hi", "action": "check_site", "slots": {"site": "site"}},  # bad
    ])
    corrections = tmp_path / "corrections.jsonl"
    write_jsonl(corrections, [  # oldest first, as appended at runtime
        {"text": "agla wala", "action": "media_control", "slots": {}, "control": "pause", "source": "correction"},
        {"text": "agla wala", "action": "media_control", "slots": {}, "control": "next", "lang": "hi",
         "source": "correction"},
    ])
    write_jsonl(evals / "commands_v6.jsonl", [{"text": "Mysore Palace bagge heLu", "action": "web_search"}])
    out = tmp_path / "dataset.jsonl"
    monkeypatch.setattr(sys, "argv", ["build_dataset.py", "--data-dir", str(data), "--out", str(out),
                                      "--corrections", str(corrections), "--eval-dir", str(evals)])
    build_dataset.main()
    rows = {r["text"]: r for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
    assert set(rows) == {"Notepad kholo", "agla wala", multi["text"], "Calculator tegi", "ration card form bharo"}
    assert rows["ration card form bharo"]["source"] == "r3/forms_hi"
    assert rows["agla wala"] == {"text": "agla wala", "action": "media_control", "slots": {}, "control": "next",
                                 "lang": "hi", "source": "correction"}  # the newest correction wins
    assert rows[multi["text"]] == multi | {"source": "r3/hi"}
    assert "test sets kept out: commands_v6.jsonl;" in capsys.readouterr().out


def test_near_duplicate_search_matches_all_pairs():
    import random

    sys.path.insert(0, "train")
    from build_dataset import NearSets, jaccard

    rng = random.Random(0)
    universe = [f"w{i}" for i in range(12)]
    sets = [frozenset(rng.sample(universe, rng.randint(0, 10))) for _ in range(300)]
    index = NearSets(sets)
    for threshold in (0.6, 0.8, 0.5, 1.0):
        for query in sets[:150] + [frozenset(), frozenset(universe)]:
            expected = any(jaccard(query, s) >= threshold for s in sets)
            assert index.any_at_least(query, threshold) == expected
    # Sizes exactly at the bound: 4 of 5 shared is Jaccard 0.8, so it must still be found.
    assert NearSets([frozenset("abcd")]).any_at_least(frozenset("abcde"), 0.8)
    assert NearSets([frozenset("abcde")]).any_at_least(frozenset("abc"), 0.6)


def test_slot_tagger_skips_compound_rows():
    from collections import Counter

    sys.path.insert(0, "train")
    from train_slots import examples

    rows = [{"text": "Chrome kholo aur Arijit chalao", "action": "multi", "slots": {}, "lang": "hi",
             "steps": [{"action": "open_app", "slots": {"app": "Chrome"}},
                       {"action": "play_media", "slots": {"query": "Arijit"}}]},
            {"text": "Chrome thira", "action": "open_app", "slots": {"app": "Chrome"}, "lang": "ta"},
            {"text": "Chromecast kholo", "action": "open_app", "slots": {"app": "Chrome"}}]  # not on a word boundary
    skipped = Counter()
    assert examples(rows, skipped) == [(["Chrome", "thira"], [TAGS.index("B-app"), 0], "open_app")]
    assert skipped == {"multi (skipped)": 1, "open_app": 1}


def test_ensemble_refuses_members_with_other_classes(tmp_path, monkeypatch):
    sys.path.insert(0, "train")
    import ensemble

    write_model(tmp_path / "old.npz", {a: 0.5 for a in ACTIONS}, favour="open_app")  # round 2: 11 actions
    monkeypatch.setattr(sys, "argv", ["ensemble.py", "--name", "e", "old", "old", "--models-dir", str(tmp_path),
                                      "--data", str(tmp_path / "missing.jsonl")])
    with pytest.raises(SystemExit, match="actions"):
        ensemble.main()
    assert not (tmp_path / "e.npz").exists()


def test_rare_language_shares_the_pooled_threshold_only_when_clean():
    sys.path.insert(0, "train")
    import train_fast

    names = train_fast.LANGUAGE_NAMES
    copies = train_fast.CAL_AUG_COPIES
    hi, ta, none, gu = (names.index(x) for x in ("hi", "ta", "none", "gu"))
    # 100 Hindi and 100 Tamil rows, all right and confident; 5 "none" rows, too few to certify alone.
    pred = np.array([hi] * 100 + [ta] * 100 + [none] * 5).repeat(copies)
    gold, conf = pred.copy(), np.full(len(pred), 0.9)
    thresholds, pooled = train_fast.fit_language_thresholds(conf, pred, gold)
    assert pooled == pytest.approx(0.9) and thresholds["hi"] == thresholds["ta"] == pytest.approx(0.9)
    assert thresholds["none"] == pytest.approx(0.9)  # shares the pooled threshold
    assert thresholds["gu"] == 1.0  # never predicted: never sure
    gold[-copies:] = gu  # one of the "none" predictions is wrong: no shortcut for it
    thresholds, _ = train_fast.fit_language_thresholds(conf, pred, gold)
    assert thresholds["none"] == 1.0 and thresholds["hi"] == pytest.approx(0.9)


def test_calibration_split_is_stratified_by_language():
    sys.path.insert(0, "train")
    from train_fast import split

    rows = [{"text": f"{lang} command {i}", "action": "open_app", "lang": lang} for lang in ("hi", "ta", "gu")
            for i in range(20)] + [{"text": f"old {i}", "action": "open_app"} for i in range(20)]
    train, cal = split(rows, 0.1, 0)
    assert len(cal) == 8 and len(train) == 72
    assert sorted(r.get("lang", "") for r in cal) == ["", "", "gu", "gu", "hi", "hi", "ta", "ta"]
    assert split(rows, 0.1, 0) == (train, cal)


def test_language_calibration_meets_target_and_ignores_unlabelled_rows():
    import random

    import torch

    sys.path.insert(0, "train")
    import train_fast

    langs = ["hi", "ta", "kn", "none"]
    # Each text carries a 3-digit code for its label, which augmentation never changes (no typos in words
    # under 4 characters, no respelling of digits): 7xx is a language, 699 a compound without a language.
    code = {f"7{train_fast.LANGUAGE_NAMES.index(lang):02d}": lang for lang in langs}
    cal = [{"text": f"{i} {c}", "action": "open_app", "lang": lang} for c, lang in code.items() for i in range(60)]
    cal += [{"text": f"{i} 699", "action": "multi"} for i in range(30)]

    def logits(texts):
        """Perfectly separable logits, read from the codes."""
        n = len(texts)
        la = torch.zeros(n, len(train_fast.MODEL_ACTIONS))
        ll = torch.zeros(n, len(train_fast.LANGUAGE_NAMES))
        for k, t in enumerate(texts):
            words = t.replace(".", " ").split()
            la[k, train_fast.MODEL_ACTIONS.index("multi" if "699" in words else "open_app")] = 6.0
            for w in words:
                if w in code:
                    ll[k, train_fast.LANGUAGE_NAMES.index(code[w])] = 6.0
        return la, torch.zeros(n, len(train_fast.CONTROL_NAMES)), ll

    fit = train_fast.calibrate(logits, cal, random.Random(0))
    assert all(fit["language_thresholds"][lang] < 1.0 for lang in langs)
    assert fit["language_thresholds"]["gu"] == 1.0  # never predicted: never sure
    assert fit["thresholds"]["multi"] < 1.0
    report = fit["report"]["language"]
    assert report["cal_rows"] == 240 and report["acc"] == 1.0 and report["coverage"] == 1.0
    assert fit["report"]["multi_rows"]["acc"] == 1.0
