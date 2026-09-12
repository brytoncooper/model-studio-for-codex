# Model Deck

An independent, experimental macOS utility; not an official OpenAI or OpenRouter product. Version 2.1 adds context compaction and Cursor Fast mode for routed models; version 2.0 added Cursor SDK agents, a redesigned responsive interface, and benchmark discovery to the loopback model router: every agent keeps Codex's normal OpenAI connection, and the router picks the billing route per request from the model name. Runtime compatibility is version-dependent; see [verification notes](VERIFICATION.md) for tested behavior and remaining limitations.

A native macOS control panel for registered OpenRouter models, native mixed-provider subagents, and the launch-time router that makes them work. Your normal OpenAI connection and global provider stay intact.

## Use

Architecture development is tracked in the [B00–B27 delivery checklist](docs/plans/plugin-architecture/STATUS.md).
The [subsystem catalog](docs/architecture/CATALOG.md) links implementation guides,
contracts and extension boundaries. The architecture replacement is unfinished;
the usage instructions below describe the existing application.

Open **Model Deck** in Applications. The window is named **Model Deck for Codex**, with Overview, Models, Endpoints, Usage, and Advanced pages. OpenAI models are already built in; no endpoint setup is needed for them.

In **Models**, search the unified library or filter by OpenAI or added models. OpenAI names come from the current native runtime; added entries come from actual local registrations. Selecting a row previews its connection and usage instructions; it does not change an active task. **Add models** opens a searchable checklist. Choose a saved connection and its catalog loads automatically. Check several models, keep searching without losing checked entries, and add the selection in one batch. Already-added models are skipped; failed additions remain selected so you can retry. A model registered on a different connection cannot be silently moved: remove that registration first. Registration does not run inference or prove native lifecycle compatibility.

**Connections** offers presets for Kimi Code, Z.AI, MiniMax, Google Gemini, and DeepSeek alongside OpenRouter, Cursor, and custom servers. Presets fill the documented endpoint and transport and explain which billing account is used. Save the provider key in the secure native field. Catalog discovery sends it only to the saved connection and does not follow redirects. When a known provider lacks a models endpoint, clearly labeled suggestions are available; authentication and network failures remain errors. Custom local servers can be saved without a key; plain `http` is only accepted for local addresses.

| Direct provider | Billing route | Native inference transport |
| --- | --- | --- |
| [Kimi Code](https://www.kimi.com/en/help/kimi-code/membership-guide) | Kimi Code membership key and allowance | Chat completions |
| [Z.AI](https://docs.z.ai/devpack/tool/codex) | GLM Coding Plan key and allowance | Dedicated Codex Responses endpoint |
| [MiniMax](https://platform.minimax.io/docs/token-plan/other-tools) | Subscription Key for Token Plan; ordinary keys use API billing | Chat completions |
| [Google Gemini](https://ai.google.dev/gemini-api/docs/openai) | Gemini API billing; eligible activated subscription developer credits may offset it | OpenAI-compatible chat completions |
| [DeepSeek](https://api-docs.deepseek.com/) | DeepSeek API balance | Chat completions |

These five connections make inference requests directly. Codex owns their tool execution, approvals, history, and delegation; they do not launch another provider's coding agent. Live qualification is separate from installing a preset or discovering its models. Google credits require account-specific activation and do not impose a hard spending cap.

The window resizes down to a 720 × 520 content area and remembers its size and position. Pages scroll when their content no longer fits.

Registration does not modify `~/.codex/config.toml`. OpenAI model IDs cannot be registered on any endpoint. Whether a lead selects a subagent automatically depends on the task and its instructions.

## Companion panel

Choose **Overview → Open companion** or **View → Show companion**. The attached sidebar contains the actual Models, API keys, Usage, Overview, and Advanced pages. It shares their controls with the full settings window rather than maintaining separate copies.

Window management is a separate opt-in and requires macOS Accessibility permission. Enable it explicitly in the sidebar, then grant access to Model Deck yourself in System Settings → Privacy & Security → Accessibility. This permission is broad; the implementation limits its use to host window geometry/focus/visibility and position/size changes. It does not read conversation contents, screenshot the host, or modify tasks.

The intended attached behavior reserves space by narrowing the host, reclaims space on horizontal collapse, and keeps the sidebar at the host's height. Moving/resizing the host updates the attachment. Minimum-size refusals must report failure; restoration must not overwrite later manual window changes. Real host behavior requires the [live qualification checklist](SIDEBAR-QUALIFICATION.md), independently of synthetic geometry tests.

**Launch Codex** opens the companion alongside the integrated host launch. If the host is already running, it opens the companion without restarting the host or changing its connection. The panel explicitly reports that the connection state is unknown in this case. Added models set a restart reminder; no background restart, task interruption, or model-picker hot reload is performed.

Companion mode is remembered after you enable it. Model Deck must be running; no login item or background launch agent is installed. Use the sidebar's Hide control to disable it. Clicking the Dock icon or choosing **View → Full settings** reopens standalone settings. Builds are signed with the certificate named in the `signing-identity` file beside the source (never auto-selected), which keeps the app's identity, and therefore the Accessibility grant, stable across rebuilds; without that file the build is ad-hoc signed and macOS asks again after each rebuild. The separate Keychain helper keeps its own identity either way.

## Mixed-model routing

Current Codex builds no longer let a custom agent role change its provider: a spawned agent always inherits the parent's connection. Model Deck therefore stops fighting that rule and uses it. When you launch Codex from Model Deck, every agent keeps the built-in OpenAI connection, but that connection points at a router on your own Mac. The router reads the model name in each request and picks the route:

- **OpenAI model** (`gpt-*`): forwarded to chatgpt.com with your ChatGPT sign-in. Your subscription pays.
- **Registered Cursor model** (`cursor/*`): runs through the official Cursor SDK with the saved Cursor user API key. Your Cursor account's usage pool pays.
- **Registered OpenRouter model** (a name with a slash, such as `deepseek/deepseek-v4.1-flash`): the ChatGPT credentials are dropped, your OpenRouter key is attached from the Keychain helper, the request is translated to OpenRouter's stateless Responses API, and the stream is translated back. Your OpenRouter credits pay.
- **Model added on another endpoint**: the same treatment, but the request goes to that endpoint's base URL with its own key, or with no key for a local server. Servers that only speak chat completions (LM Studio, Ollama, DeepSeek's API) get the request converted to that format and the stream converted back, tool calls included. Nothing from your ChatGPT sign-in is sent to any endpoint.
- **Anything else that is not built in**: a clear error naming the model. The router never guesses a billing route.

Because the choice happens per request, it applies to every agent at every depth. A GPT lead can spawn a DeepSeek worker by passing the model name to `spawn_agent`; that worker can spawn a GPT reviewer the same way. Registered models are also added to the native model catalog, so they appear in Codex's picker and are accepted as spawn models without a role file. The registered `openrouter_*` roles still work as named presets.

For cross-provider delegation, Model Deck exposes collaboration functions to OpenAI under a generic tool namespace and restores Codex's native namespace with its explicit plaintext-message marker on the returned calls. This makes assignments readable to the selected provider. OpenAI reasoning remains encrypted. Already-encrypted agent assignments are rejected before external inference and must be resent through the updated bridge. This adaptation depends on the tested Codex protocol; it is not an upstream plaintext-delivery configuration setting.

Launching Codex from Model Deck also registers an MCP server named `model_deck` for that Codex process only (a `-c mcp_servers.model_deck=…` override, not a `config.toml` edit). It runs `Contents/Resources/model_deck_mcp.py` with the same Python as the bridge and exposes `list_endpoints`, `search_models`, `list_added_models`, `add_model`, `remove_model`, `set_display_name`, and `model_pricing`, so a task can be told to find a model on OpenRouter and add it, and the model appears in the picker and is spawnable on the next turn. It can only add models to endpoints already saved in Model Deck, never sees or writes API keys, and refuses `gpt-*` ids; `remove_model` prompts for approval in Codex, the other tools are auto-approved. Models running on other endpoints get the function tools that Codex supplies, including multi-agent, MCP, and plugin namespaces. Model Deck restores each namespace when returning a tool call; Codex owns execution and approval.

In the picker, registered models get short automatic names (`deepseek/deepseek-v4.1-flash` shows as **DeepSeek V4.1 Flash**). To use your own name, select the model in **Models** and save **Shown in Codex as**; names live in `~/Library/Application Support/Model Deck/display-names.json` and apply at the next launch from Model Deck. The model id is unchanged, so spawning by name still uses the id.

Model Deck caches OpenRouter's public model catalog (list prices per million tokens for input, output, and cached input; context length; input modalities; tool support) in `~/Library/Application Support/Model Deck/pricing-cache.json`, refreshed from `https://openrouter.ai/api/v1/models` (public, no key sent) when older than six hours. Prices show up in the picker description of every added OpenRouter model (for example "Routed to OpenRouter by Model Deck. Uses OpenRouter credits. in $0.15/M · out $0.6/M · cached $0.003/M · 1.04858M context."), on the Models page detail card, and in the developer instructions Model Deck appends to every task, which now list added models with id, picker name, endpoint, and list price and suggest preferring the cheapest model that fits and using low cached-input prices for long contexts. The picker's context window and image-input support also come from this data, replacing the earlier fixed 256k text-only default. Prices are list prices from OpenRouter, not measured spend, and models on other endpoints show none.

Codex's **Fast** and **Flex** speed choices work for registered models too: Fast sends the request as the model's `:nitro` variant (OpenRouter's fastest provider, which can cost more) and Flex as `:floor` (cheapest provider, which can be slower). Both are OpenRouter's documented routing shortcuts and apply only to models on an OpenRouter endpoint; the ledger records which variant ran.

Finish active tasks and quit ChatGPT/Codex. In Overview click **Launch Codex**. Runtime discovery prefers the validated `/Applications/ChatGPT.app` installation and falls back to `/Applications/Codex.app`. The launcher supplies `CODEX_CLI_PATH` only to that launch. The bridge starts the router on a random loopback port, adds `openai_base_url` and `enable_request_compression=false` overrides to that Codex process only, and stops the router when Codex exits. It does not patch the host bundle, edit `~/.codex/config.toml`, install a system-wide override, or log credentials. Codex's WebSocket transport is refused with HTTP 426 so it falls back to ordinary streaming.

Picker defaults are saved separately in `picker-selection.json`; the global Codex model/provider configuration is not rewritten. Tasks started through the router can switch between OpenAI and OpenRouter models mid-task. Tasks created by earlier versions keep their saved OpenRouter provider route and still need the integrated launcher to resume.

The router writes a per-request ledger to `~/Library/Application Support/Model Deck/router-ledger.jsonl` (route, model, thread, agent path, tokens, and OpenRouter's reported cost) and a short log to `router.log` beside it. Neither file contains keys or message content.

Limits in this version: external agents receive the function tools supplied by Codex. Unsupported custom/freeform tools fail explicitly; provider-native web search is not translated. Context compaction works for routed models: when Codex asks its backend to compact a long thread, the router has the same model write Codex's own handoff summary (tools withheld) and returns a compaction item that carries that summary; later turns on any routed model see the summary in its place, a switch to an OpenAI model sends it as a plain message, and a compaction made by OpenAI is announced as unreadable rather than silently dropped. Both of Codex's compaction paths are served: the `compaction_trigger` item on a normal turn and the older unary `/responses/compact` call. Direct chat reasoning and tool signatures are saved privately under IDs that Codex carries in its history, then restored only for the same endpoint, credential account, and model. Missing required continuation fails clearly. Foreign continuation is removed before a request reaches OpenAI or another provider. Stream completion requires an upstream terminal outcome; a truncated stream cannot complete a pending tool call. These translation safeguards are separate from live provider qualification; compaction was verified against local stand-ins, not every live provider. Quit and open Codex normally to turn the router off. The router is tied to the installed app-server protocol and must be reverified after Codex updates.

Choosing a saved connection loads its model catalog automatically. Search matches model names and IDs; Space toggles the focused row, and **Select shown** affects the current results while preserving other checked models. **Test key** verifies authentication without model inference. Catalog availability does not guarantee Responses or tool compatibility. Earlier saved shortcuts are preserved but no longer appear as a separate competing workflow.

## Cursor agents and benchmark tools

In **Endpoints**, choose **Cursor SDK**, install the SDK, and save a Cursor **user API key** from the Cursor dashboard. Test the connection to discover the models available to that account. In **Models**, browse the selected Cursor endpoint and add a discovered model. Registered IDs such as `cursor/composer-2.5` use the same native catalog and picker injection as OpenRouter registrations and can be passed directly to `spawn_agent`. Account availability is discovered rather than assumed.

The official `cursor-sdk==1.0.31` package and its bundled Node bridge install into `~/Library/Application Support/Model Deck/cursor-sdk`, separately from the signed app. Installation is explicit, staged, and checked before replacing an earlier runtime. Keys remain in macOS Keychain and reach the SDK over a private subprocess input pipe, never command arguments or router logs.

Cursor models get Codex's **Fast** toggle: it maps to Cursor's per-model `fast` parameter, so Fast on sends `fast=true` and Fast off sends `fast=false` explicitly (Cursor's own default variant is not assumed). The router caches Cursor's model catalog in `cursor-models.json` (ids and parameters only, never the key) at launch and whenever the model list is refreshed, and offers Fast only for models whose catalog has the parameter; before the first cache every Cursor model offers Fast and the SDK broker refuses it explicitly for one that lacks it. Cursor's reasoning parameter is matched by any of its names (`effort`, `reasoning`, `reasoning_effort`). Cursor runs request Codex's tools through SDK callbacks. The router ends a Responses stream with the native tool call, retains the Cursor run, then delivers Codex's tool result on the next request. Codex retains tool execution, approvals, and task UI. Cursor's built-in file/shell tools, project settings, external MCP servers, and subagents are disabled. SDK `tools=["mcp"]` is required to enable custom callbacks; `tools=[]` disables them too. This is a native routed Cursor **agent**, with Cursor's own harness instructions. Python's SDK does not expose raw system-message replacement, so the Codex conversation is supplied as an explicit context envelope. See [Cursor integration notes](CURSOR-INTEGRATION.md) for lifecycle and live verification.

Cursor's [official billing contract](https://cursor.com/docs/sdk/python#usage-and-billing) uses the same pricing, request pools, and Privacy Mode as the IDE and Cloud Agents, with an SDK tag in its usage dashboard. Your Cursor account's plan limits and overage settings still apply. Model Deck never routes a `cursor/` ID to OpenRouter or sends it ChatGPT credentials. The usage ledger separates the Cursor route and reports charged SDK costs only when available; missing cost is unknown.

The Model Deck MCP server adds `cursor_status`, `benchmark_status`, `refresh_benchmarks`, `model_benchmarks`, `compare_models`, and `rank_models`. Benchmark tools read OpenRouter's full public catalog, including its Artificial Analysis and Design Arena evidence. They support task-specific rankings, comparisons, exact model identities, pagination, metric direction, source timestamps when published, cache age, and unscored models. An optional authenticated refresh enriches public results using an already-saved OpenRouter key and the [official benchmark endpoint](https://openrouter.ai/docs/api/api-reference/benchmarks/list-benchmarks). It makes no inference requests. Scores from different tests or evaluation snapshots remain separate; unsupported and missing evidence is disclosed.

The native `spawn_agent` tool description also carries a compact roster of up to 40 registered choices: exact model and role IDs, saved billing route, available input/output/cache-read/cache-write USD prices per million tokens, context size, tool and vision support, and published benchmark scores with source and cache freshness. This reaches OpenAI, OpenRouter, Cursor, and other routed agents before they choose a child. Cursor and native GPT choices identify their subscription pools; unknown API rates and missing benchmark matches stay explicit. Prices are catalog list prices, not settled task charges. Benchmark identities are not inferred across providers, and scores are compared within the same test and snapshot. Use the MCP tools for the complete evidence or larger catalogs.

Spawn metadata reads cached files only. Public benchmark refresh happens in the background at router startup when needed; `refresh_benchmarks` updates are picked up on subsequent requests. Existing native descriptions, schemas and approval policies remain intact, and unrelated MCP tools with a `spawn_agent` name are preserved. Quit ChatGPT when your current work is finished and relaunch through Model Deck to load an updated router.

## Usage and costs

Usage refreshes automatically every 10 seconds while the Usage page is open, and on demand with **Refresh usage**. Automatic refreshes never blank the page or disable controls, and a failed automatic refresh keeps the last successful data on screen. OpenAI shows account-wide subscription quota windows, reset times, and token totals when available, using the native Codex app-server. Subscription limits are not converted to invented per-request dollar costs.

Usage windows are grouped by provider-reported pool. Several windows for one pool are simultaneous limits, not percentages to add together. General and named Spark pools have readable explanations; unknown pools such as `gpt-reserve` explicitly retain their unverified purpose rather than claiming bonus credits or a model mapping. Billing depends on the route of each parent or child independently, not just the lead model.

Recent token activity shows the latest available daily records (up to 14), with peak-day, lifetime, and streak metrics when reported. Missing records are not filled with zero and tokens are not translated to messages or dollar charges. Activity may cover multiple eligible account surfaces; it is not a per-model cost ledger.

OpenRouter shows provider-reported USD credit usage for the selected saved key: today, this week, this month, and all time, plus its remaining spending cap when configured. These totals include other apps using that key; they are not a Codex-only ledger or the balance of the entire account. Periods follow OpenRouter's UTC accounting. Missing data is unavailable, not zero.

Those time periods overlap; do not add them together. Key-cap progress and reset cadence are shown when known. Bring-your-own-provider-key usage, when reported, is separate from OpenRouter credit spending and is not silently added to it.

Refreshing makes read-only usage requests, never model inference. Keychain reads are noninteractive; if access is unavailable, authorize through **API keys → Test key**. Each provider reports failures independently. Sources: [Codex app-server](https://learn.chatgpt.com/docs/app-server) and [OpenRouter current-key usage](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key).

## Storage and recovery

Model Deck was previously called OpenRouter Settings / Model Studio. The app is now identified as `com.cooper.model-deck` with executable `ModelDeck`, and its state folder is `~/Library/Application Support/Model Deck` (the earlier "Codex OpenRouter" folder is renamed automatically on first use; saved settings under the old bundle identifier carry over once). Two things deliberately keep their original identifiers: the Keychain service (`com.cooper.codex-openrouter.keys`) and the credential helper, because changing either would force every saved key to be re-authorized. Two compatibility links keep older model registrations working, since their key lookups call the old paths: `/Applications/OpenRouter Settings.app` → `Model Deck.app`, and `Contents/MacOS/OpenRouterSettings` → `ModelDeck` inside the bundle. The build output is `../Model Deck.app`.

- Keys live in macOS Keychain under `com.cooper.codex-openrouter.keys`, never in role files or preferences.
- A separate signed credential helper owns Keychain access. Its exact signed bytes are retained across UI-only rebuilds. Background reads cannot display authorization prompts; use **Test key** for explicit foreground authorization. Existing keys may need authorization for this new helper identity.
- Roles live in `~/.codex/agents/openrouter_*.toml`. Each holds its model, its endpoint's base URL and, for keyed endpoints, a command that privately retrieves its key.
- Endpoint settings (name, base URL, wire format; never keys) live in `~/Library/Application Support/Model Deck/endpoints.json`, written by the app and read by the router.
- Direct-provider reasoning, signatures, and continuation data live in the private `provider-continuation.sqlite` file in the same support folder. This is sensitive conversation data, separate from the credential-free usage ledger. Keep it when resuming existing provider tasks; deleting required records may require starting a new task.
- Keep the app at `/Applications/Model Deck.app`. If moved, register roles again from the new location.
- Account names and model shortcuts live in `~/Library/Application Support/Model Deck/preferences.json`.
- `~/Library/Application Support/Model Deck/pricing-cache.json` holds OpenRouter's public list prices. It is safe to delete; Model Deck refetches it.
- The Models page detail card has a **Remove from Codex** button for an added model: after a confirmation dialog it deletes the managed role file and its custom name, leaves the endpoint and key in place, and drops the model from the picker on the next turn. Removing a generated role file by hand and restarting Codex also works. Removing a shortcut does not unregister an agent.
- **Advanced → Restore previous default** only recovers global-provider changes made by version 1.0; it does not remove native roles.

Requests routed to an endpoint use that endpoint's key or credits and send the supplied context to that endpoint (and, for OpenRouter, to the provider it picks). Default reasoning effort is low. ChatGPT credentials are kept on the OpenAI route.

## Build and verification

Run `zsh build.sh` here. Requires macOS, Swift (Xcode Command Line Tools), and Python 3.11 or newer. The build vendors `tomlkit==0.13.3` and records the Python executable path. The app uses the configured local signing identity, falling back to an ad-hoc signature when none is configured; it is not notarized.

The JSON backend actions include `status`, read-only `list_models`, `register_agent`, and legacy `apply`/`restore`. Fixture overrides `config_path`, `state_dir`, and `agents_dir` support isolated tests. Run unittest discovery with the built app's `Contents/Resources/vendor` on `PYTHONPATH`.

The executable's `--self-test-keychain` tests and removes a disposable credential. Never manually invoke `--token` with a real account ID: its output is the API key intended for Codex.
