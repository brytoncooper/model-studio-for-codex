# Panel tree semantic validation

## Purpose and ownership

This package runs the semantic rules the ``ui.panel.v1`` JSON schema
deliberately does not enforce. Callers hand it an already-decoded panel
document (typically after structural schema validation against
``contracts/ui.panel.v1/tree.schema.json``); the validator returns a
detached :class:`PanelSemanticReport` describing the rule outcomes.

The package is generic: it has no knowledge of plugin manifests, archive
layouts, or engine wiring. Callers supply the declared panel id and the
declared operation ids, and the validator runs the following rules:

- ``ready`` panels must carry a non-empty ``root`` node;
- tree depth must not exceed :data:`MAX_PANEL_DEPTH` (``16``);
- total node count must not exceed :data:`MAX_PANEL_NODES` (``256``);
- node ids must be unique inside the tree;
- button ``field_bindings`` values must resolve to ``text_input``
  nodes in the same tree;
- a button may not declare the same key in both ``params`` and
  ``field_bindings``;
- the panel's top-level ``panel_id`` must equal the caller-supplied
  declared panel id;
- every button ``operation_id`` must belong to the caller-supplied
  declared operation ids.

The validator performs no filesystem access, no network call, no
schema lookup, and no panel rendering.

## Public contracts

- ``validate_panel_semantics(document, *, declared_panel_id, declared_operation_ids=None)``
  returns a :class:`PanelSemanticReport`. ``report.ok`` is true only
  when every rule passed; the report carries the visited node count and
  the maximum observed depth alongside the failures.
- :class:`PanelSemanticValidator` is a thin convenience wrapper that
  remembers the declared ids; ``validator.validate(document)`` runs
  the same rules.
- :class:`PanelSemanticCode` exposes the stable rule codes
  (``PANEL_ID_MISMATCH``, ``READY_STATE_MISSING_ROOT``,
  ``DEPTH_EXCEEDED``, ``NODES_EXCEEDED``, ``DUPLICATE_NODE_ID``,
  ``BINDING_NOT_TEXT_INPUT``, ``PARAMS_BINDINGS_COLLISION``,
  ``OPERATION_UNKNOWN``).
- :class:`PanelSemanticFailure` carries one code, a JSON-path-style
  ``field``, and a human-readable ``detail`` that never echoes caller
  values.
- ``MAX_PANEL_DEPTH`` and ``MAX_PANEL_NODES`` document the current
  limits as module-level ``Final`` constants.

## Invariants

The walk is iterative-friendly and single-pass; the validator never
descends past :data:`MAX_PANEL_DEPTH` levels even when a defect would
otherwise let the walk run away. The text-input id set is built
depth-first so a button may reference a text input nested below it.
The first ``NODES_EXCEEDED`` failure is recorded once; subsequent
nodes still consume the budget so other rules (duplicate ids,
bindings, params/bindings collision) remain meaningful.

## Extending

Add a new semantic rule by introducing a stable code in
:class:`PanelSemanticCode` and recording a
:class:`PanelSemanticFailure` from inside :func:`validate_panel_semantics`.
Keep the new rule pure: no filesystem access, no network call, no
schema lookup, no plugin state. Document the rule in this README and
in :file:`contracts/ui.panel.v1/README.md`.

## Tests

From ``python/``:

```sh
PYTHONPATH=src .venv/bin/python -B -m unittest tests.plugins.authoring.test_panel_validation
```

The tests use in-memory panel mappings and the :class:`PanelSemanticValidator`
fixture. They cover every rule, the ready/loading state difference,
non-string declared ids, and the prepared Session Notebook list and
editor trees.

## Limitations

The validator never executes the panel, never resolves operation ids
through plugin authority, and never enforces styling, layout direction,
or permission policy. Buttons still need the engine-side policy check
at invoke time.
