from __future__ import annotations
import json
import unittest

from model_deck.plugins.stdio_codec import CodecError, StdioCodec, encode_frame, MAX_FRAME_BYTES


def feed_code(raw: bytes) -> str:
    try:
        StdioCodec().feed(raw)
    except CodecError as exc:
        return exc.code
    raise AssertionError("expected CodecError")


class StdioCodecTest(unittest.TestCase):
    def test_cap_matches_spec(self):
        self.assertEqual(MAX_FRAME_BYTES, 1_048_576)

    def test_roundtrip(self):
        codec = StdioCodec()
        raw = encode_frame({"jsonrpc": "2.0", "id": "1", "method": "plugin.v1.hello"})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(codec.feed(raw), [{"jsonrpc": "2.0", "id": "1", "method": "plugin.v1.hello"}])
        codec.finish()

    def test_fragmented_and_multiple_frames(self):
        codec = StdioCodec()
        a = encode_frame({"jsonrpc": "2.0", "id": "a"})
        b = encode_frame({"jsonrpc": "2.0", "id": "b"})
        self.assertEqual(codec.feed(a[:5]), [])
        self.assertEqual(
            codec.feed(a[5:] + b),
            [{"jsonrpc": "2.0", "id": "a"}, {"jsonrpc": "2.0", "id": "b"}],
        )
        codec.finish()

    def test_two_large_frames_in_one_chunk(self):
        codec = StdioCodec()
        pad = "x" * 600_000
        a = encode_frame({"jsonrpc": "2.0", "id": "a", "pad": pad})
        b = encode_frame({"jsonrpc": "2.0", "id": "b", "pad": pad})
        self.assertLess(len(a) - 1, MAX_FRAME_BYTES)
        self.assertLess(len(b) - 1, MAX_FRAME_BYTES)
        self.assertGreater(len(a) + len(b), MAX_FRAME_BYTES)
        out = codec.feed(a + b)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["id"], "a")
        self.assertEqual(out[1]["id"], "b")
        codec.finish()

    def test_exact_boundary_encode(self):
        overhead = len(json.dumps({"jsonrpc": "2.0", "pad": ""}, separators=(",", ":")).encode())
        ok = encode_frame({"jsonrpc": "2.0", "pad": "x" * (MAX_FRAME_BYTES - overhead)})
        self.assertEqual(len(ok), MAX_FRAME_BYTES + 1)
        with self.assertRaises(CodecError) as ctx:
            encode_frame({"jsonrpc": "2.0", "pad": "x" * (MAX_FRAME_BYTES - overhead + 1)})
        self.assertEqual(ctx.exception.code, "frame_too_large")

    def test_exact_boundary_decode(self):
        line = b"{" + b'"a":"' + b"x" * (MAX_FRAME_BYTES - 8) + b'"}' + b"\n"
        self.assertEqual(len(line), MAX_FRAME_BYTES + 1)
        self.assertEqual(len(StdioCodec().feed(line)), 1)
        big = b"{" + b'"a":"' + b"x" * (MAX_FRAME_BYTES - 7) + b'"}' + b"\n"
        with self.assertRaises(CodecError) as ctx:
            StdioCodec().feed(big)
        self.assertEqual(ctx.exception.code, "frame_too_large")

    def test_rejections(self):
        self.assertEqual(feed_code(b"\n"), "empty_frame")
        self.assertEqual(feed_code(b"[1,2]\n"), "batch_not_supported")
        self.assertEqual(feed_code(b"42\n"), "not_object")
        self.assertEqual(feed_code(b'{"a":1,"a":2}\n'), "duplicate_key")
        self.assertEqual(feed_code(b'{"a":NaN}\n'), "non_finite_number")
        self.assertEqual(feed_code(b'{"a":Infinity}\n'), "non_finite_number")
        self.assertEqual(feed_code(b'{"a":1e999}\n'), "non_finite_number")
        self.assertEqual(feed_code(b'{"a":{"b":[1,1e999]}}\n'), "non_finite_number")
        self.assertEqual(feed_code(b"{bad}\n"), "invalid_json")
        self.assertEqual(feed_code(b"\xff\xfe\n"), "invalid_utf8")

    def test_no_input_echo(self):
        secret = b"supersecret-payload-marker-123"
        with self.assertRaises(CodecError) as ctx:
            StdioCodec().feed(b'{"a":"' + secret + b'"bad: ' + secret + b'}\n')
        self.assertNotIn(secret, str(ctx.exception).encode())
        with self.assertRaises(CodecError) as ctx:
            encode_frame({"pad": "x" * (MAX_FRAME_BYTES + 1)})
        self.assertEqual(ctx.exception.code, "frame_too_large")
        self.assertNotIn(b"x" * 16, str(ctx.exception).encode())

    def test_finish_truncation(self):
        codec = StdioCodec()
        codec.feed(b'{"jsonrpc":"2.0",')
        with self.assertRaises(CodecError) as ctx:
            codec.finish()
        self.assertEqual(ctx.exception.code, "truncated")

    def test_oversize_stream_fails_closed_bounded(self):
        codec = StdioCodec()
        with self.assertRaises(CodecError) as ctx:
            codec.feed(b"x" * (MAX_FRAME_BYTES + 2))
        self.assertEqual(ctx.exception.code, "frame_too_large")
        self.assertEqual(len(codec), 0)
        with self.assertRaises(CodecError) as ctx:
            codec.feed(encode_frame({"jsonrpc": "2.0", "id": "ok"}))
        self.assertEqual(ctx.exception.code, "failed")
        with self.assertRaises(CodecError) as ctx:
            codec.finish()
        self.assertEqual(ctx.exception.code, "failed")

    def test_error_freezes_lifecycle(self):
        codec = StdioCodec()
        with self.assertRaises(CodecError) as ctx:
            codec.feed(b"{bad}\n")
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertEqual(len(codec), 0)
        with self.assertRaises(CodecError) as ctx:
            codec.feed(encode_frame({"jsonrpc": "2.0", "id": "later"}))
        self.assertEqual(ctx.exception.code, "failed")
        with self.assertRaises(CodecError) as ctx:
            codec.finish()
        self.assertEqual(ctx.exception.code, "failed")

    def test_encode_rejects_non_object_and_non_finite(self):
        for bad in ([1], "s", 42):
            with self.assertRaises(CodecError) as ctx:
                encode_frame(bad)
            self.assertEqual(ctx.exception.code, "not_object")
        with self.assertRaises(CodecError) as ctx:
            encode_frame({"a": float("nan")})
        self.assertEqual(ctx.exception.code, "non_finite_number")

    def test_encode_rejects_types_keys_cycles_safely(self):
        marker = "secret-marker-789"
        with self.assertRaises(CodecError) as ctx:
            encode_frame({1: marker, "1": "other"})
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertNotIn(marker, str(ctx.exception))
        with self.assertRaises(CodecError) as ctx:
            encode_frame({"a": object()})
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertNotIn("object", str(ctx.exception))
        cyclic: dict = {"a": marker}
        cyclic["self"] = cyclic
        with self.assertRaises(CodecError) as ctx:
            encode_frame(cyclic)
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertNotIn(marker, str(ctx.exception))
        nested = {"a": [1, {"b": marker}]}
        nested["loop"] = [nested]
        with self.assertRaises(CodecError) as ctx:
            encode_frame(nested)
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertNotIn(marker, str(ctx.exception))

    def test_deep_nesting_decode_latches_failed(self):
        codec = StdioCodec()
        depth = 10000
        raw = b"[" * depth + b"]" * depth + b"\n"
        with self.assertRaises(CodecError) as ctx:
            codec.feed(raw)
        self.assertNotIn(str(depth), str(ctx.exception))
        with self.assertRaises(CodecError) as ctx2:
            codec.feed(b'{"jsonrpc":"2.0"}\n')
        self.assertEqual(ctx2.exception.code, "failed")

    def test_deep_nesting_encode_normalized(self):
        value: object = []
        for _ in range(2000):
            value = [value]
        with self.assertRaises(CodecError):
            encode_frame({"jsonrpc": "2.0", "v": value})


if __name__ == "__main__":
    unittest.main()
