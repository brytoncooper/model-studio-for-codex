# ExtensionUI

Native Swift panel values, identity, and semantic validation for the
extension UI surface. No renderer, no AppKit, no provider/network use.

## Purpose

Decode a raw panel payload into an immutable snapshot validated for the
structural and local semantic rules this module owns. A renderer can consume
the resulting `PanelDocument` directly. Typed errors let it handle failures
without parsing JSON. The accepted panel contract shape is owned by the
contract schema;
this module mirrors that shape without duplicating it. Trusted operation
discovery and authority checks remain outside this codec.

## Public surface (immutable, validated snapshot)

- `PanelDocumentLimits` — frozen bounds: `maxRawBytes` (1 MiB),
  `maxDepth` (16), `maxNodes` (256), `maxChildrenPerStack` (64),
  `maxParamsKeys` (64), `maxBindingsKeys` (64), `maxJsonValueDepth`
  (64), plus internal scalar length and JSON-value bounds.
- `PanelDocument` — immutable snapshot value: `panelID`, `revision`,
  `title`, `state`, `message`, `root` (all `public let`).
  Constructed only by the codec/module via internal `init`. Computed:
  `isReady`, `hasStaleRoot`.
- `PanelState` — `loading`, `ready`, `empty`, `error`, `unavailable`.
- `NodeKind` — `stack`, `text`, `textInput` (`"text_input"`), `button`.
- `NodeID` — validated `String` wrapper. ASCII-only
  (a-z, 0-9, `_`, `-`); non-ASCII scalars are rejected.
- `PanelNode` (indirect enum) — `stack(StackNode)`, `text(TextNode)`,
  `textInput(TextInputNode)`, `button(ButtonNode)`. Computed `id`,
  `kind`.
- `StackNode`, `TextNode`, `TextInputNode`, `ButtonNode` —
  immutable snapshots (public `let`); initializers are `internal`.
  `ButtonNode.params: [String: JSONValue]` (existing `ModelDeckContracts`
  type). `ButtonNode.fieldBindings: [String: NodeID]`.
- `PanelDocumentError` — typed, `Equatable` errors for rejection paths.
  `description` carries structural codes and
  controlled categories (`stack.children`, controlled field paths) but
  no client-supplied kind, key, node id, binding key, binding target, or
  parent identifier.
- `PanelDocumentCodec.decode(_:) -> PanelDocument` — entry point.
- `PanelNode` extensions: `walk(_:)`, `totalNodeCount()`,
  `textInputCount()`, `node(withID:)`.

`JSONValue` is the existing public type from `ModelDeckContracts`. It is
reused for `params` so renderer and contracts agree on shape. Because
`JSONValue` is not `Sendable`, the document/node types that transitively
hold it are not `Sendable` either.

## Invariants

- The decoder validates in three layers and only returns success when
  all three pass: payload shape, per-field bounds, and post-walk
  semantics. Semantic verification is internal; the renderer never
  revalidates because it never mutates a `PanelDocument`.
- `ready` state requires a `root`; other states may omit `root` or carry a
  retained stale node (`hasStaleRoot == true`). An explicit JSON `null` is not
  a node and is rejected in every state.
- Node ids are unique across the entire tree; depth is bounded at 16;
  total node count is bounded at 256; stack children are bounded at 64.
- `params` and `field_bindings` keys cannot collide on the same button;
  collision is rejected with `paramsBindingsCollision`.
- `field_bindings` values must resolve to a `text_input` node within the
  same tree; resolution failures raise `bindingTargetNotFound` or
  `bindingTargetNotTextInput`.
- `textInput.label` is required and non-empty; an empty or missing label
  fails decoding so a renderer can rely on it for accessibility.
- String scalar length is counted in Unicode scalars (code points), per
  JSON Schema `maxLength`. UTF-8 byte count and grapheme cluster count
  are not used.
- Revisions accept any JSON number that converts exactly to a nonnegative
  native `Int`, including integral decimal and exponent spellings such as
  `1.0` and `1e0`.
- `JSONValue` carried in `params` is bounded in depth, string size,
  array length, and object key count via `maxJsonValueDepth` and the
  related JSON value limits.
- Booleans are accepted only when the value is a JSON `true`/`false`;
  numeric `0`/`1` and string `"true"`/`"false"` are rejected as
  `invalidJSON`.
- Static JSON values round-trip via `JSONValue`; the codec does not
  synthesize or coerce values.
- The decoder never inspects the panel payload as source code or
  templates; errors carry structural codes only.

## Extend

- New node kinds require a panel schema change first; this module does
  not invent kinds.
- New `JSONValue` bounds belong to the schema's `json_value` shape.
- Error cases should describe a structural failure; never include
  source values or labels in the error description.
- The renderer must use `NodeID` and `JSONValue` for any node ids or
  static parameter values it produces, not raw `String`.

## Tests

Run the focused suite with an isolated build directory:

```sh
swift test --package-path macos --scratch-path /tmp/model-deck-panel-build \
  --filter PanelDocumentTests
```

## Limits

- 1 MiB raw payload ceiling.
- 16 nesting levels, 256 nodes total, 64 children per stack.
- 64 `params` keys, 64 `fieldBindings` keys per button.
- Revisions are bounded to `0...9_007_199_254_740_991` (IEEE-754 safe
  integer maximum, 2^53 - 1). This matches the canonical `revision`
  definition in `contracts/common/types.schema.json` and is enforced
  identically across JSON, Python, and Swift without coercion. Larger
  mathematical integers are rejected as `revisionOutOfRange`.
- 64-deep `JSONValue` nesting with 1 MiB string scalars, 4 096 array
  items, 1 024 object keys.
- No AppKit / SwiftUI / runtime use; pure Foundation + `ModelDeckContracts`.
- Operation ids are checked for reverse-domain format only. This codec does
  not resolve them through discovery or decide whether an invocation is
  authorized.

## Repair notes

- Errors are exhaustive `Equatable` cases. Match on cases, not on
  descriptions, when reporting back to the UI.
- `payloadTooLarge` is raised before UTF-8/JSON parsing; size-bound
  measurement is the raw `Data` count.
- `invalidUTF8` is raised only when the raw bytes fail UTF-8 decoding;
  any valid UTF-8 that is not JSON raises `invalidJSON` (never
  `invalidUTF8`).
- The semantic registry walks the tree once at decode time. Because
  `PanelDocument` is immutable, the renderer does not (and cannot)
  re-validate; rebuild the document via `decode` if upstream input
  changes.
- `state != .ready && root != nil` is `hasStaleRoot == true`; a renderer
  may keep the stale tree visible while showing the new state.
