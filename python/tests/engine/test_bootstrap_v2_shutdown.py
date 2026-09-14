"""Tests for bootstrap shutdown of engine-owned extension resources."""
from __future__ import annotations

import unittest
from unittest import mock

from model_deck.bootstrap import _compose_shutdown_callback


class ComposeShutdownCallbackTests(unittest.TestCase):
    def test_returns_none_when_nothing_to_close(self) -> None:
        self.assertIsNone(_compose_shutdown_callback(None))

    def test_returns_none_when_extension_host_has_no_close(self) -> None:
        extension_host = object()
        self.assertIsNone(_compose_shutdown_callback(extension_host))

    def test_returns_extension_host_close(self) -> None:
        extension_host = mock.MagicMock()
        callback = _compose_shutdown_callback(extension_host)
        assert callback is not None
        callback()
        extension_host.close.assert_called_once_with()

    def test_extension_host_failure_propagates(self) -> None:
        extension_host = mock.MagicMock()
        extension_host.close.side_effect = RuntimeError("extension close failed")
        callback = _compose_shutdown_callback(extension_host)
        assert callback is not None
        with self.assertRaises(RuntimeError) as raised:
            callback()
        self.assertEqual(str(raised.exception), "extension close failed")

    def test_idempotent_after_engine_server_marks_complete(self) -> None:
        # The EngineServer wraps the callback in _run_shutdown_callback and
        # short-circuits when shutdown_complete is True. The composed callback
        # itself remains a plain callable and is safe to invoke repeatedly.
        extension_host = mock.MagicMock()
        callback = _compose_shutdown_callback(extension_host)
        assert callback is not None
        callback()
        callback()
        self.assertEqual(extension_host.close.call_count, 2)


if __name__ == "__main__":
    unittest.main()
