"""Acceptance tests for the B13 A OpenAI-compatible SSE adapter slice."""
from __future__ import annotations

import unittest

from model_deck.engine.runs.ports import ProviderRunEvent
from model_deck.integrations.providers.openai_compatible import (
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feed_all(decoder: SseDecoder, payload: bytes, chunk_size: int = 1) -> list[SseMessage]:
    """Feed ``payload`` one byte at a time and accumulate every dispatch."""

    out: list[SseMessage] = []
    for offset in range(0, len(payload), chunk_size):
        out.extend(decoder.feed(payload[offset : offset + chunk_size]))
    return out


# ---------------------------------------------------------------------------
# Decoder: framing & chunk boundaries
# ---------------------------------------------------------------------------


class DecoderFramingTests(unittest.TestCase):
    def test_single_data_field_dispatches(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data: hello\n\n")
        self.assertEqual(
            out,
            (SseMessage(data="hello"),),
        )

    def test_arbitrary_chunk_boundaries_byte_by_byte(self) -> None:
        decoder = SseDecoder()
        payload = b"data: hello\n\n"
        messages = _feed_all(decoder, payload, chunk_size=1)
        self.assertEqual(messages, [SseMessage(data="hello")])

    def test_chunk_splits_mid_field(self) -> None:
        decoder = SseDecoder()
        out: list[SseMessage] = []
        out.extend(decoder.feed(b"da"))
        out.extend(decoder.feed(b"ta: he"))
        out.extend(decoder.feed(b"llo\n"))
        out.extend(decoder.feed(b"\n"))
        self.assertEqual(out, [SseMessage(data="hello")])

    def test_chunk_splits_mid_blank_line(self) -> None:
        decoder = SseDecoder()
        out: list[SseMessage] = []
        out.extend(decoder.feed(b"data: a\n"))
        out.extend(decoder.feed(b"\n"))
        out.extend(decoder.feed(b"data: b\n\n"))
        self.assertEqual(
            out,
            [
                SseMessage(data="a"),
                SseMessage(data="b"),
            ],
        )

    def test_event_field_separate_from_data(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"event: message\ndata: hello\n\n")
        self.assertEqual(
            out,
            (SseMessage(data="hello", event="message"),),
        )

    def test_id_field_persists_last_event_id(self) -> None:
        decoder = SseDecoder()
        first = decoder.feed(b"id: 7\ndata: a\n\n")
        second = decoder.feed(b"data: b\n\n")
        self.assertEqual(decoder.last_event_id, "7")
        self.assertEqual(first, (SseMessage(data="a", event_id="7"),))
        self.assertEqual(second, (SseMessage(data="b", event_id="7"),))

    def test_id_only_block_updates_state_without_emitting(self) -> None:
        decoder = SseDecoder()
        self.assertEqual(decoder.feed(b"id: 7\n\n"), ())
        self.assertEqual(decoder.last_event_id, "7")
        self.assertEqual(
            decoder.feed(b"data: later\n\n"),
            (SseMessage(data="later", event_id="7"),),
        )

    def test_id_field_overwritten_later(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"id: 7\ndata: a\n\n")
        decoder.feed(b"id: 9\ndata: b\n\n")
        self.assertEqual(decoder.last_event_id, "9")

    def test_empty_data_field_dispatches(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data:\n\n")
        self.assertEqual(out, (SseMessage(data=""),))


# ---------------------------------------------------------------------------
# Decoder: line endings & UTF-8
# ---------------------------------------------------------------------------


class DecoderLineEndingTests(unittest.TestCase):
    def test_crlf_line_endings(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data: hello\r\n\r\n")
        self.assertEqual(out, (SseMessage(data="hello"),))

    def test_cr_only_line_endings(self) -> None:
        decoder = SseDecoder()
        out = list(decoder.feed(b"data: hello\r\r"))
        out.extend(decoder.finish())
        self.assertEqual(out, [SseMessage(data="hello")])

    def test_crlf_split_across_chunks_preserves_multiline_event(self) -> None:
        decoder = SseDecoder()
        out: list[SseMessage] = []
        out.extend(decoder.feed(b"event: message\r"))
        out.extend(decoder.feed(b"\ndata: first\r"))
        out.extend(decoder.feed(b"\ndata: second\r"))
        out.extend(decoder.feed(b"\n\r"))
        out.extend(decoder.feed(b"\n"))
        self.assertEqual(
            out,
            [SseMessage(data="first\nsecond", event="message")],
        )

    def test_crlf_byte_by_byte_is_one_line_ending(self) -> None:
        decoder = SseDecoder()
        out = _feed_all(decoder, b"data: first\r\ndata: second\r\n\r\n")
        self.assertEqual(out, [SseMessage(data="first\nsecond")])

    def test_mixed_line_endings(self) -> None:
        decoder = SseDecoder()
        out: list[SseMessage] = []
        out.extend(decoder.feed(b"data: a\r\n"))
        out.extend(decoder.feed(b"\r"))
        out.extend(decoder.feed(b"data: b\n\n"))
        self.assertEqual(
            out,
            [SseMessage(data="a"), SseMessage(data="b")],
        )

    def test_multibyte_utf8_value(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed("data: héllo 🌍\n\n".encode("utf-8"))
        self.assertEqual(out, (SseMessage(data="héllo 🌍"),))

    def test_chunk_splits_utf8_multibyte(self) -> None:
        decoder = SseDecoder()
        payload = "data: 🌍\n\n".encode("utf-8")
        # Split the 4-byte emoji across two chunks.
        out: list[SseMessage] = []
        out.extend(decoder.feed(payload[:6]))
        out.extend(decoder.feed(payload[6:]))
        self.assertEqual(out, [SseMessage(data="🌍")])

    def test_strict_utf8_rejects_invalid_byte(self) -> None:
        decoder = SseDecoder()
        with self.assertRaises(SseProtocolError):
            decoder.feed(b"data: \xff\xfe\n\n")


# ---------------------------------------------------------------------------
# Decoder: multi-line data, comments, DONE, bounds
# ---------------------------------------------------------------------------


class DecoderMultiLineCommentDoneTests(unittest.TestCase):
    def test_multiline_data_joined_with_lf(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data: line one\ndata: line two\n\n")
        self.assertEqual(out, (SseMessage(data="line one\nline two"),))

    def test_comments_are_ignored(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b": this is a comment\ndata: hello\n\n")
        self.assertEqual(out, (SseMessage(data="hello"),))

    def test_comment_must_be_valid_utf8(self) -> None:
        decoder = SseDecoder()
        with self.assertRaises(SseProtocolError):
            decoder.feed(b": \xff\n\n")

    def test_comment_only_line_does_not_dispatch(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b": ping\n\ndata: hello\n\n")
        self.assertEqual(out, (SseMessage(data="hello"),))

    def test_done_marker_dispatches_done_true(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data: hello\n\ndata: [DONE]\n\n")
        self.assertEqual(
            out,
            (
                SseMessage(data="hello"),
                SseMessage(data="[DONE]", done=True),
            ),
        )

    def test_done_with_leading_whitespace(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data: [DONE]\n\n")
        self.assertEqual(out, (SseMessage(data="[DONE]", done=True),))

    def test_field_after_done_is_protocol_error(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: [DONE]\n\n")
        with self.assertRaises(SseProtocolError):
            decoder.feed(b"data: trailing\n\n")

    def test_comment_after_done_is_allowed(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: [DONE]\n\n")
        # Comments are always permitted, even after DONE.
        out = decoder.feed(b": trailing comment\n\n")
        self.assertEqual(out, ())

    def test_blank_line_after_done_is_noop(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: [DONE]\n\n")
        # A second blank line dispatch attempt should also be rejected
        # because the trailing blank line is itself a non-comment field
        # dispatch path. The decoder treats the empty event without pending
        # fields as a no-op, so this case stays silent.
        out = decoder.feed(b"\n")
        self.assertEqual(out, ())


# ---------------------------------------------------------------------------
# Decoder: byte budget & finish / truncation
# ---------------------------------------------------------------------------


class DecoderBoundsAndFinishTests(unittest.TestCase):
    def test_default_budget_accepts_moderate_event(self) -> None:
        decoder = SseDecoder()
        body = "x" * 1024
        out = decoder.feed(f"data: {body}\n\n".encode("utf-8"))
        self.assertEqual(out, (SseMessage(data=body),))

    def test_byte_limit_raises(self) -> None:
        decoder = SseDecoder(max_event_bytes=64)
        with self.assertRaises(SseByteLimitError):
            decoder.feed(b"data: " + b"x" * 100 + b"\n\n")

    def test_byte_limit_is_per_event_in_multi_event_chunk(self) -> None:
        decoder = SseDecoder(max_event_bytes=16)
        self.assertEqual(
            decoder.feed(b"data: x\n\ndata: y\n\ndata: z\n\n"),
            (
                SseMessage(data="x"),
                SseMessage(data="y"),
                SseMessage(data="z"),
            ),
        )

    def test_finish_returns_empty_after_clean_stream(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: hello\n\n")
        self.assertEqual(decoder.finish(), ())
        self.assertTrue(decoder.finished)

    def test_finish_rejects_unterminated_event(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: hello\n")  # no blank line dispatch
        with self.assertRaises(SseTruncatedError):
            decoder.finish()

    def test_finish_rejects_partial_field(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: hel")  # no newline at all
        with self.assertRaises(SseTruncatedError):
            decoder.finish()

    def test_finish_rejects_truncated_utf8(self) -> None:
        decoder = SseDecoder()
        # Start of a 4-byte emoji, never completed.
        decoder.feed(b"data: \xf0\x9f")  # incomplete codepoint
        with self.assertRaises(SseTruncatedError):
            decoder.finish()

    def test_finish_accepts_comment_only_residual(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b": trailing comment")
        self.assertEqual(decoder.finish(), ())

    def test_feed_after_finish_raises(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: hello\n\n")
        decoder.finish()
        with self.assertRaises(SseProtocolError):
            decoder.feed(b"data: more\n\n")

    def test_finish_is_idempotent(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"data: hello\n\n")
        self.assertEqual(decoder.finish(), ())
        # Second finish should return empty, not raise.
        self.assertEqual(decoder.finish(), ())

    def test_non_bytes_input_rejected(self) -> None:
        decoder = SseDecoder()
        with self.assertRaises(SseProtocolError):
            decoder.feed("not bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decoder: field semantics & unknowns
# ---------------------------------------------------------------------------


class DecoderFieldSemanticsTests(unittest.TestCase):
    def test_unknown_field_is_ignored(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"random: ignore me\ndata: hello\n\n")
        self.assertEqual(out, (SseMessage(data="hello"),))

    def test_event_without_data_dispatches(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"event: ping\n\n")
        self.assertEqual(out, (SseMessage(data="", event="ping"),))

    def test_value_without_leading_space_after_colon(self) -> None:
        decoder = SseDecoder()
        out = decoder.feed(b"data:hello\n\n")
        self.assertEqual(out, (SseMessage(data="hello"),))

    def test_id_with_null_byte_is_ignored(self) -> None:
        decoder = SseDecoder()
        decoder.feed(b"id: bad\x00id\ndata: a\n\n")
        # WHATWG: a null byte in the id value causes the field to be ignored,
        # so last_event_id stays None and the event has no event_id.
        self.assertIsNone(decoder.last_event_id)
        self.assertEqual(decoder.feed(b"\n"), ())

    def test_field_name_with_unicode_rejected(self) -> None:
        decoder = SseDecoder()
        with self.assertRaises(SseProtocolError):
            decoder.feed(b"\xc3\xa9vent: oops\n\n")

    def test_bom_may_be_split_across_chunks(self) -> None:
        decoder = SseDecoder()
        self.assertEqual(decoder.feed(b""), ())
        self.assertEqual(decoder.feed(b"\xef"), ())
        self.assertEqual(decoder.feed(b"\xbb"), ())
        self.assertEqual(decoder.feed(b"\xbfdata: ok\n\n"), (SseMessage(data="ok"),))


# ---------------------------------------------------------------------------
# JSON envelope helper
# ---------------------------------------------------------------------------


class DecodeJsonObjectTests(unittest.TestCase):
    def test_decodes_simple_object(self) -> None:
        decoder = SseDecoder()
        messages = decoder.feed(b'data: {"a": 1, "b": "x"}\n\n')
        parsed = decode_json_object(messages[0])
        self.assertEqual(parsed, {"a": 1, "b": "x"})

    def test_returns_detached_copy(self) -> None:
        decoder = SseDecoder()
        messages = decoder.feed(b'data: {"a": 1}\n\n')
        parsed = decode_json_object(messages[0])
        parsed["a"] = 999
        # Re-decoding must yield the original value.
        again = decode_json_object(messages[0])
        self.assertEqual(again, {"a": 1})

    def test_done_marker_rejected(self) -> None:
        marker = SseMessage(data="[DONE]", done=True)
        with self.assertRaises(SseJsonError):
            decode_json_object(marker)

    def test_malformed_json_rejected(self) -> None:
        message = SseMessage(data="not json")
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_top_level_array_rejected(self) -> None:
        message = SseMessage(data="[1, 2, 3]")
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_top_level_scalar_rejected(self) -> None:
        message = SseMessage(data="42")
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_top_level_null_rejected(self) -> None:
        message = SseMessage(data="null")
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_nan_rejected(self) -> None:
        message = SseMessage(data='{"x": NaN}')
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_infinity_rejected(self) -> None:
        message = SseMessage(data='{"x": Infinity}')
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_overflowed_number_rejected(self) -> None:
        message = SseMessage(data='{"outer": [{"value": 1e999}]}')
        with self.assertRaises(SseJsonError):
            decode_json_object(message)

    def test_non_message_rejected(self) -> None:
        with self.assertRaises(SseJsonError):
            decode_json_object("not a message")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Provider lifecycle validator
# ---------------------------------------------------------------------------


RUN_ID = "run-123"


def _event(kind: str, *, payload: Any = None, run_id: str = RUN_ID, observed_at: str = "2026-01-01T00:00:00Z") -> ProviderRunEvent:
    return ProviderRunEvent(
        kind=kind,
        run_id=run_id,
        observed_at=observed_at,
        payload=payload,
    )


class ValidatorLifecycleTests(unittest.TestCase):
    def test_happy_path_completes(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("content.delta", payload={"delta": "hi"}))
        v.submit(_event("usage.observed", payload={"usage": {"tokens": 3}}))
        v.submit(_event("run.completed"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_failed_terminal_is_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.failed", payload={"code": "x"}))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_cancelled_terminal_is_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.cancelled"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_interrupted_terminal_is_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.interrupted"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_failed_terminal_before_started_is_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.failed", payload={"code": "provider_unavailable"}))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_interrupted_terminal_before_started_is_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.interrupted"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_run_cancelling_is_non_terminal(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.cancelling"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.finish_segment()
        v.submit(_event("run.cancelled"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_missing_terminal_raises(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.finish_segment()

    def test_tool_suspended_when_outstanding(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={
                    "call_id": "call-1",
                    "tool_name": "search",
                    "arguments": {"q": "fixture"},
                },
            )
        )
        self.assertEqual(v.finish_segment(), SegmentTermination.TOOL_SUSPENDED)

    def test_segment_continues_after_tool_result_without_second_started(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        v.mark_tool_result("call-1")
        v.submit(_event("content.delta", payload={"delta": "ok"}))
        v.submit(_event("run.completed"))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_second_tool_call_requires_mark_tool_result(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(
                _event(
                    "tool.requested",
                    payload={"call_id": "call-2", "tool_name": "x", "arguments": {}},
                )
            )


class ValidatorRejectionTests(unittest.TestCase):
    def test_duplicate_started_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("run.started"))

    def test_event_before_started_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("content.delta", payload={"delta": "hi"}))

    def test_completed_before_started_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("run.completed"))

    def test_unknown_kind_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("totally.bogus"))

    def test_run_id_mismatch_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("run.started", run_id="other"))

    def test_terminal_twice_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.completed"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("run.completed"))

    def test_event_after_terminal_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(_event("run.completed"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("content.delta", payload={"delta": "x"}))

    def test_completed_with_outstanding_tool_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("run.completed"))

    def test_failed_with_outstanding_tool_allowed(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        v.submit(_event("run.failed", payload={"code": "x"}))
        self.assertEqual(v.finish_segment(), SegmentTermination.TERMINAL)

    def test_tool_requested_missing_payload_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("tool.requested", payload=None))

    def test_tool_requested_missing_call_id_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit(_event("tool.requested", payload={"tool_name": "search", "arguments": {}}))

    def test_mark_tool_result_without_outstanding_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        with self.assertRaises(ProviderValidatorProtocolError):
            v.mark_tool_result("call-1")

    def test_mark_tool_result_mismatch_rejected(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        with self.assertRaises(ProviderValidatorProtocolError):
            v.mark_tool_result("call-2")

    def test_submit_requires_provider_run_event(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        with self.assertRaises(ProviderValidatorProtocolError):
            v.submit("not an event")  # type: ignore[arg-type]

    def test_constructor_rejects_empty_run_id(self) -> None:
        with self.assertRaises(ProviderValidatorError):
            ProviderEventTerminalValidator("")

    def test_mark_tool_result_rejects_non_string(self) -> None:
        v = ProviderEventTerminalValidator(RUN_ID)
        v.submit(_event("run.started"))
        v.submit(
            _event(
                "tool.requested",
                payload={"call_id": "call-1", "tool_name": "search", "arguments": {}},
            )
        )
        with self.assertRaises(ProviderValidatorError):
            v.mark_tool_result(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Adapter error visibility (no raw bodies / credentials)
# ---------------------------------------------------------------------------


class AdapterErrorVisibilityTests(unittest.TestCase):
    def test_sse_errors_subclass_base(self) -> None:
        self.assertTrue(issubclass(SseProtocolError, SseError))
        self.assertTrue(issubclass(SseByteLimitError, SseError))
        self.assertTrue(issubclass(SseTruncatedError, SseError))
        self.assertTrue(issubclass(SseJsonError, SseError))

    def test_validator_errors_subclass_base(self) -> None:
        self.assertTrue(
            issubclass(ProviderValidatorProtocolError, ProviderValidatorError)
        )

    def test_json_error_does_not_echo_payload(self) -> None:
        sentinel_secret = "sk-VENDOR-SECRET-TOKEN-1234567890"
        message = SseMessage(data=f'{{"token": "{sentinel_secret}"')
        try:
            decode_json_object(message)
        except SseJsonError as exc:
            self.assertNotIn(sentinel_secret, str(exc))
        else:
            self.fail("expected SseJsonError")


if __name__ == "__main__":
    unittest.main()
