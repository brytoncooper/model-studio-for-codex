# Model Deck V2

`ModelDeckV2` is the isolated AppKit application for exercising installed
extensions and one bounded Codex coding route through the public engine API.
It reuses `ModelDeckClient` and the generic `PanelRenderer`; it does not
initialize or modify the live Model Deck or Codex configuration.

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

## Isolated Codex coding workflow

The coding proof uses the actual Codex CLI, not Codex Desktop. The helper gives
Codex separate `HOME`, `CODEX_HOME`, `XDG_CONFIG_HOME`, temporary storage,
configuration, history, and local bridge credentials below the supplied V2
state root. That configuration selects only V2's authenticated random-port
loopback Responses bridge. Codex owns its tool loop, approvals, project writes,
and conversation history; the engine owns sessions, runs, routing, provider
execution, cancellation, and usage records.

Create a non-secret provider profile from an existing authorized managed agent,
build, and launch with disposable paths:

```sh
scripts/v2/prepare_coding_provider.py \
  --managed-agent /absolute/path/to/managed-openrouter-agent.toml \
  --output /tmp/model-deck-v2-provider.json

scripts/v2/build.sh \
  --python-executable /tmp/md-b18-venv/bin/python \
  --output /tmp/model-deck-v2-coding-build

open "/tmp/model-deck-v2-coding-build/Model Deck V2.app" --args \
  --state-root /tmp/model-deck-v2-coding-state \
  --provider-config /tmp/model-deck-v2-provider.json
```

For a Cursor-managed agent, add `--cursor-sdk-python`,
`--cursor-workspace`, and `--cursor-state-root`. The header identifies the
`com.modeldeck.provider.cursor / cursor/<model>` route and Cursor IDE/Cloud
Agent-pool billing before any request. `Refresh usage` displays only committed
SDK input, output, and cached tokens; unavailable monetary cost is not shown as
zero.

After V2 reports the coding route, start a disposable project conversation and
continue it using the stored isolated thread identifier:

```sh
scripts/v2/run_isolated_codex.py start \
  --state-root /tmp/model-deck-v2-coding-state \
  --project /absolute/path/to/disposable-project \
  --codex-executable /opt/homebrew/bin/codex \
  --prompt 'Read the fixture, fix the failing function, and run its existing test.'

scripts/v2/run_isolated_codex.py resume \
  --state-root /tmp/model-deck-v2-coding-state \
  --project /absolute/path/to/disposable-project \
  --codex-executable /opt/homebrew/bin/codex \
  --prompt 'State the successful test command from the previous turn.'
```

Cancellation sends SIGINT to the isolated Codex process. The bridge detects the
closed local response stream and asks the engine to cancel the exact run:

```sh
scripts/v2/run_isolated_codex.py cancel \
  --state-root /tmp/model-deck-v2-coding-state \
  --project /absolute/path/to/disposable-project \
  --codex-executable /opt/homebrew/bin/codex \
  --cancel-after-seconds 0.5 \
  --prompt 'Do not use tools. Write a long explanation of integer addition.'
```

The original proof route is OpenRouter's HTTP-compatible Responses endpoint and
`deepseek/deepseek-v4.1-flash`. Cursor profiles instead use the isolated Cursor
SDK broker with Codex-owned tools. OpenRouter charges consume API credits, not a
ChatGPT subscription allowance. The profile stores only an executable credential
command and opaque reference; secrets are resolved at run start and are never
written into the profile.

## Isolated Codex compaction reproduction

The bridge accepts both Codex compaction entry paths: a streamed
`POST /v1/responses` request with `compaction_trigger`, and a unary
`POST /v1/responses/compact` request. Both use the same configured route and
model for a tool-less summary run. The bridge emits one `compaction` item only
after a terminal `run.completed` with a non-empty summary. A failed run,
including one that emitted partial text, or an empty summary is discarded and
does not replace the host history. A router-owned item is later replayed to the
host as summary text; provider-native encrypted continuation state is a
separate provider-scoped concern and is not host-visible replay.

Reproduce this behavior only with a fresh absolute scratch `--state-root`, an
isolated fixture project, and the loopback bridge descriptor, token, sockets,
and Codex configuration created below that root. The `run_isolated_codex.py`
harness provides the isolated `HOME`, `CODEX_HOME`, `XDG_CONFIG_HOME`, and
temporary directory; direct endpoint fixtures must use the same descriptor's
authenticated `127.0.0.1` URL and the V2-owned state. Do not point a fixture at
the installed app, live Model Deck or Codex state, a live socket, or a live
configuration, and do not treat this reproduction as a live cutover. The
deterministic integrated fixture qualifies the loopback bridge, engine,
OpenAI-compatible execution, private continuation store, tool callback, both
compaction endpoints, and post-compaction continuation. It does not qualify
Codex Desktop UI or a live provider.

On 2026-09-14 an isolated Cursor Composer 2.5 run used Codex shell tools, changed
subtraction to addition, and passed two `python3 -m unittest -v` checks. A
follow-up on the same Codex thread recalled the exact bug and test count. V2
displayed the committed totals: 151,956 input, 1,190 output, and 63,552 cached
input tokens. The SDK did not report settled monetary cost, so V2 displayed no
cost. A separate cancelled run recorded one local cancellation transition and
one `run.interrupted` terminal outcome, with no tool dispatch; no broker process
remained after cancellation or app quit.

## Session Notebook walkthrough

1. Package `examples/session-notebook` with `model_deck plugin pack`.
2. Launch V2 with a fresh state root and choose **Install**.
3. Select the package, choose **Enable**, and select the editor panel.
4. Enter a title and body, choose **Save note**, then edit and save repeatedly.
5. Select the list panel and choose **Refresh notes** to see actual note rows.
6. Open a row to load the current title, body, and revision into the editor.
7. Select the installed extension, choose **Update…**, and select a valid newer
   package. The status line reports the returned version, and the refreshed
   panel proves which packaged executable is serving.
8. Edit a retained note again, then choose **Export Markdown**. The generic job view shows the job identity,
   state, and progress. **Request cancel** records intent; keep observing until
   the worker reports the terminal `cancelled` state.
9. Start another export and let it complete. The selectable result JSON contains
   `media_type`, `suggested_filename`, and the exported Markdown `content`.
10. Select a valid package whose worker deliberately fails version validation.
    V2 reports the lifecycle error; the prior version, notes, editor, and export
    remain usable.
11. Quit and reopen V2 with the same state root to continue with the same data.
12. Choose **Disable** to remove the extension's panels and operations. Enabling
   it again restores access to the retained plugin-owned data.

Panel IDs, operation IDs, fields, and action parameters remain opaque to the
app. Returned panel documents are validated and update the current generic
renderer; stale operation conflicts appear in the status line without replacing
the editor with unvalidated state.

## Generic jobs.get / jobs.cancel

`EngineExtensionPanelService` exposes the typed `getJob(jobID:)` and
`cancelJob(jobID:idempotencyKey:)` methods that wrap the frozen
`engine.v1.jobs.get` and `engine.v1.jobs.cancel` RPCs. Both methods are
plugin-agnostic: they never parse the persisted output envelope or map
plugin-specific fields. The result is a `JobSnapshot` whose `output`
field is the raw `JSONValue` (or `nil` when storage has no record for
the job); an explicit JSON `null` is preserved as `.null`.

When `invokeOperation` returns a non-`nil` `result.jobID`, `V2WorkspaceController`
presents a `JobObservationViewController` child that:

- shows "Running" or "Queued" status with a progress bar,
- keeps the label "Cancel requested…" until a subsequent poll observes
  a terminal state (`completed`, `failed`, `cancelled`, `interrupted`),
- renders the canonical JSON envelope verbatim once the job is
  terminal — no plugin-specific string formatting.

`cancelJob` returns the server-side acknowledgement only; the controller
must not claim termination until `getJob` reports a terminal state. The
controller runs its own polling loop on a 0.4s timer; presentation tests apply
deterministic job snapshots without adding production delays.

## Limitations

The generic extension controls support installing and updating a selected
`.zip` package with the selected detail revision as the optimistic concurrency
guard. A successful update refreshes the workspace and reports the returned
version; failures remain visible as `Error: ...`.

This is an unsigned local development artifact. Its configured Python
interpreter must remain available and contain the dependencies from
`python/pyproject.toml`. The coding composition supports one configured
OpenAI-compatible route and serial tool calls only; parallel tool-call responses
are rejected rather than truncated. Codex Desktop UI integration,
live-provider opaque reasoning/compaction qualification, provider-reported cost, marketplace,
signing, distribution, bundled Python, and live-app cutover are not qualified.
Normal quit drains and reaps the engine and extension workers. The final
SIGKILL fallback is deliberately scoped to the known engine PID; a deliberately
nonresponsive extension that survives closed stdio could require a future exact
owned-descendant registry rather than broad process-name termination.
