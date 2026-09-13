# Panel renderer

`PanelRenderer` is the native AppKit presentation boundary for validated
`PanelDocument` snapshots. It renders the four declarative node kinds without
interpreting text as a URL, plugin selector, native class, script, or command.

## Ownership

The renderer owns AppKit view construction, current text-field values, focus,
button availability, and action-intent assembly. `PanelDocumentCodec` owns
document validation. The trusted host owns operation discovery, authorization,
execution, retries, and presentation refreshes.

The renderer never imports a provider runtime, calls a transport, executes an
operation, or mutates the immutable `PanelDocument` tree.

## Public contract

- `TrustedPanelOperationDescriptor` is the small presentation-layer result of
  trusted host discovery. It carries only the resolved operation id and is not a
  wire or transport descriptor.
- `TrustedPanelOperationLookup` asynchronously resolves an operation id. A
  missing result leaves the corresponding button disabled.
- `PanelActionIntent` carries the panel id, document revision, resolved
  operation id, and a click-time parameter snapshot.
- `PanelActionIntentCallback` receives the intent on the main actor. The host
  decides whether and how to execute it.
- `PanelRenderer.init(document:operationLookup:onActionIntent:)` creates the
  renderer. `update(document:)` accepts a different panel or a newer revision
  of the current panel and reports whether the snapshot was applied.

## Rendering and state

`stack` becomes a vertical `NSStackView`. `text` becomes a selectable but inert
wrapping label with no target, action, link detection, or attributed link.
`text_input` becomes a labeled `NSTextField` or an `NSTextView` in a scroll view
when `multiline` is true. `button` becomes a native `NSButton`.

All five panel states render native status text:

- `loading`: the document message or `Loading…`.
- `ready`: the optional document message and an interactive root.
- `empty`: the document message or `No content.`.
- `error`: the document message or `Unable to load this panel.`.
- `unavailable`: the document message or `This panel is unavailable.`.

A non-ready document may retain a stale root. The renderer keeps that tree
visible at reduced emphasis while disabling every button and text edit. It does
not perform operation lookup for a non-ready document.

## Field values and action intents

Current text lives in renderer-owned `[NodeID: String]` state. Editing a native
control updates that state and never changes the `PanelDocument`. On an enabled
button click, the renderer starts with the button's static `params` and inserts
the current text for each `fieldBindings` entry. The validated document already
guarantees that static and bound keys do not overlap; the renderer also refuses
to emit if an overlap or missing input is encountered.

Static `JSONValue` values are copied without coercion, so `false`, numeric zero,
and `null` remain distinct from absent values and from strings. The callback is
an intent boundary only; no operation runs inside the renderer.

## Refresh policy

Revisions are monotonic within a panel id. `update(document:)` ignores equal or
older revisions, which keeps the current controls, edits, focus, resolved
operations, and pending lookup generation intact. A different panel id begins a
new revision sequence regardless of its revision number.

An accepted newer snapshot replaces field values with that document's declared
values. If the same input id remains in a ready snapshot, keyboard focus and its
UTF-16 selection range are restored after rebuilding the native controls. The
range is clamped when the replacement text is shorter. Moving to a non-ready
state intentionally releases editing focus because its controls are disabled.

Each accepted snapshot starts a new lookup generation and cancels the prior
tasks. A late result is applied only when panel id, revision, generation, node
id, button identity, and operation id still match. Cancellation alone is not
trusted to suppress a host lookup that is already in flight.

## Accessibility

The title and state expose stable native accessibility labels. Every text input
uses its required document label for both its visible title and accessibility
label. Buttons use their visible label. Stack and input groups use AppKit group
roles, multiline fields remain native text views, and the enabled input/button
controls form a keyboard key-view loop.

## Tests

The focused suite uses two synthetic, provider-neutral panels. It covers all
four nodes, inert URL-shaped text, single- and multi-line editing, unknown
operation disabling, exact parameter binding, five states, stale roots, focus,
older revisions, and stale asynchronous lookup results.

For isolated verification, copy the Contracts, Client and Presentation library
targets and these tests into a fresh temporary Swift package, preserving their
dependency order and resources. Run `PanelRendererTests` and `PanelDocumentTests`
there. The source package also declares an executable; a test filter alone does
not restrict which targets SwiftPM builds.

Independent verification compiled that library-only package and passed both
renderer tests, all 42 document tests and two additional state/lookup probes.
No installed application or original build directory was used.

## Limitations

- Layout is a single vertical stack contract; panels cannot select native view
  classes, orientation, colors, fonts, links, or application destinations.
- The renderer does not show operation failures because it does not execute
  operations. The host supplies a newer `PanelDocument` for progress or errors.
- Accepted newer revisions reset current text to the new document values. The
  host should avoid advancing the revision while it intends to preserve an
  unsubmitted draft.
