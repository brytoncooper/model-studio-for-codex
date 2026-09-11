# OpenRouter Settings for Codex

An independent, experimental macOS utility; not an official OpenAI or OpenRouter product. This repository contains the v1.5 source checkpoint. Companion sidebar mode is planned, not implemented. Runtime compatibility is version-dependent; see [verification notes](VERIFICATION.md) for tested behavior and remaining limitations.

A native macOS control panel for registered OpenRouter models, native mixed-provider subagents, and an opt-in desktop model-picker bridge. Your normal OpenAI connection and global provider stay intact.

## Use

Open **OpenRouter Settings** in Applications. The window is named **Model Studio for Codex**, with Overview, Models, API keys, Usage, and Advanced pages. OpenAI models are already built in; no OpenRouter setup is needed for them.

In **Models**, search the unified library or filter by OpenAI/OpenRouter. OpenAI names come from the current native runtime; OpenRouter entries come from actual local registrations. Selecting a row previews its connection and usage instructions; it does not change an active task. Choose the actual model inside Codex. **Add OpenRouter model…** reveals the separate setup form: save/select an API key, choose a non-OpenAI model ID, and click **Enable this model in Codex**. Registration does not run inference or prove tool compatibility. Registering again updates that role's selected key reference.

The window resizes down to an 840 × 520 content area and remembers its size and position. Pages scroll when their content no longer fits.

Registration does not modify `~/.codex/config.toml`. OpenAI model IDs cannot be registered through OpenRouter. Whether a lead selects a subagent automatically depends on the task and its instructions.

## Mixed-model desktop picker

Finish active tasks and quit ChatGPT/Codex. In Overview click **Launch Codex**. Runtime discovery prefers the validated `/Applications/ChatGPT.app` installation and falls back to `/Applications/Codex.app`. The launcher, catalog, usage reader, and bridge use the same resolver. This supplies `CODEX_CLI_PATH` only to that launch and starts the unmodified, signed app with a local JSON-RPC bridge. It does not proxy HTTP, retrieve keys, copy subscription tokens, patch the host bundle, or install a system-wide environment override.

In a **new task**, choose a registered model marked **OpenRouter** to use API credits, or an OpenAI model to use the normal Codex connection. Picker defaults are saved separately in `picker-selection.json`; the global Codex model/provider configuration is not rewritten. Native OpenAI subscription roles let an OpenRouter lead delegate back to OpenAI. Each OpenRouter credential route has a deterministic native provider ID so resuming cannot silently select a different saved key.

Cross-provider switching in an existing task is deliberately rejected. Start a new task instead. Native unload/resume can reset permissions unless all task state is carried forward; this bridge does not perform that operation. Same-provider selections remain available. A changed key registration can require restoring the previous selection to resume an older task whose provider ID references that route.

Quit and open Codex normally to turn the picker bridge off. Existing OpenRouter tasks need the integrated launcher to resume because their provider definitions are supplied by the bridge. Registered subagent role files remain available independently. The bridge is tied to the installed app-server protocol and must be reverified after Codex updates; account-specific UI catalog filtering may require a separate compatibility check.

**Browse model catalog** retrieves public model IDs into the searchable model field. **Check key** verifies authentication without model inference. Catalog availability does not guarantee Responses or tool compatibility. Earlier saved shortcuts are preserved but no longer appear as a separate competing workflow.

## Usage and costs

Usage refreshes only when requested. OpenAI shows account-wide subscription quota windows, reset times, and token totals when available, using the native Codex app-server. Subscription limits are not converted to invented per-request dollar costs.

Usage windows are grouped by provider-reported pool. Several windows for one pool are simultaneous limits, not percentages to add together. General and named Spark pools have readable explanations; unknown pools such as `gpt-reserve` explicitly retain their unverified purpose rather than claiming bonus credits or a model mapping. Billing depends on the route of each parent or child independently, not just the lead model.

Recent token activity shows the latest available daily records (up to 14), with peak-day, lifetime, and streak metrics when reported. Missing records are not filled with zero and tokens are not translated to messages or dollar charges. Activity may cover multiple eligible account surfaces; it is not a per-model cost ledger.

OpenRouter shows provider-reported USD credit usage for the selected saved key: today, this week, this month, and all time, plus its remaining spending cap when configured. These totals include other apps using that key; they are not a Codex-only ledger or the balance of the entire account. Periods follow OpenRouter's UTC accounting. Missing data is unavailable, not zero.

Those time periods overlap; do not add them together. Key-cap progress and reset cadence are shown when known. Bring-your-own-provider-key usage, when reported, is separate from OpenRouter credit spending and is not silently added to it.

Refreshing makes read-only usage requests, never model inference. Keychain reads are noninteractive; if access is unavailable, authorize through **API keys → Check key**. Each provider reports failures independently. Sources: [Codex app-server](https://learn.chatgpt.com/docs/app-server) and [OpenRouter current-key usage](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key).

## Storage and recovery

- Keys live in macOS Keychain under `com.cooper.codex-openrouter.keys`, never in role files or preferences.
- A separate signed credential helper owns Keychain access. Its exact signed bytes are retained across UI-only rebuilds. Background reads cannot display authorization prompts; use **Check key** for explicit foreground authorization. Existing keys may need authorization for this new helper identity.
- Roles live in `~/.codex/agents/openrouter_*.toml`. Each holds its model, provider endpoint and a command that privately retrieves its key.
- Keep the app at `/Applications/OpenRouter Settings.app`. If moved, register roles again from the new location.
- Account names and model shortcuts live in `~/Library/Application Support/Codex OpenRouter/preferences.json`.
- Remove a generated role file and restart Codex to unregister it. Removing a shortcut does not unregister an agent.
- **Advanced → Restore previous default** only recovers global-provider changes made by version 1.0; it does not remove native roles.

Requests delegated to these roles use OpenRouter credits and send the supplied context to OpenRouter and the selected provider. Default reasoning effort is low. No subscription credentials are forwarded to OpenRouter.

## Build and verification

Run `zsh build.sh` here. Requires macOS, Swift (Xcode Command Line Tools), and Python 3.11 or newer. The build vendors `tomlkit==0.13.3` and records the Python executable path. This local app is ad-hoc signed, not notarized.

The JSON backend actions include `status`, read-only `list_models`, `register_agent`, and legacy `apply`/`restore`. Fixture overrides `config_path`, `state_dir`, and `agents_dir` support isolated tests. Run unittest discovery with the built app's `Contents/Resources/vendor` on `PYTHONPATH`.

The executable's `--self-test-keychain` tests and removes a disposable credential. Never manually invoke `--token` with a real account ID: its output is the API key intended for Codex.
