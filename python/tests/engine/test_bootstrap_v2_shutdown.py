"""Tests for bootstrap shutdown composition across extension host + provider.

The bootstrap composes a single shutdown callback for the EngineServer that
closes the external extension host and the injected provider execution in
sequence. Provider close failures must not skip extension host close, and the
first error propagates after both steps have run, preserving the existing
"raise on shutdown error" semantics.
"""
from __future__ import annotations

import unittest
from unittest import mock

from model_deck.bootstrap import _compose_shutdown_callback


class ComposeShutdownCallbackTests(unittest.TestCase):
    def test_returns_none_when_nothing_to_close(self) -> None:
        self.assertIsNone(_compose_shutdown_callback(None, None))

    def test_returns_none_when_provider_has_no_close(self) -> None:
        # When only a provider that lacks ``close`` is supplied, there is
        # nothing to shut down, so the callback is None. We use an extension
        # host without a ``close`` callable (a plain object) so the provider
        # is the sole subject of the test.
        extension_host = object()
        provider = object()
        self.assertIsNone(_compose_shutdown_callback(extension_host, provider))

    def test_returns_just_extension_host_close_when_no_provider(self) -> None:
        extension_host = mock.MagicMock()
        callback = _compose_shutdown_callback(extension_host, None)
        assert callback is not None
        callback()
        extension_host.close.assert_called_once_with()

    def test_returns_just_provider_close_when_no_extension_host(self) -> None:
        provider = mock.MagicMock()
        callback = _compose_shutdown_callback(None, provider)
        assert callback is not None
        callback()
        provider.close.assert_called_once_with()

    def test_closes_extension_host_then_provider_in_order(self) -> None:
        extension_host = mock.MagicMock()
        provider = mock.MagicMock()
        order: list[str] = []
        extension_host.close.side_effect = lambda: order.append("extension")
        provider.close.side_effect = lambda: order.append("provider")
        callback = _compose_shutdown_callback(extension_host, provider)
        assert callback is not None
        callback()
        self.assertEqual(order, ["extension", "provider"])
        extension_host.close.assert_called_once_with()
        provider.close.assert_called_once_with()

    def test_provider_failure_does_not_skip_extension_host(self) -> None:
        extension_host = mock.MagicMock()
        provider = mock.MagicMock()
        provider.close.side_effect = RuntimeError("provider close failed")
        callback = _compose_shutdown_callback(extension_host, provider)
        assert callback is not None
        with self.assertRaises(RuntimeError) as raised:
            callback()
        self.assertEqual(str(raised.exception), "provider close failed")
        extension_host.close.assert_called_once_with()

    def test_extension_host_failure_runs_provider_then_raises(self) -> None:
        extension_host = mock.MagicMock()
        extension_host.close.side_effect = RuntimeError("extension close failed")
        provider = mock.MagicMock()
        callback = _compose_shutdown_callback(extension_host, provider)
        assert callback is not None
        with self.assertRaises(RuntimeError) as raised:
            callback()
        self.assertEqual(str(raised.exception), "extension close failed")
        provider.close.assert_called_once_with()

    def test_first_error_propagates_when_both_fail(self) -> None:
        extension_host = mock.MagicMock()
        extension_host.close.side_effect = RuntimeError("first failure")
        provider = mock.MagicMock()
        provider.close.side_effect = ValueError("second failure")
        callback = _compose_shutdown_callback(extension_host, provider)
        assert callback is not None
        with self.assertRaises(RuntimeError) as raised:
            callback()
        self.assertEqual(str(raised.exception), "first failure")
        extension_host.close.assert_called_once_with()
        provider.close.assert_called_once_with()

    def test_idempotent_after_engine_server_marks_complete(self) -> None:
        # The EngineServer wraps the callback in _run_shutdown_callback and
        # short-circuits when shutdown_complete is True. The composed callback
        # itself remains a plain callable and is safe to invoke repeatedly.
        extension_host = mock.MagicMock()
        provider = mock.MagicMock()
        callback = _compose_shutdown_callback(extension_host, provider)
        assert callback is not None
        callback()
        callback()
        self.assertEqual(extension_host.close.call_count, 2)
        self.assertEqual(provider.close.call_count, 2)


if __name__ == "__main__":
    unittest.main()
