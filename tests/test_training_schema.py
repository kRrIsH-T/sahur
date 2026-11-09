"""Training labels must express exactly the commands the runtime can accept."""

import pytest

from bolos import actions
from bolos.compound import MAX_PARTS
from bolos.llm import MAX_STEPS as LLM_MAX_STEPS
from train.check_rows import MAX_STEPS, SPAN_SLOTS, problem


def test_training_actions_and_slots_match_runtime_schema():
    assert set(SPAN_SLOTS) == set(actions.ACTION_NAMES)
    for action, (allowed, required) in SPAN_SLOTS.items():
        assert allowed == set(actions.SLOT_FIELDS.get(action, [])), action
        assert required == actions.REQUIRED_FIELDS.get(action, set()), action
    assert MAX_STEPS == MAX_PARTS == LLM_MAX_STEPS


@pytest.mark.parametrize("action", ["explain_form", "fill_form", "check_site"])
def test_form_intents_need_no_personal_values(action):
    row = {"text": "please help with this form", "action": action, "slots": {}, "lang": "en"}
    assert problem(row) is None
    assert "does not take" in problem(row | {"slots": {"text": "this form"}})


def test_upload_target_is_optional_but_must_be_copied_when_present():
    row = {"text": "upload the address proof", "action": "upload_file", "slots": {}, "lang": "en"}
    assert problem(row) is None
    assert problem(row | {"slots": {"target": "address proof"}}) is None
    assert "not an exact piece" in problem(row | {"slots": {"target": "identity proof"}})
    assert "empty" in problem(row | {"slots": {"target": ""}})


def test_training_compounds_stop_at_runtime_limit():
    text = "open Chrome then explain this form then fill this form then upload the proof"
    steps = [
        {"action": "open_app", "slots": {"app": "Chrome"}},
        {"action": "explain_form", "slots": {}},
        {"action": "fill_form", "slots": {}},
        {"action": "upload_file", "slots": {"target": "proof"}},
    ]
    row = {"text": text, "action": "multi", "slots": {}, "lang": "en"}
    for length in (2, 3):
        assert problem(row | {"steps": steps[:length]}) is None
    assert "2-3" in problem(row | {"steps": steps})
