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
the runtime dependency contract, and the explicit Python executable path. The
resulting app is named **Model Deck V2** with bundle identifier
`com.coopertechnology.modeldeck.v2.dev`. Before compiling, the builder runs the
selected interpreter in isolated mode and requires Python 3.11 or newer, the
exact direct dependency versions declared by `python/pyproject.toml`, and
successful imports. It also discovers the JSON Schema formats used by the
packaged contracts and functionally probes their `jsonschema[format]`
implementations. A non-Python executable, missing package or selected extra,
wrong version, or broken native dependency therefore fails before an output
directory is made.

The selected interpreter remains an external runtime prerequisite; it is not
copied into the app. The bundle retains `python/pyproject.toml` and
`python/check_python_runtime.py` so the same prerequisite can be rechecked after
relocation. Local `__pycache__` directories and `.pyc` files are excluded from
the app composition.

## Relocation and runtime check

Build into one fresh directory, copy the completed output to another, and run
the retained checker with the configured interpreter:

```sh
scripts/v2/build.sh \
  --python-executable /tmp/md-b18-venv/bin/python \
  --output /private/tmp/model-deck-v2-stage

/usr/bin/ditto \
  /private/tmp/model-deck-v2-stage \
  /private/tmp/model-deck-v2-relocated

/tmp/md-b18-venv/bin/python -I -B \
  "/private/tmp/model-deck-v2-relocated/Model Deck V2.app/Contents/Resources/python/check_python_runtime.py" \
  "/private/tmp/model-deck-v2-relocated/Model Deck V2.app/Contents/Resources/python/pyproject.toml"
```

Launch from an unrelated directory with development Python and engine variables
removed:

```sh
cd /private/tmp
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  -u MODEL_DECK_ENGINE_RENDEZVOUS_PATH \
  -u MODEL_DECK_ENGINE_CREDENTIAL_PATH \
  open -n "/private/tmp/model-deck-v2-relocated/Model Deck V2.app" --args \
  --state-root /private/tmp/model-deck-v2-state
```

At runtime, `model_deck`, `model_deck_contracts`, and
`model_deck_root_guard` resolve from the relocated app's
`Contents/Resources/python/src`; third-party dependencies resolve from the
declared external interpreter. SwiftPM binaries can contain source/debug strings
and generated fallback paths to the build scratch directory. Those strings are
not runtime dependencies when both packaged resource bundles are present at the
app root; missing packaged bundles remain a packaging failure.

Launch with a fresh acceptance state directory, optionally supplying the
non-secret coding-provider profile described in the V2 application guide:

```sh
open "/tmp/model-deck-v2-artifact/Model Deck V2.app" \
  --args --state-root /tmp/model-deck-v2-acceptance \
  --provider-config /tmp/model-deck-v2-provider.json
```

Cursor profile preparation additionally requires the pinned SDK interpreter,
disposable project, and isolated SDK state paths:

```sh
scripts/v2/prepare_coding_provider.py \
  --managed-agent /absolute/path/to/cursor-agent.toml \
  --output /tmp/model-deck-v2-cursor.json \
  --cursor-sdk-python /absolute/path/to/cursor-sdk/venv/bin/python \
  --cursor-workspace /tmp/disposable-project \
  --cursor-state-root /tmp/model-deck-v2-cursor-state
```

V2 owns only that state tree and its directly launched engine child. No launch
agent or background service is installed. See the
[V2 application guide](../../macos/Sources/ModelDeckV2/README.md) for the state
layout, Codex isolation, coding and cancellation commands, usage evidence, and
Notebook walkthrough.
