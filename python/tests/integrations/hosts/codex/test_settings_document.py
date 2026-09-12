from __future__ import annotations

import copy
import json
import traceback
import unittest
from dataclasses import replace

from model_deck_contracts.validator import validate_schema_ref
from model_deck.integrations.hosts.codex.settings_document import (
    DocumentContext, DocumentError, EntrySpec, FieldSpec, ProtectedDeniedError,
    SectionSpec, SourceTooLargeError, assert_save_allowed, preview_candidate,
    read_snapshot, sha256_hex, validate_candidate,
)

BASE = b'# comment\nmodel = "gpt-5.5"\nunknown = "keep" # untouched\n[server]\nport = 8080\n'
SPECS = (
    FieldSpec("model", "Model", "string", ("model",)),
    FieldSpec("api_key", "API key", "string", ("api_key",), sensitivity="secret"),
    FieldSpec("port", "Port", "integer", ("server", "port"), constraints={"minimum": 1, "maximum": 65535}),
)
SECTIONS = (SectionSpec("general", "General", ("model", "api_key", "port")),)


def context(source=BASE, *, exists=True, **overrides):
    values = dict(host_id="codex.cli", document_id="config.toml",
                  document_revision=sha256_hex(source) if exists else "absent", exists=exists,
                  target={"display_name": "config", "display_path": "fixture/config.toml", "scope": "user", "writable": True},
                  schema_profile={"schema_id": "codex", "schema_revision": "r1", "host_version": "v1", "support_level": "supported"},
                  precedence=[], context_revision="rev-1")
    values.update(overrides)
    return DocumentContext(**values)


class SettingsDocumentTest(unittest.TestCase):
    def check_wire(self, method, result):
        wire = copy.deepcopy(result)
        if method == "read":
            wire = {"snapshot": wire}
        if method == "preview" and wire["preview"] is not None:
            self.assertNotIn("preview_id", wire["preview"])
            wire["preview"]["preview_id"] = "server-issued-fixture-token"
        validate_schema_ref("contracts/engine.v1/methods/hosts.settings." + method + ".result.schema.json", wire)
        return result

    def validate(self, source=BASE, draft=None, *, ctx=None, specs=SPECS, sections=SECTIONS):
        if draft is None:
            draft = {"kind": "raw", "raw_toml": source.decode()}
        result = validate_candidate(source, ctx or context(source), sections, specs, draft)
        return self.check_wire("validate", result)

    def preview(self, source=BASE, draft=None, *, ctx=None, specs=SPECS, sections=SECTIONS):
        result = preview_candidate(source, ctx or context(source), sections, specs,
                                   draft or {"kind": "raw", "raw_toml": source.decode()})
        return self.check_wire("preview", result)

    def read(self, source=BASE, *, ctx=None, specs=SPECS, sections=SECTIONS):
        return self.check_wire("read", read_snapshot(source, ctx or context(source), sections, specs))

    def test_read_secret_omissions_and_lossless_raw(self):
        source = b'model="x"\napi_key="private-sentinel"\n'
        spec = replace(SPECS[1], default="private-default", has_default=True)
        result = self.read(source, specs=(SPECS[0], spec, SPECS[2]),
                           ctx=context(source, inherited_values={"api_key": "private-effective"}))
        field = result["structured"]["sections"][0]["fields"][1]
        self.assertTrue(field["configured"])
        for key in ("value", "default", "effective", "entries"):
            self.assertNotIn(key, field)
        self.assertNotIn("private-", json.dumps(result["structured"]))
        self.assertEqual(result["raw_toml"].encode(), source)

    def test_structured_noop_preserves_every_byte(self):
        result = self.validate(draft={"kind": "structured", "changes": [{"field_id": "model", "operation": "set", "value": "gpt-5.5"}]})
        self.assertTrue(result["valid"])
        self.assertEqual(result["candidate_raw_toml"].encode(), BASE)

    def test_structured_change_preserves_comments_unknown_and_order(self):
        result = self.validate(draft={"kind": "structured", "changes": [{"field_id": "port", "operation": "set", "value": 9000}]})
        self.assertEqual(result["candidate_raw_toml"].encode(), BASE.replace(b"8080", b"9000"))

    def test_raw_unknown_values_preserved(self):
        source = BASE + b'unknown_date = 2026-01-01\n'
        result = self.validate(source)
        self.assertTrue(result["valid"])
        self.assertEqual(result["candidate_raw_toml"].encode(), source)

    def test_protected_raw_unchanged_falsey_changed_removed(self):
        for value in ("8080", "0", "false", '""'):
            source = ('guard=' + value + '\nother=1\n').encode()
            ctx = context(source, protected_paths=frozenset({"guard"}))
            result = self.validate(source, ctx=ctx, specs=(), sections=())
            self.assertTrue(result["valid"])
            result = self.validate(source, {"kind": "raw", "raw_toml": source.decode().replace("other=1", "other=2")}, ctx=ctx, specs=(), sections=())
            self.assertTrue(result["valid"])
            for candidate in ('other=1\n', 'guard="changed"\nother=1\n'):
                with self.assertRaises(ProtectedDeniedError):
                    self.validate(source, {"kind": "raw", "raw_toml": candidate}, ctx=ctx, specs=(), sections=())

    def test_protected_parent_removal_and_reformat_rejected(self):
        ctx = context(protected_paths=frozenset({"server"}))
        for raw in (BASE.decode().split("[server]")[0], BASE.decode().replace("port = 8080", "port=8080")):
            with self.assertRaises(ProtectedDeniedError):
                self.validate(draft={"kind": "raw", "raw_toml": raw}, ctx=ctx)

    def test_all_noneditable_modes_reject_raw_and_structured_changes(self):
        for mode in ("managed", "projection_owned", "unsupported"):
            specs = (replace(SPECS[0], editability=mode), *SPECS[1:])
            self.assertTrue(self.validate(specs=specs)["valid"])
            for draft in ({"kind": "raw", "raw_toml": BASE.decode().replace("gpt-5.5", "changed")},
                          {"kind": "structured", "changes": [{"field_id": "model", "operation": "unset"}]}):
                with self.assertRaises(ProtectedDeniedError):
                    self.validate(draft=draft, specs=specs)

    def test_raw_known_type_and_numeric_constraints_invalid_with_candidates(self):
        for raw in ('model=42\n', '[server]\nport=0\n', '[server]\nport=99999\n', '[server]\nport=true\n'):
            result = self.validate(draft={"kind": "raw", "raw_toml": raw})
            self.assertFalse(result["valid"])
            self.assertIn("candidate_raw_toml", result)
            self.assertIn("candidate_structured", result)
            self.assertTrue(result["diagnostics"])

    def test_string_list_enum_and_finite_constraints(self):
        cases = [
            (FieldSpec("x", "X", "string", ("x",), constraints={"min_length": 2, "max_length": 3}), 'x="a"\n'),
            (FieldSpec("x", "X", "string_list", ("x",), constraints={"max_items": 1}), 'x=["a","b"]\n'),
            (FieldSpec("x", "X", "enum", ("x",), constraints={"choices": [{"value": 1, "label": "one"}]}), 'x=true\n'),
            (FieldSpec("x", "X", "number", ("x",)), 'x=inf\n'),
        ]
        for spec, raw in cases:
            result = self.validate(b'', {"kind": "raw", "raw_toml": raw}, specs=(spec,), sections=(SectionSpec("g", "G", ("x",)),))
            self.assertFalse(result["valid"])

    def test_structured_constraints_reject_and_keep_candidate(self):
        result = self.validate(draft={"kind": "structured", "changes": [{"field_id": "port", "operation": "set", "value": 0}]})
        self.assertFalse(result["valid"])
        self.assertEqual(result["candidate_raw_toml"].encode(), BASE)

    def test_multiline_secret_diff_context_completely_suppressed(self):
        source = b'api_key="""\nprivate-sentinel\nsecond-secret-line\n"""\nmodel="x"\n'
        result = self.preview(source, {"kind": "raw", "raw_toml": source.decode().replace('model="x"', 'model="y"')})
        self.assertEqual(result["preview"]["diff"], "<redacted>\n")
        self.assertTrue(result["preview"]["diff_truncated"])
        self.assertFalse(result["preview"]["protected_projection_changes"])
        self.assertIn("private-sentinel", result["candidate_raw_toml"])
        self.assertNotIn("private-sentinel", json.dumps(result["candidate_structured"]))

    def test_secret_noop_diff_empty(self):
        result = self.preview()
        self.assertFalse(result["preview"]["changed"])
        self.assertFalse(result["preview"]["diff_truncated"])
        self.assertEqual(result["preview"]["diff"], "")

    def test_absent_distinct_from_empty_document(self):
        for exists in (False, True):
            ctx = context(b'', exists=exists)
            snapshot = self.read(b'', ctx=ctx, specs=(), sections=())
            result = self.preview(b'', {"kind": "raw", "raw_toml": 'x=1\n'}, ctx=ctx, specs=(), sections=())
            self.assertEqual(result["preview"]["base_content_hash"], sha256_hex(b'') if exists else "absent")
            self.assertEqual(snapshot["document_revision"], result["preview"]["base_content_hash"])

    def test_bad_source_context_rejected(self):
        with self.assertRaises(DocumentError):
            self.read(ctx=context(document_revision="absent"))
        with self.assertRaises(DocumentError):
            self.read(ctx=context(exists=False))

    def test_malformed_toml_locations_safe(self):
        result = self.validate(draft={"kind": "raw", "raw_toml": 'model = [\n"private-sentinel"\n'})
        self.assertFalse(result["valid"])
        diagnostic = result["diagnostics"][0]
        self.assertGreaterEqual(diagnostic["line"], 1)
        self.assertGreaterEqual(diagnostic["column"], 1)
        self.assertNotIn("private-sentinel", json.dumps(diagnostic))
        self.assertNotIn("candidate_raw_toml", result)

    def test_invalid_preview_has_no_token(self):
        result = self.preview(draft={"kind": "raw", "raw_toml": 'model=[\n'})
        self.assertFalse(result["valid"])
        self.assertIsNone(result["preview"])

    def test_duplicate_and_unknown_targets_rejected(self):
        changes = [
            [{"field_id": "model", "operation": "unset"}] * 2,
            [{"field_id": "unknown", "operation": "unset"}],
            [{"field_id": "model", "entry_id": "unknown", "operation": "unset"}],
        ]
        for batch in changes:
            with self.assertRaises(DocumentError):
                self.validate(draft={"kind": "structured", "changes": batch})

    def test_descriptor_duplicate_emission_and_section_bounds(self):
        with self.assertRaises(DocumentError):
            self.read(sections=(SECTIONS[0], replace(SECTIONS[0], section_id="other")))
        with self.assertRaises(SourceTooLargeError):
            self.read(sections=tuple(SectionSpec(str(i), "S", ()) for i in range(65)))

    def test_entries_use_injected_stable_ids_and_absolute_paths(self):
        child = FieldSpec("enabled", "Enabled", "boolean", ("servers", 0, "enabled"))
        spec = FieldSpec("servers", "Servers", "entries", ("servers",), entries=(EntrySpec("server-a", "A", (child,)),))
        source = b'[[servers]]\nname="A" # unknown\nenabled=true\n'
        sections = (SectionSpec("g", "G", ("servers",)),)
        snapshot = self.read(source, specs=(spec,), sections=sections)
        descriptor = snapshot["structured"]["sections"][0]["fields"][0]
        self.assertNotIn("value", descriptor)
        self.assertEqual(descriptor["entries"][0]["entry_id"], "server-a")
        result = self.validate(source, {"kind": "structured", "changes": [{"field_id": "enabled", "entry_id": "server-a", "operation": "set", "value": False}]}, specs=(spec,), sections=sections)
        self.assertEqual(result["candidate_raw_toml"].encode(), source.replace(b'true', b'false'))

    def test_secret_entry_container_never_displays_public_children(self):
        child = FieldSpec("token", "Token", "string", ("secrets", "token"))
        spec = FieldSpec("secrets", "Secrets", "entries", ("secrets",), sensitivity="secret",
                         entries=(EntrySpec("entry", "E", (child,)),))
        snapshot = self.read(b'[secrets]\ntoken="private-sentinel"\n', specs=(spec,), sections=(SectionSpec("g", "G", ("secrets",)),))
        descriptor = snapshot["structured"]["sections"][0]["fields"][0]
        self.assertNotIn("entries", descriptor)
        self.assertNotIn("private-sentinel", json.dumps(snapshot["structured"]))

    def test_entry_descriptor_count_and_depth_bounds(self):
        children = tuple(FieldSpec(str(i), "F", "integer", ("root", str(i))) for i in range(256))
        spec = FieldSpec("root", "Root", "entries", ("root",), entries=(EntrySpec("one", "One", children),))
        with self.assertRaises(SourceTooLargeError):
            self.read(b'', specs=(spec,), sections=(SectionSpec("g", "G", ("root",)),))
        child = FieldSpec("leaf", "Leaf", "string", ("a", "b", "c", "d", "e", "value"))
        for index, name in reversed(list(enumerate(("a", "b", "c", "d", "e")))):
            child = FieldSpec(name, name, "entries", tuple(("a", "b", "c", "d", "e")[:index+1]), entries=(EntrySpec(name, name, (child,)),))
        with self.assertRaises(SourceTooLargeError):
            self.read(b'', specs=(child,), sections=(SectionSpec("g", "G", ("a",)),))

    def test_descriptor_metadata_invalid_fails_closed(self):
        with self.assertRaises(DocumentError):
            self.read(specs=(replace(SPECS[0], constraints={"maximum": float('inf')}), *SPECS[1:]))

    def test_utf8_source_and_frame_bounds(self):
        with self.assertRaises(SourceTooLargeError):
            self.validate(draft={"kind": "raw", "raw_toml": '#'+ 'é' * 140000})
        with self.assertRaises(DocumentError):
            self.read(b'\xff')

    def test_toml_only_raw_allowed_structured_denied(self):
        ctx = context(support_level="toml_only", schema_profile={"schema_id": "codex", "schema_revision": "r1", "host_version": "v1", "support_level": "toml_only"})
        self.assertTrue(self.validate(ctx=ctx)["valid"])
        with self.assertRaises(DocumentError):
            self.validate(ctx=ctx, draft={"kind": "structured", "changes": [{"field_id": "model", "operation": "unset"}]})

    def test_result_metadata_deep_detached(self):
        ctx = context(inherited_values={"model": "inherited"})
        snapshot = self.read(ctx=ctx)
        snapshot["target"]["display_name"] = "changed"
        self.assertEqual(ctx.target["display_name"], "config")

    def test_secret_child_cannot_escape_as_separate_section_field(self):
        specs = (FieldSpec("secret", "Secret", "entries", ("secret",), sensitivity="secret"),
                 FieldSpec("child", "Child", "string", ("secret", "token")))
        sections = (SectionSpec("main", "Main", ("secret", "child")),)
        with self.assertRaises(DocumentError):
            self.read(b'[secret]\ntoken="private-sentinel"\n', specs=specs, sections=sections)

    def test_nested_secret_sibling_alias_rejected_but_owned_child_hidden(self):
        token = FieldSpec("token", "Token", "string", ("profiles", "credentials", "token"))
        secret = FieldSpec("credentials", "Credentials", "entries", ("profiles", "credentials"),
                           sensitivity="secret")
        source = b'[profiles.credentials]\ntoken="NESTED_SECRET_SENTINEL"\n'
        sections = (SectionSpec("main", "Main", ("profiles",)),)
        for children in ((secret, token), (token, secret)):
            parent = FieldSpec("profiles", "Profiles", "entries", ("profiles",),
                               entries=(EntrySpec("profile-a", "A", children),))
            with self.assertRaises(DocumentError):
                self.read(source, specs=(parent,), sections=sections)
        owned_secret = replace(secret, entries=(EntrySpec("credentials-a", "Credentials", (token,)),))
        parent = FieldSpec("profiles", "Profiles", "entries", ("profiles",),
                           entries=(EntrySpec("profile-a", "A", (owned_secret,)),))
        result = self.read(source, specs=(parent,), sections=sections)
        self.assertNotIn("NESTED_SECRET_SENTINEL", json.dumps(result["structured"]))
        rendered_secret = result["structured"]["sections"][0]["fields"][0]["entries"][0]["fields"][0]
        self.assertTrue(rendered_secret["configured"])
        self.assertNotIn("entries", rendered_secret)

    def test_save_guard_only_checks_bound_hash(self):
        result = self.preview(draft={"kind": "raw", "raw_toml": BASE.decode().replace('8080', '9000')})
        self.assertEqual(assert_save_allowed(BASE, result["candidate_raw_toml"], result["candidate_content_hash"]), result["candidate_content_hash"])
        with self.assertRaises(DocumentError):
            assert_save_allowed(BASE, result["candidate_raw_toml"], "sha256:"+'0'*64)


if __name__ == '__main__':
    unittest.main()
