"""Native desktop JSON-RPC bridge entrypoint and loopback model router.

The mapping itself lives in
``python/src/model_deck/integrations/hosts/codex/app_server.py``. This
module is a thin legacy wrapper that re-exports the package's public
surface, supplies root-owned defaults (model-roster composition via
``spawn_benchmarks.model_choice_lines`` and the root ``discover_runtime``
process launcher), and preserves the original ``main()`` entrypoint.

Never handles API keys itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make the package importable when this module is loaded from the root
# checkout (e.g. ``python3 -m unittest test_provider_bridge``). The path
# is resolved against this file so it works regardless of CWD.
_PACKAGE_SRC = Path(__file__).resolve().parent / "python" / "src"
if str(_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_SRC))

import pricing  # noqa: E402  root-owned dependency
from local_router import LocalRouter  # noqa: E402  root-owned dependency
from routing_registry import RoutingRegistry  # noqa: E402  root-owned dependency
from codex_runtime import discover_runtime  # noqa: E402  root-owned dependency
from spawn_benchmarks import model_choice_lines  # noqa: E402  root-owned dependency

# Re-export the structural/transport pieces from the package. The package
# never imports the root composition graph (LocalRouter, RoutingRegistry,
# pricing, provider adapters) so this import direction is one-way only.
from model_deck.integrations.hosts.codex.app_server import (  # noqa: E402
    MODEL_FIELDS,
    ROUTING_INSTRUCTIONS,
    MCP_INSTRUCTIONS,
    toml_inline,
    mcp_server_arguments,
    AppServerBridge,
    BridgeError,
    BackendError,
)


def routing_instructions(registry, price_table=None, benchmark_lines=None):
    """The routing rules plus the same model evidence supplied on spawn_agent.

    The roster of added models with cached prices and benchmark evidence is
    composed here at the root because ``spawn_benchmarks.model_choice_lines``
    is a root-owned dependency that the package deliberately does not import.
    """
    parts = [ROUTING_INSTRUCTIONS]
    try:
        models = registry.load_models()
    except Exception:
        models = {}
    roster = ['- ' + line for line in model_choice_lines(models, price_table, benchmark_lines).values()]
    if roster:
        parts.append('Models added in Model Deck, spawnable by exact id (USD list prices per million tokens; '
                     'provider and routing tier can change cost; not settled task charges):\n' + '\n'.join(roster))
    parts.append(MCP_INSTRUCTIONS)
    return '\n\n'.join(parts)


def _root_process_launcher():
    """Default process launcher that defers to the root ``codex_runtime``.

    The root ``codex_runtime.discover_runtime`` returns a plain dict shaped
    like ``{"application_path": ..., "executable_path": ...,
    "bundle_identifier": ...}``. We normalize it to the same shape the
    package's ``ProcessLauncher`` Protocol documents so consumers can rely
    on every key being present (defaulting to empty strings when the root
    discovery omits one, e.g. in legacy test fixtures).
    """
    descriptor = discover_runtime()
    return {
        'application_path': str(descriptor.get('application_path', '')),
        'executable_path': str(descriptor['executable_path']),
        'bundle_identifier': str(descriptor.get('bundle_identifier', '')),
    }


class ProviderBridge(AppServerBridge):
    """Legacy root entrypoint that wires the package bridge to root defaults.

    The class exists only to inject the root-owned ``instruction_builder``
    (which composes the model roster) and ``process_launcher`` (which uses
    the root ``codex_runtime.discover_runtime``) so existing callers that
    construct ``ProviderBridge(registry, emit, router)`` keep working.
    All protocol behavior comes from :class:`AppServerBridge`.
    """

    def __init__(self, registry, emit, router=None):
        super().__init__(
            catalog=registry,
            emit=emit,
            router=router,
            instruction_builder=routing_instructions,
            process_launcher=_root_process_launcher,
        )

    async def run(self, argv):
        """Spawn a Codex desktop child process and pump JSON-lines in both directions.

        Mirrors the original root entrypoint: spin up a ``LocalRouter`` if
        one was not supplied, then drive the app-server protocol loop.
        """
        descriptor = self.process_launcher()
        executable = descriptor["executable_path"]
        if self.router is None:
            # The package never imports LocalRouter; the root wrapper supplies it.
            self.router = LocalRouter(self.registry, pricing_loader=pricing.load,
                                      refresh_benchmarks=True, refresh_cursor_catalog=True)
        self.router.start()
        try:
            self.process = await asyncio.create_subprocess_exec(
                executable, *self.backend_arguments(argv),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024)
            reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
            await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
            backend_reader = asyncio.create_task(self.read_backend())
            try:
                while line := await reader.readline():
                    message = json.loads(line)
                    task = asyncio.create_task(self.handle_client(message))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            finally:
                for task in self.tasks:
                    task.cancel()
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        self.process.kill()
                await backend_reader
        finally:
            self.router.stop()


def main():
    argv = sys.argv[1:]
    if 'app-server' not in argv:
        executable = discover_runtime()["executable_path"]
        os.execv(executable, [executable, *argv])

    def emit(message):
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    asyncio.run(ProviderBridge(RoutingRegistry(), emit).run(argv))


if __name__ == '__main__':
    main()
