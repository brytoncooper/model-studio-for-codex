"""OpenAI-compatible SSE adapter (B13 slice A).

Production adapter infrastructure for OpenAI-compatible streaming responses.
This slice contains only the SSE wire decoder, JSON envelope helpers, and the
B12 ProviderRunEvent segment-terminal validator. It performs no HTTP, no host
conversion, and no real network calls.
"""
from __future__ import annotations

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
    "ProviderEventTerminalValidator",
    "ProviderValidatorError",
    "ProviderValidatorProtocolError",
    "SegmentTermination",
    "SseByteLimitError",
    "SseDecoder",
    "SseError",
    "SseJsonError",
    "SseMessage",
    "SseProtocolError",
    "SseTruncatedError",
    "decode_json_object",
]
