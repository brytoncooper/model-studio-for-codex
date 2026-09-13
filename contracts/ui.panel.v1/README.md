# ui.panel.v1 declarative tree

Application-owned contract for validated extension panel documents. Static
documents are fetched through `engine.v1.ui.panel.get`; an operation may also
return a newer document in the optional `panel` member of its invocation result.
The trusted host validates both shapes before a native client renders them.

## What it covers

A panel tree carries `panel_id`, `revision`, `title`, `state`, an optional `message`, and an optional `root` node. `root` is required for `ready` trees. Non-ready states (`loading`, `empty`, `error`, `unavailable`) may omit `root` or retain a stale `root` from a previous ready render; the renderer MUST treat retained content as stale and disable all button actions and `text_input` edits unless `state` is `ready`. Non-ready states explain themselves through `message`.

Four node kinds exist: `stack` (vertical children), `text` (static value), `text_input` (editable value with `multiline` and a required nonempty `label` that names the field for visible labeling and assistive association), and `button` (invokes one `operation_id` with static `params` plus `field_bindings` that map a param field to a `text_input` node id in the same tree).

## What it deliberately excludes

No code, URLs, selectors, WebView, expression DSL, layout directions, or styling. The two Session Notebook panels fit inside this: the list panel stacks `text` rows with per-row `button` actions, and the editor panel binds one `text_input` to a save `button` through `field_bindings`.

## Rules enforced by semantic validation

Max depth 16, max 256 nodes, unique node ids, bindings that resolve to a
`text_input` node in the same tree, and `operation_id` values declared by the
same extension are enforced by the semantic validator. A returned document's
`panel_id` must also name a panel contributed by that extension. A param key
present in both `params` and `field_bindings` is rejected; the maps are never
merged and neither side overrides the other. Buttons carry no permission or
confirmation semantics; the host applies its own policy at invoke time.
