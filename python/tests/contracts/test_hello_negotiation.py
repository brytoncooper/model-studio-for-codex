import unittest

from model_deck_contracts.negotiation import (
    ApiVersion,
    NegotiationFailure,
    evaluate_api_version,
    evaluate_hello_negotiation,
    evaluate_required_capabilities,
    rendezvous_matches,
)


class HelloNegotiationTests(unittest.TestCase):
    def test_incompatible_major(self) -> None:
        result = evaluate_api_version(ApiVersion(2, 0), ApiVersion(1, 0))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, NegotiationFailure.INCOMPATIBLE_MAJOR)

    def test_insufficient_minor(self) -> None:
        result = evaluate_api_version(ApiVersion(1, 2), ApiVersion(1, 1))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, NegotiationFailure.INSUFFICIENT_MINOR)

    def test_compatible_minor(self) -> None:
        result = evaluate_api_version(ApiVersion(1, 0), ApiVersion(1, 3))
        self.assertTrue(result.ok)

    def test_missing_required_capability_when_unknown(self) -> None:
        result = evaluate_required_capabilities(
            ["tools"],
            {"tools": "unknown"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, NegotiationFailure.MISSING_CAPABILITIES)
        self.assertEqual(result.missing_capabilities, ("tools",))

    def test_missing_required_capability_when_unsupported(self) -> None:
        result = evaluate_required_capabilities(
            ["compaction"],
            {"compaction": "unsupported"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.missing_capabilities, ("compaction",))

    def test_required_capability_satisfied(self) -> None:
        result = evaluate_required_capabilities(
            ["tools", "resume"],
            {"tools": "supported", "resume": "supported"},
        )
        self.assertTrue(result.ok)

    def test_combined_negotiation_failure_major(self) -> None:
        result = evaluate_hello_negotiation(
            ApiVersion(2, 0),
            ApiVersion(1, 0),
            ["tools"],
            {"tools": "supported"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, NegotiationFailure.INCOMPATIBLE_MAJOR)

    def test_combined_negotiation_missing_capability(self) -> None:
        result = evaluate_hello_negotiation(
            ApiVersion(1, 0),
            ApiVersion(1, 0),
            ["tools"],
            {"tools": "unknown"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, NegotiationFailure.MISSING_CAPABILITIES)

    def test_rendezvous_mismatch_blocks_second_hello(self) -> None:
        self.assertFalse(
            rendezvous_matches(
                "550e8400-e29b-41d4-a716-446655440000",
                "nonce-a",
                "550e8400-e29b-41d4-a716-446655440000",
                "nonce-b",
            )
        )

    def test_rendezvous_match(self) -> None:
        self.assertTrue(
            rendezvous_matches(
                "550e8400-e29b-41d4-a716-446655440000",
                "nonce-a",
                "550e8400-e29b-41d4-a716-446655440000",
                "nonce-a",
            )
        )


if __name__ == "__main__":
    unittest.main()
