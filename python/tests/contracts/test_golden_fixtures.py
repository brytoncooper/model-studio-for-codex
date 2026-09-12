import json
import unittest

from model_deck_contracts.json_util import canonical_json_equal
from model_deck_contracts.paths import fixtures_root
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref
from model_deck_contracts.wire_types import JsonRpcRequest, RunEventRunCompleted, RunRequest, ToolCall


def _manifest() -> dict:
    return json.loads((fixtures_root() / "manifest.json").read_text(encoding="utf-8"))


def _load(name: str, *, valid: bool) -> dict:
    folder = "valid" if valid else "invalid"
    return json.loads((fixtures_root() / folder / name).read_text(encoding="utf-8"))


class GoldenFixturesTests(unittest.TestCase):
    def test_valid_manifest_fixtures_validate(self) -> None:
        for entry in _manifest()["valid"]:
            with self.subTest(file=entry["file"]):
                instance = _load(entry["file"], valid=True)
                validate_schema_ref(entry["schema"], instance)

    def test_invalid_manifest_fixtures_reject(self) -> None:
        for entry in _manifest()["invalid"]:
            with self.subTest(file=entry["file"]):
                instance = _load(entry["file"], valid=False)
                with self.assertRaises(SchemaValidationError):
                    validate_schema_ref(entry["schema"], instance)

    def test_capability_unknown_remains_unknown(self) -> None:
        features = _load("capability_features_unknown.json", valid=True)["capabilities"]["features"]
        self.assertEqual(features["tools"], "unknown")

    def test_capability_denied_not_tri_state(self) -> None:
        denied = _load("capability_tri_state_denied.json", valid=False)["capabilities"]["features"]["tools"]
        self.assertEqual(denied, "denied")
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/common/types.schema.json#/definitions/capability_tri_state",
                denied,
            )

    def test_wire_types_roundtrip_preserves_json(self) -> None:
        cases = [
            ("jsonrpc_request.json", JsonRpcRequest.parse),
            ("run_request_minimal.json", RunRequest.parse),
            ("tool_call.json", ToolCall.parse),
            ("run_event_completed.json", RunEventRunCompleted.parse),
        ]
        for name, parser in cases:
            with self.subTest(file=name):
                raw = _load(name, valid=True)
                parsed = parser(raw)
                self.assertTrue(canonical_json_equal(raw, parsed.raw))
