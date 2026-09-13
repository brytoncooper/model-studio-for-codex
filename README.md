# Model Deck

**Use different coding models in the same Codex task.**

Model Deck is a native Mac app that connects Codex to models through Cursor,
OpenRouter and compatible endpoints. Your lead agent can delegate to another
model while Codex keeps the conversation, tools, approvals and review workflow.

## Why Model Deck?

Choose a model for the work in front of it: planning, implementation, investigation
or review. Use the accounts and endpoints you already have, without moving prompts
and results between separate coding apps.

Model Deck manages model registrations, connections and routing. Its usage view
helps explain which account pays for each route: OpenAI subscription access,
Cursor access and API credits remain separate.

## Using the Mac app

In **Models**, choose **Add models**, select a saved connection and pick models
from its catalog. Added models become available to Codex for delegation. Use
**Endpoints** to manage connections and **Usage** to inspect available usage data.

The current app integrates with Codex on macOS. See the
[compatibility notes](VERIFICATION.md) and [Cursor guide](CURSOR-INTEGRATION.md)
for supported behavior and setup details.

**This branch is an architecture refactor in progress.** Its new engine and plugin
components have focused tests; a complete replacement app has not yet passed
packaging and live qualification. The [delivery checklist](docs/plans/plugin-architecture/STATUS.md)
records what is integrated and what remains.

## Build on Model Deck

The architecture follows the **Independent Evolution Principle**: a subsystem
owns its implementation and data, and other systems use its public contracts.
Provider logic, host integration and the native interface sit outside the core
engine so they can change independently.

There are two extension paths:

- **Providers** supply model execution through a shared protocol. The
  [standalone provider example](examples/deterministic-provider/README.md) runs in
  a separate process without importing the engine.
- **Features** contribute operations and declarative panels, using brokered
  storage, jobs and events. The contracts and components exist; the complete
  install-to-UI workflow and authoring tools are still being connected.

Start with the [system catalog](docs/architecture/CATALOG.md). Each guide explains
what its system owns, its contracts and invariants, how to extend it, and its tests.

## Developer entry points

| I want to… | Start here |
| --- | --- |
| Understand the architecture | [Principles and plan](docs/plans/plugin-architecture/PLAN.md) |
| Work with the headless engine | [Python engine and CLI](python/README.md) |
| Implement a protocol client or plugin | [Shared contracts](contracts/README.md) |
| Assemble an isolated Mac app | [Packaging guide](scripts/package/README.md) |
| Find remaining work | [Delivery checklist](docs/plans/plugin-architecture/STATUS.md) |

The engine requires Python 3.11 or newer. Native app development also requires
macOS and the Swift toolchain. Follow the packaging guide for this branch; it
uses explicit staging paths and keeps an existing installation separate.

Independent project; not affiliated with OpenAI, Cursor or OpenRouter.
