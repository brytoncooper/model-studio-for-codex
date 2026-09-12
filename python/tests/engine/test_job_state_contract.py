"""Public job status preserves crash interruption as a distinct outcome."""
import unittest

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
