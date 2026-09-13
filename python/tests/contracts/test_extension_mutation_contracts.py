"""Revision visibility and retry identity for extension lifecycle commands."""
import unittest

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref


class ExtensionMutationContractTests(unittest.TestCase):
    def test_every_mutation_requires_revision_and_retry_identity(self):
        for method in ("install", "update", "remove", "enable", "disable", "grants.change"):
            with self.subTest(method=method):
                params = {"expected_revision": 0, "idempotency_key": "fixture-key"}
                if method != "install":
                    params["extension_id"] = "com.example.notes"
                if method in ("install", "update"):
                    params["archive_path"] = "/fixture/notes.zip"
                if method == "grants.change":
                    params["approved_scopes"] = []
                schema = f"contracts/engine.v1/methods/extensions.{method}.params.schema.json"
                validate_schema_ref(schema, params)
                for required in ("expected_revision", "idempotency_key"):
                    incomplete = dict(params)
                    del incomplete[required]
                    with self.assertRaises(SchemaValidationError):
                        validate_schema_ref(schema, incomplete)
                for invalid_revision in (-1, True, "0"):
                    with self.assertRaises(SchemaValidationError):
                        validate_schema_ref(schema, {**params, "expected_revision": invalid_revision})

    def test_get_exposes_the_revision_needed_for_mutations(self):
        schema = "contracts/engine.v1/methods/extensions.get.result.schema.json"
        result = {"extension_id": "com.example.notes", "status": "disabled", "revision": 7}
        validate_schema_ref(schema, result)
        del result["revision"]
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(schema, result)
