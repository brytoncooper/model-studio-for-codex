import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = REPO_ROOT / "contracts"


def _canonical_validator(schema_path: Path) -> Draft202012Validator:
    resources: dict[str, Resource] = {}
    for path in CONTRACTS_ROOT.rglob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        schema_id = document.get("$id")
        if isinstance(schema_id, str):
            resources[schema_id] = Resource.from_contents(document)
    registry = Registry().with_resources(resources.items())
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(
        schema,
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _broker_context() -> dict:
    return {
        "activation_id": "31fcad7d-c29c-4af9-b5f3-cfd6d3d376e2",
        "plugin_id": "org.example.notebook",
        "invocation_handle": "broker-handle-issued-by-supervisor",
        "revocation_generation": 4,
    }


def _panel() -> dict:
    return {
        "panel_id": "org.example.notebook.panel",
        "revision": 2,
        "title": "Session Notebook",
        "state": "ready",
        "root": {
            "id": "root",
            "kind": "stack",
            "children": [
                {"id": "empty", "kind": "text", "value": "No notes yet"},
            ],
        },
    }


class PluginInvocationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _canonical_validator(
            CONTRACTS_ROOT / "plugin.v1" / "lifecycle" / "invoke.params.schema.json"
        )

    def test_invoke_carries_required_broker_context(self) -> None:
        self.validator.validate(
            {
                "operation_id": "org.example.notebook.notes.create",
                "input": {"body": "Remember this"},
                "broker_context": _broker_context(),
            }
        )

    def test_invoke_without_broker_context_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.validator.validate(
                {
                    "operation_id": "org.example.notebook.notes.create",
                    "input": {"body": "Remember this"},
                }
            )

    def test_invoke_context_without_supervisor_handle_is_rejected(self) -> None:
        context = _broker_context()
        del context["invocation_handle"]
        with self.assertRaises(ValidationError):
            self.validator.validate(
                {
                    "operation_id": "org.example.notebook.notes.create",
                    "input": {},
                    "broker_context": context,
                }
            )

    def test_invoke_context_rejects_caller_supplied_authority_fields(self) -> None:
        context = _broker_context()
        context["origin_principal_id"] = "5448b158-c353-4a98-a38c-ea08b678d2f0"
        with self.assertRaises(ValidationError):
            self.validator.validate(
                {
                    "operation_id": "org.example.notebook.notes.create",
                    "input": {},
                    "broker_context": context,
                }
            )


class PanelFetchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _canonical_validator(
            CONTRACTS_ROOT / "engine.v1" / "methods" / "ui.panel.get.result.schema.json"
        )

    def test_panel_fetch_returns_renderable_tree(self) -> None:
        self.validator.validate({"panel": _panel()})

    def test_panel_fetch_rejects_old_descriptor_pointer(self) -> None:
        with self.assertRaises(ValidationError):
            self.validator.validate(
                {
                    "descriptor": {
                        "panel_id": "org.example.notebook.panel",
                        "schema_id": "contracts/ui.panel.v1/tree.schema.json",
                    }
                }
            )

    def test_panel_fetch_rejects_invalid_tree(self) -> None:
        panel = _panel()
        del panel["root"]["children"][0]["value"]
        with self.assertRaises(ValidationError):
            self.validator.validate({"panel": panel})


if __name__ == "__main__":
    unittest.main()
