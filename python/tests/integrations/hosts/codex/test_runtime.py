"""Tests for python/src/model_deck/integrations/hosts/codex/runtime.py.

B10: typed runtime discovery, capability probe, app-server protocol
classification, and pure launch-preparation assembly. No live
/Applications access, no process launch, no credentials.
"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from model_deck.integrations.hosts.codex.runtime import (
    APP_SERVER_ARGUMENT,
    BUNDLE_NAME_PREFERENCE,
    EXECUTABLE_RELATIVE_PATH,
    SUPPORTED_BUNDLE_IDENTIFIER,
    CodexCompatibility,
    CodexLaunchPreparation,
    CodexProcessProbe,
    CodexRuntimeDescriptor,
    CodexRuntimeError,
    capability_status as runtime_capability_status,
    classify_compatibility,
    discover_runtime,
    prepare_launch,
)

DEFAULT_PROTOCOL_VERSION = "1.0.0"
SUPPORTED_SET: frozenset[str] = frozenset({DEFAULT_PROTOCOL_VERSION})


def _write_bundle(
    applications: Path,
    name: str,
    *,
    identifier: str = SUPPORTED_BUNDLE_IDENTIFIER,
    executable: bool = True,
    short_version: str | None = DEFAULT_PROTOCOL_VERSION,
) -> tuple[Path, Path]:
    application = applications / name
    parts = Path(*EXECUTABLE_RELATIVE_PATH.split("/")[:-1])
    executable_dir = application / parts
    executable_dir.mkdir(parents=True, exist_ok=True)
    binary = application / EXECUTABLE_RELATIVE_PATH
    if executable:
        binary.write_text("fixture, not executed")
        binary.chmod(0o755)
    elif binary.exists():
        binary.unlink()
    info_plist = application / "Contents/Info.plist"
    payload: dict[str, object] = {"CFBundleIdentifier": identifier}
    if short_version is not None:
        payload["CFBundleShortVersionString"] = short_version
    info_plist.write_bytes(plistlib.dumps(payload))
    return application, binary


class _StaticProbe:
    """Deterministic CodexProcessProbe for tests; never touches the OS."""

    def __init__(self, running: bool) -> None:
        self._running = running
        self.calls = 0

    def is_codex_running(self) -> bool:
        self.calls += 1
        return self._running


class DiscoverRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.applications = Path(self.tmp.name)

    def test_chatgpt_preferred_when_both_bundles_present(self) -> None:
        codex_app, _ = _write_bundle(self.applications, "Codex.app")
        chatgpt_app, chatgpt_binary = _write_bundle(self.applications, "ChatGPT.app")
        descriptor = discover_runtime(self.applications)
        self.assertEqual(descriptor.application_path, chatgpt_app)
        self.assertEqual(descriptor.executable_path, chatgpt_binary)
        self.assertEqual(descriptor.bundle_identifier, SUPPORTED_BUNDLE_IDENTIFIER)
        self.assertNotEqual(descriptor.application_path, codex_app)
        self.assertEqual(BUNDLE_NAME_PREFERENCE, ("ChatGPT.app", "Codex.app"))

    def test_codex_fallback_when_chatgpt_absent(self) -> None:
        codex_app, codex_binary = _write_bundle(self.applications, "Codex.app")
        descriptor = discover_runtime(self.applications)
        self.assertEqual(descriptor.application_path, codex_app)
        self.assertEqual(descriptor.executable_path, codex_binary)

    def test_wrong_identifier_falls_back_to_legacy_bundle(self) -> None:
        _write_bundle(self.applications, "ChatGPT.app", identifier="com.some.other")
        codex_app, codex_binary = _write_bundle(self.applications, "Codex.app")
        descriptor = discover_runtime(self.applications)
        self.assertEqual(descriptor.application_path, codex_app)
        self.assertEqual(descriptor.executable_path, codex_binary)

    def test_malformed_plist_falls_back_to_legacy_bundle(self) -> None:
        preferred, _ = _write_bundle(self.applications, "ChatGPT.app")
        (preferred / "Contents/Info.plist").write_bytes(b"this is not a plist")
        codex_app, codex_binary = _write_bundle(self.applications, "Codex.app")
        descriptor = discover_runtime(self.applications)
        self.assertEqual(descriptor.application_path, codex_app)
        self.assertEqual(descriptor.executable_path, codex_binary)

    def test_missing_or_nonexecutable_runtime_raises(self) -> None:
        with self.assertRaises(CodexRuntimeError):
            discover_runtime(self.applications)
        _, binary = _write_bundle(self.applications, "ChatGPT.app")
        binary.chmod(0o600)
        with self.assertRaises(CodexRuntimeError):
            discover_runtime(self.applications)

    def test_explicit_empty_directory_raises(self) -> None:
        # An explicit injected directory must be honoured; empty contents
        # must raise CodexRuntimeError rather than silently falling back.
        with self.assertRaises(CodexRuntimeError):
            discover_runtime(self.applications)

    def test_bundle_short_version_captured_as_descriptive_metadata(self) -> None:
        codex_app, _ = _write_bundle(self.applications, "Codex.app", short_version="2.3.4")
        descriptor = discover_runtime(self.applications)
        self.assertEqual(descriptor.application_path, codex_app)
        self.assertEqual(descriptor.bundle_short_version, "2.3.4")

    def test_bundle_short_version_is_none_when_missing(self) -> None:
        _write_bundle(self.applications, "Codex.app", short_version=None)
        descriptor = discover_runtime(self.applications)
        self.assertIsNone(descriptor.bundle_short_version)


class DescriptorTests(unittest.TestCase):
    def test_descriptor_is_frozen(self) -> None:
        _, binary = _write_bundle(Path(tempfile.mkdtemp()), "Codex.app")
        descriptor = CodexRuntimeDescriptor(
            application_path=binary.parent.parent,
            executable_path=binary,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=DEFAULT_PROTOCOL_VERSION,
        )
        with self.assertRaises(FrozenInstanceError):
            descriptor.bundle_identifier = "com.openai.chatgpt"  # type: ignore[misc]

    def test_descriptor_carries_bundle_short_version(self) -> None:
        _, binary = _write_bundle(Path(tempfile.mkdtemp()), "Codex.app")
        descriptor = CodexRuntimeDescriptor(
            application_path=binary.parent.parent,
            executable_path=binary,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version="1.2.3",
        )
        self.assertEqual(descriptor.bundle_short_version, "1.2.3")


class CompatibilityTests(unittest.TestCase):
    def _descriptor(self, *, bundle_short_version: str | None = "9.9.9") -> CodexRuntimeDescriptor:
        # The bundle short version is intentionally distinct from
        # DEFAULT_PROTOCOL_VERSION to prove it plays no role in classification.
        tmp = Path(tempfile.mkdtemp())
        _, binary = _write_bundle(tmp, "Codex.app", short_version=bundle_short_version)
        return CodexRuntimeDescriptor(
            application_path=binary.parent.parent,
            executable_path=binary,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=bundle_short_version,
        )

    def test_supported_when_observed_in_set(self) -> None:
        descriptor = self._descriptor()
        result = classify_compatibility(
            descriptor,
            observed_protocol_version=DEFAULT_PROTOCOL_VERSION,
            supported_protocol_versions=SUPPORTED_SET,
        )
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.protocol_version, DEFAULT_PROTOCOL_VERSION)
        self.assertIsNone(result.reason)

    def test_supported_with_multi_element_set(self) -> None:
        descriptor = self._descriptor()
        supported = frozenset({"0.9.0", DEFAULT_PROTOCOL_VERSION, "1.1.0"})
        result = classify_compatibility(
            descriptor,
            observed_protocol_version="1.1.0",
            supported_protocol_versions=supported,
        )
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.protocol_version, "1.1.0")
        self.assertIsNone(result.reason)

    def test_unknown_when_observed_protocol_version_is_none(self) -> None:
        descriptor = self._descriptor()
        result = classify_compatibility(
            descriptor,
            observed_protocol_version=None,
            supported_protocol_versions=SUPPORTED_SET,
        )
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.protocol_version)
        self.assertIsNotNone(result.reason)

    def test_unsupported_when_observed_not_in_set(self) -> None:
        descriptor = self._descriptor()
        result = classify_compatibility(
            descriptor,
            observed_protocol_version="0.9.0",
            supported_protocol_versions=SUPPORTED_SET,
        )
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.protocol_version, "0.9.0")
        self.assertIsNotNone(result.reason)

    def test_bundle_short_version_does_not_drive_classification(self) -> None:
        # bundle_short_version is intentionally a nonsense value distinct
        # from both the observed protocol version and the supported set;
        # classification must still report "supported" because the observed
        # version is in the supported set.
        descriptor = self._descriptor(bundle_short_version="zzz")
        result = classify_compatibility(
            descriptor,
            observed_protocol_version=DEFAULT_PROTOCOL_VERSION,
            supported_protocol_versions=SUPPORTED_SET,
        )
        self.assertEqual(result.status, "supported")

    def test_bundle_short_version_missing_does_not_force_unknown(self) -> None:
        descriptor = self._descriptor(bundle_short_version=None)
        result = classify_compatibility(
            descriptor,
            observed_protocol_version=DEFAULT_PROTOCOL_VERSION,
            supported_protocol_versions=SUPPORTED_SET,
        )
        self.assertEqual(result.status, "supported")


class CapabilityTests(unittest.TestCase):
    def _descriptor(self) -> CodexRuntimeDescriptor:
        tmp = Path(tempfile.mkdtemp())
        _, binary = _write_bundle(tmp, "Codex.app")
        return CodexRuntimeDescriptor(
            application_path=binary.parent.parent,
            executable_path=binary,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=DEFAULT_PROTOCOL_VERSION,
        )

    def test_available_when_probe_idle_and_compatibility_supported(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        status = runtime_capability_status(
            descriptor,
            process_probe=probe,
            compatibility=CodexCompatibility(
                status="supported", protocol_version=DEFAULT_PROTOCOL_VERSION, reason=None,
            ),
        )
        self.assertEqual(status, "available")
        self.assertEqual(probe.calls, 1)

    def test_already_running_unverified_when_probe_running(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=True)
        status = runtime_capability_status(
            descriptor,
            process_probe=probe,
            compatibility=CodexCompatibility(
                status="supported", protocol_version=DEFAULT_PROTOCOL_VERSION, reason=None,
            ),
        )
        self.assertEqual(status, "already-running-unverified")
        self.assertEqual(probe.calls, 1)

    def test_incompatible_when_compatibility_unsupported(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        status = runtime_capability_status(
            descriptor,
            process_probe=probe,
            compatibility=CodexCompatibility(
                status="unsupported", protocol_version=DEFAULT_PROTOCOL_VERSION,
                reason="observed protocol version 0.9.0 not in supported set",
            ),
        )
        self.assertEqual(status, "incompatible")
        self.assertEqual(probe.calls, 0)

    def test_incompatible_when_compatibility_unknown(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        status = runtime_capability_status(
            descriptor,
            process_probe=probe,
            compatibility=CodexCompatibility(
                status="unknown", protocol_version=None, reason="observed version unreadable",
            ),
        )
        self.assertEqual(status, "incompatible")
        # Probe must NOT be touched when compatibility is already uncertain.
        self.assertEqual(probe.calls, 0)

    def test_probe_must_satisfy_protocol(self) -> None:
        class BadProbe:
            pass

        descriptor = self._descriptor()
        with self.assertRaises(TypeError):
            runtime_capability_status(
                descriptor,
                process_probe=BadProbe(),  # type: ignore[arg-type]
                compatibility=CodexCompatibility(
                    status="supported", protocol_version=DEFAULT_PROTOCOL_VERSION, reason=None,
                ),
            )


class PrepareLaunchTests(unittest.TestCase):
    def _descriptor(self) -> CodexRuntimeDescriptor:
        tmp = Path(tempfile.mkdtemp())
        _, binary = _write_bundle(tmp, "Codex.app")
        return CodexRuntimeDescriptor(
            application_path=binary.parent.parent,
            executable_path=binary,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=DEFAULT_PROTOCOL_VERSION,
        )

    def _compatibility_supported(self) -> CodexCompatibility:
        return CodexCompatibility(
            status="supported", protocol_version=DEFAULT_PROTOCOL_VERSION, reason=None,
        )

    def test_supported_returns_executable_app_server_argv_overrides(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        result = prepare_launch(
            descriptor,
            argv=("-c", 'openai_base_url="http://127.0.0.1:1/x"'),
            overrides=("-c", "mcp_servers.model_deck={inline=true}"),
            compatibility=self._compatibility_supported(),
            process_probe=probe,
        )
        self.assertEqual(result.executable, descriptor.executable_path)
        self.assertEqual(result.argv[0], str(descriptor.executable_path))
        self.assertEqual(result.argv[1], APP_SERVER_ARGUMENT)
        self.assertEqual(result.argv[2:], (
            "-c", 'openai_base_url="http://127.0.0.1:1/x"',
            "-c", "mcp_servers.model_deck={inline=true}",
        ))
        self.assertEqual(result.overrides, ("-c", "mcp_servers.model_deck={inline=true}"))

    def test_refuses_unknown_protocol_version(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        with self.assertRaises(CodexRuntimeError):
            prepare_launch(
                descriptor,
                argv=(),
                overrides=(),
                compatibility=CodexCompatibility(
                    status="unknown", protocol_version=None, reason="observed version unreadable",
                ),
                process_probe=probe,
            )
        # Probe must not be touched when protocol is unknown.
        self.assertEqual(probe.calls, 0)

    def test_refuses_already_running(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=True)
        with self.assertRaises(CodexRuntimeError):
            prepare_launch(
                descriptor,
                argv=(),
                overrides=(),
                compatibility=self._compatibility_supported(),
                process_probe=probe,
            )
        self.assertEqual(probe.calls, 1)

    def test_refuses_unsupported_version(self) -> None:
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        with self.assertRaises(CodexRuntimeError):
            prepare_launch(
                descriptor,
                argv=(),
                overrides=(),
                compatibility=CodexCompatibility(
                    status="unsupported", protocol_version="0.9.0",
                    reason="not in supported set",
                ),
                process_probe=probe,
            )
        self.assertEqual(probe.calls, 0)

    def test_does_not_read_or_write_global_config(self) -> None:
        """prepare_launch must be a pure transform of its inputs."""
        descriptor = self._descriptor()
        probe = _StaticProbe(running=False)
        result = prepare_launch(
            descriptor,
            argv=("user-arg",),
            overrides=("override-arg",),
            compatibility=self._compatibility_supported(),
            process_probe=probe,
        )
        self.assertIsInstance(result, CodexLaunchPreparation)
        expected_argv = (
            str(descriptor.executable_path),
            APP_SERVER_ARGUMENT,
            "user-arg",
            "override-arg",
        )
        self.assertEqual(result.argv, expected_argv)
        self.assertEqual(result.overrides, ("override-arg",))


class ProbeProtocolShapeTests(unittest.TestCase):
    def test_runtime_checkable_protocol(self) -> None:
        self.assertTrue(issubclass(_StaticProbe, CodexProcessProbe))
        self.assertIsInstance(_StaticProbe(False), CodexProcessProbe)

        class FunctionalProbe:
            def is_codex_running(self) -> bool:
                return False
        self.assertIsInstance(FunctionalProbe(), CodexProcessProbe)


class NoGlobalStateAccessTests(unittest.TestCase):
    def test_no_live_applications_access_in_tests(self) -> None:
        # Belt-and-braces: every discovery call in these tests uses an
        # explicit injected directory; we never pass None so we never
        # read the host /Applications. An explicit empty directory must
        # raise CodexRuntimeError rather than silently fall back.
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(CodexRuntimeError):
                discover_runtime(Path(empty))
        with self.assertRaises(CodexRuntimeError):
            discover_runtime(Path(tempfile.gettempdir()) / "definitely-m-not-present-xyz")

    def test_no_process_spawning_in_tests(self) -> None:
        probe = _StaticProbe(False)
        descriptor = CodexRuntimeDescriptor(
            application_path=Path(tempfile.mkdtemp()) / "Codex.app",
            executable_path=Path("/nonexistent/codex"),
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=None,
        )
        runtime_capability_status(
            descriptor,
            process_probe=probe,
            compatibility=CodexCompatibility(
                status="supported", protocol_version=DEFAULT_PROTOCOL_VERSION, reason=None,
            ),
        )
        self.assertEqual(probe.calls, 1)


if __name__ == "__main__":
    unittest.main()
