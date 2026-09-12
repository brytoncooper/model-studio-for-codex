# OpenAI-compatible request translation

Pure Responses-to-chat-completions request translation, extracted from
`chat_wire.chat_request_from_responses` and `chat_wire._apply_reasoning_settings`.

## What it preserves

- Message shapes: system/developer-to-system mapping, text-only collapsing,
  mixed text/image parts, assistant `output_text` join.
- Tool calls grouped onto the same-turn assistant message via continuation
  `response_id`; `call_fields`/`assistant_fields` metadata merged by copy.
- Tool results: list-of-`input_text` joined, non-string JSON-encoded.
- Reasoning items are never sent; reasoning effort becomes
  provider-specific chat controls (DeepSeek V4 thinking toggle plus effort
  remap; Gemini effort remap with the disable-thinking guard).
- Model, `max_output_tokens` to `max_tokens`, text format to
  `response_format`, tool definitions narrowed to name/description/
  parameters/strict, function `tool_choice` normalization.
- No input mutation: metadata merges use deep copies.

## Host-state boundary

- Legacy `continuation.load(scope, item)` becomes the explicit `load_record`
  callable `(item, index) -> {metadata, response_id} | None`.
- Legacy `provider_for_base_url` becomes the explicit `provider_id` string.
- ID generation defaults to UUID but accepts an injectable `new_id`.
- No env, home, network, or credential access.

## Known limits

- Input must already be Responses-normalized; this module does not run
  `translate_request`/`translate_input`/`flatten_tools`.
- Streaming (`ChatStreamTranslator`) and HTTP/wire fallback live elsewhere.
- OpenAI-bound input stripping (`sanitize_openai_input`, healing) lives in
  the continuation package.
