from bolos.decide import Parse
from eval.evaluate_pipeline import evaluate


def test_evaluation_measures_ordered_plans_and_refusals_without_effects():
    class Parser:
        def parse(self, text):
            return {"open Chrome then search mango prices": Parse("multi", True),
                    "open Chrome": Parse("open_app", True, {"app": "Chrome"}, language="en"),
                    "search mango prices": Parse("web_search", True, {"query": "mango prices"}, language="en"),
                    "hello friend": Parse("unknown", True, language="en")}[text]

    rows = [
        {"id": "compound", "text": "open Chrome then search mango prices", "lang": "en", "action": "multi",
         "steps": [{"action": "open_app", "slots": {"app": "Chrome"}},
                   {"action": "web_search", "slots": {"query": "mango prices"}}]},
        {"id": "chatter", "text": "hello friend", "lang": "en", "action": "unknown", "slots": {}},
    ]
    report = evaluate(Parser(), rows)
    assert report["summary"]["n"] == 2
    assert report["summary"]["actions_correct"] == report["summary"]["exact"] == 2
    assert report["summary"]["planned"] == 1
    assert report["results"][0]["predicted"][1]["browser"] == "chrome"


def test_evaluation_treats_incomplete_commands_as_refusals_and_uses_runtime_language_detection():
    class Parser:
        def parse(self, text):
            return Parse("send_message", True, {"contact": "Sunil"}, frozenset({"text"}))

    rows = [{"text": "Sunil la message pathav", "lang": "mr", "action": "send_message",
             "slots": {"contact": "Sunil"}}]
    result = evaluate(Parser(), rows, language_detector=lambda text: "mr")["results"][0]
    assert result["expected"] == result["predicted"] == []
    assert result["exact"] and result["language_correct"]
