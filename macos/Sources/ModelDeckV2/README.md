# Model Deck V2

`ModelDeckV2` is the isolated AppKit application for exercising installed
extensions through the public engine API. It reuses `ModelDeckClient` and the
generic `PanelRenderer`; it does not initialize the legacy settings, provider,
router, or Codex integration code.

## Build and launch

Build a fresh unsigned development app with the separate
[`scripts/v2/build.sh`](../../../scripts/v2/README.md) composition. The result
has the display name **Model Deck V2** and bundle identifier
`com.coopertechnology.modeldeck.v2.dev`.

```sh
scripts/v2/build.sh \
  --python-executable /tmp/md-b18-venv/bin/python \
  --output /tmp/model-deck-v2-artifact

open "/tmp/model-deck-v2-artifact/Model Deck V2.app" \
  --args --state-root /tmp/model-deck-v2-acceptance
```

The app starts one engine child automatically and waits for that engine's
rendezvous and credential files. Cmd-Q asks only that exact child to shut down,
waits for its extension processes, and then exits the app.

## Owned state

The default root is `~/Library/Application Support/Model Deck V2`. Acceptance
runs should pass a fresh absolute `--state-root`. Under that root, V2 uses
separate directories for:

- application state and artifacts;
- sockets and engine rendezvous files;
- extension lifecycle state, artifacts, and plugin-owned data;
- temporary files and engine logs.

The engine receives a small explicit environment and the bundled engine source
on `PYTHONPATH`. V2 installs no launch agent or background service and does not
read or modify live Model Deck or Codex state.

## Session Notebook walkthrough

1. Package `examples/session-notebook` with `model_deck plugin pack`.
2. Launch V2 with a fresh state root and choose **Install**.
3. Select the package, choose **Enable**, and select the editor panel.
4. Enter a title and body, choose **Save note**, then edit and save repeatedly.
5. Select the list panel and choose **Refresh notes** to see actual note rows.
6. Open a row to load the current title, body, and revision into the editor.
7. Quit and reopen V2 with the same state root to continue with the same data.
8. Choose **Disable** to remove the extension's panels and operations. Enabling
   it again restores access to the retained plugin-owned data.

Panel IDs, operation IDs, fields, and action parameters remain opaque to the
app. Returned panel documents are validated and update the current generic
renderer; stale operation conflicts appear in the status line without replacing
the editor with unvalidated state.

## Limitations

This is an unsigned local development artifact. Its configured Python
interpreter must remain available and contain the dependencies from
`python/pyproject.toml`. V2 intentionally has no providers, credentials,
marketplace, Codex integration, signing, distribution, or live-app cutover.
Normal quit drains and reaps the engine and extension workers. The final
SIGKILL fallback is deliberately scoped to the known engine PID; a deliberately
nonresponsive extension that survives closed stdio could require a future exact
owned-descendant registry rather than broad process-name termination.
