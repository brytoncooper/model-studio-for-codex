from __future__ import annotations

import json
import traceback
import unittest
from dataclasses import FrozenInstanceError

from model_deck.plugins.schema_bundle import (
    PluginSchemaBundle,
    PluginSchemaBundleError,
    PluginSchemaDataError,
)


DRAFT = "https://json-schema.org/draft/2020-12/schema"
BUNDLE_ERROR = "plugin schema bundle is invalid"
DATA_ERROR = "plugin schema data is invalid"


def _schema_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _resources() -> dict[str, bytes]:
    return {
        "contracts/common.json": _schema_bytes(
            {
                "$schema": DRAFT,
                "$id": "contracts/common.json",
                "definitions": {
                    "name": {"type": "string", "minLength": 1},
                    "a/b": {"type": "integer", "minimum": 0},
                    "sp ace#": {"type": "boolean"},
                    "%20": {"type": "null"},
                },
            }
        ),
        "contracts/input.json": _schema_bytes(
            {
                "$schema": DRAFT,
                "$id": "contracts/input.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"$ref": "common.json#/definitions/name"},
                },
            }
        ),
        "contracts/output.json": _schema_bytes(
            {
                "$schema": DRAFT,
                "$id": "contracts/output.json",
                "$ref": "common.json#/definitions/a~1b",
            }
        ),
    }


def _assert_fixed_error(
    case: unittest.TestCase,
    error_type: type[Exception],
    expected: str,
    operation,
) -> None:
    try:
        operation()
    except error_type as exc:
        case.assertEqual(str(exc), expected)
        case.assertIsNone(exc.__cause__)
        case.assertIsNone(exc.__context__)
        case.assertNotIn("PRIVATE_SENTINEL", "".join(traceback.format_exception(exc)))
    else:
        case.fail(f"expected {error_type.__name__}")


class PluginSchemaBundleTests(unittest.TestCase):
    def test_local_cross_file_refs_and_json_pointers_validate(self) -> None:
        bundle = PluginSchemaBundle.from_resources(_resources())

        self.assertEqual(
            bundle.resource_paths,
            ("contracts/common.json", "contracts/input.json", "contracts/output.json"),
        )
        self.assertIsNone(bundle.check_reference("contracts/input.json"))
        self.assertIsNone(
            bundle.check_reference("contracts/common.json#/definitions/a~1b")
        )
        self.assertEqual(bundle.validate("contracts/input.json", {"name": "Ada"}), {"name": "Ada"})
        self.assertEqual(bundle.validate("contracts/output.json", 3), 3)
        self.assertEqual(
            bundle.validate("contracts/common.json#/definitions/name", "Ada"),
            "Ada",
        )
        self.assertTrue(
            bundle.validate(
                "contracts/common.json#/definitions/sp%20ace%23", True
            )
        )
        self.assertIsNone(
            bundle.validate("contracts/common.json#/definitions/%2520", None)
        )

    def test_resources_and_validated_data_are_detached(self) -> None:
        resources = _resources()
        bundle = PluginSchemaBundle.from_resources(resources)
        resources["contracts/input.json"] = _schema_bytes({"type": "null"})
        instance = {"name": "Ada"}

        result = bundle.validate("contracts/input.json", instance)
        instance["name"] = "changed"

        self.assertEqual(result, {"name": "Ada"})
        self.assertEqual(bundle.validate("contracts/input.json", {"name": "Grace"}), {"name": "Grace"})
        with self.assertRaises(FrozenInstanceError):
            bundle.resource_paths = ()  # type: ignore[misc]

    def test_reference_has_no_remote_absolute_traversal_or_missing_fallback(self) -> None:
        bundle = PluginSchemaBundle.from_resources(_resources())
        invalid = (
            "https://example.invalid/schema.json",
            "file:///tmp/schema.json",
            "/absolute/schema.json",
            "../outside.json",
            "contracts/../outside.json",
            "contracts/missing.json",
            "contracts/input.json#/missing",
            "contracts/input.json#named-anchor",
            "contracts/input.json#/%ZZ",
            "contracts/input.json#/properties//name",
            "http://[PRIVATE_SENTINEL",
            "contracts/common.json#/definitions",
            "contracts/input.json#/required/0",
        )
        for reference in invalid:
            with self.subTest(reference=reference):
                _assert_fixed_error(
                    self,
                    PluginSchemaBundleError,
                    BUNDLE_ERROR,
                    lambda reference=reference: bundle.check_reference(reference),
                )

    def test_schema_refs_must_resolve_within_supplied_resources(self) -> None:
        invalid_refs = (
            "https://example.invalid/schema.json",
            "file:///tmp/schema.json",
            "/absolute/schema.json",
            "../outside.json",
            "missing.json#/definitions/value",
            "#/missing",
        )
        for reference in invalid_refs:
            resources = {
                "contracts/input.json": _schema_bytes(
                    {"$schema": DRAFT, "type": "object", "properties": {"x": {"$ref": reference}}}
                )
            }
            with self.subTest(reference=reference):
                _assert_fixed_error(
                    self,
                    PluginSchemaBundleError,
                    BUNDLE_ERROR,
                    lambda resources=resources: PluginSchemaBundle.from_resources(resources),
                )

    def test_resource_paths_are_strict_relative_paths(self) -> None:
        for path in (
            "",
            "/absolute.json",
            "../outside.json",
            "contracts/../outside.json",
            "contracts\\input.json",
            "https://example.invalid/input.json",
            "contracts/input.json#fragment",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                PluginSchemaBundleError, f"^{BUNDLE_ERROR}$"
            ):
                PluginSchemaBundle.from_resources({path: _schema_bytes({"type": "object"})})

    def test_rejects_malformed_or_unapproved_schemas(self) -> None:
        invalid_resources = (
            {"contracts/input.json": b'{"type":"PRIVATE_SENTINEL"}'},
            {"contracts/input.json": _schema_bytes({"type": "object", "unevaluatedProperties": False})},
            {"contracts/input.json": _schema_bytes({"type": "object", "format": "PRIVATE_SENTINEL"})},
            {"contracts/input.json": b'{"type":"object","type":"null"}'},
            {"contracts/input.json": b'{"const":NaN}'},
            {"contracts/input.json": b'not-json-PRIVATE_SENTINEL'},
            {"contracts/input.json": _schema_bytes({"$id": "other.json", "type": "object"})},
        )
        for resources in invalid_resources:
            with self.subTest(resources=resources):
                _assert_fixed_error(
                    self,
                    PluginSchemaBundleError,
                    BUNDLE_ERROR,
                    lambda resources=resources: PluginSchemaBundle.from_resources(resources),
                )

    def test_invalid_instance_is_distinct_fixed_data_error(self) -> None:
        bundle = PluginSchemaBundle.from_resources(_resources())
        invalid_values = (
            {"name": ""},
            {"name": "PRIVATE_SENTINEL", "unknown": True},
            {"name": float("nan")},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                _assert_fixed_error(
                    self,
                    PluginSchemaDataError,
                    DATA_ERROR,
                    lambda value=value: bundle.validate("contracts/input.json", value),
                )

        cyclic: list[object] = []
        cyclic.append(cyclic)
        _assert_fixed_error(
            self,
            PluginSchemaDataError,
            DATA_ERROR,
            lambda: bundle.validate("contracts/input.json", cyclic),
        )

    def test_resource_count_and_byte_bounds_are_enforced(self) -> None:
        too_many = {
            f"contracts/{index}.json": _schema_bytes({"type": "null"})
            for index in range(513)
        }
        with self.assertRaisesRegex(PluginSchemaBundleError, f"^{BUNDLE_ERROR}$"):
            PluginSchemaBundle.from_resources(too_many)
        with self.assertRaisesRegex(PluginSchemaBundleError, f"^{BUNDLE_ERROR}$"):
            PluginSchemaBundle.from_resources(
                {"contracts/large.json": b" " * ((1 << 20) + 1)}
            )
        one_megabyte_schema = _schema_bytes({"type": "null"}).ljust(1 << 20, b" ")
        with self.assertRaisesRegex(PluginSchemaBundleError, f"^{BUNDLE_ERROR}$"):
            PluginSchemaBundle.from_resources(
                {
                    f"contracts/large-{index}.json": one_megabyte_schema
                    for index in range(9)
                }
            )

    def test_instance_depth_bound_is_enforced(self) -> None:
        bundle = PluginSchemaBundle.from_resources(
            {"contracts/any.json": _schema_bytes({})}
        )
        value: object = "leaf"
        for _ in range(66):
            value = [value]
        with self.assertRaisesRegex(PluginSchemaDataError, f"^{DATA_ERROR}$"):
            bundle.validate("contracts/any.json", value)

    def test_instance_byte_bound_and_recursive_schema_fail_closed(self) -> None:
        bundle = PluginSchemaBundle.from_resources(
            {
                "contracts/input.json": _schema_bytes(
                    {
                        "$id": "contracts/input.json",
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                    }
                ),
                "contracts/recursive.json": _schema_bytes(
                    {"$id": "contracts/recursive.json", "$ref": "#"}
                ),
            }
        )
        _assert_fixed_error(
            self,
            PluginSchemaDataError,
            DATA_ERROR,
            lambda: bundle.validate(
                "contracts/input.json", {"value": "x" * (1 << 20)}
            ),
        )
        _assert_fixed_error(
            self,
            PluginSchemaDataError,
            DATA_ERROR,
            lambda: bundle.validate("contracts/recursive.json", None),
        )


if __name__ == "__main__":
    unittest.main()
