import json
import unittest


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        unittest.FunctionTestCase(test_round_trip_and_summary),
        unittest.FunctionTestCase(test_truncation_and_trigger),
    ])

from model_deck.integrations.providers.continuation import compaction as c


def test_round_trip_and_summary():
    events = [
        {"type": "response.output_text.delta", "delta": "hello "},
        {"type": "response.output_text.delta", "delta": "world"},
    ]
    summary = c.summary_from_events(events)
    assert summary.endswith("hello world")
    enc = c.encode(summary)
    assert c.decode(enc)["summary"] == summary
    item = c.compaction_item(summary)
    msg = c.message_for_item(item)
    assert msg["role"] == "user"
    assert msg["content"][0]["text"] == c.FOREIGN_NOTE


def test_truncation_and_trigger():
    assert c.has_trigger({"input": [{"type": "compaction_trigger"}]})
    assert c.is_summarization_request({"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": c.SUMMARIZATION_PROMPT}]}]})
    long_text = "x" * (c.MAX_SUMMARY_CHARS + 50)
    assert c.decode(c.encode(long_text))["summary"] == long_text[:c.MAX_SUMMARY_CHARS]
    req = c.summarization_request({"model": "m", "input": [], "tools": ["t"]})
    assert "tools" not in req
    assert req["input"][-1]["content"][0]["text"] == c.SUMMARIZATION_PROMPT
    assert c.summary_message("abc")["content"][0]["text"].startswith(c.SUMMARY_PREFIX)
