"""Pure semantic validation for ``ui.panel.v1`` declarative trees.

The package is intentionally generic: callers hand it an already-decoded
panel document (typically a ``dict``), and it returns a detached
:class:`PanelSemanticReport` describing the rule outcomes. It performs
no filesystem access, no schema lookup, and no panel rendering.

The validator runs the structural rules the schema does not cover:

- ``ready`` panels must carry a ``root`` node.
- The tree depth must not exceed :data:`MAX_PANEL_DEPTH`.
- The total node count must not exceed :data:`MAX_PANEL_NODES`.
- Every node ``id`` must be unique inside the tree.
- Every button ``field_bindings`` value must resolve to a ``text_input``
  node in the same tree.
- A button may not declare the same key in both ``params`` and
  ``field_bindings``; the two maps are never merged.
- The panel's top-level ``panel_id`` must match the value supplied by
  the caller (typically the manifest contribution id).
- Every button ``operation_id`` must belong to the caller-supplied set
  of declared operation ids.

Each violation carries a stable :class:`PanelSemanticCode` so callers
can branch on results without parsing free-form detail messages.
"""
from __future__ import annotations

from .semantics import (
    MAX_PANEL_DEPTH,
    MAX_PANEL_NODES,
    PANEL_NODE_KIND_STACK,
    PANEL_NODE_KIND_TEXT,
    PANEL_NODE_KIND_TEXT_INPUT,
    PANEL_NODE_KIND_BUTTON,
    PANEL_STATE_READY,
    PanelSemanticCode,
    PanelSemanticFailure,
    PanelSemanticReport,
    PanelSemanticValidator,
    validate_panel_semantics,
)

__all__ = [
    "MAX_PANEL_DEPTH",
    "MAX_PANEL_NODES",
    "PANEL_NODE_KIND_STACK",
    "PANEL_NODE_KIND_TEXT",
    "PANEL_NODE_KIND_TEXT_INPUT",
    "PANEL_NODE_KIND_BUTTON",
    "PANEL_STATE_READY",
    "PanelSemanticCode",
    "PanelSemanticFailure",
    "PanelSemanticReport",
    "PanelSemanticValidator",
    "validate_panel_semantics",
]
