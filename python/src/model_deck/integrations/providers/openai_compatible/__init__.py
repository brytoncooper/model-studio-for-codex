"""OpenAI-compatible provider integration (V2 surface).


This package adapts an OpenAI-compatible HTTP endpoint into the engine

`ProviderExecutionPort` and exposes a V2 profile loader and composer for

validated non-secret provider configuration. The SSE decoder, JSON envelope

helpers, and segment-terminal validator remain in the same package.



V2 profile loading:



- `OpenAICompatibleProfile.load(path)` validates and parses a non-secret

  JSON profile document (schema_version 1) into an immutable record.

- `compose_openai_compatible_profile(profile, *, post_stream, ...)` wires

  the profile into an execution port and a `ProviderRouteDefinition`

  with declared capabilities (tools supported, parallel_tool_calls

  unsupported, execution mode RESPONSES).

- `subprocess_credential_resolver(command)` returns a `CredentialResolver`

  that executes the profile's `credential_command` with no shell, a

  bounded timeout, captured stdout, and a fixed sanitized error path.

- `endpoint_resolver_from_records(records)` returns an `EndpointResolver`

  that accepts only matching `(ref, revision)` records and returns the

  parsed host/port/TLS/path/wire/vendor fields.



Identity-aware usage normalization:



`ResponsesEventTranslator` requires identity (session_id, registration_id,

connection_id, provider_model_id) at construction. Production execution

always supplies identity from `RunRequest`. On a terminal provider usage

envelope, the translator emits one `usage.observed` ProviderRunEvent per

available non-negative integer token counter (input_tokens,

output_tokens, input_tokens_details.cached_tokens). Each payload conforms

to `{usage: {run_id, session_id, registration_id, connection_id,

provider_model_id, observed_at, units, unit_kind}}` and validates against

the engine usage reconciliation schema. Missing counters remain absent;

no zero-fill, no cost fields.

"""
from __future__ import annotations

from model_deck.integrations.providers.openai_compatible.configuration import (
    OpenAICompatibleProfile,
    OpenAICompatibleProfileError,
    compose_openai_compatible_profile,
    endpoint_resolver_from_records,
    subprocess_credential_resolver,
)
from model_deck.integrations.providers.openai_compatible.events import (
    OpenAICompatibleEventError,
    ResponsesEventTranslator,
    RunIdentity,
)
from model_deck.integrations.providers.openai_compatible.execution import (
    CredentialResolver,
    EndpointResolver,
    OpenAICompatibleEndpointConfig,
    OpenAICompatibleExecutionError,
    OpenAICompatibleExecutionPort,
    OpenAICompatibleRunHandle,
    WireMode,
)
from model_deck.integrations.providers.openai_compatible.sse import (
    ProviderEventTerminalValidator,
    ProviderValidatorError,
    ProviderValidatorProtocolError,
    SegmentTermination,
    SseByteLimitError,
    SseDecoder,
    SseError,
    SseJsonError,
    SseMessage,
    SseProtocolError,
    SseTruncatedError,
    decode_json_object,
)

__all__ = [
    "CredentialResolver",
    "EndpointResolver",
    "OpenAICompatibleEndpointConfig",
    "OpenAICompatibleEventError",
    "OpenAICompatibleExecutionError",
    "OpenAICompatibleExecutionPort",
    "OpenAICompatibleProfile",
    "OpenAICompatibleProfileError",
    "OpenAICompatibleRunHandle",
    "ProviderEventTerminalValidator",
    "ProviderValidatorError",
    "ProviderValidatorProtocolError",
    "ResponsesEventTranslator",
    "RunIdentity",
    "SegmentTermination",
    "SseByteLimitError",
    "SseDecoder",
    "SseError",
    "SseJsonError",
    "SseMessage",
    "SseProtocolError",
    "SseTruncatedError",
    "WireMode",
    "compose_openai_compatible_profile",
    "decode_json_object",
    "endpoint_resolver_from_records",
    "subprocess_credential_resolver",
]
