from __future__ import annotations

import unittest

from model_deck.engine.runs.options import run_options_to_wire
from model_deck.engine.runs.ports import (
    NormalizedRunInput,
    RestartRecoveryResult,
    RunAdmissionRequestHashConflictError,
    RunJsonSchemaOutputFormat,
    RunNamedToolChoice,
    RunNoToolChoice,
    RunOptions,
    RunRequest,
    RunServiceTier,
    ToolDefinition,
)
from model_deck.engine.runs.use_cases import RunApplicationCoordinator, StartRunUseCase
from tests.engine.test_run_use_cases import (
    HOST_CTX,
    PRINCIPAL_ID,
    RUN_ID,
    SESSION_ID,
    RecordingProvider,
    RecordingRouteResolver,
    RecordingRunRepository,
    RecordingSessionRepository,
    _route_snapshot,
    _run_record,
    _start_params,
)


def _tool(*, description: str = "Search") -> dict[str, object]:
    return {
        "name": "search",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        "host_execution_required": True,
    }


class RunOptionsAdmissionTests(unittest.TestCase):
    def _build(self):
        runs = RecordingRunRepository()
        sessions = RecordingSessionRepository()
        routes = RecordingRouteResolver()
        provider = RecordingProvider()
        coordinator = RunApplicationCoordinator(runs)
        start = StartRunUseCase(runs, sessions, routes, provider, coordinator)
        return runs, sessions, routes, provider, coordinator, start

    def _execute(self, start: StartRunUseCase, params: dict[str, object]):
        return start.execute(
            params,
            principal_id=PRINCIPAL_ID,
            authorized_host_context_ref=HOST_CTX,
        )

    def test_omitted_and_empty_options_preserve_the_same_admission_hash(self) -> None:
        runs, _, _, provider, _, start = self._build()

        first = self._execute(start, _start_params())
        replay = self._execute(start, _start_params(options={}))

        self.assertEqual(first["run"]["run_id"], replay["run"]["run_id"])
        self.assertEqual(len(runs.admit_calls), 1)
        self.assertEqual(len(provider.starts), 1)
        self.assertEqual(runs.admit_calls[0].options, RunOptions())

    def test_changed_options_conflict_and_explicit_falsy_values_are_retained(self) -> None:
        runs, _, _, provider, _, start = self._build()
        explicit = {
            "instructions": "",
            "service_tier": "standard",
            "parallel_tool_calls": False,
        }

        self._execute(start, _start_params(options=explicit))
        with self.assertRaises(RunAdmissionRequestHashConflictError):
            self._execute(
                start,
                _start_params(options={**explicit, "parallel_tool_calls": True}),
            )

        self.assertEqual(len(runs.admit_calls), 1)
        self.assertEqual(len(provider.starts), 1)
        expected_wire = {
            "instructions": "",
            "service_tier": "standard",
            "parallel_tool_calls": False,
        }
        self.assertEqual(run_options_to_wire(runs.admit_calls[0].options), expected_wire)
        self.assertEqual(run_options_to_wire(provider.starts[0].options), expected_wire)

    def test_options_are_detached_from_input_and_provider_schema_is_detached_from_admission(
        self,
    ) -> None:
        runs, _, _, provider, _, start = self._build()
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        }
        options = {
            "output_format": {
                "type": "json_schema",
                "name": "answer",
                "schema": schema,
                "strict": True,
            }
        }

        self._execute(start, _start_params(options=options))
        admitted_format = runs.admit_calls[0].options.output_format
        provider_format = provider.starts[0].options.output_format
        self.assertIsInstance(admitted_format, RunJsonSchemaOutputFormat)
        self.assertIsInstance(provider_format, RunJsonSchemaOutputFormat)
        self.assertIsNot(admitted_format.schema, schema)
        self.assertIsNot(provider_format.schema, admitted_format.schema)

        schema["properties"]["answer"]["type"] = "number"
        provider_format.schema["properties"]["answer"]["type"] = "integer"
        self.assertEqual(
            admitted_format.schema["properties"]["answer"]["type"],
            "string",
        )

    def test_bad_options_and_unknown_named_tool_do_not_admit(self) -> None:
        invalid_params = (
            _start_params(options={"max_output_tokens": 0}),
            _start_params(
                tools=[_tool()],
                options={"tool_choice": {"type": "named", "tool_name": "missing"}},
            ),
        )

        for params in invalid_params:
            with self.subTest(params=params):
                runs, sessions, routes, provider, _, start = self._build()
                with self.assertRaises(ValueError):
                    self._execute(start, params)
                self.assertEqual(runs.admit_calls, [])
                self.assertEqual(sessions.get_calls, [])
                self.assertEqual(routes.requests, [])
                self.assertEqual(provider.starts, [])

    def test_named_choice_accepts_a_declared_authorized_tool(self) -> None:
        runs, _, _, provider, _, start = self._build()

        self._execute(
            start,
            _start_params(
                tools=[_tool()],
                options={"tool_choice": {"type": "named", "tool_name": "search"}},
            ),
        )

        self.assertEqual(len(runs.admit_calls), 1)
        self.assertEqual(len(provider.starts), 1)
        self.assertIsInstance(provider.starts[0].options.tool_choice, RunNamedToolChoice)
        self.assertEqual(provider.starts[0].options.tool_choice.tool_name, "search")

    def test_none_suppresses_provider_tools_but_hashes_all_requested_tools(self) -> None:
        runs, _, routes, provider, _, start = self._build()
        params = _start_params(
            tools=[_tool()],
            options={"tool_choice": {"type": "none"}},
        )

        self._execute(start, params)

        self.assertEqual(len(runs.admit_calls[0].tools), 1)
        self.assertEqual(provider.starts[0].tools, ())
        self.assertIsInstance(provider.starts[0].options.tool_choice, RunNoToolChoice)
        self.assertIsNone(routes.requests[0].capability_requirements)

        with self.assertRaises(RunAdmissionRequestHashConflictError):
            self._execute(
                start,
                _start_params(
                    tools=[_tool(description="Changed")],
                    options={"tool_choice": {"type": "none"}},
                ),
            )
        self.assertEqual(len(provider.starts), 1)

    def test_options_count_toward_combined_request_payload_limit(self) -> None:
        runs, sessions, routes, provider, _, start = self._build()
        params = _start_params(
            input={"messages": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "i" * 525_000}],
            }]},
            options={
                "output_format": {
                    "type": "json_schema",
                    "name": "large",
                    "schema": {"description": "o" * 525_000},
                }
            },
        )

        with self.assertRaisesRegex(ValueError, "payload exceeds maximum size"):
            self._execute(start, params)

        self.assertEqual(runs.admit_calls, [])
        self.assertEqual(sessions.get_calls, [])
        self.assertEqual(routes.requests, [])
        self.assertEqual(provider.starts, [])

    def test_recovered_provider_request_detaches_options_and_suppresses_tools(self) -> None:
        runs, _, _, provider, coordinator, _ = self._build()
        options = RunOptions(
            service_tier=RunServiceTier.STANDARD,
            output_format=RunJsonSchemaOutputFormat(
                name="answer",
                schema={"type": "object", "properties": {"answer": {"type": "string"}}},
            ),
            tool_choice=RunNoToolChoice(),
        )
        request = RunRequest(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            client_request_id="client-recovery",
            idempotency_key="idem-recovery",
            route_snapshot=_route_snapshot(),
            input=NormalizedRunInput(),
            tools=(
                ToolDefinition(
                    name="search",
                    input_schema={"type": "object"},
                    host_execution_required=True,
                ),
            ),
            options=options,
        )
        runs.runs[RUN_ID] = _run_record(client_request_id=request.client_request_id)
        runs.recovery_result = RestartRecoveryResult(
            observed_at="ignored",
            dispatchable_requests=(request,),
            interrupted_run_ids=(),
        )

        coordinator.recover_after_restart(
            provider,
            observed_at="2026-01-02T00:00:00Z",
        )

        provider_request = provider.starts[0]
        self.assertEqual(provider_request.tools, ())
        self.assertEqual(provider_request.options, options)
        self.assertIsNot(provider_request.options, options)
        self.assertIsNot(
            provider_request.options.output_format.schema,
            options.output_format.schema,
        )


if __name__ == "__main__":
    unittest.main()
