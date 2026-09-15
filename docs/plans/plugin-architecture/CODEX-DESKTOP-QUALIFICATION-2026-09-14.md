# Codex Desktop qualification — 2026-09-15 update

## Result

D1a is qualified against the installed Codex Desktop app-server contract in a
fully isolated state root. V2 now packages a Desktop attachment entrypoint that
reuses the prototype's extracted `AppServerBridge`, obtains registrations from
the V2 engine, and routes only the selected external model through V2's
authenticated loopback Responses bridge. Host-owned OpenAI models retain their
native subscription route.

| Requirement | Observed evidence |
| --- | --- |
| Model discovery and selection | Installed Codex `0.154.0-alpha.6.2` returned `deepseek/deepseek-v4.1-flash` from `model/list`; `thread/start` returned `modelProvider: model_deck_v2`. |
| Coding task | Thread `01a0a3b3-541d-7413-a8bc-7ab1ce9f8f4b` edited `math_utils.py` in the disposable project and `python3 -m unittest -v` reported one passing test. |
| Same-thread continuation | A second `turn/start` on that same thread reran the test and completed with the exact count: one test, one passing. |
| Registration reload | In one bridge process, `model/list` changed the display name from `deepseek/deepseek-v4.1-flash` to `D1a Reloaded` immediately after public `engine.v1.models.rename`; no Desktop or bridge restart was required. The isolated registration was then restored. |
| Clean shutdown | Both acceptance harnesses reported bridge exit `0`. Exact process inspection found no staged bridge, native Codex child, harness, or isolated engine after shutdown. |

## Isolated setup

The source engine used fresh roots under:

```text
/private/tmp/md-v2-d1a-source-qual-20260915-3
```

The clean staged artifact was:

```text
/private/tmp/md-v2-d1a-build-20260915-6/Model Deck V2.app
```

The disposable coding project was:

```text
/private/tmp/md-v2-d1a-project-20260914-1
```

The installed ChatGPT/Codex Desktop build was `26.908.70816` (bundle build
`9275`) with embedded Codex `0.154.0-alpha.6.2`. Its actual app-server
`initialize`, `model/list`, `thread/start`, and `turn/start` methods supplied
the qualification evidence; this was not a projection-file-only test.

No installed application, live state, credential, provider setting, or live
configuration was changed. The running ChatGPT, prototype bridge, prototype
native Codex child, and Model Deck processes remained at their baseline PIDs
after isolated cleanup.

## Route ownership and billing

- Inference provider: `com.modeldeck.openrouter`.
- Provider model: `deepseek/deepseek-v4.1-flash`.
- Desktop provider route: `model_deck_v2`, defined only for the selected V2
  registration and pointed at the authenticated `127.0.0.1` V2 bridge.
- Tool ownership: Codex Desktop owned the agent loop, approvals, shell/file
  execution, thread history, and tool results. V2 owned registration lookup,
  route selection, provider execution, event translation, and usage evidence.
- Billing source: OpenRouter API usage consumes OpenRouter credits; it is not
  ChatGPT subscription usage. Native `gpt-*` entries remain on Codex's native
  `openai` provider and ChatGPT subscription route.

No token, credential value, or provider-private continuation data is recorded
here.

## Supported reload and lifecycle

`EngineRegisteredModelCatalog` reads `engine.v1.models.list` for every Desktop
`model/list` and selection check. Public registration changes are therefore
visible through Desktop's existing catalog reload request. The attachment does
not watch private storage or invent a second discovery mechanism.

The bridge entrypoint is packaged in the staged V2 app and execs the declared
validated Python runtime. The reused app-server bridge owns its exact native
Codex child and reaps it when stdin closes. The source engine was stopped by
its foreground interrupt and left no isolated descendants.

## Native setup composition — 2026-09-15

A separately staged unsigned V2 app at
`/private/tmp/model-deck-v2-gui-setup-20260915-6/Model Deck V2.app` used a fresh
state root to enter a disposable OpenRouter credential and model entirely in
the native UI. The key was saved through the existing Keychain helper; direct
inspection of the mode-0600 provider profile confirmed it held the helper
command and opaque references, not the key.

The owned engine restarted in process and displayed the OpenRouter route,
billing source, revision-1 connection, registered model, and ready host
projection. The registration was renamed to `GUI Setup Renamed` in the native
controls. After two clean Cmd-Q/reopen cycles, the route, connection, renamed
registration, and projection persisted. The protected running Codex process
was detected: Connect was disabled and the UI required a full user-controlled
Codex restart. No live process, setting, credential, or application was changed.

The unchanged D1a evidence above remains the routing proof for the same bridge,
provider execution, coding edit/test, continuation, and `model/list` reload.
This GUI run proves the new setup composition, persistence, and safe connection
gate; it does not add a visible picker-click claim.

## Remaining limitations

- Qualification is against the real Desktop app-server backend contract. A
  separate visible Electron picker click was not used because manipulating the
  running Desktop UI would cross the protected live-session boundary.
- The configured OpenAI-compatible execution path supports serial tool calls;
  provider responses containing concurrent outstanding calls fail closed.
- Live-provider opaque compaction, provider-reported monetary cost,
  signing/notarization, installation, and live cutover remain B27 concerns.
