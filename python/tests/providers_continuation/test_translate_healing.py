import json
import unittest


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        unittest.FunctionTestCase(test_strip_reasoning_and_local_ids),
        unittest.FunctionTestCase(test_compaction_item_becomes_message),
        unittest.FunctionTestCase(test_heal_rejected_item),
        unittest.FunctionTestCase(test_make_foreign_safe_mutates),
    ])

from model_deck.integrations.providers.continuation import translate as tr
from model_deck.integrations.providers.continuation import compaction as c


def _body(items):
    return json.dumps({"model": "m", "input": items}, separators=(",", ":")).encode()


def test_strip_reasoning_and_local_ids():
    body = _body([
        {"type": "reasoning", "id": "mdkc_9", "encrypted_content": "e"},
        {"type": "message", "id": "mdkc_1", "role": "user", "content": []},
        {"type": "message", "role": "user", "content": []},
    ])
    out, dropped = tr.sanitize_openai_input(body)
    req = json.loads(out)
    assert dropped == 2
    assert all(i.get("type") != "reasoning" for i in req["input"])
    assert all(i.get("id") != "mdkc_1" for i in req["input"])


def test_compaction_item_becomes_message():
    item = c.compaction_item(c.encode("kept summary"))
    body = _body([item])
    out, dropped = tr.sanitize_openai_input(body)
    req = json.loads(out)
    assert dropped == 1
    assert req["input"][0]["type"] == "message"
    assert "kept summary" in req["input"][0]["content"][0]["text"]


def test_heal_rejected_item():
    body = _body([
        {"type": "reasoning", "id": "rs_abc", "encrypted_content": "e"},
        {"type": "message", "role": "user", "content": []},
    ])
    err = b"No encrypted content found for reasoning item rs_abc"
    healed = tr.heal_rejected_encrypted_item(body, err)
    req = json.loads(healed)
    assert len(req["input"]) == 1
    assert req["input"][0]["type"] == "message"


def test_make_foreign_safe_mutates():
    item = {"type": "reasoning", "id": "rs_z", "encrypted_content": "e",
            "summary": [], "content": [{"type": "x", "text": "t"}]}
    tr._make_item_foreign_safe(item)
    assert item["id"].startswith("mdk-")
    assert "encrypted_content" not in item
    assert item["summary"] == [{"type": "summary_text", "text": "t"}]
    assert "content" not in item
