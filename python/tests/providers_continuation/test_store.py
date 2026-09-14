"""TDD tests for the private durable SQLite continuation store (B15).

Behavior-preserving scope checks (mirrors legacy ProviderContinuationStore) plus
the B15 contract: trusted nine-field scope, session/scope binding, identity
match, atomicity, sanitized errors, batch atomic ``save_response``, identity
resolver (``load_for_item`` rejects ambiguous collisions; ``load_all`` returns
``[]`` for first-turn replay), and ``clear(scope)``. Tests are registered
through ``load_tests`` so the package's unittest discovery picks them up.
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from model_deck.integrations.providers.continuation import store as cs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _scope(**overrides):
    base = dict(
        session_id="sess-1",
        connection_id="conn-1",
        connection_revision=7,
        provider_id="com.example.provider",
        provider_model_id="model-x",
        execution_mode="responses",
        endpoint_config_ref="ref:endpoint.opaque-1",
        credential_ref="ref:credential.opaque-1",
        continuation_handle="ref:continuation.default",
    )
    base.update(overrides)
    return cs.ContinuationRouteScope(**base)


def _visible_item(text="hello", item_id="mdkc_responses_test_abcdef"):
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _metadata():
    return {"reasoning_content": "secret-reasoning", "signature": "deadbeef"}


def _make_store(parent: str) -> cs.ContinuationStore:
    return cs.ContinuationStore(Path(parent) / "continuations.sqlite3")


def load_tests(loader, tests, pattern):
    fns = [
        test_route_scope_repr_omits_credential_ref,
        test_route_scope_repr_includes_other_fields,
        test_record_repr_omits_metadata_and_response_id,
        test_route_scope_is_frozen,
        test_route_scope_validates_field_types,
        test_route_scope_rejects_non_int_revision,
        test_route_scope_rejects_raw_credential_value,
        test_creates_private_directory_and_file,
        test_rejects_symlink_db,
        test_rejects_world_or_group_writable_parent,
        test_save_load_round_trip,
        test_persists_across_reopen,
        test_session_cannot_load_other_session_record,
        test_scope_mismatch_rejected_on_save,
        test_scope_mismatch_rejected_on_load,
        test_duplicate_exact_save_is_idempotent,
        test_conflicting_save_replacement_rejected,
        test_identity_mismatch_on_load_rejected,
        test_missing_record_raises,
        test_corrupt_metadata_raises,
        test_schema_incompatible_raises,
        test_record_schema_incompatible_raises,
        test_failed_write_installs_nothing,
        test_concurrent_saves_do_not_corrupt_store,
        test_error_messages_omit_sensitive_content,
        test_load_wal_symlink_rejected,
        test_load_for_item_finds_record_by_identity,
        test_load_for_item_raises_when_missing,
        test_load_for_item_raises_on_scope_mismatch,
        test_load_for_item_raises_for_other_session,
        test_load_for_item_rejects_ambiguous_duplicate_identities,
        test_load_all_returns_all_records_for_session,
        test_load_all_returns_empty_for_unbound_first_turn,
        test_load_all_raises_on_scope_mismatch,
        test_load_for_item_uses_visible_item_without_id,
        test_save_response_atomic_for_completed_response,
        test_save_response_preserves_provider_item_order,
        test_load_all_preserves_response_commit_order_when_clock_moves_backward,
        test_save_response_idempotent_for_exact_replay,
        test_save_response_conflicting_replacement_rejected,
        test_save_response_failed_write_installs_nothing,
        test_save_response_rolls_back_after_mid_transaction_conflict,
        test_empty_response_does_not_bind_session,
        test_oversized_metadata_is_rejected,
        test_save_response_binds_session_and_persists,
        test_clear_removes_session_and_records,
        test_two_phase_reset_authorizes_engine_reset_by_handle,
        test_prepare_reset_rejects_wrong_handle,
        test_rollback_reset_preserves_old_continuation,
        test_pending_reset_is_adopted_by_replacement_scope,
        test_clear_is_idempotent_when_unbound,
        test_clear_raises_on_scope_mismatch,
    ]
    return unittest.TestSuite([unittest.FunctionTestCase(fn) for fn in fns])


# ---------------------------------------------------------------------------
# Sanitized repr / dataclass invariants
# ---------------------------------------------------------------------------

def test_route_scope_repr_omits_credential_ref():
    scope = _scope(credential_ref="ref:credential.private-value")
    rendered = repr(scope)
    assert "ref:credential.private-value" not in rendered
    assert "<opaque>" in rendered


def test_route_scope_repr_includes_other_fields():
    scope = _scope(provider_id="com.example.provider", session_id="sess-1",
                    continuation_handle="ref:continuation.handle-x")
    rendered = repr(scope)
    assert "com.example.provider" in rendered
    assert "sess-1" in rendered
    assert "ref:continuation.handle-x" in rendered


def test_record_repr_omits_metadata_and_response_id():
    record = cs.ContinuationRecord(
        response_id="RESPONSE-SECRET",
        item_ref="mdkc_responses_test_abcdef",
        identity_signature="abc123",
        metadata={"reasoning_content": "must-not-appear"},
        created_at=1.0,
    )
    rendered = repr(record)
    assert "RESPONSE-SECRET" not in rendered
    assert "must-not-appear" not in rendered


def test_route_scope_is_frozen():
    scope = _scope()
    try:
        scope.session_id = "other"
    except Exception as exc:
        # dataclasses.FrozenInstanceError is the canonical exception.
        assert "frozen" in type(exc).__name__.lower(), (
            f"unexpected exception: {type(exc).__name__}: {exc!r}")
    else:
        raise AssertionError("Expected ContinuationRouteScope to reject attribute assignment.")


def test_route_scope_validates_field_types():
    try:
        cs.ContinuationRouteScope(
            session_id=123,  # type: ignore[arg-type]
            connection_id="c", connection_revision=1, provider_id="p",
            provider_model_id="m", execution_mode="chat_completions",
            endpoint_config_ref="e", credential_ref="cred",
            continuation_handle="h",
        )
    except cs.ContinuationError:
        return
    raise AssertionError("Expected ContinuationError for non-string session_id.")


def test_route_scope_rejects_non_int_revision():
    try:
        _scope(connection_revision="rev-7")  # type: ignore[arg-type]
    except cs.ContinuationError:
        return
    raise AssertionError("Expected ContinuationError for string connection_revision.")


def test_route_scope_rejects_raw_credential_value():
    with unittest.TestCase().assertRaises(cs.ContinuationError):
        _scope(credential_ref="sk-not-an-opaque-reference")


# ---------------------------------------------------------------------------
# Private-directory / private-file enforcement
# ---------------------------------------------------------------------------

def test_creates_private_directory_and_file():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(), "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        parent_mode = Path(parent).lstat().st_mode
        file_mode = store.path.lstat().st_mode
        assert parent_mode & 0o777 == 0o700, f"parent mode {oct(parent_mode & 0o777)}"
        assert file_mode & 0o777 == 0o600, f"file mode {oct(file_mode & 0o777)}"


def test_rejects_symlink_db():
    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "real.sqlite3"
        target.write_bytes(b"")
        link = Path(parent) / "continuations.sqlite3"
        link.symlink_to(target)
        try:
            cs.ContinuationStore(link).save(_scope(), "resp", "item", _visible_item(), _metadata())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for symlinked DB path.")


def test_rejects_world_or_group_writable_parent():
    with tempfile.TemporaryDirectory() as parent:
        # The check rejects group- or other-writable parents (mask 0o022).
        os.chmod(parent, 0o770)
        try:
            store = _make_store(parent)
            store.save(_scope(), "resp", "item", _visible_item(), _metadata())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for group-writable parent.")


# ---------------------------------------------------------------------------
# Round-trip, persistence, atomicity
# ---------------------------------------------------------------------------

def test_save_load_round_trip():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-42", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        record = store.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        assert record.response_id == "resp-42"
        assert record.metadata == _metadata()


def test_persists_across_reopen():
    with tempfile.TemporaryDirectory() as parent:
        path = Path(parent) / "continuations.sqlite3"
        store = cs.ContinuationStore(path)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        reopened = cs.ContinuationStore(path)
        record = reopened.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        assert record.response_id == "resp-1"


def test_failed_write_installs_nothing():
    """A failure during save must leave the store without partial state."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        # Pass metadata that breaks JSON serialization to force failure.
        bad = {"bad": object()}
        try:
            store.save(scope, "resp-x", "item-x", _visible_item(), bad)
        except cs.ContinuationError:
            pass
        else:
            raise AssertionError("Expected ContinuationError for bad metadata.")
        # Now save a good record — it should land cleanly.
        store.save(scope, "resp-y", "item-y", _visible_item(), _metadata())
        record = store.load(scope, "item-y", _visible_item())
        assert record.response_id == "resp-y"
        assert "item-x" not in record.metadata


# ---------------------------------------------------------------------------
# Session / scope binding
# ---------------------------------------------------------------------------

def test_session_cannot_load_other_session_record():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope_a = _scope(session_id="sess-A")
        store.save(scope_a, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        scope_b = _scope(session_id="sess-B")
        try:
            store.load(scope_b, "mdkc_responses_test_abcdef", _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for cross-session load.")


def test_scope_mismatch_rejected_on_save():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(provider_model_id="model-a"),
                   "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.save(_scope(provider_model_id="model-b"),
                       "resp-1", "mdkc_responses_test_abcdef",
                       _visible_item(), _metadata())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for scope mismatch on resave.")


def test_scope_mismatch_rejected_on_load():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(provider_id="com.example.provider"),
                   "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.load(_scope(provider_id="com.other.provider"),
                       "mdkc_responses_test_abcdef", _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for scope mismatch on load.")


# ---------------------------------------------------------------------------
# Idempotent save, identity mismatch, missing/corrupt
# ---------------------------------------------------------------------------

def test_duplicate_exact_save_is_idempotent():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        # Exact duplicate must not raise.
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        record = store.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        assert record.response_id == "resp-1"


def test_conflicting_save_replacement_rejected():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.save(scope, "resp-2", "mdkc_responses_test_abcdef",
                       _visible_item(), _metadata())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for conflicting replacement.")


def test_identity_mismatch_on_load_rejected():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        tampered = {
            "type": "message",
            "id": "mdkc_responses_test_abcdef",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "tampered"}],
        }
        try:
            store.load(scope, "mdkc_responses_test_abcdef", tampered)
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for identity mismatch on load.")


def test_missing_record_raises():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(), "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.load(_scope(), "mdkc_responses_test_missing", _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for missing record.")


def test_corrupt_metadata_raises():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        connection = sqlite3.connect(store.path)
        try:
            row = connection.execute(
                "SELECT session_id, item_ref FROM records").fetchone()
            connection.execute(
                "UPDATE records SET metadata_json = ? WHERE session_id = ? AND item_ref = ?",
                ("not-json", row[0], row[1]))
            connection.commit()
        finally:
            connection.close()
        try:
            store.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for corrupt metadata JSON.")


def test_schema_incompatible_raises():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                ("999",))
            connection.commit()
        finally:
            connection.close()
        try:
            store.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        except cs.ContinuationError:
            return
    raise AssertionError("Expected ContinuationError for incompatible schema_version.")


def test_record_schema_incompatible_raises():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        item = _visible_item()
        store.save(scope, "resp-1", item["id"], item, _metadata())
        with sqlite3.connect(store.path) as connection:
            connection.execute("UPDATE records SET schema_version = '999'")
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.load(scope, item["id"], item)


# ---------------------------------------------------------------------------
# Concurrency + safety
# ---------------------------------------------------------------------------

def test_concurrent_saves_do_not_corrupt_store():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()

        def worker(index):
            item = {
                "type": "message",
                "id": f"mdkc_responses_t{index}_abcdef",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"hi-{index}"}],
            }
            store.save(scope, f"resp-{index}", item["id"], item, {"index": index})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for index in range(20):
            item = {
                "type": "message",
                "id": f"mdkc_responses_t{index}_abcdef",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"hi-{index}"}],
            }
            record = store.load(scope, item["id"], item)
            assert record.response_id == f"resp-{index}"


# ---------------------------------------------------------------------------
# Sanitized errors
# ---------------------------------------------------------------------------

def test_error_messages_omit_sensitive_content():
    sensitive = "MUST-NOT-LEAK-RESP-ID-9999"
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, sensitive, "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.load(scope, "mdkc_responses_test_abcdef",
                       {"type": "message", "role": "user", "content": []})
        except cs.ContinuationError as error:
            assert sensitive not in str(error), (
                f"ContinuationError leaked response_id: {error!r}")
            assert "must-not-appear" not in str(error), (
                f"ContinuationError leaked metadata: {error!r}")
        else:
            raise AssertionError("Expected ContinuationError for missing record.")


def test_load_wal_symlink_rejected():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        wal_path = store.path.with_name(store.path.name + "-wal")
        shm_path = store.path.with_name(store.path.name + "-shm")
        # Force a WAL checkpoint so sidecars exist on disk.
        connection = sqlite3.connect(store.path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        if wal_path.exists():
            wal_path.unlink()
        if shm_path.exists():
            shm_path.unlink()
        # Replace the WAL sidecar with a symlink to a foreign regular file.
        external = Path(parent) / "external.sqlite3-wal"
        external.write_bytes(b"")
        wal_path.symlink_to(external)
        try:
            cs.ContinuationStore(store.path).load(
                scope, "mdkc_responses_test_abcdef", _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError when WAL sidecar is symlink.")


# ---------------------------------------------------------------------------
# Identity resolver (V2 history strips provider item IDs)
# ---------------------------------------------------------------------------

def test_load_for_item_finds_record_by_identity():
    """V2 strips provider item IDs; next-run load has only visible content."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(text="hello"), _metadata())
        # Re-hydrated visible item with a different id (V2 normalized history).
        stripped = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}]}
        record = store.load_for_item(scope, stripped)
        assert record.response_id == "resp-1"
        assert record.item_ref == "mdkc_responses_test_abcdef"
        assert record.metadata == _metadata()


def test_load_for_item_raises_when_missing():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(text="hello"), _metadata())
        different = {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "different"}]}
        try:
            store.load_for_item(scope, different)
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for missing-by-identity.")


def test_load_for_item_raises_on_scope_mismatch():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(provider_id="com.example.provider"),
                   "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.load_for_item(_scope(provider_id="com.other.provider"),
                                _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for scope mismatch on load_for_item.")


def test_load_for_item_raises_for_other_session():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope_a = _scope(session_id="sess-A")
        store.save(scope_a, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        scope_b = _scope(session_id="sess-B")
        try:
            store.load_for_item(scope_b, _visible_item())
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for cross-session load_for_item.")


def test_load_for_item_rejects_ambiguous_duplicate_identities():
    """Two records under different item_refs but identical content must not silently match one."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        # Two distinct item_refs, same visible content -> same identity_signature.
        item_a = _visible_item(text="same", item_id="mdkc_responses_a_abcdef")
        item_b = _visible_item(text="same", item_id="mdkc_responses_b_abcdef")
        store.save(scope, "resp-a", item_a["id"], item_a, {"k": "a"})
        store.save(scope, "resp-b", item_b["id"], item_b, {"k": "b"})
        try:
            store.load_for_item(scope, _visible_item(text="same"))
        except cs.ContinuationError as error:
            assert "ambiguous" in str(error).lower() or "multiple" in str(error).lower(), (
                f"expected ambiguity message, got: {error!r}")
            return
        raise AssertionError("Expected ContinuationError for ambiguous identity collision.")


def test_load_for_item_uses_visible_item_without_id():
    """V2 normalized items may carry no id at all; the identity match still works."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(text="hello"), _metadata())
        # No id key in the visible item at all.
        stripped = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}]}
        record = store.load_for_item(scope, stripped)
        assert record.response_id == "resp-1"


# ---------------------------------------------------------------------------
# load_all: enumeration + first-turn unbound
# ---------------------------------------------------------------------------

def test_load_all_returns_all_records_for_session():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        # Three distinct records under the same session.
        for index in range(3):
            item = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": f"msg-{index}"}],
                    "id": f"mdkc_responses_t{index}_abcdef"}
            store.save(scope, f"resp-{index}", item["id"], item,
                       {"index": index})
        # Add a record under a different session — must not be returned.
        other_scope = _scope(session_id="sess-other")
        other_item = {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": "other"}],
                      "id": "mdkc_responses_other_abcdef"}
        store.save(other_scope, "resp-other", other_item["id"], other_item, {})

        records = store.load_all(scope)
        assert len(records) == 3
        response_ids = sorted(record.response_id for record in records)
        assert response_ids == ["resp-0", "resp-1", "resp-2"]


def test_load_all_returns_empty_for_unbound_first_turn():
    """First-turn replay on a never-saved session must return [] cleanly."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope(session_id="never-bound")
        records = store.load_all(scope)
        assert records == []


def test_load_all_raises_on_scope_mismatch():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(provider_id="com.example.provider"),
                   "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.load_all(_scope(provider_id="com.other.provider"))
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for scope mismatch on load_all.")


# ---------------------------------------------------------------------------
# Batch atomic save_response
# ---------------------------------------------------------------------------

def test_save_response_atomic_for_completed_response():
    """Every item from one response lands in a single transaction."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        items = []
        for index in range(4):
            item = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": f"r{index}"}],
                    "id": f"mdkc_responses_b{index}_abcdef"}
            items.append((item["id"], item, {"i": index}))
        store.save_response(scope, "resp-batch", items)
        records = store.load_all(scope)
        assert sorted(r.item_ref for r in records) == sorted(item[0] for item in items)
        assert all(r.response_id == "resp-batch" for r in records)


def test_save_response_preserves_provider_item_order():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        items = [
            (
                item_ref,
                _visible_item(text=text, item_id=item_ref),
                {"position": position},
            )
            for position, (item_ref, text) in enumerate(
                (("z-provider-item", "first"), ("a-provider-item", "second"))
            )
        ]
        store.save_response(scope, "resp-ordered", items)
        records = store.load_all(scope)
        assert [record.item_ref for record in records] == ["z-provider-item", "a-provider-item"]
        assert [record.item_index for record in records] == [0, 1]


def test_load_all_preserves_response_commit_order_when_clock_moves_backward():
    """Provider response order must not depend on wall-clock monotonicity."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        first = _visible_item(text="first", item_id="item-first")
        second = _visible_item(text="second", item_id="item-second")
        with patch.object(cs.time, "time", side_effect=[20.0, 10.0]):
            store.save_response(scope, "resp-z", [(first["id"], first, {"order": 1})])
            store.save_response(scope, "resp-a", [(second["id"], second, {"order": 2})])

        records = store.load_all(scope)
        assert [record.item_ref for record in records] == ["item-first", "item-second"]


def test_save_response_idempotent_for_exact_replay():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        item = _visible_item()
        store.save_response(scope, "resp-1", [(item["id"], item, _metadata())])
        # Exact replay must not raise.
        store.save_response(scope, "resp-1", [(item["id"], item, _metadata())])
        record = store.load(scope, item["id"], item)
        assert record.response_id == "resp-1"


def test_save_response_conflicting_replacement_rejected():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        item = _visible_item()
        store.save_response(scope, "resp-1", [(item["id"], item, _metadata())])
        try:
            store.save_response(scope, "resp-2", [(item["id"], item, _metadata())])
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for batch conflicting replacement.")


def test_save_response_failed_write_installs_nothing():
    """A failure mid-batch must leave the store with no partial records."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        good = _visible_item(item_id="mdkc_responses_g_abcdef")
        bad = {"bad": object()}
        # Second item has unencodable metadata — entire batch must be rejected.
        items = [
            (good["id"], good, _metadata()),
            ("mdkc_responses_x_abcdef", good, bad),
        ]
        try:
            store.save_response(scope, "resp-1", items)
        except cs.ContinuationError:
            pass
        else:
            raise AssertionError("Expected ContinuationError for bad metadata.")
        # After the failed batch, the session must remain unbound and empty.
        assert store.load_all(scope) == []


def test_save_response_rolls_back_after_mid_transaction_conflict():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        existing = _visible_item(item_id="mdkc_existing")
        store.save(scope, "resp-old", existing["id"], existing, _metadata())
        new_item = _visible_item(text="new", item_id="mdkc_new")
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.save_response(
                scope,
                "resp-new",
                [
                    (new_item["id"], new_item, _metadata()),
                    (existing["id"], existing, _metadata()),
                ],
            )
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.load(scope, new_item["id"], new_item)
        assert store.load(scope, existing["id"], existing).response_id == "resp-old"


def test_empty_response_does_not_bind_session():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save_response(_scope(), "resp-empty", [])
        assert store.load_all(_scope(provider_model_id="other-model")) == []


def test_oversized_metadata_is_rejected():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.save(
                _scope(),
                "resp-large",
                "mdkc_large",
                _visible_item(item_id="mdkc_large"),
                {"opaque": "x" * (cs.MAX_RECORD_METADATA_BYTES + 1)},
            )


def test_save_response_binds_session_and_persists():
    with tempfile.TemporaryDirectory() as parent:
        path = Path(parent) / "continuations.sqlite3"
        store = cs.ContinuationStore(path)
        scope = _scope()
        item = _visible_item()
        store.save_response(scope, "resp-1", [(item["id"], item, _metadata())])
        # Re-open the store and confirm the session is bound and the record lands.
        reopened = cs.ContinuationStore(path)
        record = reopened.load(scope, item["id"], item)
        assert record.response_id == "resp-1"


# ---------------------------------------------------------------------------
# clear(scope)
# ---------------------------------------------------------------------------

def test_clear_removes_session_and_records():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        store.save(scope, "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        store.clear(scope)
        assert store.load_all(scope) == []
        # A subsequent save re-binds the session cleanly.
        store.save(scope, "resp-2", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        record = store.load(scope, "mdkc_responses_test_abcdef", _visible_item())
        assert record.response_id == "resp-2"


def test_two_phase_reset_authorizes_engine_reset_by_handle():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        old_scope = _scope()
        item = _visible_item()
        store.save(old_scope, "resp-1", item["id"], item, _metadata())

        reset_token = store.prepare_session_reset(
            old_scope.session_id, old_scope.continuation_handle
        )
        assert reset_token is not None
        assert store.load(old_scope, item["id"], item).response_id == "resp-1"
        store.commit_session_reset(reset_token)

        replacement_scope = _scope(
            provider_model_id="model-y",
            continuation_handle="ref:continuation.replacement",
        )
        replacement_item = _visible_item(text="replacement", item_id="replacement-item")
        store.save(
            replacement_scope,
            "resp-2",
            replacement_item["id"],
            replacement_item,
            _metadata(),
        )
        assert store.load_all(replacement_scope)[0].response_id == "resp-2"


def test_prepare_reset_rejects_wrong_handle():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        item = _visible_item()
        store.save(scope, "resp-1", item["id"], item, _metadata())
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.prepare_session_reset(
                scope.session_id, "ref:continuation.wrong"
            )
        assert store.load(scope, item["id"], item).response_id == "resp-1"


def test_rollback_reset_preserves_old_continuation():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope()
        item = _visible_item()
        store.save(scope, "resp-1", item["id"], item, _metadata())
        reset_token = store.prepare_session_reset(
            scope.session_id, scope.continuation_handle
        )
        assert reset_token is not None

        store.rollback_session_reset(reset_token)

        assert store.load(scope, item["id"], item).response_id == "resp-1"
        with unittest.TestCase().assertRaises(cs.ContinuationError):
            store.load_all(
                _scope(
                    provider_model_id="model-y",
                    continuation_handle="ref:continuation.replacement",
                )
            )


def test_pending_reset_is_adopted_by_replacement_scope():
    """A crash after the session commit can finish retirement on next load."""
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        old_scope = _scope()
        item = _visible_item()
        store.save(old_scope, "resp-1", item["id"], item, _metadata())
        reset_token = store.prepare_session_reset(
            old_scope.session_id, old_scope.continuation_handle
        )
        assert reset_token is not None
        replacement_scope = _scope(
            provider_model_id="model-y",
            continuation_handle="ref:continuation.replacement",
        )

        assert store.load_all(replacement_scope) == []
        replacement_item = _visible_item(text="new", item_id="new-item")
        store.save(
            replacement_scope,
            "resp-2",
            replacement_item["id"],
            replacement_item,
            _metadata(),
        )
        assert store.load_all(replacement_scope)[0].response_id == "resp-2"


def test_clear_is_idempotent_when_unbound():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        scope = _scope(session_id="never-bound")
        # No error, no side effect.
        store.clear(scope)
        assert store.load_all(scope) == []


def test_clear_raises_on_scope_mismatch():
    with tempfile.TemporaryDirectory() as parent:
        store = _make_store(parent)
        store.save(_scope(provider_id="com.example.provider"),
                   "resp-1", "mdkc_responses_test_abcdef",
                   _visible_item(), _metadata())
        try:
            store.clear(_scope(provider_id="com.other.provider"))
        except cs.ContinuationError:
            return
        raise AssertionError("Expected ContinuationError for scope mismatch on clear.")
