"""An unresolved later task must not be silently dropped, while titles stay whole."""

import pytest

from bolos.compound import needs_step_resolution, split
from bolos.decide import Parse


class Parser:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def parse(self, text):
        self.calls.append(text)
        return self.rows.get(text, Parse("unknown", False))


@pytest.mark.parametrize("command,left", [
    ("Chrome kholo aur mere liye dusra kaam karo", "Chrome kholo"),
    ("pehle Chrome kholo phir mere liye dusra kaam karo", "Chrome kholo"),
    ("Chrome khol ke mere liye dusra kaam karo", "Chrome khol ke"),
    ("Chrome kholo, mere liye dusra kaam karo.", "Chrome kholo"),
])
def test_known_first_task_and_unknown_second_needs_a_complete_plan(command, left):
    whole = Parse("open_app", True, {"app": "Chrome"})
    parser = Parser({left: whole})
    assert split(command, parser, whole) is None
    assert needs_step_resolution(command, parser, whole)
    assert command not in parser.calls  # the caller's whole parse is reused


@pytest.mark.parametrize("first", [
    Parse("open_app", False, {"app": "Chrome"}),
    Parse("open_app", True, {}),
    Parse("open_app", True, {"app": "Chrome"}, unsure=frozenset({"app"})),
])
def test_uncertain_or_incomplete_prefix_is_not_evidence_of_two_requests(first):
    parser = Parser({"Chrome kholo": first})
    assert not needs_step_resolution("Chrome kholo aur music", parser, Parse("unknown", True))


@pytest.mark.parametrize("command,left,whole", [
    ("Tom and Jerry chalao", "Tom", Parse("play_media", True, {"query": "Tom and Jerry"})),
    ("Harry Potter aur Avengers chalao", "Harry Potter",
     Parse("play_media", True, {"query": "Harry Potter aur Avengers"})),
    ("search for salt and pepper chicken recipe", "search for salt",
     Parse("web_search", True, {"query": "salt and pepper chicken recipe"})),
    ("Raju ko bolo ki Chrome kholo aur music chalao", "Raju ko bolo ki Chrome kholo",
     Parse("send_message", True, {"contact": "Raju", "text": "Chrome kholo aur music chalao"})),
    ("Chrome kholo aur music chalao likho", "Chrome kholo",
     Parse("type_text", True, {"text": "Chrome kholo aur music chalao"})),
])
def test_titles_queries_and_dictation_are_not_treated_as_unfinished_compounds(command, left, whole):
    if left == "Harry Potter":
        prefix = Parse("web_search", True, {"query": left})
    else:
        prefix = Parse("open_app", True, {"app": "Chrome"})
    parser = Parser({left: prefix})
    assert not needs_step_resolution(command, parser, whole)


def test_query_that_swallowed_a_real_first_request_still_needs_resolution():
    command = "ಕ್ರೋಮ್ ತೆರೆ ಮತ್ತು ಉಳಿದ ಕೆಲಸ ಮಾಡು"
    whole = Parse("web_search", True, {"query": command})
    parser = Parser({"ಕ್ರೋಮ್ ತೆರೆ": Parse("open_app", True, {"app": "ಕ್ರೋಮ್"})})
    assert needs_step_resolution(command, parser, whole)


def test_no_boundary_means_no_extra_parse():
    parser = Parser({})
    assert not needs_step_resolution("Chrome kholo", parser)
    assert not needs_step_resolution("aur", parser)
    assert not needs_step_resolution("", parser)
    assert parser.calls == []
