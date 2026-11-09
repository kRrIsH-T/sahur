import dataclasses
import json
from types import SimpleNamespace

import pytest

from bolos import actions as a
from bolos.config import load_settings
from bolos.llm import CodexLLM, OpenAICompatibleLLM, SlotError


def response(content):
    if content == "no-message":
        return SimpleNamespace(choices=[SimpleNamespace(message=None)])
    if content == "no-choices":
        return SimpleNamespace(choices=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def llm(monkeypatch):
    llm = OpenAICompatibleLLM(dataclasses.replace(load_settings(), llm_api_key="test-key", llm_rpm=0))
    llm.replies = []
    monkeypatch.setattr(llm, "_chat", lambda **_: response(llm.replies.pop(0)))
    return llm


@pytest.mark.parametrize("reply", ["no-message", "no-choices", None, "", "not json", "[1, 2]",
                                   '{"action": "format_disk"}'])
def test_unusable_resolve_raises_slot_error(llm, reply):
    llm.replies = [reply]
    with pytest.raises(SlotError):
        llm.resolve("kuch bhi")


def test_resolve_builds_a_full_action(llm):
    llm.replies = ['{"action": "play_media", "query": "Radiohead", "app": "Spotify", "contact": "", "text": "",'
                   ' "site": "", "target": "", "control": ""}']
    assert llm.resolve("Spotify pe Radiohead chalao") == a.PlayMedia(query="Radiohead", app="Spotify")


def test_resolve_media_control(llm):
    llm.replies = ['{"action": "media_control", "control": "pause", "app": "", "query": ""}']
    assert llm.resolve("gaana rok do") == a.MediaControl(control="pause")


@pytest.mark.parametrize("reply", ["no-message", "not json", '{"contact": "Raju"}',
                                   '{"contact": "Raju", "text": null}', '{"contact": "Raju", "text": 5}'])
def test_fill_raises_slot_error_for_unusable_or_incomplete(llm, reply):
    llm.replies = [reply]
    with pytest.raises(SlotError):
        llm.fill("send_message", "Raju ko message karo", ["contact", "text"])


def test_fill_leaves_unsaid_optional_fields_out(llm):
    llm.replies = ['{"query": "running shoes", "site": ""}']
    assert llm.fill("web_search", "running shoes dhundho", ["query", "site"]) == a.WebSearch(query="running shoes")


def test_describe_screen_falls_back_to_raw_text(llm):
    llm.replies = ["no-message"]
    assert llm.describe_screen("Inbox 3 unread", "hi-IN") == "Inbox 3 unread"


def test_codex_runs_one_shot_with_a_schema(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, input, **kwargs):
        calls.append((cmd, input))
        out = cmd[cmd.index("-o") + 1]
        schema = json.loads(open(cmd[cmd.index("--output-schema") + 1], encoding="utf-8").read())
        assert schema["additionalProperties"] is False and "action" in schema["required"]
        with open(out, "w", encoding="utf-8") as f:
            f.write('{"action": "open_website", "site": "GitHub.com", "app": "", "query": "", "contact": "",'
                    ' "text": "", "target": "", "control": ""}')
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("bolos.llm.subprocess.run", fake_run)
    settings = dataclasses.replace(load_settings(), codex_path="codex.exe", codex_model="gpt-6-luna",
                                   codex_effort="low", codex_fast=True)
    llm = CodexLLM(settings)
    assert llm.resolve("open github dot com") == a.OpenWebsite(site="GitHub.com")
    cmd, prompt = calls[0]
    assert cmd[:2] == ["codex.exe", "exec"] and "--ephemeral" in cmd and "--ignore-user-config" in cmd
    assert "service_tier=fast" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only" and "model_reasoning_effort=low" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-6-luna" and "open github dot com" in prompt


def test_codex_failure_is_an_error_not_an_answer(monkeypatch):
    monkeypatch.setattr("bolos.llm.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr="not signed in"))
    llm = CodexLLM(dataclasses.replace(load_settings(), codex_path="codex.exe"))
    with pytest.raises(RuntimeError):
        llm.resolve("chrome kholo")


# Several steps in one command (System 2's part of bolos.compound).

def steps_reply(*steps: str) -> str:
    return '{"steps": [' + ", ".join(steps) + "]}"


def test_resolve_steps_gives_the_actions_in_order(llm):
    llm.replies = [steps_reply('{"action": "open_app", "app": "Google Chrome", "query": ""}',
                               '{"action": "web_search", "query": "Bangalore weather", "site": "", "app": ""}')]
    assert llm.resolve_steps("Chrome kholo aur Bangalore ka weather search karo") == [
        a.OpenApp(app="Google Chrome"), a.WebSearch(query="Bangalore weather")]


def test_resolve_steps_single_and_unknown(llm):
    llm.replies = [steps_reply('{"action": "play_media", "query": "Tom and Jerry", "app": ""}'),
                   steps_reply('{"action": "unknown"}')]
    assert llm.resolve_steps("Tom and Jerry chalao") == [a.PlayMedia(query="Tom and Jerry")]
    assert llm.resolve_steps("kuch bhi") == [a.Unknown()]


def test_resolve_steps_media_controls(llm):
    llm.replies = [steps_reply('{"action": "media_control", "control": "volume_up"}',
                               '{"action": "media_control", "control": "next"}')]
    assert llm.resolve_steps("awaaz badhao phir agla gaana") == [a.MediaControl(control="volume_up"),
                                                                  a.MediaControl(control="next")]


@pytest.mark.parametrize("reply", [
    "not json", '{"steps": []}', '{"steps": "open_app"}', '{"steps": [1]}', '{"other": []}',
    steps_reply(*['{"action": "open_app", "app": "Notepad"}'] * 4),  # more than 3 steps
    steps_reply('{"action": "open_app", "app": "Notepad"}', '{"action": "unknown"}'),  # half a command
    steps_reply('{"action": "open_app", "app": "Notepad"}', '{"action": "cancel"}'),
    steps_reply('{"action": "open_app", "app": "Notepad"}', '{"action": "format_disk"}'),
    steps_reply('{"action": "open_app", "app": "Notepad"}', '{"action": "open_app", "app": ""}'),
])
def test_resolve_steps_unusable_raises_slot_error(llm, reply):
    llm.replies = [reply]
    with pytest.raises(SlotError):
        llm.resolve_steps("Notepad kholo aur kuch karo")


def test_resolve_steps_never_takes_a_browser_from_the_model(llm):
    llm.replies = [steps_reply('{"action": "web_search", "query": "news", "browser": "Firefox"}')]
    assert llm.resolve_steps("news dhundho") == [a.WebSearch(query="news")]


def test_codex_resolve_steps_asks_for_a_list_of_steps(monkeypatch):
    seen = {}

    def fake_run(cmd, input, **kwargs):
        seen["schema"] = json.loads(open(cmd[cmd.index("--output-schema") + 1], encoding="utf-8").read())
        seen["prompt"] = input
        empty = dict.fromkeys(["app", "site", "query", "contact", "text", "target", "control"], "")
        steps = [{**empty, "action": "open_app", "app": "Notepad"},
                 {**empty, "action": "type_text", "text": "meeting kal hai"}]  # every field, as Codex must
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
            f.write(json.dumps({"steps": steps}))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("bolos.llm.subprocess.run", fake_run)
    llm = CodexLLM(dataclasses.replace(load_settings(), codex_path="codex.exe"))
    assert llm.resolve_steps("Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari") == [
        a.OpenApp(app="Notepad"), a.TypeText(text="meeting kal hai")]
    schema = seen["schema"]
    assert schema["required"] == ["steps"] and schema["additionalProperties"] is False
    item = schema["properties"]["steps"]["items"]
    assert item["additionalProperties"] is False and "action" in item["required"]
    assert "browser" not in item["properties"]
    assert "Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari" in seen["prompt"]


# Corrections ("galat tha, Spotify pe chalao").

def test_correct_gives_the_wanted_action(llm):
    prompts = []
    replies = ['{"action": "play_media", "query": "Radiohead", "app": "Spotify"}']
    llm._chat = lambda **kw: prompts.append(kw["messages"]) or response(replies.pop(0))
    fixed = llm.correct("Radiohead chalao", a.PlayMedia(query="Radiohead", app="YouTube"), "galat tha, Spotify pe")
    assert fixed == a.PlayMedia(query="Radiohead", app="Spotify")
    user = prompts[0][1]["content"]
    assert "Radiohead chalao" in user and "galat tha, Spotify pe" in user and '"app": "YouTube"' in user


def test_correct_does_not_show_the_carried_browser(llm):
    prompts = []
    llm._chat = lambda **kw: prompts.append(kw["messages"]) or response('{"action": "web_search", "query": "x"}')
    llm.correct("x dhundho", a.WebSearch(query="x", browser="Chrome"), "nahi, Amazon pe")
    assert "browser" not in prompts[0][1]["content"] and "Chrome" not in prompts[0][1]["content"]


@pytest.mark.parametrize("reply", ["not json", '{"action": "teleport"}', '{"action": "open_app", "app": ""}'])
def test_unusable_correction_raises_slot_error(llm, reply):
    llm.replies = [reply]
    with pytest.raises(SlotError):
        llm.correct("notes kholo", a.OpenApp(app="Sticky Notes"), "wrong, I meant")
