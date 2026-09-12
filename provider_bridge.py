"""Native desktop JSON-RPC bridge plus the loopback model router. Never handles API keys itself."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import pricing
from local_router import LocalRouter
from routing_registry import RoutingRegistry
from codex_runtime import discover_runtime
from spawn_benchmarks import model_choice_lines

MODEL_FIELDS = {'model', 'model_reasoning_effort'}
ROUTING_INSTRUCTIONS = (
    'Mixed-provider routing: every model request is routed by model id. OpenAI models (gpt-*) '
    'use the ChatGPT subscription. Models added in Model Deck run on the endpoint they were added '
    'to: OpenRouter credits, Cursor SDK, another provider\'s API key, or a local server that bills nothing. '
    'cursor/<SDK id> models use Cursor SDK pricing and the same request pools as IDE/Cloud Agents; '
    'account limits and overages apply, with no free or unlimited guarantee. '
    'To delegate work to another model, pass its exact model id in the spawn_agent model parameter, '
    'or select its registered openrouter_* role. Unknown models fail with a clear error instead of '
    'silently changing the billing route. Preserve task ownership and permission boundaries.'
)
MCP_INSTRUCTIONS = (
    'The model_deck MCP tools (search_models, model_pricing, add_model, list_added_models) find '
    'models on OpenRouter or any saved endpoint, show list prices, and add a model to Model Deck; '
    'an added model can be picked or spawned on the next turn. Prefer the cheapest model that fits '
    'the job; long contexts benefit from low cached-input prices. Use benchmark_status and refresh_benchmarks '
    'to inspect benchmark freshness, and model_benchmarks, compare_models, and rank_models for '
    'source-backed quality comparisons. The native spawn_agent description includes cached model choices, '
    'prices, capabilities and benchmark evidence before delegation. Compare scores within the same '
    'source, test and snapshot; benchmarks do not guarantee task success. '
    'Unknown prices or missing benchmark scores are not zero.'
)
def routing_instructions(registry, price_table=None, benchmark_lines=None):
    """The routing rules plus the same model evidence supplied on spawn_agent."""
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


def toml_inline(value):
    """A TOML inline value for a -c override. JSON string escapes are valid TOML basic strings."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(toml_inline(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(f'{json.dumps(key)} = {toml_inline(item)}' for key, item in value.items()) + '}'
    raise TypeError(f'Cannot express {type(value).__name__} in TOML')


def mcp_server_arguments(script=None, python=None):
    """Config override that registers Model Deck's MCP server for this Codex process only."""
    script = Path(script) if script is not None else Path(__file__).resolve().with_name('model_deck_mcp.py')
    if not script.is_file():
        return []
    table = {'command': python or sys.executable, 'args': ['-B', str(script)], 'enabled': True,
             'startup_timeout_sec': 20, 'tool_timeout_sec': 120, 'default_tools_approval_mode': 'approve',
             'tools': {'remove_model': {'approval_mode': 'prompt'}}}
    return ['-c', 'mcp_servers.model_deck=' + toml_inline(table)]


class BridgeError(Exception):
    pass


class BackendError(Exception):
    def __init__(self, error):
        self.error = error


class ProviderBridge:
    def __init__(self, registry, emit, router=None):
        self.registry = registry
        self.emit = emit
        self.router = router
        self.pending = {}
        self.sequence = 0
        self.prefix = 'provider-bridge-' + uuid.uuid4().hex + '-'
        self.process = None
        self.tasks = set()
        self.thread_providers = {}
        self.thread_locks = {}
        self.openai_models = set()
        self.config_lock = asyncio.Lock()

    async def request(self, method, params):
        self.sequence += 1
        request_id = self.prefix + str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send_backend({'id': request_id, 'method': method, 'params': params})
        try:
            return await future
        finally:
            self.pending.pop(request_id, None)

    async def send_backend(self, message):
        self.process.stdin.write((json.dumps(message, separators=(',', ':')) + '\n').encode())
        await self.process.stdin.drain()

    async def read_backend(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            future = self.pending.get(message.get('id'))
            if future is not None and 'method' not in message:
                if not future.done():
                    if 'error' in message:
                        future.set_exception(BackendError(message['error']))
                    else:
                        future.set_result(message.get('result'))
            else:
                if message.get('method') == 'thread/started':
                    self.remember(message.get('params'))
                self.emit(message)
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(BridgeError('Codex backend stopped. Restart the integrated app.'))

    def route(self, model):
        """Every routable model runs on the built-in openai provider; the router bills by name."""
        if not model:
            return None
        registered = self.registry.load_models()
        if model in registered:
            endpoint = registered[model].get('endpoint') or {}
            billing = 'cursor' if endpoint.get('cursor') else 'openrouter'
            return {'provider': 'openai', 'billing': billing}
        if model in self.openai_models or model.startswith('gpt-'):
            return {'provider': 'openai', 'billing': 'subscription'}
        if '/' in model:
            raise BridgeError('Register this model in Model Deck first.')
        return None

    def legacy_route(self, model):
        """Provider identity used by tasks created before the router existed."""
        registered = self.registry.load_models()
        if model not in registered:
            return None
        definition = registered[model]['config']['model_providers']['openrouter-settings']
        digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:16]
        provider = 'openrouter-bridge-' + digest
        return {'provider': provider, 'config': {'model_providers': {provider: definition}}}

    @staticmethod
    def is_legacy_provider(provider):
        return provider == 'openrouter-settings' or str(provider).startswith('openrouter-bridge-')

    def remember(self, result):
        if not isinstance(result, dict):
            return
        thread = result.get('thread') or {}
        if not isinstance(thread, dict):
            return
        provider = result.get('modelProvider') or thread.get('modelProvider')
        if thread.get('id') and provider:
            self.thread_providers[thread['id']] = provider
        cwd = result.get('cwd') or thread.get('cwd')
        remember_thread = getattr(self.router, 'remember_thread', None)
        if thread.get('id') and isinstance(cwd, str) and cwd.strip() and remember_thread is not None:
            remember_thread(thread['id'], cwd)

    async def provider_for_thread(self, thread_id):
        if thread_id not in self.thread_providers:
            self.remember(await self.request('thread/read', {'threadId': thread_id, 'includeTurns': False}))
        provider = self.thread_providers.get(thread_id)
        if provider is None:
            raise BridgeError('Could not verify this task\'s provider. No model request was sent.')
        return provider

    async def validate_existing_selection(self, thread_id, model):
        route = self.route(model)
        if route is None:
            return
        provider = await self.provider_for_thread(thread_id)
        if not self.is_legacy_provider(provider):
            return  # Router-backed tasks can switch freely; billing follows the model name.
        legacy = self.legacy_route(model)
        if legacy is None or legacy['provider'] != provider:
            raise BridgeError('This older task is tied to its saved OpenRouter route. Start a new task to change models.')

    async def handle_config_write(self, method, params):
        edits = params.get('edits', []) if method == 'config/batchWrite' else [params]
        virtual = [edit for edit in edits if edit.get('keyPath') in MODEL_FIELDS]
        if not virtual:
            return await self.request(method, params)
        file_path = params.get('filePath')
        default_config = str(Path.home() / '.codex/config.toml')
        if file_path and str(Path(file_path).absolute()) != default_config:
            raise BridgeError('The integrated picker supports user defaults, not project or profile model writes.')
        selection = dict(self.registry.selected())
        for edit in virtual:
            if edit['keyPath'] == 'model':
                if not isinstance(edit.get('value'), str) or self.route(edit['value']) is None:
                    raise BridgeError('Choose a registered OpenRouter model or an available OpenAI model.')
                selection['model'] = edit['value']
            else:
                selection['effort'] = edit.get('value')
        current = await self.request('config/read', {'includeLayers': True})
        if not selection.get('model'):
            selection['model'] = current['config'].get('model')
        user_layer = next((layer for layer in current.get('layers', [])
                           if layer.get('name', {}).get('type') == 'user'), None)
        version = user_layer.get('version', '') if user_layer else ''
        if params.get('expectedVersion') is not None and params['expectedVersion'] != version:
            raise BridgeError('Settings changed. Refresh the picker before selecting a model again.')
        remaining = [edit for edit in edits if edit.get('keyPath') not in MODEL_FIELDS]
        if remaining:
            forwarded = dict(params, edits=remaining)
            for key in ('keyPath', 'mergeStrategy', 'value'):
                forwarded.pop(key, None)
            result = await self.request('config/batchWrite', forwarded)
        else:
            result = {'filePath': default_config, 'status': 'ok', 'version': version}
        self.registry.select(selection['model'], selection.get('effort'))
        return result

    async def dispatch(self, method, params):
        if method == 'turn/interrupt':
            result = await self.request(method, params)
            cancel_cursor_turn = getattr(self.router, 'cancel_cursor_turn', None)
            if cancel_cursor_turn is not None:
                try:
                    await asyncio.to_thread(cancel_cursor_turn, params.get('threadId'), params.get('turnId'))
                except Exception:
                    # The backend owns the protocol result, including successful interruption.
                    pass
            return result
        if method == 'model/list':
            result = await self.request(method, params)
            self.openai_models.update(entry['model'] for entry in result['data'])
            if not result.get('nextCursor'):
                existing = {entry['model'] for entry in result['data']}
                price_lines = getattr(self.router, 'price_lines', lambda: {})()
                result['data'].extend(entry for entry in self.registry.catalog_entries(price_lines) if entry['model'] not in existing)
            selected = self.registry.selected().get('model')
            if selected:
                for entry in result['data']:
                    entry['isDefault'] = entry['model'] == selected
            return result
        if method == 'config/read':
            result = await self.request(method, params)
            selected = self.registry.selected()
            if selected.get('model'):
                result['config']['model'] = selected['model']
                if selected.get('effort') is not None:
                    result['config']['model_reasoning_effort'] = selected['effort']
            return result
        if method in ('config/value/write', 'config/batchWrite'):
            async with self.config_lock:
                return await self.handle_config_write(method, params)
        if method == 'thread/start':
            params = copy.deepcopy(params)
            selection = self.registry.selected()
            model = params.get('model') or selection.get('model')
            route = self.route(model)
            if route:
                explicit_provider = params.get('modelProvider')
                if explicit_provider not in (None, 'openai'):
                    raise BridgeError('This task uses another explicit provider; mixed-provider routing was not applied.')
                params['model'] = model
            instructions = params.get('developerInstructions')
            if instructions is None:
                effective = await self.request('config/read', {'cwd': params.get('cwd'), 'includeLayers': False})
                instructions = (params.get('config') or {}).get('developer_instructions',
                    effective.get('config', {}).get('developer_instructions'))
            price_table = getattr(self.router, 'price_table', lambda: {})()
            benchmark_lines = getattr(self.router, 'benchmark_lines', lambda: {})()
            params['developerInstructions'] = '\n\n'.join(filter(None, [instructions,
                routing_instructions(self.registry, price_table, benchmark_lines)]))
            wait_for_catalog = getattr(self.router, 'wait_for_catalog', None)
            if wait_for_catalog is not None:
                # Registered models are only spawnable once Codex has the injected catalog.
                await asyncio.to_thread(wait_for_catalog, 8)
            result = await self.request(method, params)
            self.remember(result)
            if route and result.get('modelProvider') not in (None, 'openai'):
                raise BridgeError('Codex did not use the OpenAI connection, so the model router was not applied. No turn was started.')
            return result
        if method == 'thread/resume':
            params = copy.deepcopy(params)
            thread_id = params.get('threadId')
            if params.get('model'):
                await self.validate_existing_selection(thread_id, params['model'])
            # Tasks created before the router still carry their command-auth provider definition.
            provider = await self.provider_for_thread(thread_id)
            if self.is_legacy_provider(provider):
                models = self.registry.load_models()
                if not models:
                    raise BridgeError('Register an OpenRouter model before reopening this task.')
                routes = {self.legacy_route(model)['provider']: self.legacy_route(model) for model in models}
                if provider == 'openrouter-settings' and len(routes) == 1:
                    definition = next(iter(models.values()))['config']['model_providers']['openrouter-settings']
                    provider_config = {'model_providers': {provider: definition}}
                elif provider in routes:
                    provider_config = routes[provider]['config']
                else:
                    raise BridgeError('This task\'s saved credential route is unavailable. Restore its registered key selection before resuming.')
                config = params.get('config') or {}
                config['model_providers'] = dict(config.get('model_providers') or {}, **provider_config['model_providers'])
                params['config'] = config
                params['modelProvider'] = provider
            result = await self.request(method, params)
            self.remember(result)
            return result
        if method in ('thread/settings/update', 'turn/start'):
            model = params.get('model')
            mode_model = (params.get('collaborationMode') or {}).get('settings', {}).get('model')
            for selected_model in (model, mode_model):
                if selected_model:
                    await self.validate_existing_selection(params['threadId'], selected_model)
            provider = await self.provider_for_thread(params['threadId'])
            if str(provider).startswith('openrouter-bridge-'):
                params = copy.deepcopy(params)
                params['effort'] = 'low'
                if params.get('collaborationMode'):
                    params['collaborationMode']['settings']['reasoning_effort'] = 'low'
            return await self.request(method, params)
        result = await self.request(method, params)
        if method in ('thread/read', 'thread/fork'):
            self.remember(result)
        return result

    async def handle_client(self, message):
        if 'method' not in message or 'id' not in message:
            await self.send_backend(message)
            return
        try:
            params = message.get('params') or {}
            thread_id = params.get('threadId')
            # Never serialize approvals, interrupts, or unrelated task work behind a turn.
            if thread_id and message['method'] in ('thread/settings/update', 'thread/resume', 'turn/start'):
                lock = self.thread_locks.setdefault(thread_id, asyncio.Lock())
                async with lock:
                    result = await self.dispatch(message['method'], params)
            else:
                result = await self.dispatch(message['method'], params)
            self.emit({'id': message['id'], 'result': result})
        except BackendError as error:
            self.emit({'id': message['id'], 'error': error.error})
        except BridgeError as error:
            self.emit({'id': message['id'], 'error': {'code': -32000, 'message': str(error)}})
        except Exception:
            self.emit({'id': message['id'], 'error': {'code': -32000, 'message': 'Provider bridge failed safely. Check registered models and restart the integrated app.'}})

    def backend_arguments(self, argv):
        """The desktop's own arguments plus the overrides that send model traffic through the router
        and register Model Deck's MCP server."""
        if self.router is None:
            return list(argv)
        return list(argv) + self.router.codex_arguments() + mcp_server_arguments()

    async def run(self, argv):
        executable = discover_runtime()["executable_path"]
        if self.router is None:
            self.router = LocalRouter(self.registry, pricing_loader=pricing.load, refresh_benchmarks=True,
                                      refresh_cursor_catalog=True)
        self.router.start()
        try:
            self.process = await asyncio.create_subprocess_exec(executable, *self.backend_arguments(argv),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024)
            reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
            await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
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
