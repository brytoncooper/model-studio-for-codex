from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class V2ProviderSetupPackagingTests(unittest.TestCase):
    def test_builder_packages_existing_credential_helper_with_stable_identity(self) -> None:
        source = (REPOSITORY_ROOT / "scripts/v2/build.sh").read_text(encoding="utf-8")

        self.assertIn("OpenRouterCredentialHelper.swift", source)
        self.assertIn("Contents/Helpers/OpenRouterCredentialHelper", source)
        self.assertIn("com.cooper.codex-openrouter.credential-helper", source)

    def test_desktop_bridge_exposes_connector_mode(self) -> None:
        source = (REPOSITORY_ROOT / "scripts/v2/CodexDesktopBridge").read_text(encoding="utf-8")
        self.assertIn("--model-deck-connect", source)
        self.assertIn("model_deck.integrations.hosts.codex.desktop_connector", source)


if __name__ == "__main__":
    unittest.main()
