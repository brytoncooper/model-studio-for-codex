# Model Deck

**Bring more models into your coding workflow.**

Model Deck lets your coding agent delegate work to models from different providers
without moving conversations between apps. Use one model to plan a change,
another to implement it, and another to review it—all within the same task.

The current native Mac app connects Codex to Cursor, OpenRouter and compatible
model endpoints. It manages connections, model selection and routing, with usage
information to help you understand where requests are billed.

## Use it

In the app, open **Models → Add models**, choose a connection and select the
models you want to make available to Codex. Manage connections in **Endpoints**
and inspect usage in **Usage**.

See the [Cursor setup guide](CURSOR-INTEGRATION.md) and
[compatibility notes](VERIFICATION.md) for integration details.

## Build on it

Model Deck is being reorganized around a small, provider-independent engine.
The aim is simple: adding a provider, changing the host integration or building
an optional feature should not require rewriting the rest of the app.

We follow the **Independent Evolution Principle**: each system owns its behavior
and data, and communicates through explicit contracts. The native interface,
host integrations and model providers belong outside the core.

Providers supply model execution. Feature plugins contribute operations and
panels, with access to storage, jobs and events through engine APIs.
The [system catalog](docs/architecture/CATALOG.md) explains these boundaries and
links each system's contracts, invariants, extension guide and tests.

| Start here | What you'll find |
| --- | --- |
| [Engine and CLI](python/README.md) | Local development and engine composition |
| [Provider example](examples/deterministic-provider/README.md) | A standalone provider running outside the engine |
| [Shared contracts](contracts/README.md) | The protocols clients and plugins use |
| [System catalog](docs/architecture/CATALOG.md) | How the pieces fit together and how to extend them |
| [Mac packaging](scripts/package/README.md) | Assemble an isolated app from this branch |

## Project status

This branch contains an **unfinished architecture refactor**. Individual engine
and plugin components have tests; the complete plugin installation workflow and
replacement Mac app still need integration and qualification. Follow the
[delivery checklist](docs/plans/plugin-architecture/STATUS.md) for completed work
and remaining steps.

Development requires Python 3.11 or newer; the native app also requires macOS
and the Swift toolchain.

Independent project; not affiliated with OpenAI, Cursor or OpenRouter.
