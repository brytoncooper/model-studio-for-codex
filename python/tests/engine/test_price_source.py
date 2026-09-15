"""Offline tests for the price evidence adapter (python/src/model_deck/adapters/evidence/prices.py).

No test here touches the network: `fetch_prices` is always given a fake transport, and
`parse_prices`/`(de)serialize_price_snapshot` never accept one in the first place.
"""
import json
import unittest
from pathlib import Path

from model_deck.adapters.evidence.prices import (
    CURRENCY,
    PRICING_URL,
    SOURCE_ID,
    PriceFormatError,
    PriceRecord,
    PriceSnapshot,
    PriceSourceError,
    PriceTransportError,
    UnitPrices,
    deserialize_price_snapshot,
    fetch_prices,
    is_stale,
    parse_prices,
    serialize_price_snapshot,
)
from model_deck.adapters.evidence.provenance import SourceProvenance
from model_deck.engine.evidence import (
    PriceRecord as EnginePriceRecord,
    SourceProvenance as EngineSourceProvenance,
    estimate_cost,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evidence"
NOW = 1_757_900_000.0  # arbitrary fixed epoch so expectations are exact, not clock-dependent


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ParsePricesTests(unittest.TestCase):
    def test_parse_success_skips_rows_without_a_usable_id(self) -> None:
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        self.assertEqual(
            [record.provider_model_id for record in snapshot.records],
            ["openai/gpt-5", "some/unpriced-model"],
        )

    def test_priced_row_stays_priced_per_single_token(self) -> None:
        """`price_record.unit_prices` prices ONE unit, because that is what the frozen
        contract says and what `estimate_cost` multiplies by a raw token count. A $3/M
        model records 0.000003, not 3.0; scaling to per-million here would inflate every
        engine-side money figure by a factor of a million."""
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        priced = snapshot.records[0]
        self.assertEqual(priced.unit_prices.input_tokens, 0.000003)
        self.assertEqual(priced.unit_prices.output_tokens, 0.000015)
        self.assertEqual(priced.unit_prices.cached_tokens, 0.00000075)
        self.assertEqual(priced.currency, CURRENCY)

    def test_parsed_prices_estimate_a_realistic_dollar_figure(self) -> None:
        """The unit the adapter emits is the unit the engine spends. One million input
        tokens of a $3/M model must cost about $3, not $3,000,000."""
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        body = snapshot.records[0].to_wire()
        body.pop("provenance")
        engine_record = EnginePriceRecord.from_body(
            body,
            EngineSourceProvenance(
                source_id=SOURCE_ID, fetched_at="2026-09-15T00:00:00Z", stale=False
            ),
        )
        self.assertAlmostEqual(
            estimate_cost(engine_record, {"input_tokens": 1_000_000}), 3.00, places=6
        )
        self.assertAlmostEqual(
            estimate_cost(
                engine_record, {"input_tokens": 1_000_000, "output_tokens": 200_000}
            ),
            6.00,
            places=6,
        )

    def test_unknown_prices_stay_null_never_zero(self) -> None:
        """OpenRouter's -1 ("priced per request / dynamic") must not become a literal -1
        price, and must never be read as free (0)."""
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        unpriced = snapshot.records[1]
        self.assertIsNone(unpriced.unit_prices.input_tokens)
        self.assertIsNone(unpriced.unit_prices.output_tokens)
        self.assertIsNone(unpriced.unit_prices.cached_tokens)
        # unit_prices always carries all three keys even when every value is unknown.
        self.assertEqual(
            set(unpriced.unit_prices.to_wire().keys()),
            {"input_tokens", "output_tokens", "cached_tokens"},
        )

    def test_every_record_and_the_snapshot_provenance_matches_the_frozen_schema(self) -> None:
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        self.assertTrue(snapshot.records, "fixture must produce at least one record")
        for record in snapshot.records:
            validate_schema_ref(
                "engine.v1/vocabulary.schema.json#/definitions/price_record", record.to_wire()
            )
        validate_schema_ref(
            "engine.v1/vocabulary.schema.json#/definitions/source_provenance",
            snapshot.provenance.to_wire(),
        )

    def test_snapshot_provenance_identifies_the_openrouter_source(self) -> None:
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        self.assertEqual(snapshot.provenance.source_id, SOURCE_ID)
        self.assertEqual(snapshot.provenance.source_url, PRICING_URL)
        self.assertFalse(snapshot.provenance.stale)
        self.assertEqual(snapshot.provenance.fetched_at, "2025-09-15T01:33:20Z")
        # Every record shares the one fetch's provenance.
        for record in snapshot.records:
            self.assertEqual(record.provenance, snapshot.provenance)

    def test_malformed_payload_missing_data_key_is_rejected(self) -> None:
        with self.assertRaises(PriceFormatError):
            parse_prices(_load("prices_malformed.json"), NOW)

    def test_malformed_payload_wrong_top_level_type_is_rejected(self) -> None:
        with self.assertRaises(PriceFormatError):
            parse_prices(["not", "an", "object"], NOW)

    def test_data_not_a_list_is_rejected(self) -> None:
        with self.assertRaises(PriceFormatError):
            parse_prices({"data": {"not": "a list"}}, NOW)


class FetchPricesTests(unittest.TestCase):
    def test_fetch_uses_the_injected_transport_and_the_documented_url(self) -> None:
        seen_urls = []

        def fake_transport(url: str) -> bytes:
            seen_urls.append(url)
            return json.dumps(_load("prices_openrouter_sample.json")).encode("utf-8")

        snapshot = fetch_prices(fake_transport, NOW)
        self.assertEqual(seen_urls, [PRICING_URL])
        self.assertEqual(len(snapshot.records), 2)

    def test_transport_failure_surfaces_as_a_typed_error_with_no_partial_snapshot(self) -> None:
        def broken_transport(url: str) -> bytes:
            raise ConnectionError("simulated network failure")

        with self.assertRaises(PriceTransportError) as ctx:
            fetch_prices(broken_transport, NOW)
        # The typed error is the only thing returned; there is no snapshot object at all
        # to be "partial" -- the exception itself proves that.
        self.assertIsInstance(ctx.exception, PriceSourceError)

    def test_transport_returning_invalid_json_surfaces_as_a_typed_format_error(self) -> None:
        def garbage_transport(url: str) -> bytes:
            return b"{not-json"

        with self.assertRaises(PriceFormatError):
            fetch_prices(garbage_transport, NOW)


class StalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)

    def test_fresh_snapshot_is_not_stale(self) -> None:
        self.assertFalse(is_stale(self.snapshot, NOW, max_age=3600))

    def test_just_under_max_age_is_not_stale(self) -> None:
        self.assertFalse(is_stale(self.snapshot, NOW + 3599, max_age=3600))

    def test_at_max_age_boundary_is_stale(self) -> None:
        self.assertTrue(is_stale(self.snapshot, NOW + 3600, max_age=3600))

    def test_well_past_max_age_is_stale(self) -> None:
        self.assertTrue(is_stale(self.snapshot, NOW + 7200, max_age=3600))


class SerializeRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_records_and_provenance(self) -> None:
        original = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        text = serialize_price_snapshot(original)
        restored = deserialize_price_snapshot(text)
        self.assertEqual(restored, original)
        self.assertEqual(restored.provenance, original.provenance)
        self.assertEqual(
            [r.to_wire() for r in restored.records],
            [r.to_wire() for r in original.records],
        )

    def test_serialized_document_carries_a_top_level_fetched_at_and_provenance(self) -> None:
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        document = json.loads(serialize_price_snapshot(snapshot))
        self.assertEqual(document["fetched_at"], snapshot.provenance.fetched_at)
        self.assertEqual(document["provenance"]["source_id"], SOURCE_ID)
        self.assertIn("records", document)

    def test_round_trip_never_touches_the_network(self) -> None:
        """Nothing about serialize/deserialize accepts a transport callable at all; this
        test just documents that reading back a snapshot cannot fetch."""
        snapshot = parse_prices(_load("prices_openrouter_sample.json"), NOW)
        text = serialize_price_snapshot(snapshot)
        restored = deserialize_price_snapshot(text)  # no fetcher argument exists to pass
        self.assertEqual(restored, snapshot)

    def test_deserialize_rejects_a_document_missing_required_fields(self) -> None:
        with self.assertRaises(PriceFormatError):
            deserialize_price_snapshot(json.dumps({"provenance": {}}))

    def test_deserialize_rejects_invalid_json_text(self) -> None:
        with self.assertRaises(PriceFormatError):
            deserialize_price_snapshot("{not valid json")

    def test_deserialize_rejects_a_json_array_document(self) -> None:
        with self.assertRaises(PriceFormatError):
            deserialize_price_snapshot("[]")


class WireDataclassTests(unittest.TestCase):
    """Direct round trips of the small dataclasses, independent of the parser."""

    def test_unit_prices_wire_round_trip(self) -> None:
        prices = UnitPrices(input_tokens=1.5, output_tokens=None, cached_tokens=0.0)
        self.assertEqual(UnitPrices.from_wire(prices.to_wire()), prices)

    def test_price_record_wire_round_trip_with_optional_registration_id(self) -> None:
        provenance = SourceProvenance(
            source_id="ai.openrouter", fetched_at="2026-09-15T00:00:00Z", stale=True,
            source_url="https://openrouter.ai/api/v1/models", last_refresh_error="timeout",
        )
        record = PriceRecord(
            provider_model_id="vendor/model",
            currency=None,
            unit_prices=UnitPrices(None, None, None),
            provenance=provenance,
            registration_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        )
        wire = record.to_wire()
        self.assertEqual(wire["registration_id"], "6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        self.assertEqual(PriceRecord.from_wire(wire), record)
        validate_schema_ref("engine.v1/vocabulary.schema.json#/definitions/price_record", wire)

    def test_price_record_omits_registration_id_key_when_absent(self) -> None:
        provenance = SourceProvenance(source_id="ai.openrouter", fetched_at="2026-09-15T00:00:00Z", stale=False)
        record = PriceRecord(
            provider_model_id="vendor/model",
            currency="USD",
            unit_prices=UnitPrices(1.0, 2.0, 0.5),
            provenance=provenance,
        )
        self.assertNotIn("registration_id", record.to_wire())

    def test_price_snapshot_with_zero_records_still_validates_its_provenance(self) -> None:
        provenance = SourceProvenance(source_id=SOURCE_ID, fetched_at="2026-09-15T00:00:00Z", stale=True)
        snapshot = PriceSnapshot(records=(), provenance=provenance)
        self.assertEqual(snapshot.to_wire()["records"], [])
        validate_schema_ref(
            "engine.v1/vocabulary.schema.json#/definitions/source_provenance",
            snapshot.to_wire()["provenance"],
        )


if __name__ == "__main__":
    unittest.main()
