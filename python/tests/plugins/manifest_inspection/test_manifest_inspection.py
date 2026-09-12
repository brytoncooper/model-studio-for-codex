"""Acceptance tests for the B18 manifest inspection slice.

These tests exercise the in-memory inspector only. They never touch the
filesystem beyond loading the bundled ``manifest_minimal.json`` fixture via
``model_deck_contracts.paths.fixtures_root``, and they never spawn
processes or perform network calls.
"""
from __future__ import annotations

import copy
import json
import traceback
import unittest

from model_deck_contracts.paths import fixtures_root

from model_deck.plugins.manifest_inspection import (
    InspectionError,
    InspectionErrorCode,
    InspectionFailure,
    InspectionResult,
    inspect_entrypoint_path,
    inspect_manifest,
)


CALLER_MAJOR = 1
CALLER_MINOR = 2


def _load_minimal_fixture() -> dict:
    """Load the bundled ``manifest_minimal.json`` valid fixture."""
    return json.loads(
        (fixtures_root() / "valid" / "manifest_minimal.json").read_text(
            encoding="utf-8"
        )
    )


def _manifest(**overrides):
    """Return a fresh copy of the minimal fixture with optional overrides."""
    base = _load_minimal_fixture()
    for key, value in overrides.items():
        base[key] = value
    return base


class InspectManifestHappyPathTests(unittest.TestCase):
    def test_minimal_fixture_returns_ok(self) -> None:
        result = inspect_manifest(
            _load_minimal_fixture(),
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertIsInstance(result, InspectionResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.identity.manifest_id, "org.example.notebook")
        self.assertEqual(result.identity.manifest_version, 1)
        self.assertEqual(result.identity.version, "1.0.0")
        self.assertEqual(result.api.major, 1)
        self.assertEqual(result.api.minimum_minor, 0)
        self.assertEqual(result.entrypoint.runtime, "python")
        self.assertEqual(result.entrypoint.path, "plugin.py")
        self.assertEqual(result.contributions.operations, ())
        self.assertEqual(result.contributions.panels, ())
        self.assertEqual(result.contributions.providers, ())
        self.assertEqual(result.contributions.permissions, ())

    def test_minimum_minor_equal_to_caller_is_accepted(self) -> None:
        manifest = _manifest(
            plugin_api={"major": CALLER_MAJOR, "minimum_minor": CALLER_MINOR},
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.api.minimum_minor, CALLER_MINOR)

    def test_nested_path_segment_is_accepted(self) -> None:
        manifest = _manifest(
            entrypoint={"runtime": "python", "path": "pkg/sub/plugin.py"},
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.entrypoint.path, "pkg/sub/plugin.py")

    def test_full_contributions_section(self) -> None:
        manifest = _manifest(
            permissions=["read:config", "write:state"],
            contributes={
                "operations": [
                    {
                        "id": "org.example.op_one",
                        "input_schema": "contracts/op_one_in.json",
                        "output_schema": "contracts/op_one_out.json",
                        "effect": "read",
                    },
                    {
                        "id": "org.example.op_two",
                        "input_schema": "contracts/op_two_in.json",
                        "output_schema": "contracts/op_two_out.json",
                        "effect": "write",
                    },
                ],
                "panels": [
                    {"id": "org.example.panel_one", "schema": "contracts/panel_one.json"},
                ],
                "providers": [
                    {
                        "id": "org.example.provider_one",
                        "port": "provider.execution/v1",
                        "execution_mode": "responses",
                        "features": ["tools", "resume"],
                    },
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertTrue(result.ok, msg=f"errors: {result.errors}")
        self.assertEqual(len(result.contributions.operations), 2)
        self.assertEqual(
            result.contributions.operations[0].operation_id,
            "org.example.op_one",
        )
        self.assertEqual(result.contributions.operations[1].effect, "write")
        self.assertEqual(len(result.contributions.panels), 1)
        self.assertEqual(result.contributions.panels[0].panel_id, "org.example.panel_one")
        self.assertEqual(len(result.contributions.providers), 1)
        self.assertEqual(
            result.contributions.providers[0].features, ("tools", "resume")
        )
        self.assertEqual(
            result.contributions.permissions, ("read:config", "write:state")
        )


class InspectManifestMutationAliasingTests(unittest.TestCase):
    def test_mutating_input_after_inspection_does_not_change_result(self) -> None:
        manifest = _load_minimal_fixture()
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        # Snapshot values via copies before any mutation.
        snapshot_identity = copy.deepcopy(result.identity)
        snapshot_api = copy.deepcopy(result.api)
        snapshot_entrypoint = copy.deepcopy(result.entrypoint)
        snapshot_contrib = copy.deepcopy(result.contributions)
        snapshot_errors = tuple(copy.deepcopy(e) for e in result.errors)

        # Mutate the input document in-place.
        manifest["id"] = "com.example.tampered"
        manifest["plugin_api"]["major"] = 99
        manifest["entrypoint"]["path"] = "../elsewhere.py"
        manifest.setdefault("contributes", {})["operations"] = [
            {
                "id": "com.example.evil",
                "input_schema": "x",
                "output_schema": "y",
                "effect": "write",
            }
        ]
        manifest["permissions"] = ["write:everything"]

        # Result must remain identical to its original values.
        self.assertEqual(result.identity, snapshot_identity)
        self.assertEqual(result.api, snapshot_api)
        self.assertEqual(result.entrypoint, snapshot_entrypoint)
        self.assertEqual(result.contributions, snapshot_contrib)
        self.assertEqual(result.errors, snapshot_errors)

    def test_deepcopy_of_input_before_inspection_yields_equal_result(self) -> None:
        original = _load_minimal_fixture()
        clone = copy.deepcopy(original)
        result = inspect_manifest(
            original,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        # Mutate the clone; the result was already produced from `original`.
        clone["id"] = "com.example.tampered"
        self.assertEqual(result.identity.manifest_id, "org.example.notebook")


class InspectManifestSchemaDefectTests(unittest.TestCase):
    def test_missing_required_field_raises_schema_invalid(self) -> None:
        manifest = _load_minimal_fixture()
        del manifest["version"]
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_non_mapping_input_raises_schema_invalid(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                ["not", "a", "mapping"],
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_extra_property_rejected_by_schema(self) -> None:
        manifest = _load_minimal_fixture()
        manifest["unknown_field"] = "nope"
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_invalid_id_format_rejected(self) -> None:
        manifest = _manifest(id="NotReverseDomain")
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_backslash_path_rejected_in_manifest(self) -> None:
        # The schema regex `^[^/\\][^\\0]*$` is authoritative for embedded
        # backslashes inside a full manifest inspection.
        manifest = _manifest(
            entrypoint={"runtime": "python", "path": "pkg\\sub\\plugin.py"},
        )
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)


class InspectEntrypointPathTests(unittest.TestCase):
    def test_simple_relative_path_passes(self) -> None:
        self.assertEqual(inspect_entrypoint_path("plugin.py"), "plugin.py")

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_absolute_path_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("/etc/plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_backslash_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("pkg\\sub\\plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_parent_traversal_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("../plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_nested_parent_traversal_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("pkg/../../plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_dot_segment_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("./plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_empty_segment_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("pkg//plugin.py")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_nul_byte_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("plugin.py\x00.sh")
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )

    def test_path_too_long_rejected(self) -> None:
        long_path = "a" * 257
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path(long_path)
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.ENTRYPOINT_PATH_INVALID
        )


class InspectDuplicateContributionTests(unittest.TestCase):
    def test_duplicate_operation_id_recorded_not_raised(self) -> None:
        manifest = _manifest(
            contributes={
                "operations": [
                    {
                        "id": "org.example.op",
                        "input_schema": "contracts/in.json",
                        "output_schema": "contracts/out.json",
                        "effect": "read",
                    },
                    {
                        "id": "org.example.op",
                        "input_schema": "contracts/in.json",
                        "output_schema": "contracts/out.json",
                        "effect": "write",
                    },
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        error = result.errors[0]
        self.assertEqual(error.code, InspectionErrorCode.DUPLICATE_CONTRIBUTION)
        self.assertEqual(error.kind, "operation")
        self.assertEqual(error.id, "org.example.op")
        self.assertEqual(error.index, 1)

    def test_duplicate_panel_id_recorded(self) -> None:
        manifest = _manifest(
            contributes={
                "panels": [
                    {"id": "org.example.panel", "schema": "contracts/p.json"},
                    {"id": "org.example.panel", "schema": "contracts/p.json"},
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].kind, "panel")

    def test_duplicate_provider_id_recorded(self) -> None:
        manifest = _manifest(
            contributes={
                "providers": [
                    {
                        "id": "org.example.provider",
                        "port": "provider.execution/v1",
                        "execution_mode": "chat_completions",
                    },
                    {
                        "id": "org.example.provider",
                        "port": "provider.execution/v1",
                        "execution_mode": "responses",
                    },
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].kind, "provider")

    def test_same_id_across_different_kinds_is_not_duplicate(self) -> None:
        shared_id = "org.example.shared"
        manifest = _manifest(
            contributes={
                "operations": [
                    {
                        "id": shared_id,
                        "input_schema": "contracts/in.json",
                        "output_schema": "contracts/out.json",
                        "effect": "read",
                    },
                ],
                "panels": [
                    {"id": shared_id, "schema": "contracts/p.json"},
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertTrue(result.ok, msg=f"errors: {result.errors}")


class InspectApiCompatibilityTests(unittest.TestCase):
    def test_major_mismatch_raises(self) -> None:
        manifest = _manifest(
            plugin_api={"major": 2, "minimum_minor": 0},
        )
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.INCOMPATIBLE_API_VERSION
        )
        self.assertEqual(ctx.exception.field, "plugin_api.major")

    def test_minor_below_minimum_raises(self) -> None:
        manifest = _manifest(
            plugin_api={"major": CALLER_MAJOR, "minimum_minor": CALLER_MINOR + 1},
        )
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(
            ctx.exception.code, InspectionErrorCode.INCOMPATIBLE_API_VERSION
        )
        self.assertEqual(ctx.exception.field, "plugin_api.minimum_minor")

    def test_caller_major_must_be_integer(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                _load_minimal_fixture(),
                caller_plugin_api_major="1",  # type: ignore[arg-type]
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_caller_minor_must_be_integer(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                _load_minimal_fixture(),
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=0.5,  # type: ignore[arg-type]
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)

    def test_boolean_arguments_are_rejected(self) -> None:
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                _load_minimal_fixture(),
                caller_plugin_api_major=True,  # type: ignore[arg-type]
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)


class InspectResultImmutabilityTests(unittest.TestCase):
    def test_frozen_result_rejects_attribute_assignment(self) -> None:
        result = inspect_manifest(
            _load_minimal_fixture(),
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        with self.assertRaises(Exception):
            result.entrypoint = None  # type: ignore[misc]

    def test_collected_errors_are_tuples(self) -> None:
        manifest = _manifest(
            contributes={
                "operations": [
                    {
                        "id": "org.example.op",
                        "input_schema": "in.json",
                        "output_schema": "out.json",
                        "effect": "read",
                    },
                    {
                        "id": "org.example.op",
                        "input_schema": "in.json",
                        "output_schema": "out.json",
                        "effect": "write",
                    },
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        # ``errors`` is exposed as a tuple so callers cannot replace entries.
        self.assertIsInstance(result.errors, tuple)
        with self.assertRaises(Exception):
            result.errors = ()  # type: ignore[misc]
        # The outer structure is immutable; each entry is a frozen
        # InspectionFailure so the caller can read attributes safely.
        self.assertEqual(result.errors[0].code, "duplicate_contribution")



class InspectSanitizationRegressionTests(unittest.TestCase):
    """Focused regression tests for the three sanitization blockers."""

    def test_drive_letter_path_rejected(self) -> None:
        """Windows-style drive prefixes (``C:``, ``C:/``, ``C:\\``) and any
        other colon usage must be rejected as ``entrypoint_path_invalid``.

        The schema regex does not forbid embedded ``:``, so the inspector
        owns this check. UNC paths (``\\\\server\\share\\...``) are
        rejected by the leading-backslash rule, but their underlying
        backslash is also a colon-free shape, so the colon rule covers
        them via the backslash check.
        """
        for bad in (
            "C:/outside.py",
            "C:outside.py",
            "C:\\outside.py",
            "d:/pkg/plugin.py",
        ):
            with self.subTest(path=bad):
                with self.assertRaises(InspectionError) as ctx:
                    inspect_entrypoint_path(bad)
                self.assertEqual(
                    ctx.exception.code,
                    InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
                )
                # The detail must describe the defect without quoting the
                # caller's path string back.
                self.assertNotIn(bad, ctx.exception.detail)
                self.assertNotIn(bad, str(ctx.exception))

    def test_unc_path_still_rejected(self) -> None:
        """UNC paths (``\\\\server\\share\\plugin.py``) are rejected
        by the leading-backslash rule. They contain no colon, so the new
        colon rule does not interfere; the backslash rule owns them.
        """
        with self.assertRaises(InspectionError) as ctx:
            inspect_entrypoint_path("\\\\server\\share\\plugin.py")
        self.assertEqual(
            ctx.exception.code,
            InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
        )

    def test_schema_invalid_detail_does_not_echo_value(self) -> None:
        """When the schema validator reports an invalid value, the public
        ``InspectionError`` must use a fixed ``detail`` constant and must
        not echo the failing caller-supplied value through any field.
        """
        bad_value = "1.0-bogus-token-XYZ"
        manifest = _manifest(version=bad_value)
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )
        self.assertEqual(ctx.exception.code, InspectionErrorCode.SCHEMA_INVALID)
        self.assertEqual(ctx.exception.detail, "manifest failed schema validation")
        # The bad value must not appear in any visible field or in str(exc).
        self.assertNotIn(bad_value, ctx.exception.detail)
        if ctx.exception.field is not None:
            self.assertNotIn(bad_value, ctx.exception.field)
        self.assertNotIn(bad_value, str(ctx.exception))

    def test_duplicate_detail_does_not_echo_id(self) -> None:
        """The duplicate-contribution failure records the caller-supplied id
        on the structured ``id`` attribute, but the human-readable ``detail``
        never interpolates that id verbatim — so the public surface cannot
        be tricked into echoing attacker-controlled text.
        """
        attacker_id = "evil.attacker.contribution_bad-token"
        manifest = _manifest(
            contributes={
                "operations": [
                    {
                        "id": attacker_id,
                        "input_schema": "in.json",
                        "output_schema": "out.json",
                        "effect": "read",
                    },
                    {
                        "id": attacker_id,
                        "input_schema": "in.json",
                        "output_schema": "out.json",
                        "effect": "write",
                    },
                ],
            },
        )
        result = inspect_manifest(
            manifest,
            caller_plugin_api_major=CALLER_MAJOR,
            caller_plugin_api_minor=CALLER_MINOR,
        )
        self.assertEqual(len(result.errors), 1)
        failure = result.errors[0]
        self.assertIsInstance(failure, InspectionFailure)
        self.assertEqual(failure.code, InspectionErrorCode.DUPLICATE_CONTRIBUTION)
        # Structured attribute carries the id; ``detail`` must not.
        self.assertEqual(failure.id, attacker_id)
        self.assertEqual(failure.kind, "operation")
        self.assertEqual(failure.index, 1)
        self.assertNotIn(attacker_id, failure.detail)

    def test_schema_invalid_traceback_does_not_echo_cause(self) -> None:
        """``InspectionError`` raised at the schema boundary must suppress
        the chained ``SchemaValidationError`` in the standard traceback
        output, so a caller that runs ``traceback.format_exception`` on
        the exception cannot recover the validator's raw message (and the
        caller-supplied value embedded in it).

        The boundary uses ``raise InspectionError(...) from None`` so that
        ``__suppress_context__`` is set and Python's default traceback
        formatter omits the chained ``SchemaValidationError`` entirely.
        """
        import io

        bad_value = "1.0-bogus-token-XYZ"
        manifest = _manifest(version=bad_value)
        with self.assertRaises(InspectionError) as ctx:
            inspect_manifest(
                manifest,
                caller_plugin_api_major=CALLER_MAJOR,
                caller_plugin_api_minor=CALLER_MINOR,
            )

        # Build a full traceback exactly the way the standard library
        # would when a caller asks for ``traceback.format_exception``.
        exc = ctx.exception
        formatted = traceback.format_exception(type(exc), exc, exc.__traceback__)
        rendered = "".join(formatted)
        # Sanity: the public surface is still correct.
        self.assertEqual(exc.code, InspectionErrorCode.SCHEMA_INVALID)
        self.assertEqual(exc.detail, "manifest failed schema validation")
        # The validator's message must not appear anywhere in the rendered
        # traceback; the chained ``SchemaValidationError`` (and the bad
        # value it embeds) must be suppressed.
        self.assertNotIn(bad_value, rendered)
        self.assertNotIn("does not match", rendered)
        self.assertNotIn("SchemaValidationError", rendered)
        # And the standard library path ``traceback.print_exception``
        # (which writes to a stream) must agree.
        buf = io.StringIO()
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=buf)
        rendered_via_print = buf.getvalue()
        self.assertNotIn(bad_value, rendered_via_print)
        self.assertNotIn("SchemaValidationError", rendered_via_print)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
