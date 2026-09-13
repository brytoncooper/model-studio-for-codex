# Chat-completions stream translation

`chat_stream.py` translates decoded OpenAI-compatible chat-completions chunks
into the Responses-style event sequence consumed by existing native clients.
It owns stream state and wire-shape translation only. SSE byte decoding, HTTP,
provider routing, credentials, host history, engine run state, and persistence
remain outside this module.

## Contract

Create one `ChatStreamTranslator` per response. Pass each decoded JSON object to
`feed(chunk)` in arrival order, then call `finish()` exactly once when the stream
ends. Both methods return lists of Responses-style event dictionaries. Calls to
`feed` after completion and repeated calls to `finish` return an empty list.

The translator preserves the established chat-wire behavior:

- the first accepted chunk emits `response.created`;
- reasoning and text fragments produce distinct output items in first-seen
  order;
- fragmented tool names and arguments accumulate by tool-call index;
- a tool item is announced only after both its name and call ID exist;
- usage maps prompt, completion, cached, reasoning, and total token fields to
  the existing Responses document shape;
- `stop` and `tool_calls` complete only when the terminal reason matches the
  accumulated outputs; `length` and `content_filter` remain incomplete results;
- provider errors, multiple choices, conflicting terminals, post-terminal
  output, and incomplete tool calls fail with the existing display-safe
  messages.

Provider metadata fields `reasoning_content`, `reasoning`,
`reasoning_details`, and `extra_content` merge across fragments. Indexed lists
are bounded to indices 0 through 1024. IDs and type markers replace earlier
values, equal signatures are not duplicated, other string fragments append,
and all stored values are detached copies.

## Continuation and IDs

An optional continuation collaborator may implement
`save(scope, response_id, records)`. Successful completion supplies assistant,
tool-call, and reasoning metadata records before done events are returned. The
translator does not open or import a store.

Default response and output IDs preserve the legacy UUID formats. Tests and
other isolated callers may inject `new_id(prefix)` and `new_item_id(scope)`;
these factories supply identity only and cannot change event ordering or
content.

## Extension and tests

Add vendor-specific chunk normalization before this translator unless it is an
already-supported metadata field. Keep transport retries and fallback outside
the state machine. New terminal reasons or output item types require copied
stable fixtures that show the complete event order and failure behavior.

From `python/`, run:

```sh
PYTHONPATH=src python3.12 -m unittest \
  tests.provider_openai_compatible.test_chat_stream
```

The focused tests use deterministic IDs and copied expected events for
fragmented reasoning/text, fragmented tools with late metadata, usage, terminal
failures, continuation records, and empty streams. They do not exercise SSE,
HTTP, a live provider, or host integration.
