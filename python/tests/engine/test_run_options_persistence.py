from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_session_run_repository import (
    SQLiteSessionRunRepository,
    StoredRunOptionsCompatibilityError,
)
from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.ports import (
    ClaimDispatchCommand,
    NormalizedRunInput,
    RunAdmissionKey,
    RunJsonSchemaOutputFormat,
    RunNamedToolChoice,
    RunOptions,
    RunServiceTier,
    StartRunCommand,
)
from model_deck.engine.sessions.ports import CreateSessionCommand


REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"
RUN_ID = "550e8400-e29b-41d4-a716-446655440004"
DATABASE_NAME = "run-options.sqlite3"


def _route_snapshot() -> RouteSnapshot:
    return RouteSnapshot(
        registration_id=REGISTRATION_ID,
        registration_revision=3,
        connection_id=CONNECTION_ID,
        connection_revision=4,
        provider_id="com.example.provider",
        provider_model_id="provider/model",
        execution_mode=ExecutionMode.RESPONSES,
        endpoint_config_ref="ref:endpoint.config",
        credential_ref="ref:credential.account",
    )


def _options() -> RunOptions:
    return RunOptions(
        instructions="",
        reasoning_effort="high",
        service_tier=RunServiceTier.STANDARD,
        max_output_tokens=4096,
        parallel_tool_calls=False,
        output_format=RunJsonSchemaOutputFormat(
            name="answer",
            schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            description="Structured answer",
            strict=True,
        ),
        tool_choice=RunNamedToolChoice(tool_name="lookup"),
    )


def _repository(directory: str, *, create_ids: bool = False) -> SQLiteSessionRunRepository:
    identifiers = iter((SESSION_ID, RUN_ID))
    return SQLiteSessionRunRepository(
        Path(directory) / DATABASE_NAME,
        uuid_factory=(lambda: next(identifiers)) if create_ids else None,
        utc_clock=lambda: "2026-09-12T00:00:00Z",
    )


def _admit(repository: SQLiteSessionRunRepository, options: RunOptions) -> str:
    repository.create(
        CreateSessionCommand(
            registration_id=REGISTRATION_ID,
            host_context_ref="ref:host.context",
        )
    )
    result = repository.admit(
        StartRunCommand(
            admission_key=RunAdmissionKey(
                principal_id="ref:principal",
                operation_id="engine.v1.runs.start",
                idempotency_key="run-options-1",
            ),
            request_hash="request-hash-before-options-storage",
            session_id=SESSION_ID,
            client_request_id="client-request-1",
            registration_id=REGISTRATION_ID,
            route_snapshot=_route_snapshot(),
            input=NormalizedRunInput(
                messages=({"role": "user", "content": "hello"},)
            ),
            options=options,
        )
    )
    return result.run.run_id


class RunOptionsPersistenceTests(unittest.TestCase):
    def test_admit_reopen_and_recover_preserves_exact_typed_options(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            _admit(repository, _options())

            with sqlite3.connect(database) as connection:
                stored = json.loads(
                    connection.execute("SELECT options_json FROM runs").fetchone()[0]
                )
            self.assertEqual(stored["schema_version"], 1)
            self.assertEqual(stored["options"]["instructions"], "")
            self.assertIs(stored["options"]["parallel_tool_calls"], False)
            self.assertEqual(stored["options"]["service_tier"], "standard")
            self.assertEqual(stored["options"]["tool_choice"]["type"], "named")
            self.assertEqual(stored["options"]["output_format"]["type"], "json_schema")

            recovery = _repository(directory).recover_after_restart(
                "2026-09-12T00:01:00Z"
            )

            self.assertEqual(len(recovery.dispatchable_requests), 1)
            self.assertEqual(recovery.dispatchable_requests[0].options, _options())

    def test_empty_options_round_trip_as_versioned_empty_object(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            _admit(repository, RunOptions())

            with sqlite3.connect(database) as connection:
                payload = connection.execute(
                    "SELECT options_json FROM runs"
                ).fetchone()[0]
            self.assertEqual(
                json.loads(payload),
                {"schema_version": 1, "options": {}},
            )
            recovered = _repository(directory).recover_after_restart(
                "2026-09-12T00:01:00Z"
            )
            self.assertEqual(recovered.dispatchable_requests[0].options, RunOptions())

    def test_preexisting_schema_and_row_default_to_empty_without_hash_change(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            _admit(repository, RunOptions())
            with sqlite3.connect(database) as connection:
                request_hash = connection.execute(
                    "SELECT request_hash FROM run_admissions"
                ).fetchone()[0]
                connection.execute("ALTER TABLE runs DROP COLUMN options_json")

            recovered = _repository(directory).recover_after_restart(
                "2026-09-12T00:01:00Z"
            )

            self.assertEqual(recovered.dispatchable_requests[0].options, RunOptions())
            with sqlite3.connect(database) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(runs)")
                }
                stored_hash, stored_options = connection.execute(
                    "SELECT a.request_hash, r.options_json "
                    "FROM run_admissions a JOIN runs r ON r.run_id = a.run_id"
                ).fetchone()
            self.assertIn("options_json", columns)
            self.assertEqual(stored_hash, request_hash)
            self.assertIsNone(stored_options)

    def test_claimed_run_is_interrupted_and_never_reconstructed_for_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            run_id = _admit(repository, _options())
            repository.claim_dispatch(
                ClaimDispatchCommand(run_id=run_id, dispatch_token=run_id)
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE runs SET options_json = ? WHERE run_id = ?",
                    ("corrupt claimed-run options", run_id),
                )

            recovery = _repository(directory).recover_after_restart(
                "2026-09-12T00:01:00Z"
            )

            self.assertEqual(recovery.dispatchable_requests, ())
            self.assertEqual(recovery.interrupted_run_ids, (run_id,))

    def test_present_corrupt_options_fail_closed_without_mutating_database(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            _admit(repository, _options())
            corrupt = json.dumps(
                {
                    "schema_version": 1,
                    "options": {"parallel_tool_calls": "false"},
                }
            )
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE runs SET options_json = ?", (corrupt,))
            with sqlite3.connect(database) as connection:
                before = list(connection.iterdump())

            with self.assertRaises(StoredRunOptionsCompatibilityError) as caught:
                _repository(directory).recover_after_restart(
                    "2026-09-12T00:01:00Z"
                )

            self.assertEqual(
                str(caught.exception),
                "stored run options require explicit compatibility handling",
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(list(connection.iterdump()), before)

    def test_present_null_options_fail_closed_instead_of_becoming_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / DATABASE_NAME
            repository = _repository(directory, create_ids=True)
            _admit(repository, _options())
            present_null = json.dumps(
                {
                    "schema_version": 1,
                    "options": None,
                }
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE runs SET options_json = ?",
                    (present_null,),
                )

            with self.assertRaises(StoredRunOptionsCompatibilityError) as caught:
                _repository(directory).recover_after_restart(
                    "2026-09-12T00:01:00Z"
                )

            self.assertEqual(
                str(caught.exception),
                "stored run options require explicit compatibility handling",
            )


if __name__ == "__main__":
    unittest.main()
