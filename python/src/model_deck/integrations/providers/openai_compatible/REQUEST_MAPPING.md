# OpenAI-compatible request mapping

## Purpose and ownership

`request_mapping.py` converts the engine's typed `RunRequest` into detached
OpenAI Responses request data before any HTTP or credential access. It uses the
engine-owned normalized-message, tool-definition, and run-option codecs rather
than defining another message grammar or accepting host-native history.

## Responses mapping

`build_responses_request(request)` requires an actual `RunRequest` and maps:

- `route_snapshot.provider_model_id` to `model`;
- normalized messages to ordered `input` items without rewriting roles, image
  parts, function calls, or function outputs;
- advertised tools to flat Responses function tools with `type`, `name`,
  `parameters`, and optional `description`;
- instructions, maximum output tokens, parallel-tool control, and service tier
  to their same-named Responses fields;
- reasoning effort to `reasoning.effort`;
- output format to `text.format`;
- automatic, none, and required tool choices to their string forms, and a named
  choice to `{type: function, name: ...}`.

The request sets `stream: true` for the package's streaming response decoder.
Explicit empty instructions, `false` parallel-tool control, and all three
service-tier values remain present. Omitted options remain omitted. Codec
outputs are detached, so changing the returned request cannot modify the
engine request or its nested JSON Schemas.

## Chat fallback

`build_chat_request(request, provider_id=None)` first builds the same Responses
request and then calls the existing `chat_request_from_responses` translation.
The provider ID remains an explicit input for its established DeepSeek and
Gemini reasoning rules. The service tier is retained for executor/provider
policy.

Chat translation cannot represent image parts inside function-call results.
The mapper rejects that valid Responses history before the existing helper can
drop the image. User-message images and their optional `auto`, `low`, or `high`
detail remain supported. The mapper does not add provider capability policy or
change the existing reasoning rules.

## Errors and boundaries

Malformed typed requests, codec failures, lossy chat mappings, and established
provider reasoning guards from the chat translator raise
`OpenAICompatibleRequestMappingError` with the fixed message
`openai-compatible request mapping failed`. Request content and schema details
are not included in the error.

This module performs no HTTP, credential, continuation, storage, host, or
process-runtime work. Execution policy and billing-safe fallback ownership stay
outside this pure mapping boundary.

## Verification

From `Architecture/python`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest \
  tests.provider_openai_compatible.test_request_mapping
```
