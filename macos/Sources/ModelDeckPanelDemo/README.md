# Generic Panel Demo

`ModelDeckPanelDemo` is a separately buildable AppKit client for an already
running, isolated Model Deck engine. It discovers contributed panels, fetches
and validates the selected `ui.panel.v1` document, enables only operations
declared by `engine.v1.operations.list`, invokes renderer action intents, shows
the returned JSON without interpreting it, and refetches the panel.

Build and run from this worktree with isolated output:

```sh
swift build --package-path macos \
  --scratch-path /tmp/model-deck-panel-demo-build \
  --product ModelDeckPanelDemo

/tmp/model-deck-panel-demo-build/debug/ModelDeckPanelDemo \
  --rendezvous /absolute/path/to/isolated/rendezvous.json \
  --credential /absolute/path/to/isolated/operator_credential \
  [--panel reverse.domain.panel-id]
```

Both connection paths are mandatory and must be explicit. The demo never
searches live Model Deck paths, starts or restarts services, or reads the
installed app's state. Panel and operation identifiers remain opaque; there
are no extension-specific branches. It exercises only
`engine.v1.ui.contributions.list`, `engine.v1.ui.panel.get`,
`engine.v1.operations.list`, and `engine.v1.operations.invoke` after the normal
authenticated hello exchange.

Limitations: this executable is an isolated integration/demo surface, not part
of `ModelDeckApp`. It does not install, enable, disable, or otherwise manage
extensions. Engine lifecycle and fixture preparation remain external.
