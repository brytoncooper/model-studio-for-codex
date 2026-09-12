from __future__ import annotations

import hashlib
import sys
import tomllib
import unittest
from pathlib import Path

from model_deck.integrations.hosts.codex.agent_renderer import (
    AGENT_MARKER,
    RenderError,
    RenderRequest,
    render_managed_agent,
)

ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000"
HELPER = "/Applications/Model Deck.app/Contents/MacOS/Model Deck"
INSTRUCTIONS = (
    "Complete only the bounded task assigned by the parent agent. Obey the parent's scope, "
    "ownership boundaries, fixed decisions, and verification requirements. Preserve other agents' "
    "changes. Report results and unresolved limits to the parent. Do not delegate to additional "
    "agents unless the parent explicitly asks you to do so."
)
OPENROUTER_BILLING = "Uses OpenRouter credits."


def endpoint_request(**overrides):
    fields = {
        "kind": "endpoint",
        "provider_model_id": "deepseek/deepseek-v4.1-flash",
        "display_name": "DeepSeek Flash",
        "endpoint_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "credential_account_id": ACCOUNT_ID,
        "token_helper_path": HELPER,
        "reasoning_effort": "default",
        "billing_description": OPENROUTER_BILLING,
    }
    fields.update(overrides)
    return RenderRequest(**fields)


def parsed(rendered):
    text = rendered.content.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == AGENT_MARKER
    return tomllib.loads(text)


class EndpointRendererTest(unittest.TestCase):
    def test_openrouter_full_document(self):
        rendered = render_managed_agent(endpoint_request())
        digest = hashlib.sha256(b"deepseek/deepseek-v4.1-flash").hexdigest()[:8]
        self.assertEqual(rendered.filename, f"openrouter_deepseek_deepseek_v4_1_flash_{digest}.toml")
        document = parsed(rendered)
        self.assertEqual(
            list(document.keys()),
            ["name", "description", "developer_instructions", "model",
             "model_reasoning_effort", "model_provider", "model_providers"],
        )
        self.assertEqual(
            document,
            {
                "name": f"openrouter_deepseek_deepseek_v4_1_flash_{digest}",
                "description": "Bounded task worker using deepseek/deepseek-v4.1-flash through OpenRouter. Uses OpenRouter credits.",
                "developer_instructions": INSTRUCTIONS,
                "model": "deepseek/deepseek-v4.1-flash",
                "model_reasoning_effort": "low",
                "model_provider": "openrouter-settings",
                "model_providers": {
                    "openrouter-settings": {
                        "name": "OpenRouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "wire_api": "responses",
                        "supports_websockets": False,
                        "auth": {
                            "command": HELPER,
                            "args": ["--token", ACCOUNT_ID],
                            "timeout_ms": 5000,
                            "refresh_interval_ms": 300000,
                        },
                    }
                },
            },
        )

    def test_keyless_local_endpoint_omits_auth(self):
        rendered = render_managed_agent(endpoint_request(
            provider_model_id="qwen-local",
            endpoint_name="Local Server",
            base_url="http://localhost:1234/v1",
            credential_account_id=None,
            billing_description="No API key; local server billing is managed separately.",
        ))
        digest = hashlib.sha256(b"qwen-local").hexdigest()[:8]
        self.assertEqual(rendered.filename, f"openrouter_qwen_local_{digest}.toml")
        document = parsed(rendered)
        provider = document["model_providers"]["openrouter-settings"]
        self.assertNotIn("auth", provider)
        self.assertEqual(provider["base_url"], "http://localhost:1234/v1")

    def test_cursor_endpoint(self):
        rendered = render_managed_agent(endpoint_request(
            provider_model_id="cursor/auto",
            endpoint_name="Cursor",
            base_url="https://api.cursor.com",
            billing_description="Routed through Cursor SDK by Model Deck.",
        ))
        digest = hashlib.sha256(b"cursor/auto").hexdigest()[:8]
        self.assertEqual(rendered.filename, f"openrouter_cursor_auto_{digest}.toml")
        document = parsed(rendered)
        self.assertIn("auth", document["model_providers"]["openrouter-settings"])

    def test_explicit_effort_preserved(self):
        rendered = render_managed_agent(endpoint_request(reasoning_effort="high"))
        self.assertEqual(parsed(rendered)["model_reasoning_effort"], "high")


class SubscriptionRendererTest(unittest.TestCase):
    def test_subscription_document(self):
        rendered = render_managed_agent(RenderRequest(
            kind="subscription",
            provider_model_id="gpt-5.6-sol",
            display_name="Sol",
        ))
        self.assertEqual(rendered.filename, "subscription_gpt_5_6_sol.toml")
        document = parsed(rendered)
        self.assertEqual(
            document,
            {
                "name": "subscription_gpt_5_6_sol",
                "description": "Bounded task worker using gpt-5.6-sol through the OpenAI subscription connection.",
                "developer_instructions": INSTRUCTIONS,
                "model": "gpt-5.6-sol",
                "model_provider": "openai",
                "model_reasoning_effort": "low",
            },
        )
        self.assertNotIn("model_providers", document)


class RendererValidationTest(unittest.TestCase):
    def test_rejects_reserved_prefix(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(provider_model_id="gpt-4o-mini"))

    def test_rejects_openai_route(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(provider_model_id="openai/gpt-4o"))

    def test_rejects_bad_account(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(credential_account_id="not-a-uuid"))

    def test_rejects_relative_helper(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(token_helper_path="relative/helper"))

    def test_rejects_effort(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(reasoning_effort="ultra"))

    def test_rejects_public_http(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(
                provider_model_id="qwen-local",
                base_url="http://example.com/v1",
                credential_account_id=None,
            ))

    def test_rejects_missing_billing(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(billing_description=None))

    def test_rejects_cursor_model_off_cursor(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(provider_model_id="cursor/auto"))

    def test_rejects_openrouter_without_slash(self):
        with self.assertRaises(RenderError):
            render_managed_agent(endpoint_request(provider_model_id="bare-model"))

    def test_rejects_unsupported_subscription_model(self):
        with self.assertRaises(RenderError):
            render_managed_agent(RenderRequest(
                kind="subscription", provider_model_id="deepseek/deepseek-v4.1-flash", display_name="X"))

    def test_rejects_subscription_with_endpoint_fields(self):
        with self.assertRaises(RenderError):
            render_managed_agent(RenderRequest(
                kind="subscription", provider_model_id="gpt-5.6-sol", display_name="X",
                endpoint_name="OpenRouter"))

    def test_rejects_unknown_kind(self):
        with self.assertRaises(RenderError):
            render_managed_agent(RenderRequest(
                kind="other", provider_model_id="m", display_name="X"))


class LegacyParityTest(unittest.TestCase):
    def test_constants_and_provider_table_match_legacy(self):
        root = Path(__file__).resolve().parents[5]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            import codex_settings
        except ImportError:
            self.skipTest("legacy codex_settings unavailable")
        self.assertEqual(AGENT_MARKER, codex_settings.AGENT_MARKER)
        legacy_provider = codex_settings.provider_table(ACCOUNT_ID, HELPER, "https://openrouter.ai/api/v1", "OpenRouter")
        import tomlkit as legacy_tomlkit
        document = legacy_tomlkit.document()
        document["model_providers"] = legacy_tomlkit.table()
        document["model_providers"]["openrouter-settings"] = legacy_provider
        legacy_text = legacy_tomlkit.dumps(document)
        rendered = render_managed_agent(endpoint_request())
        self.assertEqual(
            tomllib.loads(legacy_text)["model_providers"],
            parsed(rendered)["model_providers"],
        )


if __name__ == "__main__":
    unittest.main()
