import unittest
from typing import Any

from model_deck.engine.routing.ports import (
    ContinuationScope,
    ExecutionMode,
    RegistrationNotFoundError,
    RegistrationRemovedError,
    RouteResolveRequest,
    RouteResolver,
    RouteSnapshot,
    UnsupportedCapabilityError,
)
from model_deck.engine.sessions.ports import (
    CreateSessionCommand,
    GetSessionCommand,
    SelectModelCommand,
    SessionActiveRunConflictError,
    SessionNotFoundError,
    SessionRecord,
    SessionRepository,
    SessionRevisionConflictError,
)
from model_deck.engine.sessions.use_cases import (
    CreateSessionUseCase,
    GetSessionUseCase,
    SelectSessionModelUseCase,
)

REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
REGISTRATION_ID_ALT = "550e8400-e29b-41d4-a716-446655440011"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
CONNECTION_ID_ALT = "550e8400-e29b-41d4-a716-446655440012"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"
HOST_CTX = "ref:host.context"


def _route_snapshot(**overrides: Any) -> RouteSnapshot:
    base = {
        "registration_id": REGISTRATION_ID,
        "registration_revision": 1,
        "connection_id": CONNECTION_ID,
        "connection_revision": 1,
        "provider_id": "com.example.provider",
        "provider_model_id": "provider/model-a",
        "execution_mode": ExecutionMode.CHAT_COMPLETIONS,
    }
    base.update(overrides)
    return RouteSnapshot(**base)


def _continuation_scope(**overrides: Any) -> ContinuationScope:
    base = {
        "connection_id": CONNECTION_ID,
        "provider_model_id": "provider/model-a",
        "provider_id": "com.example.provider",
        "execution_mode": ExecutionMode.CHAT_COMPLETIONS,
        "handle": "ref:continuation.handle",
    }
    base.update(overrides)
    return ContinuationScope(**base)


def _session_record(**overrides: Any) -> SessionRecord:
    base = {
        "session_id": SESSION_ID,
        "registration_id": REGISTRATION_ID,
        "revision": 2,
    }
    base.update(overrides)
    return SessionRecord(**base)


class RecordingRouteResolver:
    def __init__(self) -> None:
        self.requests: list[RouteResolveRequest] = []
        self.result: RouteSnapshot = _route_snapshot()
        self.error: BaseException | None = None

    def resolve_active_registration(self, request: RouteResolveRequest) -> RouteSnapshot:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingSessionRepository:
    def __init__(self) -> None:
        self.create_calls: list[CreateSessionCommand] = []
        self.get_calls: list[GetSessionCommand] = []
        self.select_calls: list[SelectModelCommand] = []
        self.create_result = _session_record(revision=1)
        self.get_result = _session_record()
        self.select_result = _session_record(revision=3)
        self.get_error: BaseException | None = None
        self.create_error: BaseException | None = None
        self.select_error: BaseException | None = None

    def create(self, command: CreateSessionCommand) -> SessionRecord:
        self.create_calls.append(command)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def get(self, command: GetSessionCommand) -> SessionRecord:
        self.get_calls.append(command)
        if self.get_error is not None:
            raise self.get_error
        return self.get_result

    def select_model(self, command: SelectModelCommand) -> SessionRecord:
        self.select_calls.append(command)
        if self.select_error is not None:
            raise self.select_error
        return self.select_result


class SessionUseCaseTests(unittest.TestCase):
    def test_create_forwards_route_resolve_and_repository(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        use_case = CreateSessionUseCase(repo, resolver)
        result = use_case.execute(
            {
                "registration_id": REGISTRATION_ID,
                "host_context_ref": HOST_CTX,
            }
        )
        self.assertEqual(len(resolver.requests), 1)
        self.assertEqual(resolver.requests[0].registration_id, REGISTRATION_ID)
        self.assertEqual(len(repo.create_calls), 1)
        self.assertEqual(repo.create_calls[0].registration_id, REGISTRATION_ID)
        self.assertEqual(repo.create_calls[0].host_context_ref, HOST_CTX)
        self.assertEqual(
            result,
            {
                "session_id": repo.create_result.session_id,
                "revision": repo.create_result.revision,
            },
        )

    def test_create_route_failure_does_not_mutate_repository(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        resolver.error = RegistrationNotFoundError("missing")
        use_case = CreateSessionUseCase(repo, resolver)
        with self.assertRaises(RegistrationNotFoundError):
            use_case.execute({"registration_id": REGISTRATION_ID})
        self.assertEqual(len(repo.create_calls), 0)

    def test_create_malformed_params_skip_route_and_repository(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        use_case = CreateSessionUseCase(repo, resolver)
        with self.assertRaises(ValueError):
            use_case.execute({"registration_id": REGISTRATION_ID, "extra": "nope"})
        with self.assertRaises(ValueError):
            use_case.execute({"registration_id": "not-a-uuid"})
        with self.assertRaises(ValueError):
            use_case.execute(
                {
                    "registration_id": REGISTRATION_ID,
                    "host_context_ref": "sk-live-secret",
                }
            )
        self.assertEqual(len(resolver.requests), 0)
        self.assertEqual(len(repo.create_calls), 0)

    def test_create_preserves_uppercase_uuid_spelling(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        upper = "550E8400-E29B-41D4-A716-446655440099"
        repo.create_result = _session_record(
            session_id="550E8400-E29B-41D4-A716-4466554400AA",
            registration_id=upper,
            revision=1,
        )
        use_case = CreateSessionUseCase(repo, resolver)
        use_case.execute({"registration_id": upper})
        self.assertEqual(repo.create_calls[0].registration_id, upper)

    def test_get_maps_not_found_unchanged(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_error = SessionNotFoundError("missing")
        use_case = GetSessionUseCase(repo)
        with self.assertRaises(SessionNotFoundError):
            use_case.execute({"session_id": SESSION_ID})

    def test_get_result_shape_omits_optional_fields(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record()
        use_case = GetSessionUseCase(repo)
        result = use_case.execute({"session_id": SESSION_ID})
        session = result["session"]
        self.assertNotIn("host_context_ref", session)
        self.assertNotIn("continuation_scope", session)

    def test_get_result_includes_optional_session_fields(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(
            host_context_ref=HOST_CTX,
            continuation_scope=_continuation_scope(),
        )
        use_case = GetSessionUseCase(repo)
        result = use_case.execute({"session_id": SESSION_ID})
        session = result["session"]
        self.assertEqual(session["host_context_ref"], HOST_CTX)
        self.assertEqual(
            session["continuation_scope"],
            {
                "connection_id": CONNECTION_ID,
                "provider_model_id": "provider/model-a",
                "provider_id": "com.example.provider",
                "execution_mode": "chat_completions",
                "handle": "ref:continuation.handle",
            },
        )

    def test_get_rejects_unknown_params(self) -> None:
        repo = RecordingSessionRepository()
        use_case = GetSessionUseCase(repo)
        with self.assertRaises(ValueError):
            use_case.execute({"session_id": SESSION_ID, "filter": "x"})
        self.assertEqual(len(repo.get_calls), 0)

    def test_select_forwards_revision_and_default_continuation_reset(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=None)
        resolver = RecordingRouteResolver()
        use_case = SelectSessionModelUseCase(repo, resolver)
        result = use_case.execute(
            {
                "session_id": SESSION_ID,
                "registration_id": REGISTRATION_ID,
                "expected_revision": 2,
            }
        )
        self.assertEqual(len(resolver.requests), 1)
        self.assertEqual(len(repo.select_calls), 1)
        command = repo.select_calls[0]
        self.assertEqual(command.expected_revision, 2)
        self.assertFalse(command.continuation_reset)
        self.assertEqual(
            result,
            {
                "session_id": repo.select_result.session_id,
                "revision": repo.select_result.revision,
            },
        )

    def test_select_forwards_explicit_continuation_reset(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=_continuation_scope())
        resolver = RecordingRouteResolver()
        resolver.result = _route_snapshot(
            registration_id=REGISTRATION_ID_ALT,
            connection_id=CONNECTION_ID_ALT,
            provider_model_id="provider/model-b",
        )
        use_case = SelectSessionModelUseCase(repo, resolver)
        use_case.execute(
            {
                "session_id": SESSION_ID,
                "registration_id": REGISTRATION_ID_ALT,
                "expected_revision": 2,
                "continuation_reset": True,
            }
        )
        self.assertTrue(repo.select_calls[0].continuation_reset)

    def test_select_route_failure_does_not_mutate(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        resolver.error = RegistrationRemovedError("removed")
        use_case = SelectSessionModelUseCase(repo, resolver)
        with self.assertRaises(RegistrationRemovedError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": 2,
                }
            )
        self.assertEqual(len(repo.select_calls), 0)

    def test_select_malformed_params_skip_route_and_repository(self) -> None:
        repo = RecordingSessionRepository()
        resolver = RecordingRouteResolver()
        use_case = SelectSessionModelUseCase(repo, resolver)
        with self.assertRaises(ValueError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": True,
                }
            )
        with self.assertRaises(ValueError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": 2,
                    "continuation_reset": "yes",
                }
            )
        self.assertEqual(len(resolver.requests), 0)
        self.assertEqual(len(repo.select_calls), 0)

    def test_select_propagates_revision_and_active_run_conflicts(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=None)
        resolver = RecordingRouteResolver()
        use_case = SelectSessionModelUseCase(repo, resolver)
        repo.select_error = SessionRevisionConflictError("stale")
        with self.assertRaises(SessionRevisionConflictError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": 2,
                }
            )
        repo.select_error = SessionActiveRunConflictError("busy")
        with self.assertRaises(SessionActiveRunConflictError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": 2,
                }
            )

    def test_select_rejects_cross_scope_without_continuation_reset(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=_continuation_scope())
        resolver = RecordingRouteResolver()
        resolver.result = _route_snapshot(
            registration_id=REGISTRATION_ID_ALT,
            connection_id=CONNECTION_ID_ALT,
            provider_model_id="provider/model-b",
        )
        use_case = SelectSessionModelUseCase(repo, resolver)
        with self.assertRaises(ValueError) as ctx:
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID_ALT,
                    "expected_revision": 2,
                }
            )
        self.assertIn("continuation scope", str(ctx.exception))
        self.assertEqual(len(repo.select_calls), 0)

    def test_select_allows_matching_scope_without_reset(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=_continuation_scope())
        resolver = RecordingRouteResolver()
        use_case = SelectSessionModelUseCase(repo, resolver)
        use_case.execute(
            {
                "session_id": SESSION_ID,
                "registration_id": REGISTRATION_ID,
                "expected_revision": 2,
            }
        )
        self.assertEqual(len(repo.select_calls), 1)

    def test_select_route_errors_surface_explicitly(self) -> None:
        repo = RecordingSessionRepository()
        repo.get_result = _session_record(continuation_scope=None)
        resolver = RecordingRouteResolver()
        use_case = SelectSessionModelUseCase(repo, resolver)
        resolver.error = UnsupportedCapabilityError("unsupported")
        with self.assertRaises(UnsupportedCapabilityError):
            use_case.execute(
                {
                    "session_id": SESSION_ID,
                    "registration_id": REGISTRATION_ID,
                    "expected_revision": 2,
                }
            )
        self.assertEqual(len(repo.select_calls), 0)


if __name__ == "__main__":
    unittest.main()
