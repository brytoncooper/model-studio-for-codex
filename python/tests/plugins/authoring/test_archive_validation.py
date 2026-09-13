"""Focused integration tests for packed-archive validation.

These tests exercise :func:`validate_project_archive` against in-memory
archives built with ``zipfile``. They cover the red and green paths
introduced by operation-schema and panel checks while keeping the
existing empty-contributions archive valid.
"""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path

from model_deck.plugins.authoring import (
    AuthoringError,
    AuthoringErrorCode,
    ValidationReport,
    validate_project_archive,
)


_REPO_ROOT = Path(
    "/Users/brytoncooper/Documents/Model Deck Architecture"
)
_NOTEBOOK_DIR = _REPO_ROOT / "examples" / "session-notebook"


_EMPTY_CONTRIBUTIONS_MANIFEST: dict = {
    "manifest_version": 1,
    "id": "org.example.archive.empty",
    "version": "1.0.0",
    "plugin_api": {"major": 1, "minimum_minor": 0},
    "entrypoint": {"runtime": "python", "path": "plugin.py"},
    "permissions": [],
    "contributes": {},
}


def _build_archive(entries: dict[str, bytes]) -> bytes:
    """Pack an in-memory archive from a name->payload mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _notebook_manifest() -> dict:
    return json.loads((_NOTEBOOK_DIR / "manifest.json").read_text())


def _notebook_archive_bytes() -> bytes:
    """Build the Session Notebook archive in memory from on-disk files.

    Operation schemas may ``$ref`` other schemas shipped in the same
    archive (for example ``note.schema.json`` is referenced from
    several output schemas). Bundle the entire ``schemas/`` directory
    so transitive ``$ref`` resolution works the same way it does for
    a packed archive on disk.
    """
    manifest = _notebook_manifest()
    entries: dict[str, bytes] = {"manifest.json": json.dumps(manifest).encode()}
    # Add the plugin entrypoint stub if it exists on disk.
    entrypoint = manifest.get("entrypoint", {}).get("path")
    if entrypoint:
        source = _NOTEBOOK_DIR / entrypoint
        if source.exists():
            entries[entrypoint] = source.read_bytes()
    # Add every schema resource under schemas/ (declared and shared).
    schemas_dir = _NOTEBOOK_DIR / "schemas"
    for path in sorted(schemas_dir.iterdir()):
        rel = str(path.relative_to(_NOTEBOOK_DIR))
        entries[rel] = path.read_bytes()
    # Add every declared panel resource.
    for panel in manifest["contributes"]["panels"]:
        entries[panel["schema"]] = (_NOTEBOOK_DIR / panel["schema"]).read_bytes()
    return _build_archive(entries)


class ArchiveValidationHappyPathTests(unittest.TestCase):
    """Archives that should pass :func:`validate_project_archive`."""

    def test_empty_contributions_archive_remains_valid(self):
        archive = _build_archive(
            {
                "manifest.json": json.dumps(_EMPTY_CONTRIBUTIONS_MANIFEST).encode(),
                "plugin.py": b"# empty plugin\n",
            }
        )
        report = validate_project_archive(archive)
        self.assertTrue(report.ok)
        self.assertTrue(report.schema_bundle_ok)
        self.assertEqual(report.panels, ())

    def test_minimal_manifest_with_entrypoint_no_contributions(self):
        archive = _build_archive(
            {
                "manifest.json": json.dumps(_EMPTY_CONTRIBUTIONS_MANIFEST).encode(),
                "plugin.py": b"# minimal\n",
            }
        )
        report = validate_project_archive(archive)
        self.assertTrue(report.ok)
        self.assertEqual(report.panels, ())

    def test_session_notebook_archive_is_accepted(self):
        archive = _notebook_archive_bytes()
        report = validate_project_archive(archive)
        self.assertTrue(report.ok, msg=str(report))
        self.assertTrue(report.schema_bundle_ok)
        self.assertEqual(len(report.panels), 2)
        self.assertTrue(all(p.ok for p in report.panels))
        panel_ids = [p.panel_id for p in report.panels]
        self.assertEqual(
            panel_ids,
            ["org.example.notebook.list", "org.example.notebook.editor"],
        )


class OperationSchemaValidationTests(unittest.TestCase):
    """Negative cases for declared operation schema resources and refs."""

    def _manifest_with_one_operation(self, *, schema_resource: str, declared_ref: str):
        return {
            "manifest_version": 1,
            "id": "org.example.archive.ops",
            "version": "1.0.0",
            "plugin_api": {"major": 1, "minimum_minor": 0},
            "entrypoint": {"runtime": "python", "path": "plugin.py"},
            "permissions": [],
            "contributes": {
                "operations": [
                    {
                        "id": "org.example.ops.create",
                        "input_schema": declared_ref,
                        "output_schema": declared_ref,
                        "effect": "write",
                    }
                ]
            },
        }

    def _valid_operation_schema_bytes(self) -> bytes:
        return json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "schemas/ops.input.schema.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            }
        ).encode()

    def test_operation_schema_reference_not_in_archive_rejected(self):
        manifest = self._manifest_with_one_operation(
            schema_resource="schemas/ops.input.schema.json",
            declared_ref="schemas/ops.input.schema.json",
        )
        archive = _build_archive(
            {
                "manifest.json": json.dumps(manifest).encode(),
                "plugin.py": b"# plugin\n",
                # No schema resource added.
            }
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
        )

    def test_operation_schema_resource_not_valid_json_schema_rejected(self):
        manifest = self._manifest_with_one_operation(
            schema_resource="schemas/ops.input.schema.json",
            declared_ref="schemas/ops.input.schema.json",
        )
        archive = _build_archive(
            {
                "manifest.json": json.dumps(manifest).encode(),
                "plugin.py": b"# plugin\n",
                "schemas/ops.input.schema.json": b"{not-json}",
            }
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
        )

    def test_operation_input_schema_reference_not_in_bundle_rejected(self):
        # Reference points to a resource that IS in the archive but uses a
        # bare fragment that the bundle cannot resolve.
        manifest = self._manifest_with_one_operation(
            schema_resource="schemas/ops.input.schema.json",
            declared_ref="schemas/ops.input.schema.json#/definitions/missing",
        )
        archive = _build_archive(
            {
                "manifest.json": json.dumps(manifest).encode(),
                "plugin.py": b"# plugin\n",
                "schemas/ops.input.schema.json": self._valid_operation_schema_bytes(),
            }
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.OPERATION_SCHEMA_REFERENCE_INVALID,
        )


class PanelValidationTests(unittest.TestCase):
    """Negative cases for declared panel resources and semantic rules."""

    def _manifest_with_panel(self, panel_decl):
        manifest = json.loads((_NOTEBOOK_DIR / "manifest.json").read_text())
        manifest["id"] = "org.example.archive.panels"
        # Replace the panels block with the supplied panel declaration.
        manifest["contributes"]["panels"] = [panel_decl]
        return manifest

    def _archive_with_extra(
        self,
        panel_decl: dict,
        *,
        extra_entries: dict[str, bytes] | None = None,
        panel_payload_override: bytes | None = None,
    ) -> bytes:
        manifest = self._manifest_with_panel(panel_decl)
        entries: dict[str, bytes] = {
            "manifest.json": json.dumps(manifest).encode(),
            "plugin.py": b"# plugin\n",
        }
        # Always include every schema under schemas/ so transitive
        # ``$ref`` chains resolve, mirroring the production archive.
        schemas_dir = _NOTEBOOK_DIR / "schemas"
        for path in sorted(schemas_dir.iterdir()):
            rel = str(path.relative_to(_NOTEBOOK_DIR))
            entries[rel] = path.read_bytes()
        schema_path = panel_decl["schema"]
        if panel_payload_override is not None:
            entries[schema_path] = panel_payload_override
        else:
            # Use the real notebook-list.json as the panel payload by default.
            entries[schema_path] = (
                _NOTEBOOK_DIR / "panels" / "notebook-list.json"
            ).read_bytes()
        if extra_entries:
            entries.update(extra_entries)
        return _build_archive(entries)

    def _list_panel_decl(self) -> dict:
        return {
            "id": "org.example.archive.panels.list",
            "schema": "panels/archive-list.json",
        }

    def test_panel_resource_missing_rejected(self):
        # Manifest references panels/archive-list.json but the archive
        # doesn't contain it. The operation schema bundle must be
        # complete so the missing-panel defect surfaces instead of a
        # bundle failure.
        manifest = self._manifest_with_panel(self._list_panel_decl())
        entries: dict[str, bytes] = {
            "manifest.json": json.dumps(manifest).encode(),
            "plugin.py": b"# plugin\n",
        }
        schemas_dir = _NOTEBOOK_DIR / "schemas"
        for path in sorted(schemas_dir.iterdir()):
            rel = str(path.relative_to(_NOTEBOOK_DIR))
            entries[rel] = path.read_bytes()
        # Deliberately omit the panel resource.
        archive = _build_archive(entries)
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.PANEL_RESOURCE_MISSING,
        )

    def test_panel_resource_not_valid_json_rejected(self):
        archive = self._archive_with_extra(
            self._list_panel_decl(),
            panel_payload_override=b"{not-json}",
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.PANEL_RESOURCE_NOT_READABLE,
        )

    def test_panel_fails_ui_panel_v1_schema_rejected(self):
        # The payload is valid JSON but does not match the panel schema
        # (missing required fields like panel_id, revision, title, state).
        bad_payload = json.dumps({"root": {}}).encode()
        archive = self._archive_with_extra(
            self._list_panel_decl(),
            panel_payload_override=bad_payload,
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.PANEL_SCHEMA_INVALID,
        )

    def test_panel_with_button_referencing_undeclared_operation_rejected(self):
        # Build a panel whose button references an undeclared operation id.
        payload = {
            "panel_id": "org.example.archive.panels.list",
            "revision": 0,
            "title": "List",
            "state": "ready",
            "root": {
                "id": "root",
                "kind": "stack",
                "children": [
                    {
                        "id": "go",
                        "kind": "button",
                        "label": "Go",
                        "operation_id": "org.example.ops.notes.delete",
                        "params": {},
                    }
                ],
            },
        }
        archive = self._archive_with_extra(
            self._list_panel_decl(),
            panel_payload_override=json.dumps(payload).encode(),
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.PANEL_OPERATION_UNKNOWN,
        )

    def test_panel_id_mismatch_rejected(self):
        # Payload's panel_id does not match the manifest contribution id.
        payload = json.loads(
            (_NOTEBOOK_DIR / "panels" / "notebook-list.json").read_text()
        )
        payload["panel_id"] = "org.example.mismatch.list"
        archive = self._archive_with_extra(
            self._list_panel_decl(),
            panel_payload_override=json.dumps(payload).encode(),
        )
        with self.assertRaises(AuthoringError) as ctx:
            validate_project_archive(archive)
        self.assertEqual(
            ctx.exception.code,
            AuthoringErrorCode.PANEL_ID_MISMATCH,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
