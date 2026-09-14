"""Tests for the V2 OpenAI-compatible provider profile loader and composer.

The profile loader and composer are the non-secret seam that lets the engine
build an `OpenAICompatibleExecutionPort` from a JSON document. These tests
cover the loader (schema + invariants), the credential and endpoint resolvers
they produce, and the composition function that returns a port plus the route
definitions the engine consumes.

Tests use temp directories and a small fake subprocess script to exercise
credential resolution without touching real credentials or network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from typing import Any

from model_deck.integrations.providers.openai_compatible.configuration import (
    OpenAICompatibleProfileError,
    OpenAICompatibleProfile,
    compose_openai_compatible_profile,
    endpoint_resolver_from_records,
    subprocess_credential_resolver,
)
from model_deck.adapters.routing.registered import ProviderRouteDefinition


CONNECTION_ID = "550e8400-e29b-41d4-a716-44665544000a"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-44665544000b"
PROVIDER_ID = "com.example.openai-compatible"
PROVIDER_MODEL_ID = "example/model-1"
ENDPOINT_REF = "ref:example.endpoint"
CREDENTIAL_REF = "ref:example.credential"
CAPABILITY_REF = "ref:example.capabilities"
VENDOR_ID = "example-vendor"
DISPLAY_NAME = "Example Compatible"
PROVIDER_NAME = "Example OpenAI-Compatible Provider"
BILLING_DESCRIPTION = "Example billing description"

_NOW = "2026-09-13T00:00:00Z"


def _profile_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": PROVIDER_ID,
        "provider_name": PROVIDER_NAME,
        "connection_id": CONNECTION_ID,
        "provider_model_id": PROVIDER_MODEL_ID,
        "display_name": DISPLAY_NAME,
        "endpoint_config_ref": ENDPOINT_REF,
        "credential_ref": CREDENTIAL_REF,
        "capability_snapshot_ref": CAPABILITY_REF,
        "endpoint": {
            "base_url": "https://provider.example.com/v1",
            "wire_mode": "responses",
            "vendor_id": VENDOR_ID,
        },
        "credential_command": {
            "executable": "/usr/bin/false",
            "args": ["--print-secret"],
            "timeout_ms": 5000,
        },
        "billing_description": BILLING_DESCRIPTION,
    }
    payload.update(overrides)
    return payload


def _write_profile(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _helper_script(directory: Path, body: str) -> Path:
    path = directory / "helper.py"
    shebang = "#!/usr/bin/env python3\n"
    path.write_text(shebang + textwrap.dedent(body), encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _endpoint_record(
    *,
    base_url: str = "https://provider.example.com/v1",
    wire_mode: str = "responses",
    vendor_id: str | None = VENDOR_ID,
) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "wire_mode": wire_mode,
        "vendor_id": vendor_id,
    }


def _records_by_ref(revision: int = 1, **overrides: Any) -> dict[tuple[str, int], dict[str, Any]]:
    record = _endpoint_record(**overrides)
    return {(ENDPOINT_REF, revision): record}


class OpenAICompatibleProfileLoadTests(unittest.TestCase):
    def test_load_returns_immutable_profile_with_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict())
            profile = OpenAICompatibleProfile.load(path)

            self.assertEqual(profile.schema_version, 1)
            self.assertEqual(profile.provider_id, PROVIDER_ID)
            self.assertEqual(profile.provider_name, PROVIDER_NAME)
            self.assertEqual(profile.connection_id, CONNECTION_ID)
            self.assertEqual(profile.provider_model_id, PROVIDER_MODEL_ID)
            self.assertEqual(profile.display_name, DISPLAY_NAME)
            self.assertEqual(profile.endpoint_config_ref, ENDPOINT_REF)
            self.assertEqual(profile.credential_ref, CREDENTIAL_REF)
            self.assertEqual(profile.capability_snapshot_ref, CAPABILITY_REF)
            self.assertEqual(profile.endpoint.base_url, "https://provider.example.com/v1")
            self.assertEqual(profile.endpoint.wire_mode.value, "responses")
            self.assertEqual(profile.endpoint.vendor_id, VENDOR_ID)
            self.assertEqual(profile.credential_command.executable, "/usr/bin/false")
            self.assertEqual(profile.credential_command.args, ("--print-secret",))
            self.assertEqual(profile.credential_command.timeout_ms, 5000)
            self.assertEqual(profile.billing_description, BILLING_DESCRIPTION)

            with self.assertRaises(Exception):
                profile.provider_id = "com.example.other"  # type: ignore[misc]

    def test_load_rejects_unknown_schema_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict(schema_version=2))
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_malformed_provider_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict(provider_id="NotAReverseDomain"))
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_non_uuid_connection_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict(connection_id="not-a-uuid"))
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_http_endpoint_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(
                root,
                _profile_dict(endpoint=_endpoint_record(base_url="http://provider.example.com/v1")),
            )
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_invalid_wire_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(
                root,
                _profile_dict(endpoint=_endpoint_record(wire_mode="invalid")),
            )
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_relative_credential_executable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(
                root,
                _profile_dict(
                    credential_command={
                        "executable": "helper.py",
                        "args": [],
                        "timeout_ms": 1000,
                    }
                ),
            )
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_zero_or_excessive_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            for timeout in (0, 30001, -5):
                path = _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": "/usr/bin/false",
                            "args": [],
                            "timeout_ms": timeout,
                        }
                    ),
                )
                with self.subTest(timeout=timeout):
                    with self.assertRaises(OpenAICompatibleProfileError):
                        OpenAICompatibleProfile.load(path)

    def test_load_rejects_blank_billing_description(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict(billing_description=""))
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_rejects_literal_secret_field(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            payload = _profile_dict()
            payload["api_key"] = "literal-secret"
            path = _write_profile(root, payload)
            with self.assertRaises(OpenAICompatibleProfileError):
                OpenAICompatibleProfile.load(path)

    def test_load_accepts_missing_capability_snapshot_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            path = _write_profile(root, _profile_dict(capability_snapshot_ref=None))
            profile = OpenAICompatibleProfile.load(path)
            self.assertIsNone(profile.capability_snapshot_ref)


class SubprocessCredentialResolverTests(unittest.TestCase):
    def test_resolver_returns_secret_stripped_of_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import sys
                sys.stdout.write('topsecret\\n')
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": [],
                            "timeout_ms": 5000,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            self.assertEqual(resolver(CREDENTIAL_REF), "topsecret")

    def test_resolver_rejects_empty_stdout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import sys
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": [],
                            "timeout_ms": 5000,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            with self.assertRaises(OpenAICompatibleProfileError):
                resolver(CREDENTIAL_REF)

    def test_resolver_rejects_secret_containing_internal_newline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import sys
                sys.stdout.write('first\\nsecond')
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": [],
                            "timeout_ms": 5000,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            with self.assertRaises(OpenAICompatibleProfileError):
                resolver(CREDENTIAL_REF)

    def test_resolver_enforces_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import time, sys
                time.sleep(5)
                sys.stdout.write('too-late')
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": [],
                            "timeout_ms": 200,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            with self.assertRaises(OpenAICompatibleProfileError):
                resolver(CREDENTIAL_REF)

    def test_resolver_sanitizes_error_messages(self) -> None:
        sentinel = "PRIVATE-CRED-SENTINEL"
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                f"""\
                import sys
                sys.stderr.write('{sentinel}\\n')
                sys.stdout.write('')
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": [],
                            "timeout_ms": 5000,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            try:
                resolver(CREDENTIAL_REF)
            except OpenAICompatibleProfileError as exc:
                self.assertNotIn(sentinel, str(exc))
                self.assertNotIn(sentinel, repr(exc))
            else:
                self.fail("expected credential resolver error")

    def test_resolver_passes_args_as_list_without_shell(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import sys
                sys.stdout.write(' '.join(sys.argv[1:]) + ' done')
                """,
            )
            profile = OpenAICompatibleProfile.load(
                _write_profile(
                    root,
                    _profile_dict(
                        credential_command={
                            "executable": str(helper),
                            "args": ["hello", "world"],
                            "timeout_ms": 5000,
                        }
                    ),
                )
            )
            resolver = subprocess_credential_resolver(profile.credential_command)
            self.assertEqual(resolver(CREDENTIAL_REF), "hello world done")


class EndpointResolverFromRecordsTests(unittest.TestCase):
    def test_resolver_returns_parsed_endpoint_for_matching_ref_and_revision(self) -> None:
        resolver = endpoint_resolver_from_records(_records_by_ref(revision=4))
        endpoint = resolver(ENDPOINT_REF, 4)
        self.assertEqual(endpoint.host, "provider.example.com")
        self.assertEqual(endpoint.port, 443)
        self.assertTrue(endpoint.secure)
        self.assertEqual(endpoint.path_prefix, "/v1")
        self.assertEqual(endpoint.wire_mode.value, "responses")
        self.assertEqual(endpoint.vendor_id, VENDOR_ID)

    def test_resolver_rejects_unknown_ref(self) -> None:
        resolver = endpoint_resolver_from_records(_records_by_ref())
        with self.assertRaises(OpenAICompatibleProfileError):
            resolver("ref:other.endpoint", 1)

    def test_resolver_rejects_mismatched_revision(self) -> None:
        resolver = endpoint_resolver_from_records(_records_by_ref(revision=1))
        with self.assertRaises(OpenAICompatibleProfileError):
            resolver(ENDPOINT_REF, 2)

    def test_resolver_rejects_invalid_url(self) -> None:
        resolver = endpoint_resolver_from_records(
            _records_by_ref(base_url="not-a-url")
        )
        with self.assertRaises(OpenAICompatibleProfileError):
            resolver(ENDPOINT_REF, 1)

    def test_resolver_rejects_http_url(self) -> None:
        resolver = endpoint_resolver_from_records(
            _records_by_ref(base_url="http://provider.example.com/v1")
        )
        with self.assertRaises(OpenAICompatibleProfileError):
            resolver(ENDPOINT_REF, 1)

    def test_resolver_rejects_invalid_wire_mode(self) -> None:
        resolver = endpoint_resolver_from_records(_records_by_ref(wire_mode="bogus"))
        with self.assertRaises(OpenAICompatibleProfileError):
            resolver(ENDPOINT_REF, 1)


class ComposeOpenAICompatibleProfileTests(unittest.TestCase):
    def test_compose_returns_port_and_route_definition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md-cfg-") as tmp:
            root = Path(tmp)
            helper = _helper_script(
                root,
                """\
                import sys
                sys.stdout.write('composed-secret')
                """,
            )
            payload = _profile_dict(
                credential_command={
                    "executable": str(helper),
                    "args": [],
                    "timeout_ms": 5000,
                }
            )
            profile = OpenAICompatibleProfile.load(_write_profile(root, payload))
            records = _records_by_ref()
            resolver = endpoint_resolver_from_records(records)
            port, definitions = compose_openai_compatible_profile(
                profile,
                post_stream=lambda **kwargs: None,
                endpoint_resolver=resolver,
                route_definition_factory=ProviderRouteDefinition,
            )
            self.assertEqual(len(definitions), 1)
            definition = definitions[PROVIDER_ID]
            self.assertEqual(definition.execution_mode.value, "responses")
            names = {(feature.name, feature.state.value) for feature in definition.capability_features}
            self.assertIn(("tools", "supported"), names)
            self.assertIn(("parallel_tool_calls", "unsupported"), names)
            self.assertEqual(definition.capability_snapshot_ref, CAPABILITY_REF)
            self.assertTrue(callable(port.start))
            self.assertTrue(callable(port.close))


if __name__ == "__main__":
    unittest.main()
