from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from model_deck.engine.connections.ports import ConnectionRecord
from model_deck.engine.model_library.ports import RegisteredModelRecord
from model_deck.integrations.hosts.codex.legacy_models import AGENT_MARKER
from model_deck.integrations.hosts.codex.migration_preview import (
    preview_legacy_import_from_fixture_root,
)

ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000"
MODEL_ID = "deepseek/deepseek-v4.1-flash"
ROLE = "openrouter_deepseek_flash"
SECRET_SENTINEL = "SUPER-SECRET-SENTINEL-VALUE"


def _managed_agent_toml(*, role: str = ROLE, model: str = MODEL_ID, account: str = ACCOUNT_ID) -> str:
    return (
        f"{AGENT_MARKER}\n"
        f'name = "{role}"\n'
        f'model = "{model}"\n'
        'model_provider = "openrouter-settings"\n'
        "[model_providers.openrouter-settings]\n"
        'name = "OpenRouter"\n'
        'base_url = "https://openrouter.ai/api/v1"\n'
        'wire_api = "responses"\n'
        "supports_websockets = false\n"
        "[model_providers.openrouter-settings.auth]\n"
        'command = "/Applications/Model Deck.app/Contents/MacOS/Model Deck"\n'
        f'args = ["--token", "{account}"]\n'
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 300000\n"
    )



LOCAL_BASE = "http://localhost:1234/v1"
KEYLESS_ACCOUNT_ID = "660e8400-e29b-41d4-a716-446655440001"
CURSOR_ACCOUNT_ID = "770e8400-e29b-41d4-a716-446655440002"


def _keyless_local_agent_toml(*, role: str, model: str = MODEL_ID) -> str:
    return (
        f"{AGENT_MARKER}\n"
        f'name = "{role}"\n'
        f'model = "{model}"\n'
        'model_provider = "openrouter-settings"\n'
        "[model_providers.openrouter-settings]\n"
        'name = "LM Studio"\n'
        f'base_url = "{LOCAL_BASE}"\n'
        'wire_api = "responses"\n'
        "supports_websockets = false\n"
    )


def _cursor_agent_toml(*, role: str, account: str = CURSOR_ACCOUNT_ID) -> str:
    return (
        f"{AGENT_MARKER}\n"
        f'name = "{role}"\n'
        'model = "cursor/composer-2.5"\n'
        'model_provider = "openrouter-settings"\n'
        "[model_providers.openrouter-settings]\n"
        'name = "Cursor"\n'
        'base_url = "https://api.cursor.com"\n'
        'wire_api = "responses"\n'
        "supports_websockets = false\n"
        "[model_providers.openrouter-settings.auth]\n"
        'command = "/Applications/Model Deck.app/Contents/MacOS/Model Deck"\n'
        f'args = ["--token", "{account}"]\n'
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 300000\n"
    )

def _write_valid_fixture(root: Path) -> None:
    (root / "preferences.json").write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": [
                    {
                        "id": ACCOUNT_ID,
                        "name": "OpenRouter",
                        "baseURL": "https://openrouter.ai/api/v1",
                        "wire": "responses",
                        "hasKey": True,
                    }
                ],
                "models": [MODEL_ID],
                "selectedAccount": ACCOUNT_ID,
                "selectedModel": MODEL_ID,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "endpoints.json").write_text(
        json.dumps({ACCOUNT_ID: {"name": "OpenRouter", "wire": "responses"}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "display-names.json").write_text(
        json.dumps({MODEL_ID: "DeepSeek Flash"}, indent=2) + "\n",
        encoding="utf-8",
    )
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{ROLE}.toml").write_text(_managed_agent_toml(), encoding="utf-8")


class FakeConnectionRepository:
    def __init__(self, rows: list[ConnectionRecord]) -> None:
        self._rows = rows

    def list_connections(self) -> list[ConnectionRecord]:
        return list(self._rows)


class FakeModelRepository:
    def __init__(self, rows: list[RegisteredModelRecord]) -> None:
        self._rows = rows

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        if connection_id is None:
            return list(self._rows)
        return [row for row in self._rows if row.connection_id == connection_id]


class MigrationPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _preview(self, root: Path | None = None, *, connections=None, models=None):
        return preview_legacy_import_from_fixture_root(
            root or self.root,
            existing_connections=connections or FakeConnectionRepository([]),
            existing_models=models or FakeModelRepository([]),
        )

    def test_deterministic_repeat_preview(self) -> None:
        _write_valid_fixture(self.root)
        first = self._preview().to_sortable_dict()
        second = self._preview().to_sortable_dict()
        self.assertEqual(first, second)

    def test_source_bytes_preserved(self) -> None:
        _write_valid_fixture(self.root)
        before = {
            path: path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self._preview()
        after = {
            path: path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(before, after)

    def test_source_hashes_reported(self) -> None:
        _write_valid_fixture(self.root)
        plan = self._preview()
        preferences = next(row for row in plan.sources if row.path == "preferences.json")
        expected = hashlib.sha256((self.root / "preferences.json").read_bytes()).hexdigest()
        self.assertEqual(preferences.sha256, expected)

    def test_valid_managed_match_import_ready(self) -> None:
        _write_valid_fixture(self.root)
        plan = self._preview()
        self.assertEqual(len(plan.import_ready_models), 1)
        self.assertEqual(len(plan.import_ready_connections), 1)
        self.assertEqual(plan.import_ready_models[0].provider_model_id, MODEL_ID)

    def test_missing_preferences_source(self) -> None:
        _write_valid_fixture(self.root)
        (self.root / "preferences.json").unlink()
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "missing")

    def test_version_and_unversioned_reporting(self) -> None:
        _write_valid_fixture(self.root)
        plan = self._preview()
        pref = next(row for row in plan.sources if row.path == "preferences.json")
        agent = next(row for row in plan.sources if row.path.startswith("agents/"))
        self.assertEqual(pref.store_version, 2)
        self.assertEqual(agent.store_version, 1)

    def test_malformed_json(self) -> None:
        _write_valid_fixture(self.root)
        path = self.root / "preferences.json"
        path.write_text("{not json", encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "malformed")

    def test_malformed_managed_toml(self) -> None:
        _write_valid_fixture(self.root)
        agent_path = self.root / "agents" / f"{ROLE}.toml"
        agent_path.write_text(f"{AGENT_MARKER}\nname = \"{ROLE}\"\n[badge", encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path.endswith(f"{ROLE}.toml"))
        self.assertEqual(row.classification, "malformed")

    def test_foreign_toml(self) -> None:
        _write_valid_fixture(self.root)
        (self.root / "agents" / "custom.toml").write_text("name = 'x'\n", encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path == "agents/custom.toml")
        self.assertEqual(row.classification, "foreign")

    def test_symlink_file_and_agents_directory(self) -> None:
        _write_valid_fixture(self.root)
        agent = self.root / "agents" / f"{ROLE}.toml"
        agent.unlink()
        os.symlink(self.root / "display-names.json", agent)
        file_plan = self._preview()
        file_paths = {row.path for row in file_plan.sources if row.classification == "symlink"}
        self.assertIn(f"agents/{ROLE}.toml", file_paths)

        agents = self.root / "agents"
        agents.rename(self.root / "agents_real")
        os.symlink(self.root / "agents_real", agents)
        directory_plan = self._preview()
        directory_paths = {row.path for row in directory_plan.sources if row.classification == "symlink"}
        self.assertIn("agents", directory_paths)

    def test_secret_sentinel_absent_from_plan(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["api_key"] = SECRET_SENTINEL
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        plan = self._preview()
        blob = json.dumps(plan.to_sortable_dict())
        self.assertNotIn(SECRET_SENTINEL, blob)
        row = next(item for item in plan.sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "malformed")

    def test_endpoint_drift(self) -> None:
        _write_valid_fixture(self.root)
        endpoints = {ACCOUNT_ID: {"name": "OpenRouter", "base_url": "https://example.com/v1", "wire": "responses"}}
        (self.root / "endpoints.json").write_text(json.dumps(endpoints), encoding="utf-8")
        row = next(item for item in self._preview().proposed_connections if item.connection_id)
        self.assertEqual(row.classification, "drift")

    def test_duplicate_model_collision(self) -> None:
        _write_valid_fixture(self.root)
        duplicate_role = "openrouter_deepseek_flash_copy"
        (self.root / "agents" / f"{duplicate_role}.toml").write_text(
            _managed_agent_toml(role=duplicate_role),
            encoding="utf-8",
        )
        plan = self._preview()
        collisions = [row for row in plan.proposed_models if row.classification == "collision"]
        self.assertEqual(len(collisions), 2)
        self.assertEqual(len(plan.import_ready_models), 0)


    def test_orphan_managed_account(self) -> None:
        _write_valid_fixture(self.root)
        missing = "00000000-0000-4000-8000-000000000001"
        role = "openrouter_orphan"
        orphan_model = "qwen/qwen3.8-27b"
        (self.root / "agents" / f"{role}.toml").write_text(
            _managed_agent_toml(role=role, model=orphan_model, account=missing),
            encoding="utf-8",
        )
        rows = [item for item in self._preview().proposed_models if item.provider_model_id == orphan_model]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].classification, "orphan")

    def test_existing_engine_drift(self) -> None:
        _write_valid_fixture(self.root)
        preview = self._preview()
        existing_model = RegisteredModelRecord(
            registration_id=preview.proposed_models[0].registration_id,
            provider_model_id=preview.proposed_models[0].provider_model_id,
            connection_id=preview.proposed_models[0].connection_id,
            display_name="Different Name",
            revision=4,
        )
        plan = self._preview(models=FakeModelRepository([existing_model]))
        self.assertEqual(plan.proposed_models[0].classification, "drift")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_architecture_import_surface(self) -> None:
        package_root = Path(__file__).resolve().parents[4] / "src/model_deck/integrations/hosts/codex/migration_preview"
        forbidden = ("model_deck.adapters", "model_deck.bootstrap", "model_deck.integrations.hosts.codex.legacy.")
        preview_text = (package_root / "preview.py").read_text(encoding="utf-8")
        self.assertIn("model_deck.engine.connections.ports", preview_text)
        self.assertIn("model_deck.engine.model_library.ports", preview_text)
        for path in package_root.glob("*.py"):
            module_text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, module_text)
        constants_text = (package_root / "constants.py").read_text(encoding="utf-8")
        self.assertIn("legacy_models", constants_text)



    def test_empty_saved_preferences_defaults_shape(self) -> None:
        (self.root / "preferences.json").write_text(
            json.dumps(
                {
                    "accounts": [],
                    "models": [],
                    "selectedAccount": "",
                    "selectedModel": "openai/gpt-6-astra",
                }
            ),
            encoding="utf-8",
        )
        (self.root / "endpoints.json").write_text("{}", encoding="utf-8")
        (self.root / "display-names.json").write_text("{}", encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "managed_match")

    def test_keyless_url_endpoint_and_authless_agent_ready(self) -> None:
        role = "openrouter_local_lm"
        (self.root / "preferences.json").write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": KEYLESS_ACCOUNT_ID,
                            "name": "LM Studio",
                            "baseURL": LOCAL_BASE,
                            "wire": "chat",
                            "hasKey": False,
                        }
                    ],
                    "models": [MODEL_ID],
                    "selectedAccount": "",
                    "selectedModel": MODEL_ID,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "endpoints.json").write_text(
            json.dumps(
                {
                    LOCAL_BASE: {
                        "name": "LM Studio",
                        "base_url": LOCAL_BASE,
                        "wire": "chat",
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.root / "display-names.json").write_text(json.dumps({MODEL_ID: "DeepSeek Flash"}), encoding="utf-8")
        agents = self.root / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{role}.toml").write_text(_keyless_local_agent_toml(role=role), encoding="utf-8")
        plan = self._preview()
        self.assertEqual(len(plan.import_ready_connections), 1)
        self.assertEqual(len(plan.import_ready_models), 1)

    def test_ambiguous_keyless_accounts_same_url_collision(self) -> None:
        role = "openrouter_local_ambiguous"
        second = "660e8400-e29b-41d4-a716-446655440099"
        (self.root / "preferences.json").write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": KEYLESS_ACCOUNT_ID,
                            "name": "LM A",
                            "baseURL": LOCAL_BASE,
                            "hasKey": False,
                        },
                        {
                            "id": second,
                            "name": "LM B",
                            "baseURL": LOCAL_BASE,
                            "hasKey": False,
                        },
                    ],
                    "models": [MODEL_ID],
                    "selectedAccount": "",
                    "selectedModel": MODEL_ID,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "endpoints.json").write_text(
            json.dumps({LOCAL_BASE: {"name": "LM Studio", "base_url": LOCAL_BASE, "wire": "chat"}}),
            encoding="utf-8",
        )
        (self.root / "display-names.json").write_text("{}", encoding="utf-8")
        agents = self.root / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{role}.toml").write_text(_keyless_local_agent_toml(role=role), encoding="utf-8")
        plan = self._preview()
        row = next(item for item in plan.proposed_models if item.provider_model_id == MODEL_ID)
        self.assertEqual(row.classification, "collision")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_cursor_preference_endpoint_and_agent_ready(self) -> None:
        role = "openrouter_cursor_ready"
        model = "cursor/composer-2.5"
        (self.root / "preferences.json").write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": CURSOR_ACCOUNT_ID,
                            "name": "Cursor",
                            "baseURL": "https://api.cursor.com",
                            "wire": "cursor",
                            "hasKey": True,
                        }
                    ],
                    "models": [model],
                    "selectedAccount": CURSOR_ACCOUNT_ID,
                    "selectedModel": model,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "endpoints.json").write_text(
            json.dumps(
                {
                    CURSOR_ACCOUNT_ID: {
                        "name": "Cursor",
                        "base_url": "https://api.cursor.com",
                        "wire": "cursor",
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.root / "display-names.json").write_text(json.dumps({model: "Composer"}), encoding="utf-8")
        agents = self.root / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{role}.toml").write_text(_cursor_agent_toml(role=role), encoding="utf-8")
        plan = self._preview()
        self.assertEqual(len(plan.import_ready_connections), 1)
        self.assertEqual(len(plan.import_ready_models), 1)

    def test_non_cursor_chat_wire_ready(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["accounts"][0]["wire"] = "chat"
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        endpoints = {ACCOUNT_ID: {"name": "OpenRouter", "wire": "chat"}}
        (self.root / "endpoints.json").write_text(json.dumps(endpoints), encoding="utf-8")
        plan = self._preview()
        self.assertEqual(plan.proposed_models[0].classification, "managed_match")
        self.assertEqual(len(plan.import_ready_models), 1)

    def test_existing_connection_drift_blocks_dependent_model(self) -> None:
        _write_valid_fixture(self.root)
        preview = self._preview()
        existing = ConnectionRecord(
            connection_id=preview.proposed_connections[0].connection_id,
            provider_id="com.other.provider",
            revision=3,
            endpoint_config_ref="ref:endpoint.other",
            credential_ref="ref:credential.other",
        )
        plan = self._preview(connections=FakeConnectionRepository([existing]))
        self.assertEqual(plan.proposed_connections[0].classification, "drift")
        self.assertEqual(plan.proposed_models[0].classification, "drift")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_fixture_root_does_not_expand_tilde(self) -> None:
        tilde_dir = self.root / "~"
        tilde_dir.mkdir(parents=True, exist_ok=True)
        _write_valid_fixture(tilde_dir)
        plan = self._preview(tilde_dir)
        self.assertEqual(len(plan.import_ready_models), 1)

    def test_bool_store_version_not_integer(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["version"] = True
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.store_version, "unversioned")
        self.assertNotEqual(row.store_version, True)

    def test_secret_field_does_not_inspect_nested_value(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["apiKey"] = {"nested": "token"}
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "malformed")
        self.assertEqual(row.detail, "secret field name: api_key")

    def test_deterministic_serialization_across_fixture_roots(self) -> None:
        _write_valid_fixture(self.root)
        with tempfile.TemporaryDirectory() as second_tempdir:
            second_root = Path(second_tempdir)
            _write_valid_fixture(second_root)
            first = json.dumps(self._preview().to_sortable_dict(), sort_keys=True)
            second = json.dumps(self._preview(second_root).to_sortable_dict(), sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["fixture_root"], ".")

    def test_malformed_preferences_shape(self) -> None:
        _write_valid_fixture(self.root)
        (self.root / "preferences.json").write_text(
            json.dumps({"accounts": "not-a-list", "models": []}),
            encoding="utf-8",
        )
        row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(row.classification, "malformed")
        self.assertEqual(row.detail, "preferences accounts invalid")

    def test_malformed_bracketed_url_is_classified_in_json_and_toml(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["accounts"][0]["baseURL"] = "https://["
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        json_row = next(item for item in self._preview().sources if item.path == "preferences.json")
        self.assertEqual(json_row.classification, "malformed")
        self.assertEqual(json_row.detail, "preferences account baseURL invalid")

        _write_valid_fixture(self.root)
        agent_path = self.root / "agents" / f"{ROLE}.toml"
        malformed_agent = _managed_agent_toml().replace(
            'base_url = "https://openrouter.ai/api/v1"',
            'base_url = "https://["',
        )
        agent_path.write_text(malformed_agent, encoding="utf-8")
        toml_row = next(item for item in self._preview().sources if item.path == f"agents/{ROLE}.toml")
        self.assertEqual(toml_row.classification, "malformed")
        self.assertEqual(toml_row.detail, "managed provider base_url invalid")

    def test_fixture_root_and_ancestor_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as target_tempdir, tempfile.TemporaryDirectory() as link_tempdir:
            target_root = Path(target_tempdir)
            _write_valid_fixture(target_root)
            root_link = Path(link_tempdir) / "fixture-link"
            os.symlink(target_root, root_link)
            with self.assertRaisesRegex(ValueError, "cannot contain a symlink"):
                self._preview(root_link)

            ancestor_link = Path(link_tempdir) / "ancestor-link"
            os.symlink(target_root.parent, ancestor_link)
            nested_through_link = ancestor_link / target_root.name
            with self.assertRaisesRegex(ValueError, "cannot contain a symlink"):
                self._preview(nested_through_link)

    def test_secret_field_name_normalizes_camel_case_and_punctuation(self) -> None:
        for field_name in ("apiKey", "api-key", "API.KEY"):
            with self.subTest(field_name=field_name):
                _write_valid_fixture(self.root)
                payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
                payload[field_name] = SECRET_SENTINEL
                (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
                plan = self._preview()
                serialized = json.dumps(plan.to_sortable_dict(), sort_keys=True)
                self.assertNotIn(SECRET_SENTINEL, serialized)
                row = next(item for item in plan.sources if item.path == "preferences.json")
                self.assertEqual(row.classification, "malformed")
                self.assertEqual(row.detail, "secret field name: api_key")

    def test_duplicate_account_id_collision(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["accounts"].append(dict(payload["accounts"][0]))
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        plan = self._preview()
        self.assertEqual(len(plan.proposed_connections), 1)
        self.assertEqual(plan.proposed_connections[0].classification, "collision")
        self.assertEqual(plan.proposed_models[0].classification, "collision")
        self.assertEqual(len(plan.import_ready_connections), 0)
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_preferences_model_list_drift(self) -> None:
        _write_valid_fixture(self.root)
        payload = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        payload["models"] = []
        (self.root / "preferences.json").write_text(json.dumps(payload), encoding="utf-8")
        plan = self._preview()
        self.assertEqual(plan.proposed_models[0].classification, "drift")
        self.assertEqual(plan.proposed_models[0].detail, "managed agent model absent from preferences models")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_matching_existing_connection_does_not_clear_source_drift(self) -> None:
        _write_valid_fixture(self.root)
        endpoints = {
            ACCOUNT_ID: {
                "name": "OpenRouter",
                "base_url": "https://example.com/v1",
                "wire": "responses",
            }
        }
        (self.root / "endpoints.json").write_text(json.dumps(endpoints), encoding="utf-8")
        first = self._preview()
        proposed = first.proposed_connections[0]
        matching = ConnectionRecord(
            connection_id=proposed.connection_id,
            provider_id=proposed.provider_id,
            revision=9,
            endpoint_config_ref=proposed.endpoint_config_ref,
            credential_ref=proposed.credential_ref,
        )
        plan = self._preview(connections=FakeConnectionRepository([matching]))
        self.assertEqual(plan.proposed_connections[0].classification, "drift")
        self.assertEqual(len(plan.import_ready_connections), 0)

    def test_existing_model_with_conflicting_connection_is_collision(self) -> None:
        _write_valid_fixture(self.root)
        initial = self._preview()
        proposed = initial.proposed_models[0]
        existing = RegisteredModelRecord(
            registration_id="00000000-0000-4000-8000-000000000099",
            provider_model_id=proposed.provider_model_id,
            connection_id="00000000-0000-4000-8000-000000000098",
            display_name=proposed.display_name,
            revision=3,
        )
        plan = self._preview(models=FakeModelRepository([existing]))
        self.assertEqual(plan.proposed_models[0].classification, "collision")
        self.assertEqual(plan.proposed_models[0].detail, "existing model claims conflicting connection")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_existing_model_with_same_connection_and_different_registration_is_collision(self) -> None:
        _write_valid_fixture(self.root)
        initial = self._preview()
        proposed = initial.proposed_models[0]
        existing = RegisteredModelRecord(
            registration_id="00000000-0000-4000-8000-000000000097",
            provider_model_id=proposed.provider_model_id,
            connection_id=proposed.connection_id,
            display_name=proposed.display_name,
            revision=3,
        )
        plan = self._preview(models=FakeModelRepository([existing]))
        self.assertEqual(plan.proposed_models[0].classification, "collision")
        self.assertEqual(plan.proposed_models[0].detail, "existing model has a different registration identity")
        self.assertEqual(len(plan.import_ready_models), 0)

    def test_model_source_paths_include_every_json_input(self) -> None:
        _write_valid_fixture(self.root)
        model = self._preview().proposed_models[0]
        self.assertEqual(
            model.source_paths,
            (
                f"agents/{ROLE}.toml",
                "preferences.json",
                "endpoints.json",
                "display-names.json",
            ),
        )

if __name__ == "__main__":
    unittest.main()
