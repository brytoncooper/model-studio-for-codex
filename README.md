# Model Deck

Model Deck brings models from OpenAI, Cursor, OpenRouter and compatible endpoints
into the same Codex workflow. A lead agent can delegate work to different models
while you keep one project, one conversation and one place to review the results.

## Why use it?

Different models are useful for different jobs. You might want a capable model to
plan a change, a faster model to implement it, and another to review the result.
Model Deck makes those choices available inside the workflow you're already using.

The native Mac app lets you:

- **Choose your models:** browse catalogs and make selected models available to Codex.
- **Connect your accounts:** use subscription-backed models, paid APIs and local endpoints.
- **Understand usage:** see the billing route and available usage information, with estimates kept separate from actual charges.
- **Stay in your workspace:** use the full app or its optional companion panel.

Codex retains control of tool execution, approvals and task history. Model Deck
handles the connections and routing between models.

## Extend Model Deck

The architecture is being separated into a provider-independent Python engine,
explicit host and provider adapters, and a native Swift interface. Each subsystem
owns its implementation and exposes contracts that other systems can build on.

Start with the [subsystem catalog](docs/architecture/CATALOG.md). It links the
system guides, invariants, APIs and extension points. The
[standalone provider example](examples/deterministic-provider/README.md) shows a
plugin running outside the engine through the shared protocol.

## Documentation

- [Architecture and principles](docs/plans/plugin-architecture/PLAN.md)
- [API contracts](contracts/README.md)
- [Building and packaging](scripts/package/README.md)
- [Compatibility and verification](VERIFICATION.md)
- [Cursor integration](CURSOR-INTEGRATION.md)
- [Implementation status](docs/plans/plugin-architecture/STATUS.md)

The current app targets Codex on macOS. The plugin architecture and complete
plugin-authoring workflow are still in development.

Independent project; not affiliated with OpenAI, Cursor or OpenRouter.
