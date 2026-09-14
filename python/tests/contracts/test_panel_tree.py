import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contracts" / "ui.panel.v1" / "tree.schema.json"
TYPES_PATH = REPO_ROOT / "contracts" / "common" / "types.schema.json"
SUBSET_PATH = REPO_ROOT / "contracts" / "schema-subset.json"

SCHEMA_URI = "contracts/ui.panel.v1/tree.schema.json"
TYPES_URI = "contracts/common/types.schema.json"

_SUBSCHEMA_KEYS = ("items", "additionalProperties")
_SUBSCHEMA_LIST_KEYS = ("oneOf", "allOf")


def _normalize(uri: str) -> str:
    uri = uri.replace("\\", "/")
    uri = re.sub(r"^[^/]*://[^/]*", "", uri)
    parts: list[str] = []
    for part in uri.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    uri = "/".join(parts)
    if not uri.startswith("contracts/"):
        uri = f"contracts/{uri.lstrip('/')}"
    return uri


def _load_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    types_doc = json.loads(TYPES_PATH.read_text(encoding="utf-8"))
    store = {SCHEMA_URI: schema, TYPES_URI: types_doc}

    def retrieve(uri: str) -> Resource:
        key = _normalize(uri)
        if key in store:
            return Resource.from_contents(store[key])
        raise ValueError(f"no local schema for {uri}")

    registry = Registry(retrieve=retrieve).with_resources(
        [(uri, Resource.from_contents(doc)) for uri, doc in store.items()]
    )
    return Draft202012Validator(schema, registry=registry)


def _list_panel() -> dict:
    return {
        "panel_id": "deck.session.notebook",
        "revision": 3,
        "title": "Session notes",
        "state": "ready",
        "root": {
            "id": "root",
            "kind": "stack",
            "children": [
                {"id": "row1", "kind": "text", "value": "First note"},
                {
                    "id": "open1",
                    "kind": "button",
                    "label": "Open",
                    "operation_id": "engine.v1.sessions.open",
                    "params": {"session": "abc"},
                },
            ],
        },
    }


def _editor_panel() -> dict:
    return {
        "panel_id": "deck.session.editor",
        "revision": 0,
        "title": "Edit note",
        "state": "ready",
        "root": {
            "id": "root",
            "kind": "stack",
            "children": [
                {
                    "id": "body",
                    "kind": "text_input",
                    "label": "Note body",
                    "value": "draft",
                    "multiline": True,
                },
                {
                    "id": "save",
                    "kind": "button",
                    "label": "Save",
                    "operation_id": "engine.v1.sessions.save",
                    "params": {},
                    "field_bindings": {"body": "body"},
                },
            ],
        },
    }


class PanelTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _load_validator()

    def test_list_panel_validates(self) -> None:
        self.validator.validate(_list_panel())

    def test_editor_panel_validates(self) -> None:
        self.validator.validate(_editor_panel())

    def test_loading_state_without_root_validates(self) -> None:
        self.validator.validate(
            {
                "panel_id": "deck.session.notebook",
                "revision": 4,
                "title": "Session notes",
                "state": "loading",
                "message": "Fetching sessions",
            }
        )

    def test_unknown_node_kind_rejected(self) -> None:
        panel = _list_panel()
        panel["root"]["children"][0] = {"id": "web", "kind": "webview", "value": "x"}
        with self.assertRaises(Exception):
            self.validator.validate(panel)

    def test_undeclared_node_field_rejected(self) -> None:
        panel = _list_panel()
        panel["root"]["children"][0]["href"] = "https://example.invalid"
        with self.assertRaises(Exception):
            self.validator.validate(panel)

    def test_bad_shapes_rejected(self) -> None:
        cases = [
            ({**_list_panel(), "panel_id": "not a reverse id!"}, "panel_id"),
            ({**_list_panel(), "revision": -1}, "revision"),
            ({**_list_panel(), "state": "streaming"}, "state"),
            ({**_editor_panel(), "root": {"id": "root"}}, "root"),
        ]
        button = _editor_panel()
        del button["root"]["children"][1]["operation_id"]
        cases.append((button, "operation_id"))
        bindings = _editor_panel()
        bindings["root"]["children"][1]["field_bindings"] = {"body": 42}
        cases.append((bindings, "field_bindings"))
        for panel, _name in cases:
            with self.subTest(case=_name):
                with self.assertRaises(Exception):
                    self.validator.validate(panel)

    def test_schema_uses_only_permitted_keywords(self) -> None:
        allowed = set(json.loads(SUBSET_PATH.read_text(encoding="utf-8"))["allowed_keywords"])
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        seen: set[str] = set()

        def visit(node: object) -> None:
            if isinstance(node, list):
                for entry in node:
                    visit(entry)
                return
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key in ("properties", "definitions"):
                    seen.add(key)
                    for entry in value.values():
                        visit(entry)
                elif key in _SUBSCHEMA_KEYS:
                    seen.add(key)
                    visit(value)
                elif key in _SUBSCHEMA_LIST_KEYS:
                    seen.add(key)
                    for entry in value:
                        visit(entry)
                else:
                    seen.add(key)

        visit(schema)
        self.assertEqual(seen - allowed, set())

    def test_refs_resolve_to_files_on_disk(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        refs: list[str] = []

        def collect(node: object) -> None:
            if isinstance(node, list):
                for entry in node:
                    collect(entry)
                return
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value.split("#")[0])
                else:
                    collect(value)

        collect(schema)
        refs = [ref for ref in refs if ref]
        self.assertTrue(refs)
        for ref in refs:
            target = (SCHEMA_PATH.parent / ref).resolve()
            self.assertTrue(target.is_file(), f"missing ref target {ref}")
            self.assertTrue(str(target).startswith(str(REPO_ROOT)), f"ref escapes repo {ref}")

    def test_text_input_label_required(self) -> None:
        panel = _editor_panel()
        del panel["root"]["children"][0]["label"]
        with self.assertRaises(Exception):
            self.validator.validate(panel)

    def test_text_input_empty_label_rejected(self) -> None:
        panel = _editor_panel()
        panel["root"]["children"][0]["label"] = ""
        with self.assertRaises(Exception):
            self.validator.validate(panel)

    def test_ready_without_root_rejected(self) -> None:
        panel = _list_panel()
        del panel["root"]
        with self.assertRaises(Exception):
            self.validator.validate(panel)

    def test_revision_max_boundary_accepted(self) -> None:
        # IEEE-754 safe integer maximum (2^53 - 1). The shared bound lets
        # the value survive exact round-trip across JSON, JavaScript,
        # JSON-RPC, Swift Int, and Python int without coercion.
        panel = {**_list_panel(), "revision": 9_007_199_254_740_991}
        # No exception means the validator accepted the exact upper bound.
        self.validator.validate(panel)

    def test_revision_above_max_boundary_rejected(self) -> None:
        # One above 2^53 - 1 must reject. The shared bound is exact, not
        # silently clamped by any consumer.
        panel = {**_list_panel(), "revision": 9_007_199_254_740_992}
        with self.assertRaises(Exception):
            self.validator.validate(panel)

if __name__ == "__main__":
    unittest.main()
