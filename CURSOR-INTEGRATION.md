# Cursor native agent integration

## Native execution path

1. Save a Cursor user API key in **Endpoints → Cursor SDK**, then install/check the SDK.
2. Browse that account's Cursor models and register a model. `cursor/composer-2.5` is an example; the account catalog determines availability.
3. Model Deck injects the registration into both the native `model/list` result and the model router's `/models` catalog, just as it does for OpenRouter.
4. Codex uses its normal OpenAI connection to the loopback router. The exact `cursor/` registration selects the Cursor SDK; the SDK receives only the corresponding Cursor key.
5. The SDK calls a supplied custom tool. Model Deck emits a native Responses function call with the original Codex namespace, then completes that HTTP response. Codex executes the tool under its normal task permissions and sends the output in the following request. Model Deck resumes the waiting callback in the same SDK run.

Use `spawn_agent(model="cursor/composer-2.5", ...)` with a registered account-available ID. Cursor agents receive the same supplied function tools as other routed models, including multi-agent and MCP namespaces. There is no fallback to Cursor's own shell or file tools.

OpenAI normally encrypts native V2 collaboration task fields. The router advertises those functions under its own `model_deck_agents` namespace with ordinary string message fields, then restores `collaboration` and `encrypted_function_args: []` on the returned calls. Codex's [plaintext receiving contract](https://github.com/openai/codex/pull/35845) then delivers a readable assignment to the child. The mapping covers streamed items, completed response outputs, tool definitions in `additional_tools`, and subsequent plaintext call history. OpenAI reasoning and opaque legacy encrypted calls remain unchanged. Encrypted agent assignments are rejected before external inference; resend them after launching through the updated bridge.

This generic-tool adaptation is Model Deck's integration, not an upstream `message_delivery` setting. The [upstream cross-provider issue](https://github.com/openai/codex/issues/37197) documents why changing the reserved collaboration schema alone does not work. Reverify this boundary after a Codex runtime update.

## SDK contract and lifecycle

The pinned package is `cursor-sdk==1.0.31`. It supplies its own bridge runtime. The Python environment is installed outside the app into the Model Deck support directory; installation stages and verifies a replacement before promotion. The app contains only the small adapter modules.

The SDK requires `tools=["mcp"]` to enable `local.custom_tools`. `tools=[]` disables them. Empty settings sources, external MCP configuration, and agent definitions prevent Cursor from loading extra execution tools. Each callback only exchanges a tool request/result with Codex.

The bundled Node bridge changes its inherited standard input to nonblocking mode. Before launching it, the broker moves Codex's callback command pipe to a separate non-inheritable descriptor and gives SDK children `/dev/null` as standard input. This keeps delayed tool results from being mistaken for a closed connection.

Each fresh logical generation creates a Cursor agent with Codex's current conversation. The agent/run is retained across its tool round trips. Subsequent user turns are reconstructed from Codex's conversation rather than replaying all prior history into an already-populated Cursor conversation. Codex remains the owner of persisted conversations; resuming an interrupted SDK callback after a router restart is not supported. Start a new turn in that case.

The adapter matches pending call IDs to the Cursor account, model, Codex thread, and agent. It rejects mismatched/expired continuations and concurrent requests for one pending run. Disconnects, Codex interruption, router shutdown, and expiry release the local SDK process. Waiting callbacks have a 15-minute SDK-side deadline; the manager expires an idle run shortly before that. Long unattended approvals may therefore require a new turn.

Cursor's harness instructions remain in effect. Its Python SDK accepts a user-message context envelope and images; it does not expose a raw replacement for the model's system messages. Picker, spawning, tool execution, and approval integration are native; the inference runtime remains Cursor's official agent runtime.

`auto-smart` requires account access and an explicit optimization parameter. The adapter chooses the account-supported Balance mode; if it is unavailable, it reports the restriction rather than silently substituting another model. OpenRouter Fast/Flex suffixes are never applied to Cursor models; Codex's Fast toggle becomes Cursor's `fast` parameter instead, sent explicitly as true or false, and a model without that parameter refuses Fast with a clear message. Codex context compaction runs as a tool-less summarization turn on the same Cursor model; the compaction item Codex keeps carries the summary and is replaced by it in later prompt envelopes.

## Billing

[Cursor's official SDK documentation](https://cursor.com/docs/sdk/python#usage-and-billing) states that SDK runs use the same pricing, request pools, and Privacy Mode as IDE and Cloud Agent runs, with spend tagged SDK in the Cursor dashboard. Use a **user API key** for the intended user account. This does not imply unlimited usage or suppress that account's overage settings.

The router ledger records `route=cursor`, SDK agent ID, token usage, and provider-reported charged USD when available. Token totals account for cache reads/writes. Tool-boundary token records are deltas; the final SDK agent charge is recorded once. A bounded billed-usage lookup may finish before Cursor's billing has settled, in which case cost remains null. The dashboard is authoritative.

## Verification boundaries

Automated acceptance exercises registration, native catalog injection, tool namespace restoration, streaming callbacks and continuations, cancellation, identity isolation, missing usage, SDK setup, and benchmark discovery. The SDK callback transport must also be checked with a live account before claiming account-qualified operation.

Live qualification requires:

1. Discover models using the saved Cursor user key and select one in Codex's native picker after launching Codex through Model Deck.
2. Ask it to run a harmless command such as printing a unique token in a disposable task directory. Confirm the command appears as a normal Codex tool event.
3. Spawn the same registered Cursor model from an Astra parent and confirm its reply arrives through the normal native child-agent flow.
4. Exercise a delayed tool response and interrupt a waiting run; confirm no SDK process or callback remains blocked.
5. Check the recorded SDK agent and SDK-tagged dashboard usage against the intended Cursor account. Do not infer billing from successful model registration.

Do not restart ChatGPT/Codex while existing tasks are active. Installing a new Model Deck build does not replace the router already loaded in a running host process.
