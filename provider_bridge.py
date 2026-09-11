"""Native desktop JSON-RPC model routing. Does not proxy HTTP or handle API keys."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from routing_registry import RoutingRegistry
from codex_runtime import discover_runtime

REAL_CODEX = '/Applications/Codex.app/Contents/Resources/codex'
MODEL_FIELDS = {'model', 'model_reasoning_effort'}
ROUTING_INSTRUCTIONS = (
    'Mixed-provider routing: when delegating to an OpenAI model, select its explicit '
    'subscription_* custom agent role (for example subscription_gpt_5_6_sol). '
    'For an OpenRouter model, select its registered openrouter_* custom agent role. '
    'Do not use a model-name override alone to cross providers: it can inherit the '
    'parent provider. Preserve task ownership and permission boundaries. If the required '
    'role is unavailable, report that instead of silently changing the billing route.'
)


class BridgeError(Exception):
    pass


class BackendError(Exception):
    def __init__(self, error):
        self.error = error


class ProviderBridge:
    def __init__(self, registry, emit):
        self.registry = registry
        self.emit = emit
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
                self.emit(message)
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(BridgeError('Codex backend stopped. Restart the integrated app.'))

    def route(self, model):
        if not model:
            return None
        registered = self.registry.load_models()
        if model in registered:
            definition = registered[model]['config']['model_providers']['openrouter-settings']
            # Persist credential-route identity in native thread metadata, not API keys.
            digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:16]
            provider = 'openrouter-bridge-' + digest
            return {'provider': provider, 'config': {'model_providers': {provider: definition}}}
        if model in self.openai_models or model.startswith('gpt-'):
            return {'provider': 'openai', 'config': {}}
        if '/' in model:
            raise BridgeError('Register this OpenRouter model in OpenRouter Settings first.')
        return None

    def remember(self, result):
        if not isinstance(result, dict):
            return
        thread = result.get('thread') or {}
        provider = result.get('modelProvider') or thread.get('modelProvider')
        if thread.get('id') and provider:
            self.thread_providers[thread['id']] = provider

    async def provider_for_thread(self, thread_id):
        if thread_id not in self.thread_providers:
            self.remember(await self.request('thread/read', {'threadId': thread_id, 'includeTurns': False}))
        provider = self.thread_providers.get(thread_id)
        if provider is None:
            raise BridgeError('Could not verify this task\'s provider. No model request was sent.')
        return provider

    async def validate_existing_selection(self, thread_id, model):
        route = self.route(model)
        if route and route['provider'] != await self.provider_for_thread(thread_id):
            raise BridgeError('Start a new task to change between OpenAI and OpenRouter. This task keeps its existing provider, history, and permissions.')

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
        selected_route = self.route(selection['model'])
        if selected_route and selected_route['provider'].startswith('openrouter-bridge-'):
            selection['effort'] = 'low'
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
        if method == 'model/list':
            result = await self.request(method, params)
            self.openai_models.update(entry['model'] for entry in result['data'])
            if not result.get('nextCursor'):
                existing = {entry['model'] for entry in result['data']}
                result['data'].extend(entry for entry in self.registry.catalog_entries() if entry['model'] not in existing)
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
                selected_route = self.route(selected['model'])
                if selected_route and selected_route['provider'].startswith('openrouter-bridge-'):
                    result['config']['model_reasoning_effort'] = 'low'
                elif selected.get('effort') is not None:
                    result['config']['model_reasoning_effort'] = selected['effort']
            return result
        if method in ('config/value/write', 'config/batchWrite'):
            async with self.config_lock:
                return await self.handle_config_write(method, params)
        if method == 'thread/start':
            params = copy.deepcopy(params)
            instructions = params.get('developerInstructions')
            if instructions is None:
                effective = await self.request('config/read', {'cwd': params.get('cwd'), 'includeLayers': False})
                instructions = (params.get('config') or {}).get('developer_instructions',
                    effective.get('config', {}).get('developer_instructions'))
            params['developerInstructions'] = '\n\n'.join(filter(None, [instructions, ROUTING_INSTRUCTIONS]))
            selection = self.registry.selected()
            model = params.get('model') or selection.get('model')
            route = self.route(model)
            if route:
                explicit_provider = params.get('modelProvider')
                if explicit_provider not in (None, 'openai', 'openrouter-settings', route['provider']):
                    raise BridgeError('This task uses another explicit provider; mixed-provider routing was not applied.')
                params['model'] = model
                params['modelProvider'] = route['provider']
                config = params.setdefault('config', None) or {}
                providers = dict(config.get('model_providers') or {})
                providers.update(route['config'].get('model_providers') or {})
                if providers:
                    config['model_providers'] = providers
                # The UI may carry an OpenAI effort from its prior selection.
                if route['provider'].startswith('openrouter-bridge-'):
                    config['model_reasoning_effort'] = 'low'
                params['config'] = config
            result = await self.request(method, params)
            self.remember(result)
            if route and result.get('modelProvider') != route['provider']:
                raise BridgeError('Codex did not select the requested provider. No turn was started.')
            return result
        if method == 'thread/resume':
            params = copy.deepcopy(params)
            thread_id = params.get('threadId')
            if params.get('model'):
                await self.validate_existing_selection(thread_id, params['model'])
            # Persisted OpenRouter tasks still need their command-auth provider definition.
            provider = await self.provider_for_thread(thread_id)
            if provider == 'openrouter-settings' or provider.startswith('openrouter-bridge-'):
                models = self.registry.load_models()
                if not models:
                    raise BridgeError('Register an OpenRouter model before reopening this task.')
                routes = {self.route(model)['provider']: self.route(model) for model in models}
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
            if provider.startswith('openrouter-bridge-'):
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

    async def run(self, argv):
        executable = discover_runtime()["executable_path"]
        self.process = await asyncio.create_subprocess_exec(executable, *argv,
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
