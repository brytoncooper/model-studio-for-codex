# Model Deck

**Use different AI models together, from one native Mac app.**

Model Deck connects Codex to OpenAI, Cursor, OpenRouter and compatible model
endpoints. Pick models for different tasks, manage connections, and see which
account pays for each request.

## Why Model Deck?

A coding workflow shouldn't be limited to one model. Use one for implementation,
another for review, and another for quick, inexpensive tasks—without juggling
separate apps and conversations.

Model Deck brings model discovery, routing and usage into one place. Codex keeps
control of tools, approvals and task history. Each model uses its own configured
connection; subscription allowances, API charges and estimated prices stay distinct.

## Using the app

1. Open Model Deck and add a connection.
2. Browse its models and register the ones you want to use.
3. Launch Codex from Model Deck to make those models available.

The app includes a model library, connection settings, a usage dashboard and an
optional companion panel. Compatibility depends on the host and provider versions;
see the [verification notes](VERIFICATION.md) and
[Cursor integration guide](CURSOR-INTEGRATION.md).

## Built to extend

We're moving Model Deck toward a small, provider-independent Python engine with a
native Swift interface. The guiding principle is **independent evolution**: each
system owns its behavior and data, and connects to others through explicit contracts.

The kernel registers capabilities. Providers handle inference. Host adapters connect
coding tools. The Mac app supplies the interface and platform services. New features
should fit behind those boundaries without rewriting the engine.

**This architecture is still being implemented.** The current app integrates with
Codex on macOS; other hosts and platforms are possible future adaptations, not
supported products today. The external plugin runtime has a working deterministic
provider example; the complete plugin authoring and installation workflow is unfinished.

## Explore the project

| Start here | What you'll find |
| --- | --- |
| [Subsystem catalog](docs/architecture/CATALOG.md) | System guides, contracts, invariants and extension points |
| [Architecture plan](docs/plans/plugin-architecture/PLAN.md) | Boundaries and independent evolution principles |
| [Delivery checklist](docs/plans/plugin-architecture/STATUS.md) | Implemented, verified and remaining work |
| [API contracts](contracts/README.md) | Shared schemas and protocol definitions |
| [External provider example](examples/deterministic-provider/README.md) | A standalone plugin and its integration tests |
| [Isolated app staging](scripts/package/README.md) | Build inputs, packaging and qualification requirements |

Independent project; not affiliated with OpenAI, Cursor or OpenRouter.
