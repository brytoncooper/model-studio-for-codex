import hashlib
import itertools
import json
import unittest
from collections.abc import Mapping
from typing import Any

from model_deck.engine.dispatch import EngineDispatch, principal_id_for_client_name
from model_deck.engine.host_settings.ports import (
    READ_GRANT,
    WRITE_GRANT,
    CallerContext,
    PreviewExistsError,
    PreviewRecord,
    ReceiptConflictError,
    SettingsUnsupportedError,
)
from model_deck_contracts.validator import validate_schema_ref
from model_deck.engine.host_settings.service import HostSettingsService

ENGINE_ID = "550e8400-e29b-41d4-a716-446655440001"
NONCE = "nonce-1"
HOST = "com.example.host"
DOC = "ref:codex-user-config"
CTX = "ctx-1"
RAW = "key = 1\n"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot() -> dict[str, Any]:
    return {
        "host_id": HOST,
        "document_id": DOC,
        "document_revision": _sha(RAW),
        "exists": True,
        "target": {
            "display_name": "config",
            "display_path": "/tmp/config.toml",
            "scope": "user",
            "writable": True,
        },
        "schema_profile": {
            "schema_id": "codex-settings",
            "schema_revision": "3",
            "host_version": "1.0",
            "support_level": "supported",
        },
        "precedence": [],
        "raw_toml": RAW,
        "structured": {"sections": []},
        "context_revision": CTX,
    }


def _candidate(raw: str = RAW) -> dict[str, Any]:
    return {
        "valid": True,
        "validation_level": "schema",
        "candidate_content_hash": _sha(raw),
        "candidate_raw_toml": raw,
        "candidate_structured": {"sections": []},
        "diagnostics": [],
        "context_revision": CTX,
    }


def _adapter_preview(raw: str = RAW) -> dict[str, Any]:
    base = _candidate(raw)
    base["preview"] = {
        "base_content_hash": _sha(RAW),
        "candidate_content_hash": _sha(raw),
        "changed": raw != RAW,
        "diff": "-a\n+b\n",
        "diff_truncated": False,
        "changed_field_ids": [],
        "application_effects": [],
        "protected_projection_changes": False,
    }
    return base


def _save_result(raw: str = RAW) -> dict[str, Any]:
    return {
        "saved": True,
        "changed": raw != RAW,
        "document_id": DOC,
        "previous_content_hash": _sha(RAW),
        "document_revision": _sha(raw),
        "backup": None,
        "application_effects": [],
        "context_revision": CTX,
    }


class FakeDocument:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unsupported_host: str | None = None

    def read(self, host_id: str) -> dict[str, Any]:
        self.calls.append("read")
        if host_id == self.unsupported_host:
            raise SettingsUnsupportedError("settings capability unsupported")
        return _snapshot()

    def validate(self, *args: Any) -> dict[str, Any]:
        self.calls.append("validate")
        return _candidate()

    def preview(self, *args: Any) -> dict[str, Any]:
        self.calls.append("preview")
        return _adapter_preview()

    def save(self, *args: Any) -> dict[str, Any]:
        self.calls.append("save")
        return _save_result()


class FakePreviews:
    def __init__(self) -> None:
        self.live: dict[str, PreviewRecord] = {}
        self.consumed: set[str] = set()

    def admit(self, record: PreviewRecord) -> None:
        if record.preview_id in self.live or record.preview_id in self.consumed:
            raise PreviewExistsError("duplicate preview token")
        self.live[record.preview_id] = record

    def lookup(self, preview_id: str) -> PreviewRecord | None:
        return self.live.get(preview_id)

    def is_consumed(self, preview_id: str) -> bool:
        return preview_id in self.consumed

    def consume(self, preview_id: str) -> PreviewRecord | None:
        record = self.live.pop(preview_id, None)
        if record is not None:
            self.consumed.add(preview_id)
        return record


class FakeReceipts:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[str, dict[str, Any] | None, Any]] = {}
        self.preview_index: dict[str, tuple[str, str, str]] = {}

    def lookup(self, principal: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        hit = self.records.get((principal, key))
        if hit is None or hit[0] != fingerprint or hit[1] is None:
            return None
        return dict(hit[1])

    def store(self, principal: str, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        self.records[(principal, key)] = (fingerprint, dict(result), None)

    def claim(self, binding: Any) -> tuple[str, dict[str, Any] | None]:
        owner = self.preview_index.get(binding.preview_id)
        mine = (binding.principal, binding.idempotency_key, binding.fingerprint)
        if owner is not None and owner != mine:
            raise ReceiptConflictError("preview already reserved")
        hit = self.records.get((binding.principal, binding.idempotency_key))
        if hit is not None and hit[0] != binding.fingerprint:
            raise ReceiptConflictError("idempotency key reuse")
        if hit is None:
            self.records[(binding.principal, binding.idempotency_key)] = (
                binding.fingerprint,
                None,
                binding,
            )
            self.preview_index[binding.preview_id] = mine
            return ("admitted", None)
        if hit[1] is not None:
            return ("replay", dict(hit[1]))
        return ("in_progress", None)

    def settle(self, principal: str, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        hit = self.records.get((principal, key))
        if hit is None or hit[0] != fingerprint:
            raise ReceiptConflictError("no admitted claim")
        self.records[(principal, key)] = (fingerprint, dict(result), hit[2])

    def release(self, principal: str, key: str, fingerprint: str) -> None:
        hit = self.records.get((principal, key))
        if hit is None or hit[0] != fingerprint or hit[1] is not None:
            return
        preview_id = getattr(hit[2], "preview_id", None)
        del self.records[(principal, key)]
        if self.preview_index.get(preview_id) == (principal, key, fingerprint):
            del self.preview_index[preview_id]


class _ListModelsStub:
    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        return {"models": []}


class _IdentityStub:
    engine_instance_id = ENGINE_ID
    instance_nonce = NONCE


class _EnrollmentStub:
    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool:
        return credential == "cred"


def _service(doc: FakeDocument, local_operator: str) -> HostSettingsService:
    tokens = (f"tok-{n}" for n in itertools.count(1))
    return HostSettingsService(doc, FakePreviews(), FakeReceipts(), lambda: next(tokens), local_operator)


def _dispatch(
    service: HostSettingsService | None, caller: CallerContext | None = None
) -> EngineDispatch:
    return EngineDispatch(
        _ListModelsStub(),
        _IdentityStub(),
        _EnrollmentStub(),
        host_settings=service,
        host_settings_caller=caller,
    )


def _assert_domain_error(response: dict[str, Any], code: str) -> None:
    assert response["error"]["code"] == -32000
    data = response["error"]["data"]
    validate_schema_ref("contracts/common/error.schema.json", data)
    assert data["code"] == code
    assert set(data.keys()) <= {"code", "message", "retryable", "request_id"}
    assert RAW not in json.dumps(response)


def _hello_frame(client_name: str, request_id: int = 1, credential: str = "cred") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "engine.v1.hello",
        "params": {
            "client_name": client_name,
            "offered_api": {"major": 1, "minor": 0},
            "required_capabilities": [],
            "authentication": {
                "engine_instance_id": ENGINE_ID,
                "instance_nonce": NONCE,
                "credential": credential,
            },
        },
    }


def _call(dispatch: EngineDispatch, connection: int, method: str, params: dict[str, Any], request_id: int = 2) -> dict[str, Any]:
    response = dispatch.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, connection)
    assert response is not None
    return response


class HostSettingsDispatchTests(unittest.TestCase):
    def test_service_error_details_are_not_wire_messages(self):
        from model_deck.engine.host_settings import ports

        for error_type in (
            ports.SettingsDeniedError, ports.SettingsNotFoundError,
            ports.SettingsConflictError, ports.SettingsVersionMismatchError,
            ports.SettingsUnsupportedError, ports.SettingsExhaustedError,
            ports.SettingsInvalidError, ports.SettingsInternalError,
        ):
            with self.subTest(error=error_type.__name__):
                detail = "private-document-detail-" + "x" * 2100
                error = (error_type(detail, reason=detail)
                         if error_type is ports.SettingsConflictError else error_type(detail))

                class FailingService:
                    def read(self, *args):
                        raise error

                    validate = preview = save = read

                caller = CallerContext("operator", frozenset({READ_GRANT, WRITE_GRANT}))
                dispatch = _dispatch(FailingService(), caller)
                self._authenticated(dispatch, 1)
                response = _call(dispatch, 1, "engine.v1.hosts.settings.read", {"host_id": HOST})
                validate_schema_ref("contracts/common/error.schema.json", response["error"]["data"])
                self.assertNotIn("private-document-detail", json.dumps(response))

    def _authenticated(self, dispatch: EngineDispatch, connection: int, client_name: str = "test") -> None:
        response = dispatch.handle(_hello_frame(client_name), connection)
        assert response is not None
        self.assertTrue(response["result"]["authenticated"])

    def _operator_dispatch(self, doc: FakeDocument) -> tuple[EngineDispatch, str]:
        local_operator = principal_id_for_client_name("test", ENGINE_ID)
        caller = CallerContext(principal=local_operator, grants=frozenset({READ_GRANT, WRITE_GRANT}))
        return _dispatch(_service(doc, local_operator), caller), local_operator

    def _authenticated_as(
        self, dispatch: EngineDispatch, connection: int, client_name: str, credential: str = "cred"
    ) -> dict[str, Any]:
        response = dispatch.handle(_hello_frame(client_name, credential=credential), connection)
        assert response is not None
        return response

    def test_operations_list_includes_host_settings_when_configured(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 11)
        response = _call(dispatch, 11, "engine.v1.operations.list", {})
        ids = {entry["operation_id"] for entry in response["result"]["operations"]}
        self.assertTrue(
            {
                "engine.v1.hosts.settings.read",
                "engine.v1.hosts.settings.validate",
                "engine.v1.hosts.settings.preview",
                "engine.v1.hosts.settings.save",
            }.issubset(ids)
        )

    def test_read_routed_through_framed_dispatch(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 12)
        response = _call(dispatch, 12, "engine.v1.hosts.settings.read", {"host_id": HOST})
        self.assertEqual(response["result"]["snapshot"]["document_id"], DOC)
        self.assertEqual(doc.calls, ["read"])

    def test_validate_preview_save_routed_through_framed_dispatch(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 13)
        draft = {"kind": "raw", "raw_toml": RAW}
        base = {
            "host_id": HOST,
            "document_id": DOC,
            "expected_content_hash": _sha(RAW),
            "context_revision": CTX,
            "draft": draft,
        }
        validated = _call(dispatch, 13, "engine.v1.hosts.settings.validate", dict(base), 21)
        self.assertTrue(validated["result"]["valid"])
        previewed = _call(dispatch, 13, "engine.v1.hosts.settings.preview", dict(base), 22)
        preview_id = previewed["result"]["preview"]["preview_id"]
        saved = _call(
            dispatch,
            13,
            "engine.v1.hosts.settings.save",
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "preview_id": preview_id,
                "candidate_content_hash": _sha(RAW),
                "candidate_raw_toml": RAW,
                "idempotency_key": "key-1",
            },
            23,
        )
        self.assertTrue(saved["result"]["saved"])
        self.assertEqual(doc.calls, ["validate", "preview", "validate", "save"])

    def test_unauthenticated_connection_denied(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        response = _call(dispatch, 99, "engine.v1.hosts.settings.read", {"host_id": HOST})
        _assert_domain_error(response, "capability_denied")
        self.assertEqual(doc.calls, [])

    def test_wrong_credential_denied_without_touching_adapter(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        hello = self._authenticated_as(dispatch, 14, "other-plugin", credential="wrong")
        _assert_domain_error(hello, "capability_denied")
        response = _call(dispatch, 14, "engine.v1.hosts.settings.read", {"host_id": HOST})
        _assert_domain_error(response, "capability_denied")
        self.assertEqual(doc.calls, [])

    def test_client_name_rename_cannot_change_access(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 19, client_name="test")
        first = _call(dispatch, 19, "engine.v1.hosts.settings.read", {"host_id": HOST}, 61)
        self._authenticated_as(dispatch, 20, "renamed-plugin")
        second = _call(dispatch, 20, "engine.v1.hosts.settings.read", {"host_id": HOST}, 62)
        self.assertEqual(first["result"]["snapshot"]["document_id"], DOC)
        self.assertEqual(second["result"]["snapshot"]["document_id"], DOC)
        self.assertEqual(doc.calls, ["read", "read"])

    def test_missing_operator_context_denies_all_methods(self) -> None:
        doc = FakeDocument()
        local_operator = principal_id_for_client_name("test", ENGINE_ID)
        dispatch = _dispatch(_service(doc, local_operator))
        self._authenticated(dispatch, 21)
        for method in (
            "engine.v1.hosts.settings.read",
            "engine.v1.hosts.settings.validate",
            "engine.v1.hosts.settings.preview",
            "engine.v1.hosts.settings.save",
        ):
            response = _call(dispatch, 21, method, {}, 70)
            _assert_domain_error(response, "capability_denied")
            self.assertIn("operator context", response["error"]["data"]["message"])
        self.assertEqual(doc.calls, [])

    def test_missing_service_reports_unsupported(self) -> None:
        dispatch = _dispatch(None)
        self._authenticated(dispatch, 15)
        for method in (
            "engine.v1.hosts.settings.read",
            "engine.v1.hosts.settings.validate",
            "engine.v1.hosts.settings.preview",
            "engine.v1.hosts.settings.save",
        ):
            response = _call(dispatch, 15, method, {}, 30)
            _assert_domain_error(response, "unsupported_capability")

    def test_unsupported_host_maps_to_unsupported_capability(self) -> None:
        doc = FakeDocument()
        doc.unsupported_host = HOST
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 16)
        response = _call(dispatch, 16, "engine.v1.hosts.settings.read", {"host_id": HOST})
        _assert_domain_error(response, "unsupported_capability")

    def test_consumed_preview_conflict_carries_reason(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 17)
        draft = {"kind": "raw", "raw_toml": RAW}
        base = {
            "host_id": HOST,
            "document_id": DOC,
            "expected_content_hash": _sha(RAW),
            "context_revision": CTX,
            "draft": draft,
        }
        previewed = _call(dispatch, 17, "engine.v1.hosts.settings.preview", dict(base), 41)
        preview_id = previewed["result"]["preview"]["preview_id"]
        save_params = {
            "host_id": HOST,
            "document_id": DOC,
            "expected_content_hash": _sha(RAW),
            "context_revision": CTX,
            "preview_id": preview_id,
            "candidate_content_hash": _sha(RAW),
            "candidate_raw_toml": RAW,
        }
        first = _call(dispatch, 17, "engine.v1.hosts.settings.save", {**save_params, "idempotency_key": "key-1"}, 42)
        self.assertTrue(first["result"]["saved"])
        second = _call(dispatch, 17, "engine.v1.hosts.settings.save", {**save_params, "idempotency_key": "key-2"}, 43)
        _assert_domain_error(second, "conflict")
        self.assertIn("preview_consumed", second["error"]["data"]["message"])
        self.assertNotIn("reason", second["error"]["data"])

    def test_unknown_conflict_reason_is_not_echoed(self) -> None:
        from model_deck.engine.host_settings.ports import SettingsConflictError
        secret = "s3cret-" + ("x" * 2100)
        class LeakingDocument(FakeDocument):
            def save(self, *args: object) -> dict[str, object]:
                raise SettingsConflictError("boom " + secret, reason=secret)
        dispatch, _ = self._operator_dispatch(LeakingDocument())
        self._authenticated(dispatch, 22)
        draft = {"kind": "raw", "raw_toml": RAW}
        base = {
            "host_id": HOST,
            "document_id": DOC,
            "expected_content_hash": _sha(RAW),
            "context_revision": CTX,
            "draft": draft,
        }
        previewed = _call(dispatch, 22, "engine.v1.hosts.settings.preview", dict(base), 81)
        preview_id = previewed["result"]["preview"]["preview_id"]
        response = _call(
            dispatch,
            22,
            "engine.v1.hosts.settings.save",
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "preview_id": preview_id,
                "candidate_content_hash": _sha(RAW),
                "candidate_raw_toml": RAW,
                "idempotency_key": "key-secret",
            },
            82,
        )
        _assert_domain_error(response, "conflict")
        body = json.dumps(response)
        self.assertNotIn(secret, body)
        self.assertNotIn("s3cret", body)
        self.assertEqual(response["error"]["data"]["message"], "settings save conflict")
        self.assertNotIn("reason", response["error"]["data"])
        self.assertLessEqual(len(body), 2048)

    def test_server_errors_carry_no_raw_toml(self) -> None:
        doc = FakeDocument()
        dispatch, _ = self._operator_dispatch(doc)
        self._authenticated(dispatch, 18)
        response = _call(dispatch, 18, "engine.v1.hosts.settings.read", {"host_id": HOST, "extra": True}, 51)
        self.assertIn("error", response)
        self.assertNotIn(RAW, json.dumps(response))


if __name__ == "__main__":
    unittest.main()
