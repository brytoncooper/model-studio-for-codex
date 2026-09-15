"""Public job status preserves crash interruption as a distinct outcome."""
import tempfile
import unittest
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.first_party import (
    JOB_KIND_BENCHMARKS_REFRESH,
    JOB_KIND_PRICES_REFRESH,
    CreateFirstPartyJobUseCase,
    FirstPartyJobDirectory,
    first_party_owner,
    recover_first_party_jobs,
)
from model_deck.engine.jobs.ports import CompleteJobCommand
from model_deck_contracts import SchemaValidationError, validate_schema_ref


class JobStateContractTests(unittest.TestCase):
    def test_interrupted_job_is_a_public_terminal_outcome(self):
        validate_schema_ref(
            "contracts/engine.v1/methods/jobs.get.result.schema.json",
            {"job_id": "11111111-1111-4111-8111-111111111111", "state": "interrupted"},
        )

    def test_unrecognized_state_is_rejected(self):
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/jobs.get.result.schema.json",
                {"job_id": "11111111-1111-4111-8111-111111111111", "state": "silently_restarted"},
            )


class FirstPartyJobStateContractTests(unittest.TestCase):
    """The engine's own jobs report the same public states plugin jobs do."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = SQLitePluginJobRepository(
            Path(temporary.name) / "jobs.sqlite",
            checkpoint_validator=lambda _schema, _value: None,
        )
        self.owner = first_party_owner("11111111-1111-4111-8111-111111111111")
        self.create = CreateFirstPartyJobUseCase(self.repository, owner=self.owner)
        self.directory = FirstPartyJobDirectory(self.repository)

    def _public_state(self, job_id: str) -> dict:
        result = self.directory.job_get(
            {"job_id": job_id}, principal="model-deck:client:observer"
        )
        validate_schema_ref(
            "contracts/engine.v1/methods/jobs.get.result.schema.json", result
        )
        return result

    def test_running_engine_job_is_a_valid_public_view(self) -> None:
        start = self.create.create(JOB_KIND_PRICES_REFRESH, idempotency_key="k")
        self.assertEqual(self._public_state(start.job_id)["state"], "running")

    def test_restart_reports_interrupted_rather_than_a_silent_restart(self) -> None:
        start = self.create.create(JOB_KIND_PRICES_REFRESH, idempotency_key="k")
        recover_first_party_jobs(self.repository, self.owner)
        self.assertEqual(self._public_state(start.job_id)["state"], "interrupted")

    def test_an_interrupted_engine_job_stays_interrupted(self) -> None:
        start = self.create.create(JOB_KIND_BENCHMARKS_REFRESH, idempotency_key="k")
        recover_first_party_jobs(self.repository, self.owner)
        # A second recovery pass must not move a terminal job, and a cancel
        # arriving afterwards is acknowledged as not accepted rather than
        # reopening it.
        recover_first_party_jobs(self.repository, self.owner)
        accepted = self.directory.job_cancel(
            {"job_id": start.job_id, "idempotency_key": "late"},
            principal="model-deck:client:observer",
        )
        self.assertEqual(accepted, {"accepted": False})
        self.assertEqual(self._public_state(start.job_id)["state"], "interrupted")

    def test_completed_engine_job_carries_its_output(self) -> None:
        start = self.create.create(JOB_KIND_PRICES_REFRESH, idempotency_key="k")
        self.repository.complete(
            CompleteJobCommand(
                job_id=start.job_id,
                owner=self.owner,
                output={"kind": "prices", "record_count": 2},
                output_present=True,
            )
        )
        result = self._public_state(start.job_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["output"], {"kind": "prices", "record_count": 2})
