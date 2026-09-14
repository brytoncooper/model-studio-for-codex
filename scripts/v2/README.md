# V2 builder

Build a fresh unsigned development app without invoking the legacy staging
packager or requiring provider helpers and credentials:

```sh
scripts/v2/build.sh \
  --python-executable /tmp/md-b18-venv/bin/python \
  --output /tmp/model-deck-v2-artifact
```

The output directory must not exist. The builder never installs or overwrites
an app. It bundles the current engine source, frozen Swift contract resources,
and the explicit Python executable path. The resulting app is named **Model
Deck V2** with bundle identifier `com.coopertechnology.modeldeck.v2.dev`. That
interpreter must remain available and contain the dependencies declared by
`python/pyproject.toml`. Local `__pycache__` directories and `.pyc` files are
excluded from the app composition.

Launch with a fresh acceptance state directory, optionally supplying the
non-secret coding-provider profile described in the V2 application guide:

```sh
open "/tmp/model-deck-v2-artifact/Model Deck V2.app" \
  --args --state-root /tmp/model-deck-v2-acceptance \
  --provider-config /tmp/model-deck-v2-provider.json
```

V2 owns only that state tree and its directly launched engine child. No launch
agent or background service is installed. See the
[V2 application guide](../../macos/Sources/ModelDeckV2/README.md) for the state
layout, Codex isolation, coding and cancellation commands, usage evidence, and
Notebook walkthrough.
