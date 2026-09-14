# Codex Desktop qualification — 2026-09-14

## Result

Model Deck V2 cannot yet replace the prototype for Codex Desktop model access.
V2 registered and projected the configured OpenRouter model successfully, but
Codex Desktop's native app server did not include that model in `model/list`.
No external-provider coding turn was attempted after that decisive failure.

| Question | Observed answer |
| --- | --- |
| Can V2 expose the configured model in Codex Desktop's model picker? | **No.** The native catalog contained only OpenAI models; `deepseek/deepseek-v4.1-flash` was absent. |
| Can Codex Desktop execute a task through V2? | **Not qualified.** The intended model was unavailable, so running a task would not test the requested route. |
| Can a conversation continue after the initial task? | **Not qualified.** There was no V2-backed initial Desktop turn to continue. |
| Does Desktop reload a model registered after launch? | **Not qualified.** Native discovery already failed for the model projected before launch. |
| Can the prototype be replaced today? | **No.** The running prototype still supplies the app-server catalog/routing bridge that V2 does not attach to Desktop. |

## Isolated reproduction

The test used source commit `6f139327bf900df90eb8fa4295ea32aa7f01d956`
from a clean detached worktree. A fresh unsigned artifact was built at:

```text
/private/tmp/model-deck-v2-desktop-build.pUSW3q/artifact/Model Deck V2.app
```

V2 ran with a fresh state root at:

```text
/private/tmp/model-deck-v2-desktop-qual-current.jDLT9S/state
```

The V2 UI reported the OpenRouter connection and
`deepseek/deepseek-v4.1-flash` as registered, with host projection `ready`.
The projected agent file was written below the isolated Codex home:

```text
/private/tmp/model-deck-v2-desktop-qual-current.jDLT9S/state/codex-harness/codex-home/agents/
```

Codex Desktop 26.903.71938 (embedded Codex 0.153.4) was launched as a second
instance with separate `HOME`, `XDG_CONFIG_HOME`, Electron user-data, and
`CODEX_HOME` paths. The installed app's supported isolation inputs were used:

```sh
CODEX_HOME=/private/tmp/model-deck-v2-desktop-qual-current.jDLT9S/state/codex-harness/codex-home \
CODEX_ELECTRON_USER_DATA_PATH=/private/tmp/model-deck-v2-desktop-qual-current.jDLT9S/desktop/user-data \
CODEX_CLI_PATH=/Applications/ChatGPT.app/Contents/Resources/codex \
/usr/bin/open -n /Applications/ChatGPT.app --args \
  --user-data-dir=/private/tmp/model-deck-v2-desktop-qual-current.jDLT9S/desktop/user-data
```

`CODEX_CLI_PATH` must be explicit for this qualification. The first isolated
launch inherited the development session's prototype bridge path and was
rejected as evidence. With the installed native executable forced explicitly,
the app server initialized with the isolated V2 `CODEX_HOME`; `model/list`
returned `gpt-5.6-sol`, `gpt-6-astra`, `gpt-reserve`, `gpt-5.6-terra`,
`gpt-5.6-luna`, `gpt-5.5`, `gpt-5.3-codex-spark`, and
`codex-auto-review`. It did not return the projected DeepSeek model.

The installed app-server schemas generated during the run remain at:

```text
/private/tmp/codex-app-server-schema.5jPtNd
```

The test did not change the installed app, live Model Deck state, live Codex
state, credentials, or provider settings. Only exact isolated test processes
were stopped. No external-provider inference or tool execution occurred, so
there is no new provider charge to attribute to this qualification.

## Boundary diagnosis

The managed agent TOML produced by projection is not a primary Desktop model
catalog registration contract. Native Codex Desktop owns `model/list` and does
not derive that list from projected agent files. The prototype works because
its provider bridge sits in the app-server path and injects/routes catalog
entries; the V2 application currently starts its engine without composing the
extracted `CodexHostAdapter`/`AppServerBridge` into a Desktop attachment.

This is larger than a safe one-line qualification repair. The smallest viable
implementation is a V2-owned, isolated Desktop launch/attachment path that:

1. packages and starts the extracted app-server bridge;
2. supplies V2's public catalog and provider-routing ports to that bridge;
3. injects V2 models into `model/list` while preserving host-bound OpenAI
   subscription models;
4. routes selected V2 turns through the authenticated V2 loopback bridge; and
5. proves initial task, continuation, registration reload, billing identity,
   and exact owned-process cleanup against the real Desktop app.

If Codex Desktop exposes an official custom-provider/catalog API, that contract
should replace the launch bridge. Until either path exists, CLI qualification
must remain explicitly separate and the prototype remains required for Desktop
model access.
