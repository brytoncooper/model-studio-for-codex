"""External acceptance tests for the isolated Session Notebook package."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from model_deck.adapters.storage.sqlite_plugin_data import (
    SQLitePluginDataRepository,
)
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)
from model_deck.engine.plugin_data.service import PluginDataBroker
from model_deck.engine.plugin_data.wire import PluginDataWireAdapter
from model_deck.plugins.authoring import (
    pack_project_archive,
    validate_project_archive,
)
from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import ProcessRuntime, ProcessRuntimeConfig
from model_deck.plugins.process_runtime.errors import ProcessRuntimeError
from model_deck_contracts.validator import validate_schema_ref


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = EXAMPLE_ROOT / "plugin.py"
MANIFEST_PATH = EXAMPLE_ROOT / "manifest.json"
PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
ORIGIN_ID = "external-test-operator"

CREATE_NOTE = "org.example.notebook.notes.create"
GET_NOTE = "org.example.notebook.notes.get"
LIST_NOTES = "org.example.notebook.notes.list"
UPDATE_NOTE = "org.example.notebook.notes.update"
DELETE_NOTE = "org.example.notebook.notes.delete"
PREVIEW_EXPORT = "org.example.notebook.export.preview"

STORAGE_GET = "plugin.v1.broker.storage.get"
STORAGE_LIST = "plugin.v1.broker.storage.list"
STORAGE_PUT = "plugin.v1.broker.storage.put"
STORAGE_DELETE = "plugin.v1.broker.storage.delete"
STORAGE_METHODS = (
    STORAGE_GET,
    STORAGE_LIST,
    STORAGE_PUT,
    STORAGE_DELETE,
)

BROKER_GRANTS = {
    "get": ("read", "data.private", "data.access"),
    "list": ("read", "data.private", "data.access"),
    "put": ("write", "data.private", "data.access"),
    "delete": ("write", "data.private", "data.access"),
}

OPERATION_SCHEMA_FILES = {
    CREATE_NOTE: (
        "schemas/notes.create.input.schema.json",
        "schemas/notes.create.output.schema.json",
    ),
    GET_NOTE: (
        "schemas/notes.get.input.schema.json",
        "schemas/notes.get.output.schema.json",
    ),
    LIST_NOTES: (
        "schemas/notes.list.input.schema.json",
        "schemas/notes.list.output.schema.json",
    ),
    UPDATE_NOTE: (
        "schemas/notes.update.input.schema.json",
        "schemas/notes.update.output.schema.json",
    ),
    DELETE_NOTE: (
        "schemas/notes.delete.input.schema.json",
        "schemas/notes.delete.output.schema.json",
    ),
    PREVIEW_EXPORT: (
        "schemas/export.preview.input.schema.json",
        "schemas/export.preview.output.schema.json",
    ),
}


def _load_worker_module():
    module_spec = importlib.util.spec_from_file_location(
        "session_notebook_worker",
        PLUGIN_PATH,
    )
    if module_spec is None or module_spec.loader is None:
        raise AssertionError("could not load notebook worker")
    module = importlib.util.module_from_spec(module_spec)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    return module


def _schema_registry() -> tuple[Registry, dict[str, dict[str, Any]]]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: dict[str, Resource] = {}
    for schema_path in sorted((EXAMPLE_ROOT / "schemas").glob("*.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schemas[str(schema_path.relative_to(EXAMPLE_ROOT))] = schema
        resources[schema["$id"]] = Resource.from_contents(schema)
    return Registry().with_resources(resources.items()), schemas


class MemoryContextStore:
    def __init__(self) -> None:
        self.contexts = {}

    def put(self, context) -> None:
        if context.invocation_id in self.contexts:
            raise ValueError("invocation collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class MutableAuthorityState:
    def __init__(
        self,
        activation: ActivationState,
        origin: OriginState,
    ) -> None:
        self.activation_state = activation
        self.origin_state = origin

    def activation(self, identity):
        if identity != self.activation_state.identity:
            return None
        return self.activation_state

    def origin(self, principal_id):
        if principal_id != self.origin_state.principal_id:
            return None
        return self.origin_state

    def operation(self, operation_id):
        if operation_id not in OPERATION_SCHEMA_FILES:
            return None
        return OperationAuthority(
            operation_id=operation_id,
            effects=frozenset({"read", "write"}),
            resource_scopes=frozenset({"data.private"}),
            capability_grants=frozenset({"data.access"}),
        )


class RunningNotebook:
    """One real isolated worker wired to the real scoped SQLite broker."""

    def __init__(
        self,
        repository: SQLitePluginDataRepository,
    ) -> None:
        self.repository = repository
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self._adapter: PluginDataWireAdapter | None = None
        self._identity_counter = 0
        self._handle_counter = 0
        self.broker_calls: list[tuple[str, dict[str, Any]]] = []

        def forward_broker_request(activation_id, method, params):
            self.broker_calls.append((method, dict(params)))
            if self._adapter is None:
                raise RuntimeError("broker adapter not ready")
            return self._adapter(activation_id, method, params)

        self.runtime = ProcessRuntime(
            ProcessRuntimeConfig(
                argv=(sys.executable, "-I", "-B", str(PLUGIN_PATH)),
                package_dir=str(EXAMPLE_ROOT),
                timeout_s=3.0,
                max_frames=64,
            ),
            allowed_broker_methods=STORAGE_METHODS,
            broker_request_handler=forward_broker_request,
        )
        self.runtime.spawn()
        self.lifecycle = LifecycleSession(
            expected_plugin_id=PLUGIN_ID,
            expected_plugin_version=PLUGIN_VERSION,
            offered_api_major=1,
            offered_api_minor=0,
            activation_token="session-notebook-test-token",
            allowed_broker_methods=STORAGE_METHODS,
        )
        hello = self.runtime.run_hello(self.lifecycle, "session-notebook-test")
        if "fixture.isolated-imports" not in hello["capabilities"]:
            raise AssertionError("worker did not confirm isolated imports")
        activation = self.runtime.run_activation(self.lifecycle)
        self.identity = ActivationIdentity(
            engine_instance_id="session-notebook-test-engine",
            audience="session-notebook-test-broker",
            activation_id=activation["activation_id"],
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
        )
        permissions = {
            "effects": frozenset({"read", "write"}),
            "resource_scopes": frozenset({"data.private"}),
            "capability_grants": frozenset({"data.access"}),
        }
        self.authority_state = MutableAuthorityState(
            ActivationState(
                identity=self.identity,
                **permissions,
                expires_at=self.deadline,
                revocation_generation=1,
            ),
            OriginState(
                principal_id=ORIGIN_ID,
                engine_instance_id=self.identity.engine_instance_id,
                audience=self.identity.audience,
                **permissions,
                expires_at=self.deadline,
                revocation_generation=1,
            ),
        )

        def next_invocation_id() -> str:
            self._identity_counter += 1
            return f"notebook-invocation-{self._identity_counter}"

        def next_handle() -> str:
            self._handle_counter += 1
            return f"notebook-handle-{self._handle_counter}"

        self.authority = PluginAuthority(
            engine_instance_id=self.identity.engine_instance_id,
            audience=self.identity.audience,
            state=self.authority_state,
            contexts=MemoryContextStore(),
            clock=lambda: self.now,
            invocation_id_factory=next_invocation_id,
            handle_factory=next_handle,
        )
        mutation_lock = threading.RLock()

        @contextmanager
        def mutation_guard():
            with mutation_lock:
                yield

        broker = PluginDataBroker(
            authority=self.authority,
            repository=self.repository,
            grants=dict(BROKER_GRANTS),
            mutation_guard=mutation_guard,
        )
        self._adapter = PluginDataWireAdapter(
            trusted_activation=self.identity,
            broker=broker,
        )
        self.channel = self.runtime.invocation_channel()

    def close(self) -> None:
        self.runtime.close()

    def issue_handle(self, operation_id: str) -> str:
        return self.authority.issue(
            self.identity,
            ORIGIN_ID,
            operation_id,
            expires_at=self.deadline,
        )

    def broker_context(self, handle: str) -> dict[str, Any]:
        return {
            "activation_id": self.identity.activation_id,
            "plugin_id": PLUGIN_ID,
            "invocation_handle": handle,
            "revocation_generation": (
                self.authority_state.activation_state.revocation_generation
            ),
        }

    def invoke(
        self,
        operation_id: str,
        operation_input: dict[str, Any],
        *,
        handle: str | None = None,
        context_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invocation_handle = handle or self.issue_handle(operation_id)
        response = self.invoke_result(
            operation_id,
            operation_input,
            handle=invocation_handle,
            context_overrides=context_overrides,
        )
        return response["output"]

    def invoke_result(
        self,
        operation_id: str,
        operation_input: dict[str, Any],
        *,
        handle: str | None = None,
        context_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invocation_handle = handle or self.issue_handle(operation_id)
        context = self.broker_context(invocation_handle)
        if context_overrides:
            context.update(context_overrides)
        return self.channel.invoke(
            operation_id,
            operation_input,
            context,
            timeout_s=3.0,
        )


class PackageContractTests(unittest.TestCase):
    def test_archive_manifest_schemas_panels_and_isolated_worker_are_valid(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["permissions"], ["storage.own"])
        self.assertNotIn("session", json.dumps(manifest["permissions"]))
        self.assertNotIn("transcript", json.dumps(manifest["permissions"]))
        self.assertNotIn("jobs.own", manifest["permissions"])

        declared_operations = {
            operation["id"]: operation
            for operation in manifest["contributes"]["operations"]
        }
        self.assertEqual(set(declared_operations), set(OPERATION_SCHEMA_FILES))
        for operation_id, (input_schema, output_schema) in OPERATION_SCHEMA_FILES.items():
            self.assertEqual(
                declared_operations[operation_id]["input_schema"],
                input_schema,
            )
            self.assertEqual(
                declared_operations[operation_id]["output_schema"],
                output_schema,
            )
            self.assertTrue((EXAMPLE_ROOT / input_schema).is_file())
            self.assertTrue((EXAMPLE_ROOT / output_schema).is_file())

        registry, schemas = _schema_registry()
        for schema in schemas.values():
            Draft202012Validator(
                schema,
                registry=registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )

        manifest_operation_ids = set(declared_operations)
        for panel_entry in manifest["contributes"]["panels"]:
            panel_path = EXAMPLE_ROOT / panel_entry["schema"]
            panel = json.loads(panel_path.read_text(encoding="utf-8"))
            validate_schema_ref("contracts/ui.panel.v1/tree.schema.json", panel)
            self.assertEqual(panel["panel_id"], panel_entry["id"])
            self._assert_panel_semantics(panel, manifest_operation_ids)
            if panel_entry["id"] == "org.example.notebook.editor":
                child_ids = [child["id"] for child in panel["root"]["children"]]
                self.assertEqual(child_ids, ["title", "body", "save_note"])

        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import model_deck", source)
        self.assertNotIn("from model_deck", source)

        with tempfile.TemporaryDirectory(
            prefix="session-notebook-package-",
            dir="/tmp",
        ) as temporary_directory:
            archive_path = Path(temporary_directory) / "session-notebook.zip"
            packed = pack_project_archive(EXAMPLE_ROOT, output_path=archive_path)
            report = validate_project_archive(archive_path.read_bytes())
            self.assertTrue(report.ok)
            self.assertEqual(packed.output_path, archive_path)
            self.assertEqual(report.manifest.identity.manifest_id, PLUGIN_ID)

    def _assert_panel_semantics(
        self,
        panel: dict[str, Any],
        declared_operation_ids: set[str],
    ) -> None:
        nodes: dict[str, dict[str, Any]] = {}

        def visit(node: dict[str, Any]) -> None:
            self.assertNotIn(node["id"], nodes)
            nodes[node["id"]] = node
            for child in node.get("children", []):
                visit(child)

        visit(panel["root"])
        for node in nodes.values():
            if node["kind"] != "button":
                continue
            self.assertIn(node["operation_id"], declared_operation_ids)
            self.assertFalse(
                set(node.get("params", {})) & set(node.get("field_bindings", {}))
            )
            for target in node.get("field_bindings", {}).values():
                self.assertIn(target, nodes)
                self.assertEqual(nodes[target]["kind"], "text_input")

    def test_local_operation_schemas_accept_representative_values(self) -> None:
        registry, schemas = _schema_registry()
        worker = _load_worker_module()
        note = {
            "note_id": "11111111-2222-4333-8444-555555555555",
            "title": "",
            "body": "Body",
            "metadata": {"flag": False, "value": None},
            "revision": 1,
        }
        representative_values = {
            "schemas/notes.create.input.schema.json": {
                "title": "",
                "body": "Body",
                "metadata": {"flag": False, "value": None},
            },
            "schemas/notes.create.output.schema.json": note,
            "schemas/notes.get.input.schema.json": {"note_id": note["note_id"]},
            "schemas/notes.get.output.schema.json": note,
            "schemas/notes.list.input.schema.json": {},
            "schemas/notes.list.output.schema.json": {"notes": [note]},
            "schemas/notes.update.input.schema.json": {
                "note_id": note["note_id"],
                "expected_revision": 1,
                "title": "",
                "body": "Body",
                "metadata": [],
            },
            "schemas/notes.update.output.schema.json": note,
            "schemas/notes.delete.input.schema.json": {
                "note_id": note["note_id"],
                "expected_revision": 1,
            },
            "schemas/notes.delete.output.schema.json": {"deleted": True},
            "schemas/export.preview.input.schema.json": {},
            "schemas/export.preview.output.schema.json": {
                "markdown": "# Session Notebook\n",
                "note_count": 0,
            },
        }
        self.assertEqual(set(schemas) - {"schemas/note.schema.json"}, set(representative_values))
        for schema_name, value in representative_values.items():
            with self.subTest(schema=schema_name):
                Draft202012Validator(
                    schemas[schema_name],
                    registry=registry,
                    format_checker=Draft202012Validator.FORMAT_CHECKER,
                ).validate(value)

        markdown = worker.format_notes_as_markdown(
            [
                {**note, "note_id": "22222222-2222-4222-8222-222222222222", "title": "Second"},
                {**note, "note_id": "11111111-1111-4111-8111-111111111111", "title": ""},
            ]
        )
        self.assertEqual(
            markdown,
            "# Session Notebook\n\n## Untitled\n\nBody\n\n## Second\n\nBody\n",
        )


class RealStorageBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="session-notebook-storage-",
            dir="/tmp",
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = SQLitePluginDataRepository(
            Path(self.temporary_directory.name) / "plugin-data.sqlite3"
        )
        self.running: list[RunningNotebook] = []

    def start_notebook(self) -> RunningNotebook:
        notebook = RunningNotebook(self.repository)
        self.running.append(notebook)
        self.addCleanup(notebook.close)
        return notebook

    def test_manual_crud_preserves_json_and_uses_only_owned_namespace(self) -> None:
        notebook = self.start_notebook()
        metadata = {
            "flag": False,
            "nothing": None,
            "empty": "",
            "nested": [0, {}, []],
            "unicode": "雪",
        }

        created = notebook.invoke(
            CREATE_NOTE,
            {"title": "", "body": "First body", "metadata": metadata},
        )
        self.assertEqual(created["title"], "")
        self.assertEqual(created["body"], "First body")
        self.assertEqual(created["metadata"], metadata)
        self.assertEqual(created["revision"], 1)
        uuid.UUID(created["note_id"])

        fetched = notebook.invoke(GET_NOTE, {"note_id": created["note_id"]})
        self.assertEqual(fetched, created)
        listed = notebook.invoke(LIST_NOTES, {})
        self.assertEqual(listed, {"notes": [created]})

        updated = notebook.invoke(
            UPDATE_NOTE,
            {
                "note_id": created["note_id"],
                "expected_revision": created["revision"],
                "title": "Updated",
                "body": "",
            },
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["body"], "")
        self.assertEqual(updated["metadata"], metadata)

        preview = notebook.invoke(PREVIEW_EXPORT, {})
        self.assertEqual(preview["note_count"], 1)
        self.assertEqual(
            preview["markdown"],
            "# Session Notebook\n\n## Updated\n\n\n",
        )

        deleted = notebook.invoke(
            DELETE_NOTE,
            {
                "note_id": created["note_id"],
                "expected_revision": updated["revision"],
            },
        )
        self.assertEqual(deleted, {"deleted": True})
        self.assertEqual(notebook.invoke(LIST_NOTES, {}), {"notes": []})

        self.assertTrue(notebook.broker_calls)
        for _, params in notebook.broker_calls:
            self.assertEqual(params["namespace"], PLUGIN_ID)

    def test_new_activation_reuses_the_same_owned_notes(self) -> None:
        first = self.start_notebook()
        created = first.invoke(
            CREATE_NOTE,
            {"title": "Retained", "body": "Across activation"},
        )
        self.assertTrue(first.runtime.run_drain(first.lifecycle, 1_000)["drained"])
        first.close()

        second = self.start_notebook()
        self.assertEqual(
            second.invoke(GET_NOTE, {"note_id": created["note_id"]}),
            created,
        )

    def test_stale_revision_fails_without_overwriting_current_note(self) -> None:
        first = self.start_notebook()
        created = first.invoke(
            CREATE_NOTE,
            {"title": "First", "body": "One"},
        )
        current = first.invoke(
            UPDATE_NOTE,
            {
                "note_id": created["note_id"],
                "expected_revision": created["revision"],
                "title": "Current",
                "body": "Two",
            },
        )
        first.close()

        stale = self.start_notebook()
        conflict = stale.invoke_result(
            UPDATE_NOTE,
            {
                "note_id": created["note_id"],
                "expected_revision": created["revision"],
                "title": "Stale",
                "body": "Lost",
            },
        )
        self.assertEqual(
            conflict,
            {"error": {"code": "conflict", "message": "note revision changed"}},
        )
        self.assertEqual(
            stale.invoke(GET_NOTE, {"note_id": created["note_id"]}),
            current,
        )

    def test_deleted_note_cannot_be_resurrected_by_update(self) -> None:
        first = self.start_notebook()
        created = first.invoke(
            CREATE_NOTE,
            {"title": "Delete me", "body": ""},
        )
        self.assertEqual(
            first.invoke(
                DELETE_NOTE,
                {
                    "note_id": created["note_id"],
                    "expected_revision": created["revision"],
                },
            ),
            {"deleted": True},
        )
        first.close()

        resurrection = self.start_notebook()
        with self.assertRaises(ProcessRuntimeError):
            resurrection.invoke(
                UPDATE_NOTE,
                {
                    "note_id": created["note_id"],
                    "expected_revision": 2,
                    "title": "Resurrected",
                    "body": "No",
                },
            )

        self.assertEqual(
            self.repository.list(PLUGIN_ID, "notes/", 200),
            [],
        )

    def test_forged_namespace_input_is_rejected_before_storage(self) -> None:
        notebook = self.start_notebook()
        with self.assertRaises(ProcessRuntimeError):
            notebook.invoke(
                CREATE_NOTE,
                {
                    "title": "Forged",
                    "body": "No",
                    "namespace": "org.example.other",
                },
            )
        self.assertEqual(self.repository.list(PLUGIN_ID, "notes/", 200), [])
        self.assertEqual(notebook.broker_calls, [])

    def test_forged_and_revoked_handles_cannot_write(self) -> None:
        forged = self.start_notebook()
        with self.assertRaises(ProcessRuntimeError):
            forged.invoke(
                CREATE_NOTE,
                {"title": "Forged handle", "body": "No"},
                handle="worker-selected-handle",
            )
        self.assertEqual(self.repository.list(PLUGIN_ID, "notes/", 200), [])

        revoked = self.start_notebook()
        handle = revoked.issue_handle(CREATE_NOTE)
        revoked.authority_state.activation_state = replace(
            revoked.authority_state.activation_state,
            revocation_generation=2,
        )
        with self.assertRaises(ProcessRuntimeError):
            revoked.invoke(
                CREATE_NOTE,
                {"title": "Revoked", "body": "No"},
                handle=handle,
            )
        self.assertEqual(self.repository.list(PLUGIN_ID, "notes/", 200), [])

    def test_manual_notes_need_no_metadata_or_content_broker_method(self) -> None:
        notebook = self.start_notebook()
        created = notebook.invoke(
            CREATE_NOTE,
            {"title": "Manual", "body": "Typed by the user"},
        )
        self.assertEqual(created["body"], "Typed by the user")
        self.assertTrue(
            all(method.startswith("plugin.v1.broker.storage.") for method, _ in notebook.broker_calls)
        )


if __name__ == "__main__":
    unittest.main()
