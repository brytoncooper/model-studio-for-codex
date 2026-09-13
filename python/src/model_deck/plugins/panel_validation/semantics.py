"""Semantic panel-tree validation for ``ui.panel.v1``.

The validator walks a decoded panel document and accumulates every
violation into a detached :class:`PanelSemanticReport`. It does not
load schemas, inspect the filesystem, or contact the network; the
caller is responsible for parsing and structural schema validation
before calling :func:`validate_panel_semantics`.

The rules enforced here are documented in
``contracts/ui.panel.v1/README.md``:

- a ``ready`` panel must carry a non-empty ``root``;
- the tree depth must not exceed :data:`MAX_PANEL_DEPTH` (``16``);
- the total node count must not exceed :data:`MAX_PANEL_NODES` (``256``);
- node ids must be unique inside the tree;
- ``field_bindings`` values must resolve to ``text_input`` nodes in
  the same tree;
- a button may not declare the same key in both ``params`` and
  ``field_bindings``;
- the panel's top-level ``panel_id`` must equal the value supplied by
  the caller (typically the manifest contribution id);
- every button ``operation_id`` must belong to the caller-supplied
  declared operation ids.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

MAX_PANEL_DEPTH: Final[int] = 16
"""Maximum tree depth allowed for a ``ui.panel.v1`` panel."""

MAX_PANEL_NODES: Final[int] = 256
"""Maximum total node count allowed for a ``ui.panel.v1`` panel."""

PANEL_NODE_KIND_STACK: Final[str] = "stack"
"""Node kind for vertical container nodes."""

PANEL_NODE_KIND_TEXT: Final[str] = "text"
"""Node kind for static text nodes."""

PANEL_NODE_KIND_TEXT_INPUT: Final[str] = "text_input"
"""Node kind for editable text input nodes."""

PANEL_NODE_KIND_BUTTON: Final[str] = "button"
"""Node kind for action button nodes."""

PANEL_STATE_READY: Final[str] = "ready"
"""Panel state that requires a non-empty ``root`` node."""


class PanelSemanticCode:
    """Stable string codes returned on :class:`PanelSemanticFailure`."""

    PANEL_ID_MISMATCH = "panel_id_mismatch"
    READY_STATE_MISSING_ROOT = "ready_state_missing_root"
    DEPTH_EXCEEDED = "depth_exceeded"
    NODES_EXCEEDED = "nodes_exceeded"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    BINDING_NOT_TEXT_INPUT = "binding_not_text_input"
    PARAMS_BINDINGS_COLLISION = "params_bindings_collision"
    OPERATION_UNKNOWN = "operation_unknown"


@dataclass(frozen=True)
class PanelSemanticFailure:
    """One rule violation discovered by :func:`validate_panel_semantics`.

    Attributes:
        code: Stable error code from :class:`PanelSemanticCode`.
        field: JSON-path-style location of the defect, e.g.
            ``"root.children[1].field_bindings.title"``.
        detail: Human-readable explanation; never echoes caller values.
    """

    code: str
    field: str
    detail: str


@dataclass(frozen=True)
class PanelSemanticReport:
    """Detached, immutable outcome of :func:`validate_panel_semantics`.

    Attributes:
        failures: Tuple of :class:`PanelSemanticFailure`; empty when
            the tree satisfied every rule.
        node_count: Number of nodes visited during the walk, including
            the ``root`` (when present).
        depth: Maximum depth observed in the walked tree, where the
            ``root`` is depth ``1`` and each descent adds one.
    """

    failures: tuple[PanelSemanticFailure, ...] = ()
    node_count: int = 0
    depth: int = 0

    @property
    def ok(self) -> bool:
        """Return ``True`` when no rule was violated."""
        return not self.failures


@dataclass(frozen=True)
class PanelSemanticValidator:
    """Convenience wrapper that retains the caller's declared ids.

    Construct once per panel and reuse :meth:`validate` against multiple
    candidates. The declared ids tuple is preserved verbatim; the
    validator never mutates caller-supplied objects.
    """

    declared_panel_id: str
    declared_operation_ids: frozenset[str] = field(default_factory=frozenset)

    def validate(self, document: Mapping[str, Any]) -> PanelSemanticReport:
        """Run every semantic rule against ``document``."""
        return validate_panel_semantics(
            document,
            declared_panel_id=self.declared_panel_id,
            declared_operation_ids=self.declared_operation_ids,
        )


def _as_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _string_iterable(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (set, frozenset)):
        members = list(value)
    elif isinstance(value, list):
        members = value
    elif isinstance(value, tuple):
        members = list(value)
    else:
        raise ValueError(f"{field} must be a list of strings")
    if any(not isinstance(item, str) for item in members):
        raise ValueError(f"{field} must be a list of strings")
    return members


def validate_panel_semantics(
    document: Mapping[str, Any],
    *,
    declared_panel_id: str,
    declared_operation_ids: Iterable[str] | None = None,
) -> PanelSemanticReport:
    """Validate a panel document against the ``ui.panel.v1`` semantic rules.

    Args:
        document: Already-decoded panel mapping. Structural schema
            validation is the caller's responsibility.
        declared_panel_id: Panel id the caller expects this document to
            declare (typically the manifest contribution id).
        declared_operation_ids: Iterable of operation ids the manifest
            declares. Buttons may not reference operation ids outside
            this set. ``None`` means no operations are declared.

    Returns:
        A :class:`PanelSemanticReport` carrying every rule violation,
        the visited node count, and the observed tree depth.

    Raises:
        ValueError: When ``document`` is not a JSON object, or when
            ``declared_panel_id`` is not a string, or when
            ``declared_operation_ids`` contains non-string entries.
    """
    root = _as_mapping(document, field="document")
    if not isinstance(declared_panel_id, str):
        raise ValueError("declared_panel_id must be a string")
    declared_ops = frozenset(
        _string_iterable(declared_operation_ids, field="declared_operation_ids")
    )

    failures: list[PanelSemanticFailure] = []
    panel_id = root.get("panel_id")
    state = root.get("state")

    if panel_id != declared_panel_id:
        failures.append(
            PanelSemanticFailure(
                code=PanelSemanticCode.PANEL_ID_MISMATCH,
                field="panel_id",
                detail="panel panel_id does not match the declared panel id",
            )
        )

    raw_root = root.get("root")
    root_node: Mapping[str, Any] | None = None
    if state == PANEL_STATE_READY:
        if not isinstance(raw_root, Mapping):
            failures.append(
                PanelSemanticFailure(
                    code=PanelSemanticCode.READY_STATE_MISSING_ROOT,
                    field="root",
                    detail="ready panel must declare a root node",
                )
            )
        else:
            root_node = raw_root

    seen_ids: dict[str, int] = {}
    text_input_ids: set[str] = set()
    nodes_exceeded_reported = False
    total_nodes = 0
    observed_depth = 0

    def _record_node(node: Any, *, field: str, depth: int) -> bool:
        """Walk one node. Return ``False`` to stop descending further."""
        nonlocal total_nodes, nodes_exceeded_reported, observed_depth
        if depth > observed_depth:
            observed_depth = depth
        mapping = _as_mapping(node, field=field)
        node_id = mapping.get("id")
        kind = mapping.get("kind")

        if isinstance(node_id, str) and kind == PANEL_NODE_KIND_TEXT_INPUT:
            text_input_ids.add(node_id)

        if isinstance(node_id, str):
            prior_depth = seen_ids.get(node_id)
            if prior_depth is not None:
                failures.append(
                    PanelSemanticFailure(
                        code=PanelSemanticCode.DUPLICATE_NODE_ID,
                        field=field,
                        detail=(
                            "node id already used (prior occurrence at depth "
                            f"{prior_depth})"
                        ),
                    )
                )
            else:
                seen_ids[node_id] = depth

        total_nodes += 1
        if total_nodes > MAX_PANEL_NODES and not nodes_exceeded_reported:
            failures.append(
                PanelSemanticFailure(
                    code=PanelSemanticCode.NODES_EXCEEDED,
                    field=field,
                    detail=f"panel exceeds {MAX_PANEL_NODES} nodes",
                )
            )
            nodes_exceeded_reported = True

        if kind == PANEL_NODE_KIND_STACK:
            children = mapping.get("children")
            if not isinstance(children, list):
                return False
            for index, child in enumerate(children):
                if depth >= MAX_PANEL_DEPTH:
                    failures.append(
                        PanelSemanticFailure(
                            code=PanelSemanticCode.DEPTH_EXCEEDED,
                            field=field,
                            detail=f"panel depth exceeds {MAX_PANEL_DEPTH}",
                        )
                    )
                    return False
                _record_node(
                    child,
                    field=f"{field}.children[{index}]",
                    depth=depth + 1,
                )
            return False

        if kind == PANEL_NODE_KIND_BUTTON:
            params = mapping.get("params")
            bindings = mapping.get("field_bindings")
            if isinstance(params, Mapping) and isinstance(bindings, Mapping):
                shared_keys = sorted(set(params.keys()) & set(bindings.keys()))
                if shared_keys:
                    failures.append(
                        PanelSemanticFailure(
                            code=PanelSemanticCode.PARAMS_BINDINGS_COLLISION,
                            field=f"{field}.params",
                            detail=(
                                "params and field_bindings share keys; "
                                "the two maps must remain disjoint"
                            ),
                        )
                    )
                for binding_key, target in bindings.items():
                    if not isinstance(target, str):
                        continue
                    if target not in text_input_ids:
                        failures.append(
                            PanelSemanticFailure(
                                code=PanelSemanticCode.BINDING_NOT_TEXT_INPUT,
                                field=f"{field}.field_bindings.{binding_key}",
                                detail=(
                                    "field binding does not resolve to a "
                                    "text_input node in this tree"
                                ),
                            )
                        )
            operation_id = mapping.get("operation_id")
            if isinstance(operation_id, str):
                if operation_id not in declared_ops:
                    failures.append(
                        PanelSemanticFailure(
                            code=PanelSemanticCode.OPERATION_UNKNOWN,
                            field=f"{field}.operation_id",
                            detail=(
                                "button operation_id is not declared in "
                                "the manifest operations"
                            ),
                        )
                    )
        return False

    if root_node is not None:
        _record_node(root_node, field="root", depth=1)

    return PanelSemanticReport(
        failures=tuple(failures),
        node_count=total_nodes,
        depth=observed_depth,
    )
