import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import provider_usage as usage


class UsageParsingTests(unittest.TestCase):
    def test_usage_summary_and_latest_available_dates(self):
        buckets = [{"startDate": f"2026-06-{day:02d}", "tokens": day} for day in range(20, 0, -1)]
        result = usage.parse_openai({}, {"summary": {"peakDailyTokens": 0, "currentStreakDays": 2,
                                                      "longestStreakDays": 4}, "dailyUsageBuckets": buckets})
        self.assertEqual(result["summary"], {"peak_daily_tokens": 0, "current_streak_days": 2, "longest_streak_days": 4})
        self.assertEqual(len(result["daily_usage"]), 14)
        self.assertEqual(result["daily_usage"][0], {"date": "2026-06-07", "tokens": 7})
        self.assertEqual(result["daily_usage"][-1]["date"], "2026-06-20")

    def test_usage_missing_values_and_invalid_numbers(self):
        result = usage.parse_openai({}, {"summary": {"peakDailyTokens": True, "currentStreakDays": -1,
                                                      "longestStreakDays": float("nan")},
                                          "dailyUsageBuckets": [{"startDate": "2026-06-18", "tokens": 0},
                                                                {"startDate": "2026-06-20", "tokens": -1}]})
        self.assertTrue(all(value is None for value in result["summary"].values()))
        self.assertEqual(result["daily_usage"], [{"date": "2026-06-18", "tokens": 0}, {"date": "2026-06-20", "tokens": None}])
        self.assertIsNone(usage.parse_openai({})["daily_usage"])
        self.assertIsNone(usage.parse_openai({}, {"dailyUsageBuckets": None})["daily_usage"])

    def test_invalid_and_duplicate_dates_reject_history(self):
        for buckets in ([{"startDate": "2026-02-30", "tokens": 1}],
                        [{"startDate": "20260618", "tokens": 1}],
                        [{"startDate": "2026-06-18", "tokens": 1}, {"startDate": "2026-06-18", "tokens": 2}]):
            with self.subTest(buckets=buckets):
                result = usage.parse_openai({"rateLimits": {"primary": {"usedPercent": 0}}}, {"dailyUsageBuckets": buckets})
                self.assertTrue(result["ok"])
                self.assertIsNone(result["daily_usage"])
                self.assertIn("usage_error", result)

    def test_pool_identity_survives_display_name_change(self):
        limits = {"rateLimitsByLimitId": {"codex": {"limitId": "stable", "limitName": "First", "primary": {}}}}
        first = usage.parse_openai(limits)["windows"][0]
        limits["rateLimitsByLimitId"]["codex"]["limitName"] = "Second"
        second = usage.parse_openai(limits)["windows"][0]
        self.assertEqual(first["pool_id"], second["pool_id"])
        self.assertEqual(second["pool_name"], "Second")
        self.assertEqual(second["window_kind"], "primary")

    def test_openrouter_reset_and_byok_remain_separate(self):
        result = usage.parse_openrouter({"data": {"usage": 2, "byok_usage": 5,
                                                  "limit_reset": "monthly", "include_byok_in_limit": False}})
        self.assertEqual(result["usage_total"], 2)
        self.assertEqual(result["byok_usage"], 5)
        self.assertEqual(result["limit_reset"], "monthly")
        self.assertIs(result["include_byok_in_limit"], False)
        missing = usage.parse_openrouter({"data": {"limit_reset": "yearly", "include_byok_in_limit": 1}})
        self.assertIsNone(missing["limit_reset"])
        self.assertIsNone(missing["include_byok_in_limit"])
        self.assertIsNone(missing["byok_usage"])

    def test_multiple_buckets_preferred_over_legacy(self):
        result = usage.parse_openai({"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 123}},
            "spark": {"secondary": {"usedPercent": 0, "windowDurationMins": 10080}}},
            "rateLimits": {"primary": {"usedPercent": 99}}}, {"summary": {"lifetimeTokens": 42}})
        self.assertTrue(result["ok"])
        self.assertEqual([window["used_percent"] for window in result["windows"]], [12, 0])
        self.assertIsNone(result["windows"][1]["resets_at"])
        self.assertEqual(result["lifetime_tokens"], 42)

    def test_legacy_and_missing_values(self):
        result = usage.parse_openai({"rateLimits": {"primary": {"usedPercent": None}}})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertIsNone(result["lifetime_tokens"])
        self.assertFalse(usage.parse_openai({})["ok"])

    def test_openrouter_numbers_do_not_default_to_zero(self):
        result = usage.parse_openrouter({"data": {"usage": 3, "usage_daily": 0,
                                                  "usage_weekly": "2", "limit": None}})
        self.assertEqual(result["usage_total"], 3)
        self.assertEqual(result["usage_daily"], 0)
        self.assertIsNone(result["usage_weekly"])
        self.assertIsNone(result["limit_remaining"])
        self.assertIsNone(result["limit"])
        self.assertIsNone(usage.number(True))
        self.assertIsNone(usage.number(float("nan")))
        self.assertIsNone(usage.number(float("inf")))
        self.assertFalse(usage.parse_openrouter({})["ok"])

    def test_limit_known_distinguishes_missing_unlimited_and_numeric(self):
        for data, known, limit in (({}, False, None), ({"limit": None}, True, None),
                                   ({"limit": 0}, True, 0), ({"limit": 25.5}, True, 25.5),
                                   ({"limit": "25"}, False, None), ({"limit": True}, False, None),
                                   ({"limit": float("nan")}, False, None)):
            with self.subTest(data=data):
                parsed = usage.parse_openrouter({"data": data})
                self.assertEqual(parsed["limit_known"], known)
                self.assertEqual(parsed["limit"], limit)
        self.assertFalse(usage.parse_openrouter(None)["limit_known"])

    @patch.object(usage, "NativeUsageClient")
    def test_optional_usage_failure_preserves_valid_limits(self, client_type):
        client = client_type.return_value
        client.request.side_effect = [{}, {"rateLimits": {"primary": {"usedPercent": 20}}}, ValueError("secret")]
        result = usage.collect_openai()
        self.assertTrue(result["ok"])
        self.assertEqual(result["windows"][0]["used_percent"], 20)
        self.assertIsNone(result["lifetime_tokens"])
        client.close.assert_called_once()
        methods = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(methods, ["initialize", "account/rateLimits/read", "account/usage/read"])

    @patch.object(usage, "NativeUsageClient", side_effect=OSError("secret error"))
    def test_native_process_failure_sanitized(self, client_type):
        result = usage.collect_openai()
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", json.dumps(result))

    @patch.object(usage, "read_key", side_effect=TimeoutError("private"))
    def test_helper_failure_sanitized(self, read_key):
        result = usage.collect_openrouter("an-account")
        self.assertFalse(result["ok"])
        self.assertNotIn("private", json.dumps(result))

    @patch.object(usage.subprocess, "Popen", side_effect=OSError("secret"))
    def test_helper_spawn_failure_sanitized(self, process):
        result = usage.collect_openrouter("12345678-1234-1234-1234-123456789abc")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", json.dumps(result))

    @patch.object(usage, "read_key", return_value="test-secret")
    @patch.object(usage, "bounded_openrouter_fetch")
    def test_network_errors_sanitized(self, fetch, read_key):
        for error in (urllib.error.HTTPError(usage.KEY_ENDPOINT, 401, "test-secret", {}, None),
                      urllib.error.HTTPError(usage.KEY_ENDPOINT, 302, "test-secret", {}, None),
                      urllib.error.URLError("test-secret")):
            fetch.side_effect = error
            result = usage.collect_openrouter("account")
            self.assertFalse(result["ok"])
            self.assertNotIn("test-secret", json.dumps(result))

    @patch.object(usage.urllib.request, "build_opener")
    def test_fetch_uses_fixed_endpoint_and_no_redirects(self, build_opener):
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b'{"data":{"usage":1}}'
        self.assertEqual(usage.fetch_openrouter("test-secret")["usage_total"], 1)
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, usage.KEY_ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertIsInstance(build_opener.call_args.args[0], usage.NoRedirects)
        self.assertIsNone(usage.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example"))

    @patch.object(usage, "collect_openai", return_value={"ok": False, "windows": [], "lifetime_tokens": None})
    @patch.object(usage, "collect_openrouter", return_value={"ok": True, "usage_total": 0})
    def test_independent_provider_results(self, openrouter, openai):
        result = usage.collect_usage("")
        self.assertFalse(result["openai"]["ok"])
        self.assertTrue(result["openrouter"]["ok"])
        self.assertTrue(result["fetched_at"].endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
