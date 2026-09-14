# Session Notebook example plugin

Session Notebook is an independently packaged, standard-library-only Python
plugin. It stores user-authored notes through Model Deck's scoped storage
broker and exposes generic operations for create, get, list, update, delete,
preview, and asynchronous Markdown export. The worker never imports Model Deck
engine code and uses standard input/output as its only I/O channel.

## What this slice proves

- A manifest can contribute new operations and two declarative panels under
  the plugin's own namespace.
- An isolated worker can complete manual note CRUD through only
  `plugin.v1.broker.storage.{get,list,put,delete}`.
- The trusted host supplies the invocation handle. The worker always uses the
  fixed `org.example.notebook` namespace and never accepts a namespace from
  operation input.
- Note updates and deletes use the revision returned by storage. Stale writes
  fail in the broker rather than replacing newer content.
- Stored note metadata remains ordinary JSON. Values such as `false`, `null`,
  empty strings, arrays, and nested objects are not coerced.
- Starting a new activation over the same owned storage retains the notes.
- Export runs as a durable application-owned job. It reports progress, checks
  cancellation at real work boundaries, and persists a bounded JSON result
  containing the completed Markdown.

The manifest requests `storage.own` and `jobs.own`. It requests no session
metadata, transcript, content, network, credential, attachment, or filesystem
permission. Manual notes therefore work when the host has no session metadata
capability.

## Operations

| Operation | Effect | Input |
| --- | --- | --- |
| `org.example.notebook.notes.create` | write | `title`, `body`, optional `metadata` |
| `org.example.notebook.notes.get` | read | `note_id` |
| `org.example.notebook.notes.list` | read | empty object |
| `org.example.notebook.notes.update` | write | `note_id`, `expected_revision`, full `title`, `body`, optional `metadata` |
| `org.example.notebook.notes.delete` | write | `note_id`, `expected_revision` |
| `org.example.notebook.export.preview` | read | empty object |
| `org.example.notebook.export.start` | write | empty object |

Notes use `notes/<uuid>` keys. Listing is deliberately capped at the frozen
storage wire's 200-item limit because that wire has no cursor. Update first
reads the note, then performs compare-and-swap; a deleted note is not silently
recreated. Delete likewise verifies that the note exists before applying CAS.

`export.preview` retains the small synchronous preview. `export.start` creates a
durable job and immediately returns its UUID. The worker reads the actual stored
notes, reports monotonic progress, checks cancellation between broker-backed
work steps, and completes with this generic bounded result:

```json
{
  "media_type": "text/markdown",
  "suggested_filename": "session-notebook.md",
  "content": "# Session Notebook\n..."
}
```

The result is retrieved through public `engine.v1.jobs.get`; cancellation is
requested through `engine.v1.jobs.cancel`. A successful cancel request is only
an acknowledgement until a later read reports `cancelled`. No plugin-selected
path or broad filesystem access is involved. Interrupted jobs are not replayed
automatically, and explicit resume remains future B19 work.

## Panels

`panels/notebook-list.json` and `panels/notebook-editor.json` are the initial
`ui.panel.v1` documents. Notebook operations may return a newer validated panel
document beside their ordinary output. Refresh returns real note rows with
per-note Open actions plus the generic **Export Markdown** action. Create and
Open return a populated editor; each successful Save returns another editor
whose update action carries the note's new current revision, so the same note
can be edited repeatedly. A stale revision returns a recoverable conflict and
leaves the stored note unchanged.

Installed resources are served through `engine.v1.ui.*`. Native clients decode
both fetched and action-returned documents with the generic immutable decoder,
render them with `PanelRenderer`, and invoke only operation descriptors returned
by the engine. Panel and operation identifiers remain opaque outside the plugin.

## Wire behavior

The frozen inventory names lifecycle methods `plugin.v1.hello`,
`plugin.v1.activate`, `plugin.v1.invoke`, `plugin.v1.drain`, and
`plugin.v1.deactivate`; these are the worker's primary spellings. The current
process runtime sends `plugin.v1.lifecycle.hello`, `.activate`, and `.drain`, so
the worker accepts those three explicit compatibility aliases. Invocation uses
the inventory spelling `plugin.v1.invoke`.

The worker serializes operation execution and broker calls. Frames are bounded
to 1 MiB, decoded as strict UTF-8 JSON objects, and reject duplicate keys and
non-finite numbers. Errors use fixed messages and never echo note content,
storage keys, handles, or broker details.

## Packaging and tests

From the Architecture repository root:

```sh
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -m unittest discover \
  -s examples/session-notebook/tests -p 'test_*.py'
```

The suite packages and validates this directory, validates every local schema
and panel resource, launches `plugin.py` with Python isolated mode, and exercises
the export worker's progress-before-terminal and cancellation checkpoints. The
focused engine integration suites compose the real process runtime, authority,
storage/job wire adapters, public job use cases, and temporary SQLite state.

## Isolated installed-plugin walkthrough

Use an empty temporary directory and keep all engine, extension, socket and
native-build paths inside it. From the Architecture repository root:

```sh
DEMO_ROOT="$(mktemp -d /tmp/model-deck-notebook.XXXXXX)"
mkdir -p "$DEMO_ROOT/state" "$DEMO_ROOT/artifacts" "$DEMO_ROOT/socket" \
  "$DEMO_ROOT/legacy" "$DEMO_ROOT/extension-state" \
  "$DEMO_ROOT/extension-artifacts" "$DEMO_ROOT/swift-build"
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -B -m model_deck.cli.main \
  plugin pack "$PWD/examples/session-notebook" \
  --output "$DEMO_ROOT/session-notebook.zip"
PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -B -m model_deck.cli.main \
  engine serve \
  --state-root "$DEMO_ROOT/state" --artifact-root "$DEMO_ROOT/artifacts" \
  --socket-root "$DEMO_ROOT/socket" --legacy-agents-dir "$DEMO_ROOT/legacy" \
  --enable-application-state --enable-extensions \
  --extension-state-root "$DEMO_ROOT/extension-state" \
  --extension-artifact-root "$DEMO_ROOT/extension-artifacts"
```

Leave that command running. In another shell, use
`$DEMO_ROOT/state/engine/rendezvous.json` and
`$DEMO_ROOT/state/engine/operator_credential` with these public commands:

```sh
md() { PYTHONPATH=python/src /tmp/md-b18-venv/bin/python -B -m model_deck.cli.main "$@"; }
md plugin install "$DEMO_ROOT/session-notebook.zip" \
  --idempotency-key notebook-install --rendezvous "$DEMO_ROOT/state/engine/rendezvous.json" \
  --credential "$DEMO_ROOT/state/engine/operator_credential"
md plugin get --extension-id org.example.notebook \
  --rendezvous "$DEMO_ROOT/state/engine/rendezvous.json" \
  --credential "$DEMO_ROOT/state/engine/operator_credential"
md plugin enable --extension-id org.example.notebook --expected-revision 1 \
  --idempotency-key notebook-enable --rendezvous "$DEMO_ROOT/state/engine/rendezvous.json" \
  --credential "$DEMO_ROOT/state/engine/operator_credential"
md panels list --rendezvous "$DEMO_ROOT/state/engine/rendezvous.json" \
  --credential "$DEMO_ROOT/state/engine/operator_credential"
```

Create bounded JSON input files and call `md invoke <operation-id>
--input-file <absolute-path> --idempotency-key <unique-key>` for CRUD. Every
call first discovers the operation and then uses only
`engine.v1.operations.invoke`; direct plugin JSON-RPC methods are refused.

Build and open the isolated native client without replacing the installed app:

```sh
swift build --package-path macos --scratch-path "$DEMO_ROOT/swift-build" \
  --product ModelDeckPanelDemo
"$DEMO_ROOT/swift-build/debug/ModelDeckPanelDemo" \
  --rendezvous "$DEMO_ROOT/state/engine/rendezvous.json" \
  --credential "$DEMO_ROOT/state/engine/operator_credential" \
  --panel org.example.notebook.editor
```

Enter the id of a revision-1 note, change its title/body, and choose **Update
fresh note**. Select the list panel, choose **Export Markdown**, and use the
generic job view to observe progress, request cancellation, and inspect a
completed result. Stop only the isolated engine with Control-C, rerun the same
`engine serve` command, and invoke `notes.get` to confirm the edit survived.
Use `plugin get` to obtain the current revision before `plugin disable`; after
disable, invoking a Notebook operation returns `plugin_unavailable` while the
owned SQLite note data is retained for a later re-enable.

## Current integration boundary

The commands above remain the low-level CLI and panel-demo walkthrough. The
[Model Deck V2 application guide](../../macos/Sources/ModelDeckV2/README.md)
documents the normal isolated app flow: app-owned engine startup, generic
install and enable, dynamic list and repeated editing, cancellable export with
retrievable output, clean quit, reopen, and disable with retained data. Neither
path installs or replaces the shipping app. Optional session metadata and
package update/re-enable preservation remain future B22 work.
