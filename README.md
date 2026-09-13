# Model Deck

Use different coding models together, in one task.

Model Deck brings models from Cursor, OpenRouter and compatible endpoints into
Codex. Your agent can delegate implementation to one model and review to another,
without you copying prompts and answers between apps. A native Mac interface
manages the connections, available models and usage information.

## Why Model Deck?

Different models have different strengths, speeds and costs. Model Deck lets you
choose which ones do the work while keeping the task in your coding environment.
Connections retain their own billing: using an external provider does not turn
its requests into part of your ChatGPT allowance.

## Get started

In the current app, open **Models → Add models**, choose a connection, and select
models to make available to Codex. See the [Cursor setup guide](CURSOR-INTEGRATION.md)
for Cursor integration and the [verification notes](VERIFICATION.md) for tested
compatibility.

**This branch is the architecture refactor, not a finished replacement app.**
For development, start with the [Python setup and CLI guide](python/README.md).
The [Mac packaging guide](scripts/package/README.md) covers isolated staging;
end-to-end app qualification is still pending.

## Build something on it

The architecture separates three things: the engine coordinates work, providers
execute model requests, and host adapters connect coding environments. The Mac
interface is another client. Codex-specific behavior belongs in its adapter,
not in the engine.

We follow the **Independent Evolution Principle**: a system owns its behavior
and data, exposes explicit contracts, and can change without forcing unrelated
systems to change with it. New hosts and platforms should be possible by adding
adapters; this refactor does not implement those ports.

Feature plugins can contribute operations and panels and use engine APIs for
storage, jobs and events. These pieces are being integrated; a complete plugin
installation-to-UI workflow is not ready yet.

- **Write a plugin:** start with [plugin packaging and validation](python/src/model_deck/plugins/authoring/README.md).
- **Add a provider:** explore the [standalone provider example](examples/deterministic-provider/README.md).
- **Understand the API:** read the [shared contracts](contracts/README.md).
- **Find a system:** use the [architecture catalog](docs/architecture/CATALOG.md), which links subsystem guides, contracts, invariants and tests.
- **Track the refactor:** see the [delivery checklist](docs/plans/plugin-architecture/STATUS.md).

Independent project; not affiliated with OpenAI, Cursor or OpenRouter.
