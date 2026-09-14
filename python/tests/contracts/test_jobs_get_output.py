import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from model_deck_contracts.validator import (
    SchemaValidationError,
    validate_schema_ref,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
JOBS_GET_RESULT_SCHEMA_REF = (
    "contracts/engine.v1/methods/jobs.get.result.schema.json"
)
JOBS_COMPLETE_PARAMS_SCHEMA_REF = (
    "contracts/plugin.v1/broker/jobs.complete.params.schema.json"
)


def _load(name: str, *, valid: bool) -> dict:
    folder = "valid" if valid else "invalid"
    return json.loads(
        (REPO_ROOT / "contracts" / "fixtures" / folder / name).read_text(
            encoding="utf-8"
        )
    )


def _build_validator(schema_ref_uri: str) -> Draft202012Validator:
    root = REPO_ROOT / "python" / "src" / "model_deck_contracts" / "schemas"
    contracts_root = root / "contracts"
    schemas: dict[str, dict] = {}
    for path in sorted(contracts_root.rglob("*.schema.json")):
        rel = path.relative_to(root).as_posix()
        schemas[rel] = json.loads(path.read_text(encoding="utf-8"))

    def retrieve(uri: str) -> Resource:
        key = uri.split("#", 1)[0]
        if key in schemas:
            return Resource.from_contents(schemas[key])
        raise LookupError(f"no bundled schema for {uri}")

    registry = Registry(retrieve=retrieve).with_resources(
        [(uri, Resource.from_contents(doc)) for uri, doc in schemas.items()]
    )
    return Draft202012Validator({"$ref": schema_ref_uri}, registry=registry)


class JobsGetOutputContractTests(unittest.TestCase):
    def test_minimal_document_output_validates_against_jobs_get_result(
        self,
    ) -> None:
        result = {
            "job_id": "550e8400-e29b-41d4-a716-446655440099",
            "state": "completed",
            "output": {
                "media_type": "text/markdown",
                "suggested_filename": "session-2026-09-13.md",
                "content": "# Session notes\n\nFirst observation.\n",
            },
        }
        validate_schema_ref(JOBS_GET_RESULT_SCHEMA_REF, result)

    def test_canonical_valid_fixture_validates_against_jobs_get_result(
        self,
    ) -> None:
        fixture = _load("plugin_job_output_minimal.json", valid=True)
        result = {"state": "completed", **fixture}
        validate_schema_ref(JOBS_GET_RESULT_SCHEMA_REF, result)

    def test_canonical_valid_fixture_validates_against_jobs_complete_params(
        self,
    ) -> None:
        fixture = _load("plugin_job_output_minimal.json", valid=True)
        validate_schema_ref(JOBS_COMPLETE_PARAMS_SCHEMA_REF, fixture)

    def test_canonical_over_nested_fixture_rejects_against_jobs_get_result(
        self,
    ) -> None:
        fixture = _load("plugin_job_output_over_nested.json", valid=False)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(JOBS_GET_RESULT_SCHEMA_REF, fixture)

    def test_canonical_over_nested_fixture_rejects_against_jobs_complete_params(
        self,
    ) -> None:
        fixture = _load("plugin_job_output_over_nested.json", valid=False)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(JOBS_COMPLETE_PARAMS_SCHEMA_REF, fixture)

    def test_output_optional_on_jobs_get_result(self) -> None:
        validator = _build_validator(JOBS_GET_RESULT_SCHEMA_REF)
        result = {
            "job_id": "550e8400-e29b-41d4-a716-446655440099",
            "state": "completed",
        }
        errors = list(validator.iter_errors(result))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
