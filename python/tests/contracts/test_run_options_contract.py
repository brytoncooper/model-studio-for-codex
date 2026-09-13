import dataclasses
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.ports import (
    NormalizedRunInput,
    RunAdmissionKey,
    RunAutomaticToolChoice,
    RunJsonObjectOutputFormat,
    RunJsonSchemaOutputFormat,
    RunNamedToolChoice,
    RunNoToolChoice,
    RunOptions,
    RunRequest,
    RunRequiredToolChoice,
    RunServiceTier,
    RunTextOutputFormat,
    StartRunCommand,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
RUN_OPTIONS_REF = "contracts/engine.v1/vocabulary.schema.json#/definitions/run_options"
RUN_REQUEST_REF = "contracts/engine.v1/vocabulary.schema.json#/definitions/run_request"
RUNS_START_REF = "contracts/engine.v1/methods/runs.start.params.schema.json"


def _canonical_registry() -> Registry:
    resources: dict[str, Resource] = {}
    for path in CONTRACTS_ROOT.rglob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        relative_path = f"contracts/{path.relative_to(CONTRACTS_ROOT).as_posix()}"
        resource = Resource.from_contents(document)
        resources[relative_path] = resource
        resources[document.get("$id", relative_path)] = resource
    return Registry().with_resources(resources.items())


CANONICAL_REGISTRY = _canonical_registry()


def _validate(schema_ref: str, instance: Any) -> None:
    Draft202012Validator(
        {"$ref": schema_ref},
        registry=CANONICAL_REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(instance)


def _start_params(options: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "session_id": "550e8400-e29b-41d4-a716-446655440001",
        "client_request_id": "client-1",
        "idempotency_key": "idem-1",
        "registration_id": "550e8400-e29b-41d4-a716-446655440002",
    }
    if options is not None:
        params["options"] = options
    return params


def _run_request(options: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _start_params(options)
    request["run_id"] = "550e8400-e29b-41d4-a716-446655440003"
    return request


class RunOptionsSchemaTests(unittest.TestCase):
    def test_options_are_optional_and_empty_options_are_valid(self) -> None:
        for schema_ref, instance_factory in (
            (RUNS_START_REF, _start_params),
            (RUN_REQUEST_REF, _run_request),
        ):
            with self.subTest(schema_ref=schema_ref, form="omitted"):
                _validate(schema_ref, instance_factory())
            with self.subTest(schema_ref=schema_ref, form="empty"):
                _validate(schema_ref, instance_factory({}))

    def test_every_supported_option_is_preserved_by_the_shared_schema(self) -> None:
        options = {
            "instructions": "",
            "reasoning_effort": "xhigh",
            "service_tier": "standard",
            "max_output_tokens": 1,
            "parallel_tool_calls": False,
            "output_format": {
                "type": "json_schema",
                "name": "answer",
                "description": "Structured answer",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                },
                "strict": False,
            },
            "tool_choice": {"type": "named", "tool_name": "lookup"},
        }
        _validate(RUN_OPTIONS_REF, options)
        _validate(RUNS_START_REF, _start_params(options))
        _validate(RUN_REQUEST_REF, _run_request(options))

    def test_output_format_and_tool_choice_tags_are_closed(self) -> None:
        valid_output_formats = (
            {"type": "text"},
            {"type": "json_object"},
            {"type": "json_schema", "name": "answer", "schema": {}},
        )
        valid_tool_choices = (
            {"type": "auto"},
            {"type": "none"},
            {"type": "required"},
            {"type": "named", "tool_name": "lookup"},
        )
        for output_format in valid_output_formats:
            with self.subTest(output_format=output_format):
                _validate(RUN_OPTIONS_REF, {"output_format": output_format})
        for tool_choice in valid_tool_choices:
            with self.subTest(tool_choice=tool_choice):
                _validate(RUN_OPTIONS_REF, {"tool_choice": tool_choice})

        invalid_options = (
            {"output_format": {"type": "yaml"}},
            {"output_format": {"type": "text", "strict": True}},
            {"output_format": {"type": "json_schema", "name": "answer"}},
            {"tool_choice": {"type": "sometimes"}},
            {"tool_choice": {"type": "auto", "tool_name": "lookup"}},
            {"tool_choice": {"type": "named"}},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                _validate(RUN_OPTIONS_REF, options)

    def test_null_unknown_and_out_of_bounds_values_are_rejected(self) -> None:
        invalid_options = (
            None,
            {"unknown": True},
            {"instructions": None},
            {"instructions": "x" * 65_537},
            {"reasoning_effort": ""},
            {"reasoning_effort": "x" * 65},
            {"service_tier": "fast"},
            {"max_output_tokens": 0},
            {"max_output_tokens": 10_000_001},
            {"max_output_tokens": True},
            {"parallel_tool_calls": None},
            {"output_format": None},
            {"output_format": {"type": "json_schema", "name": "", "schema": {}}},
            {"output_format": {"type": "json_schema", "name": "x" * 65, "schema": {}}},
            {
                "output_format": {
                    "type": "json_schema",
                    "name": "ok",
                    "description": "x" * 1025,
                    "schema": {},
                }
            },
            {"output_format": {"type": "json_schema", "name": "ok", "schema": []}},
            {"tool_choice": None},
            {"tool_choice": {"type": "named", "tool_name": ""}},
            {"tool_choice": {"type": "named", "tool_name": "x" * 129}},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                _validate(RUN_OPTIONS_REF, options)

    def test_declared_bounds_accept_their_edges(self) -> None:
        _validate(
            RUN_OPTIONS_REF,
            {
                "instructions": "x" * 65_536,
                "reasoning_effort": "x" * 64,
                "max_output_tokens": 10_000_000,
                "output_format": {
                    "type": "json_schema",
                    "name": "x" * 64,
                    "description": "x" * 1024,
                    "schema": {},
                },
                "tool_choice": {"type": "named", "tool_name": "x" * 128},
            },
        )


class RunOptionsPythonContractTests(unittest.TestCase):
    def test_default_options_keep_old_run_constructors_compatible(self) -> None:
        route_snapshot = RouteSnapshot(
            registration_id="registration",
            registration_revision=1,
            connection_id="connection",
            connection_revision=1,
            provider_id="com.example.provider",
            provider_model_id="model",
            execution_mode=ExecutionMode.RESPONSES,
        )
        request = RunRequest(
            "run",
            "session",
            "client",
            "idem",
            route_snapshot,
            NormalizedRunInput(),
        )
        command = StartRunCommand(
            RunAdmissionKey("principal", "runs.start", "idem"),
            "hash",
            "session",
            "client",
            "registration",
            route_snapshot,
            NormalizedRunInput(),
        )
        self.assertEqual(request.options, RunOptions())
        self.assertEqual(command.options, RunOptions())

    def test_typed_options_preserve_explicit_values_and_tags(self) -> None:
        json_schema = RunJsonSchemaOutputFormat(
            name="answer",
            description="Structured answer",
            schema={"type": "object"},
            strict=False,
        )
        options = RunOptions(
            instructions="",
            reasoning_effort="xhigh",
            service_tier=RunServiceTier.STANDARD,
            max_output_tokens=1,
            parallel_tool_calls=False,
            output_format=json_schema,
            tool_choice=RunNamedToolChoice(tool_name="lookup"),
        )
        self.assertEqual(options.instructions, "")
        self.assertIs(options.parallel_tool_calls, False)
        self.assertEqual(options.service_tier.value, "standard")
        self.assertEqual(options.output_format.type, "json_schema")
        self.assertIs(options.output_format.strict, False)
        self.assertEqual(options.tool_choice.type, "named")
        self.assertEqual(options.tool_choice.tool_name, "lookup")

        output_tags = (
            RunTextOutputFormat().type,
            RunJsonObjectOutputFormat().type,
            RunJsonSchemaOutputFormat(name="x", schema={}).type,
        )
        tool_choice_tags = (
            RunAutomaticToolChoice().type,
            RunNoToolChoice().type,
            RunRequiredToolChoice().type,
            RunNamedToolChoice("tool").type,
        )
        self.assertEqual(output_tags, ("text", "json_object", "json_schema"))
        self.assertEqual(tool_choice_tags, ("auto", "none", "required", "named"))

    def test_options_and_tagged_values_are_frozen(self) -> None:
        options = RunOptions(output_format=RunTextOutputFormat())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.instructions = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.output_format.type = "json_object"  # type: ignore[misc,union-attr]


if __name__ == "__main__":
    unittest.main()
