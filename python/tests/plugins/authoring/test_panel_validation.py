"""Focused tests for the pure panel-semantic validator.

These tests cover the rules documented in
``contracts/ui.panel.v1/README.md`` that the structural JSON schema
deliberately does not enforce. They exercise the validator directly
against in-memory document mappings and never touch the filesystem.
"""
from __future__ import annotations

import unittest

from model_deck.plugins.panel_validation import (
    MAX_PANEL_DEPTH,
    MAX_PANEL_NODES,
    PANEL_NODE_KIND_BUTTON,
    PANEL_NODE_KIND_STACK,
    PANEL_NODE_KIND_TEXT,
    PANEL_NODE_KIND_TEXT_INPUT,
    PANEL_STATE_READY,
    PanelSemanticCode,
    PanelSemanticValidator,
    validate_panel_semantics,
)


_PANEL_ID = "org.example.test.panel"
_OP_LIST = "org.example.test.notes.list"
_OP_CREATE = "org.example.test.notes.create"


def _ready_root(children):
    return {
        "panel_id": _PANEL_ID,
        "revision": 0,
        "title": "Fixture",
        "state": PANEL_STATE_READY,
        "root": {"id": "root", "kind": PANEL_NODE_KIND_STACK, "children": children},
    }


def _button(*, node_id, operation_id, params=None, field_bindings=None):
    node = {
        "id": node_id,
        "kind": PANEL_NODE_KIND_BUTTON,
        "label": "Action",
        "operation_id": operation_id,
        "params": params if params is not None else {},
    }
    if field_bindings is not None:
        node["field_bindings"] = field_bindings
    return node


def _text_input(node_id):
    return {
        "id": node_id,
        "kind": PANEL_NODE_KIND_TEXT_INPUT,
        "value": "",
        "label": "Label",
    }


class ValidatePanelSemanticsTests(unittest.TestCase):
    """Positive and negative coverage of :func:`validate_panel_semantics`."""

    def test_accepts_notebook_list_panel(self):
        document = _ready_root(
            [
                {
                    "id": "empty_message",
                    "kind": PANEL_NODE_KIND_TEXT,
                    "value": "No notes yet.",
                },
                _button(node_id="refresh", operation_id=_OP_LIST),
            ]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[_OP_LIST, _OP_CREATE],
        )
        self.assertTrue(report.ok, msg=str(report.failures))
        self.assertEqual(report.node_count, 3)
        self.assertEqual(report.depth, 2)

    def test_accepts_notebook_editor_panel_with_text_input_bindings(self):
        document = _ready_root(
            [
                _text_input("title"),
                _text_input("body"),
                _button(
                    node_id="save",
                    operation_id=_OP_CREATE,
                    field_bindings={"title": "title", "body": "body"},
                ),
            ]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[_OP_CREATE],
        )
        self.assertTrue(report.ok, msg=str(report.failures))
        self.assertEqual(report.node_count, 4)

    def test_ready_state_without_root_fails(self):
        document = {
            "panel_id": _PANEL_ID,
            "revision": 0,
            "title": "Fixture",
            "state": PANEL_STATE_READY,
        }
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[],
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].code, PanelSemanticCode.READY_STATE_MISSING_ROOT)
        self.assertEqual(report.failures[0].field, "root")

    def test_panel_id_mismatch_fails(self):
        document = _ready_root([])
        report = validate_panel_semantics(
            document,
            declared_panel_id="org.example.different",
            declared_operation_ids=[],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.PANEL_ID_MISMATCH, codes)

    def test_depth_exceeded_at_max_plus_one(self):
        children = []
        for depth in range(MAX_PANEL_DEPTH + 2):
            children = [{"id": f"d{depth}", "kind": PANEL_NODE_KIND_STACK, "children": children}]
        document = {
            "panel_id": _PANEL_ID,
            "revision": 0,
            "title": "Deep",
            "state": PANEL_STATE_READY,
            "root": {"id": "root", "kind": PANEL_NODE_KIND_STACK, "children": children},
        }
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.DEPTH_EXCEEDED, codes)

    def test_nodes_exceeded_records_once(self):
        # A wide-but-shallow tree: one root stack with MAX_PANEL_NODES+1
        # text children keeps depth at 2 while pushing the node count
        # past the 256-node budget.
        children = [
            {"id": f"n{i}", "kind": PANEL_NODE_KIND_TEXT, "value": ""}
            for i in range(MAX_PANEL_NODES + 1)
        ]
        document = {
            "panel_id": _PANEL_ID,
            "revision": 0,
            "title": "Wide",
            "state": PANEL_STATE_READY,
            "root": {"id": "root", "kind": PANEL_NODE_KIND_STACK, "children": children},
        }
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        nodes_exceeded_count = codes.count(PanelSemanticCode.NODES_EXCEEDED)
        self.assertEqual(nodes_exceeded_count, 1)

    def test_duplicate_node_id_fails(self):
        document = _ready_root(
            [
                {"id": "shared", "kind": PANEL_NODE_KIND_TEXT, "value": "x"},
                {"id": "shared", "kind": PANEL_NODE_KIND_TEXT, "value": "y"},
            ]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.DUPLICATE_NODE_ID, codes)

    def test_field_binding_to_non_text_input_fails(self):
        document = _ready_root(
            [
                {"id": "title", "kind": PANEL_NODE_KIND_TEXT, "value": "x"},
                _button(
                    node_id="save",
                    operation_id=_OP_CREATE,
                    field_bindings={"title": "title"},
                ),
            ]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[_OP_CREATE],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.BINDING_NOT_TEXT_INPUT, codes)

    def test_params_and_field_bindings_collision_fails(self):
        document = _ready_root(
            [
                _text_input("title"),
                _button(
                    node_id="save",
                    operation_id=_OP_CREATE,
                    params={"title": "literal"},
                    field_bindings={"title": "title"},
                ),
            ]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[_OP_CREATE],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.PARAMS_BINDINGS_COLLISION, codes)

    def test_button_operation_id_not_in_declared_set_fails(self):
        document = _ready_root(
            [_button(node_id="save", operation_id="org.example.test.notes.delete")]
        )
        report = validate_panel_semantics(
            document,
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=[_OP_CREATE],
        )
        self.assertFalse(report.ok)
        codes = [f.code for f in report.failures]
        self.assertIn(PanelSemanticCode.OPERATION_UNKNOWN, codes)

    def test_non_string_declared_panel_id_raises_value_error(self):
        document = _ready_root([])
        with self.assertRaises(ValueError):
            validate_panel_semantics(
                document,
                declared_panel_id=123,
                declared_operation_ids=[],
            )

    def test_non_string_declared_operation_ids_raises_value_error(self):
        document = _ready_root([])
        with self.assertRaises(ValueError):
            validate_panel_semantics(
                document,
                declared_panel_id=_PANEL_ID,
                declared_operation_ids=["ok", 7],
            )

    def test_non_mapping_document_raises_value_error(self):
        with self.assertRaises(ValueError):
            validate_panel_semantics(
                "not a panel document",
                declared_panel_id=_PANEL_ID,
                declared_operation_ids=[],
            )


class PanelSemanticValidatorWrapperTests(unittest.TestCase):
    """Coverage for the convenience wrapper."""

    def test_wrapper_reuses_declared_ids(self):
        validator = PanelSemanticValidator(
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=frozenset({_OP_CREATE}),
        )
        document = _ready_root(
            [_button(node_id="save", operation_id=_OP_CREATE)]
        )
        report = validator.validate(document)
        self.assertTrue(report.ok, msg=str(report.failures))

    def test_wrapper_still_fails_for_unknown_operation(self):
        validator = PanelSemanticValidator(
            declared_panel_id=_PANEL_ID,
            declared_operation_ids=frozenset({_OP_CREATE}),
        )
        document = _ready_root(
            [_button(node_id="save", operation_id="org.example.test.notes.delete")]
        )
        report = validator.validate(document)
        self.assertFalse(report.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
