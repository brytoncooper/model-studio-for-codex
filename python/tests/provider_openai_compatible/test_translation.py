import copy
import unittest


from model_deck.integrations.providers.openai_compatible import translation as tr


def _request(**overrides):
    base = {
        "model": "deepseek-chat",
        "instructions": "Be brief.",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Hello "},
                {"type": "input_text", "text": "there"}]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Look"},
                {"type": "input_image", "image_url": "https://x/img.png"}]},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Hi"}]},
            {"type": "function_call", "name": "get_weather", "arguments": '{"city":"Oslo"}',
             "call_id": "call_1"},
            {"type": "function_call_output", "call_id": "call_1",
             "output": [{"type": "input_text", "text": "sunny"}]},
            {"type": "reasoning", "summary": []},
        ],
        "tools": [{"type": "function", "name": "get_weather", "description": "d",
                   "parameters": {"type": "object", "properties": {}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "max_output_tokens": 64,
        "text": {"format": {"type": "json_object"}},
    }
    base.update(overrides)
    return base


def test_shapes_and_no_mutation():
    req = _request()
    snapshot = copy.deepcopy(req)
    out = tr.chat_request_from_responses(req)
    assert req == snapshot
    assert out["model"] == "deepseek-chat"
    assert out["messages"][0] == {"role": "system", "content": "Be brief."}
    assert out["messages"][1] == {"role": "user", "content": "Hello there"}
    img = out["messages"][2]
    assert img["content"][0] == {"type": "text", "text": "Look"}
    assert img["content"][1] == {"type": "image_url", "image_url": {"url": "https://x/img.png"}}
    assert out["messages"][3]["content"] == "Hi"
    assert out["messages"][3]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert out["messages"][4] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}
    assert out["tools"][0]["function"]["name"] == "get_weather"
    assert out["tool_choice"] == "auto"
    assert out["parallel_tool_calls"] is False
    assert out["max_tokens"] == 64
    assert out["response_format"] == {"type": "json_object"}
    assert out["stream"] is True


def test_continuation_records_merge():
    req = _request(input=[
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "T"}]},
        {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1"},
    ])
    calls = {}

    def load(item, index):
        calls[index] = item["type"]
        return {"metadata": {"assistant_fields": {"foo": "bar"}, "call_fields": {"baz": 1}},
                "response_id": "r1"}

    out = tr.chat_request_from_responses(req, load_record=load)
    assert calls == {0: "message", 1: "function_call"}
    assert out["messages"][1]["foo"] == "bar"
    assert out["messages"][1]["tool_calls"][0]["baz"] == 1


def test_same_response_groups_tool_calls():
    req = _request(input=[
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "T"}]},
        {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c1"},
    ], instructions=None)
    req.pop("instructions", None)

    def load(item, index):
        return {"metadata": {}, "response_id": "same"}

    out = tr.chat_request_from_responses(req, load_record=load)
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == "T"
    assert len(out["messages"][0]["tool_calls"]) == 1


def test_deepseek_reasoning_stripping():
    req = _request(input=[], instructions=None, reasoning={"effort": "medium"}, model="deepseek-v4-x")
    req.pop("instructions", None)
    out = tr.chat_request_from_responses(req, provider_id="deepseek")
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == "high"
    req2 = _request(input=[], instructions=None, reasoning={"effort": "none"}, model="deepseek-v4-x")
    req2.pop("instructions", None)
    out2 = tr.chat_request_from_responses(req2, provider_id="deepseek")
    assert out2["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in out2


def test_gemini_guard_and_passthrough():
    req = _request(input=[], instructions=None, reasoning={"effort": "none"}, model="gemini-3-pro")
    req.pop("instructions", None)
    try:
        tr.chat_request_from_responses(req, provider_id="google")
    except tr.ContinuationError:
        pass
    else:
        raise AssertionError("expected ContinuationError")
    other = _request(input=[], instructions=None, reasoning={"effort": "low"}, model="other-model")
    other.pop("instructions", None)
    out = tr.chat_request_from_responses(other, provider_id="other")
    assert "thinking" not in out and "reasoning_effort" not in out


def test_json_schema_and_function_choice():
    fmt = {"type": "json_schema", "name": "s", "description": "d", "schema": {"type": "object"},
           "strict": True, "extra": "drop"}
    req = _request(input=[], instructions=None,
                   text={"format": fmt}, tool_choice={"type": "function", "name": "get_weather"})
    req.pop("instructions", None)
    out = tr.chat_request_from_responses(req)
    assert out["response_format"]["json_schema"]["name"] == "s"
    assert "extra" not in out["response_format"]["json_schema"]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}


def test_image_detail_is_preserved_only_when_present():
    content = [
        {"type": "input_image", "image_url": "https://x/auto.png", "detail": "auto"},
        {"type": "input_image", "image_url": "https://x/low.png", "detail": "low"},
        {"type": "input_image", "image_url": "https://x/high.png", "detail": "high"},
        {"type": "input_image", "image_url": "https://x/default.png"},
    ]
    req = _request(
        input=[{"type": "message", "role": "user", "content": content}],
        instructions=None,
    )
    req.pop("instructions", None)

    translated = tr.chat_request_from_responses(req)
    images = [part["image_url"] for part in translated["messages"][0]["content"]]

    assert images == [
        {"url": "https://x/auto.png", "detail": "auto"},
        {"url": "https://x/low.png", "detail": "low"},
        {"url": "https://x/high.png", "detail": "high"},
        {"url": "https://x/default.png"},
    ]


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        unittest.FunctionTestCase(test_shapes_and_no_mutation),
        unittest.FunctionTestCase(test_continuation_records_merge),
        unittest.FunctionTestCase(test_same_response_groups_tool_calls),
        unittest.FunctionTestCase(test_deepseek_reasoning_stripping),
        unittest.FunctionTestCase(test_gemini_guard_and_passthrough),
        unittest.FunctionTestCase(test_json_schema_and_function_choice),
        unittest.FunctionTestCase(test_image_detail_is_preserved_only_when_present),
    ])
