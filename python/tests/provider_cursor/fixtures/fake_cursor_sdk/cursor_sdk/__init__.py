"""Deterministic, no-network subset of cursor-sdk used by the B14 process gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


_TRACE_PATH = Path(__file__).resolve().parents[1] / "trace.jsonl"


def _record(event: str, **values) -> None:
    with _TRACE_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **values}, sort_keys=True) + "\n")


class CustomTool:
    def __init__(self, *, execute, description, input_schema):
        self.execute = execute
        self.description = description
        self.input_schema = input_schema


class LocalAgentOptions:
    def __init__(self, *, cwd, setting_sources, custom_tools):
        self.cwd = cwd
        self.setting_sources = setting_sources
        self.custom_tools = custom_tools


class AgentOptions:
    def __init__(self, *, model, api_key, tools, mcp_servers, agents, local):
        self.model = model
        self.api_key = api_key
        self.tools = tools
        self.mcp_servers = mcp_servers
        self.agents = agents
        self.local = local


class SendOptions:
    def __init__(self, *, on_delta):
        self.on_delta = on_delta


def _parameter(name: str, *values: str):
    return SimpleNamespace(
        id=name,
        values=[SimpleNamespace(value=value) for value in values],
    )


def _model(model_id: str, *, fast: bool):
    parameters = [_parameter("effort", "low", "high")]
    if fast:
        parameters.append(_parameter("fast", "false", "true"))
    return SimpleNamespace(
        id=model_id,
        display_name=model_id,
        description="Fake Cursor SDK model",
        parameters=parameters,
        variants=[],
    )


def _usage():
    return SimpleNamespace(
        input_tokens=7,
        output_tokens=3,
        cache_read_tokens=2,
        cache_write_tokens=1,
        reasoning_tokens=1,
    )


class _Models:
    def list(self, *, api_key):
        _record("models.list", api_key=api_key)
        return [
            _model("composer-test", fast=True),
            _model("no-fast", fast=False),
            _model("truncate", fast=True),
        ]


class _Run:
    status = "running"
    id = "fake-cursor-run"

    def __init__(self, options, send_options):
        self._options = options
        self._send_options = send_options

    def messages(self):
        model_id = self._options.model["id"]
        if model_id == "truncate":
            os._exit(0)
        tool = self._options.local.custom_tools.get("host.exec")
        if tool is not None:
            for step in (1, 2):
                result = tool.execute({"step": step}, None)
                _record("tool.result", step=step, result=result)
        self._send_options.on_delta(SimpleNamespace(type="text-delta", text="fake complete"))
        yield SimpleNamespace(type="usage", usage=_usage())

    def wait(self):
        self.status = "finished"
        return SimpleNamespace(status="finished", usage=_usage())

    def cancel(self):
        self.status = "cancelled"
        _record("run.cancelled")


class _Agent:
    agent_id = "fake-agent"

    def __init__(self, options, client):
        self._options = options
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def send(self, message, send_options):
        _record("agent.send", message=message)
        return _Run(self._options, send_options)

    def get_usage(self):
        return SimpleNamespace(cost=None)


class _Agents:
    def __init__(self, client):
        self._client = client

    def create(self, options):
        _record(
            "agents.create",
            account=options.api_key,
            model=options.model,
            tools=options.tools,
            mcp_servers=options.mcp_servers,
            agents=options.agents,
            setting_sources=options.local.setting_sources,
            custom_tools=sorted(options.local.custom_tools),
        )
        return _Agent(options, self._client)


class CursorClient:
    def __init__(self, launch_options):
        self._launch_options = launch_options
        self.models = _Models()
        self.agents = _Agents(self)

    @classmethod
    def launch_bridge(cls, **options):
        _record("bridge.launch", **options)
        return cls(options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def with_options(self, **options):
        _record("client.options", **options)
        return self

    def ping(self):
        return None

    def close(self):
        return None
