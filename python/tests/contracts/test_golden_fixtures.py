import json
import unittest

from model_deck_contracts.json_util import canonical_json_equal
from model_deck_contracts.paths import fixtures_root
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref
from model_deck_contracts.wire_types import JsonRpcRequest, RunEventRunCompleted, RunRequest, ToolCall


def _manifest() -> dict:
    return json.loads((fixtures_root() / "manifest.json").read_text(encoding="utf-8"))


def _load(name: str, *, valid: bool) -> dict:
    folder = "valid" if valid else "invalid"
    return json.loads((fixtures_root() / folder / name).read_text(encoding="utf-8"))


class GoldenFixturesTests(unittest.TestCase):
    def test_valid_manifest_fixtures_validate(self) -> None:
        for entry in _manifest()["valid"]:
            with self.subTest(file=entry["file"]):
                instance = _load(entry["file"], valid=True)
                validate_schema_ref(entry["schema"], instance)

    def test_invalid_manifest_fixtures_reject(self) -> None:
        for entry in _manifest()["invalid"]:
            with self.subTest(file=entry["file"]):
                instance = _load(entry["file"], valid=False)
                with self.assertRaises(SchemaValidationError):
                    validate_schema_ref(entry["schema"], instance)

    def test_capability_unknown_remains_unknown(self) -> None:
        features = _load("capability_features_unknown.json", valid=True)["capabilities"]["features"]
        self.assertEqual(features["tools"], "unknown")

    def test_capability_denied_not_tri_state(self) -> None:
        denied = _load("capability_tri_state_denied.json", valid=False)["capabilities"]["features"]["tools"]
        self.assertEqual(denied, "denied")
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/common/types.schema.json#/definitions/capability_tri_state",
                denied,
            )

    def test_unknown_unit_price_stays_null_rather_than_zero(self) -> None:
        record = _load("price_record_unknown_cached_price.json", valid=True)
        self.assertIsNone(record["unit_prices"]["cached_tokens"])
        self.assertNotEqual(record["unit_prices"]["cached_tokens"], 0)

    def test_price_record_requires_every_priced_unit_kind(self) -> None:
        partial = _load("price_record_missing_cached_token_price.json", valid=False)
        self.assertNotIn("cached_tokens", partial["unit_prices"])
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/vocabulary.schema.json#/definitions/price_record",
                partial,
            )

    def test_estimate_never_appears_in_settled_amount(self) -> None:
        rejected = _load("usage_record_estimate_in_settled_amount.json", valid=False)
        self.assertEqual(rejected["cost_kind"], "estimated")
        self.assertIsInstance(rejected["settled_amount"], float)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/vocabulary.schema.json#/definitions/usage_record",
                rejected,
            )
        accepted = _load("usage_record_estimated_without_settled.json", valid=True)
        self.assertEqual(accepted["cost_kind"], "estimated")
        self.assertIsNone(accepted["settled_amount"])
        validate_schema_ref(
            "contracts/engine.v1/vocabulary.schema.json#/definitions/usage_record",
            accepted,
        )

    def test_cost_kind_enum_is_frozen(self) -> None:
        ref = "contracts/engine.v1/vocabulary.schema.json#/definitions/cost_kind"
        for kind in ("estimated", "provider_settled", "subscription_allowance"):
            with self.subTest(kind=kind):
                validate_schema_ref(ref, kind)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, "guessed")

    def test_stale_provenance_keeps_last_good_value_visible(self) -> None:
        provenance = _load("source_provenance_stale.json", valid=True)
        self.assertTrue(provenance["stale"])
        self.assertIn("fetched_at", provenance)
        self.assertIn("last_refresh_error", provenance)
        result = _load("prices_query_result_stale_last_good.json", valid=True)
        self.assertTrue(result["snapshot"]["stale"])
        self.assertEqual(len(result["records"]), 1)

    def test_provenance_requires_fetched_at_so_age_is_computable(self) -> None:
        missing = _load("source_provenance_missing_fetched_at.json", valid=False)
        self.assertNotIn("fetched_at", missing)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/vocabulary.schema.json#/definitions/source_provenance",
                missing,
            )

    def test_unmapped_benchmark_keeps_provider_model_id_null(self) -> None:
        record = _load("benchmark_record_unmapped_model.json", valid=True)
        self.assertIsNone(record["provider_model_id"])
        self.assertTrue(record["source_model_ref"])

    def test_benchmarks_query_no_longer_accepts_bare_objects(self) -> None:
        bare = _load("benchmarks_query_result_bare_object.json", valid=False)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/benchmarks.query.result.schema.json",
                bare,
            )

    def test_subscription_allowance_unknown_use_is_null(self) -> None:
        allowance = _load("subscription_allowance_unknown_used.json", valid=True)
        self.assertIsNone(allowance["used"])
        self.assertIn("start", allowance["window"])
        self.assertIn("end", allowance["window"])

    def test_evidence_reads_are_always_served_from_cache(self) -> None:
        for name in ("prices_query_result_cached.json", "benchmarks_query_result_cached.json"):
            with self.subTest(file=name):
                self.assertTrue(_load(name, valid=True)["cached"])
        uncached = _load("prices_query_result_uncached.json", valid=False)
        self.assertFalse(uncached["cached"])
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/prices.query.result.schema.json",
                uncached,
            )

    def test_usage_summary_keeps_kinds_separate_and_unknown_null(self) -> None:
        totals = _load("usage_summary_result_unknown_total.json", valid=True)["totals"]
        kinds = [total["cost_kind"] for total in totals]
        self.assertEqual(len(kinds), len(set(kinds)))
        unknown = [total for total in totals if total["amount"] is None]
        self.assertTrue(unknown)
        for total in unknown:
            self.assertIsNone(total["currency"])
            self.assertGreaterEqual(total["record_count"], 0)

    def test_usage_total_unknown_currency_must_be_explicit_null(self) -> None:
        omitted = _load("usage_summary_total_missing_currency.json", valid=False)
        self.assertNotIn("currency", omitted["totals"][0])
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/usage.summary.result.schema.json",
                omitted,
            )

    def test_refresh_job_kinds_are_reserved_to_the_engine(self) -> None:
        ref = "contracts/engine.v1/vocabulary.schema.json#/definitions/job_kind"
        for kind in (
            "com.modeldeck.engine.prices.refresh",
            "com.modeldeck.engine.benchmarks.refresh",
        ):
            with self.subTest(kind=kind):
                validate_schema_ref(ref, kind)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, "com.example.plugin.prices.refresh")
        started = _load("prices_refresh_result_job.json", valid=True)
        self.assertEqual(started["job_kind"], "com.modeldeck.engine.prices.refresh")
        self.assertTrue(started["explicit_network"])

    def test_checkpoint_save_always_states_its_compare_and_swap_expectation(
        self,
    ) -> None:
        ref = "contracts/plugin.v1/broker/jobs.checkpoint.params.schema.json"
        first = _load("jobs_checkpoint_params_first_revision.json", valid=True)
        self.assertIsNone(first["expected_revision"])
        validate_schema_ref(ref, first)
        omitted = _load(
            "jobs_checkpoint_params_missing_expected_revision.json", valid=False
        )
        self.assertNotIn("expected_revision", omitted)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, omitted)

    def test_resume_result_reports_origin_revision_as_explicit_null(self) -> None:
        ref = "contracts/engine.v1/methods/jobs.resume.result.schema.json"
        accepted = _load("jobs_resume_result_without_checkpoint.json", valid=True)
        self.assertTrue(accepted["accepted"])
        self.assertIsNone(accepted["resumed_from_revision"])
        validate_schema_ref(ref, accepted)
        omitted = _load(
            "jobs_resume_result_missing_resumed_from_revision.json", valid=False
        )
        self.assertNotIn("resumed_from_revision", omitted)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, omitted)

    def test_jobs_get_resume_fields_are_optional_and_never_negative(self) -> None:
        ref = "contracts/engine.v1/methods/jobs.get.result.schema.json"
        resumable = _load("jobs_get_result_resumable_interrupted.json", valid=True)
        self.assertTrue(resumable["resumable"])
        self.assertEqual(resumable["resume_count"], 2)
        validate_schema_ref(ref, resumable)
        validate_schema_ref(
            ref,
            {
                "job_id": resumable["job_id"],
                "state": "interrupted",
            },
        )
        negative = _load("jobs_get_result_negative_resume_count.json", valid=False)
        self.assertEqual(negative["resume_count"], -1)
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, negative)

    def test_operation_contribution_may_declare_itself_resumable(self) -> None:
        ref = "contracts/plugin.v1/manifest.schema.json#/definitions/operation_contribution"
        contribution = {
            "id": "com.example.notebook.update",
            "input_schema": "schemas/update.input.json",
            "output_schema": "schemas/update.output.json",
            "effect": "write",
        }
        validate_schema_ref(ref, contribution)
        validate_schema_ref(ref, {**contribution, "resumable": True})
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(ref, {**contribution, "resumable": "yes"})

    def test_wire_types_roundtrip_preserves_json(self) -> None:
        cases = [
            ("jsonrpc_request.json", JsonRpcRequest.parse),
            ("run_request_minimal.json", RunRequest.parse),
            ("tool_call.json", ToolCall.parse),
            ("run_event_completed.json", RunEventRunCompleted.parse),
        ]
        for name, parser in cases:
            with self.subTest(file=name):
                raw = _load(name, valid=True)
                parsed = parser(raw)
                self.assertTrue(canonical_json_equal(raw, parsed.raw))
