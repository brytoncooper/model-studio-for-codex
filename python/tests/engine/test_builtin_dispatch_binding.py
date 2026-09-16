"""Routing the built-ins through their descriptors changed nothing observable.

Every built-in operation used to reach a literal ``if method == ...`` branch in
``EngineDispatch``. Now each one is a registered handler on its descriptor, so
the kernel invokes it like any other operation. These tests pin what that
migration could plausibly have broken: whether every operation is still served,
whether connection-scoped state still knows which connection asked, whether a
public domain error still reaches the caller by name, whether a foreign
feature's failure is still redacted — including when that feature names a public
code — and which job directory answers when one of them cannot serve the method.
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from model_deck.adapters.providers.deterministic import DETERMINISTIC_PROVIDER_ID
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import AdditionalEngineFeatures, build_engine_server
from model_deck.engine.builtins import (
    BUILTIN_FEATURE_IDS,
    CAPABILITY_MODELS_LIBRARY,
    CAPABILITY_SUPPORTED,
    FEATURE_CORE,
    FEATURE_MODELS_LIBRARY,
    BuiltinDispatchBinding,
    builtin_descriptor,
    builtin_handler_registry,
    builtin_operation_ids,
)
from model_deck.engine.dispatch import (
    _COMPOSED_DOMAIN_ERROR_MESSAGES,
    EngineDispatch,
)
from model_deck.engine.jobs.ports import JobResumeUnsupportedError
from model_deck.engine.jobs.use_cases import JobsNotFoundError
from model_deck.engine.kernel_composition import (
    DispatchInvocationContext,
    KernelComposition,
    KernelDomainError,
    KernelInvocationError,
    KernelPassthroughError,
    domain_error_codes,
)
from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor, compose
from model_deck_contracts.paths import repo_root

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
CLIENT_NAME = "fixture-binding-client"
UNKNOWN_JOB_ID = "00000000-0000-4000-8000-000000000000"

FOREIGN_FEATURE = "com.example.binding"
FOREIGN_OPERATION = "com.example.binding.inspect"
FOREIGN_SECRET = "foreign-handler-secret"
HEALTH_PARAMS_SCHEMA = "contracts/engine.v1/methods/health.params.schema.json"
HEALTH_RESULT_SCHEMA = "contracts/engine.v1/methods/health.result.schema.json"


class _ListModelsStub:
    def execute(self, params):
        return {"items": []}


class _EnrollmentStub:
    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool:
        return True


def _foreign_feature(build_failure) -> AdditionalEngineFeatures:
    """One composed feature whose single handler raises what the caller asks for."""

    def raise_failure(params, grants):
        raise build_failure()

    def adapter(descriptor, collaborators):
        return {item.operation_id: raise_failure for item in descriptor.operations}

    return AdditionalEngineFeatures(
        features=(
            FeatureDescriptor(
                FOREIGN_FEATURE,
                "1.0.0",
                KernelApiVersion(1, 0),
                operations=(
                    OperationDescriptor(
                        FOREIGN_OPERATION,
                        HEALTH_PARAMS_SCHEMA,
                        HEALTH_RESULT_SCHEMA,
                        "read",
                    ),
                ),
            ),
        ),
        handler_adapters={FOREIGN_FEATURE: adapter},
    )


def _domain_error_carrying_text() -> KernelDomainError:
    """A handler naming a public code while trying to smuggle text out with it.

    A feature cannot pass a message to the constructor, so this is the most a
    determined handler can do: write over the two places an exception carries
    text. Neither may reach the caller.
    """
    smuggled = KernelDomainError("resource_exhausted")
    smuggled.args = (FOREIGN_SECRET,)
    smuggled.message = FOREIGN_SECRET
    return smuggled


class _LookupOnlyJobDirectory:
    """A conforming gateway that serves lookups and neither cancel nor resume."""

    def __init__(self, owned_job_id: str | None = None) -> None:
        self.owned_job_id = owned_job_id
        self.asked: list[str] = []

    def job_get(self, params, *, principal: str) -> dict[str, object]:
        self.asked.append("job_get")
        if params["job_id"] != self.owned_job_id:
            raise JobsNotFoundError("job not found")
        return {"job_id": self.owned_job_id, "served_by": "lookup-only"}


class _FullJobDirectory:
    """A directory that serves all three job methods, for one job id."""

    def __init__(self, owned_job_id: str) -> None:
        self.owned_job_id = owned_job_id
        self.asked: list[str] = []

    def _answer(self, name: str, params) -> dict[str, object]:
        self.asked.append(name)
        if params["job_id"] != self.owned_job_id:
            raise JobsNotFoundError("job not found")
        return {"job_id": self.owned_job_id, "served_by": name}

    def job_get(self, params, *, principal: str) -> dict[str, object]:
        return self._answer("job_get", params)

    def job_cancel(self, params, *, principal: str) -> dict[str, object]:
        return self._answer("job_cancel", params)

    def job_resume(self, params, *, principal: str) -> dict[str, object]:
        return self._answer("job_resume", params)


class JobDirectoryFanOutTests(unittest.TestCase):
    """Which directory answers, when one of them cannot serve the method at all.

    ``ExtensionGateway`` does not require every job method of every gateway, so
    a directory that cannot serve one must step aside rather than decide the
    answer for the directory that owns the job.
    """

    JOB_ID = "11111111-1111-4111-8111-111111111111"

    def fan_out(self, directories, method: str, job_id: str | None = None):
        return EngineDispatch._first_matching_job_directory(
            directories,
            method,
            {"job_id": self.JOB_ID if job_id is None else job_id},
            "principal-fixture",
        )

    def test_a_directory_that_cannot_serve_does_not_end_the_search(self) -> None:
        lookup_only = _LookupOnlyJobDirectory()
        owner = _FullJobDirectory(self.JOB_ID)
        answer = self.fan_out((lookup_only, owner), "engine.v1.jobs.cancel")
        self.assertEqual(answer["served_by"], "job_cancel")
        self.assertEqual(lookup_only.asked, [])

    def test_a_cancellation_nothing_can_serve_is_not_found_not_resume(self) -> None:
        """``resume_unavailable`` says nothing about cancelling a job."""
        lookup_only = _LookupOnlyJobDirectory()
        with self.assertRaises(JobsNotFoundError):
            self.fan_out((lookup_only,), "engine.v1.jobs.cancel")

    def test_a_resume_nothing_can_serve_is_resume_unavailable(self) -> None:
        lookup_only = _LookupOnlyJobDirectory()
        with self.assertRaises(JobResumeUnsupportedError):
            self.fan_out((lookup_only,), "engine.v1.jobs.resume")

    def test_a_lookup_still_walks_past_a_directory_that_does_not_have_it(self) -> None:
        lookup_only = _LookupOnlyJobDirectory()
        owner = _FullJobDirectory(self.JOB_ID)
        answer = self.fan_out((lookup_only, owner), "engine.v1.jobs.get")
        self.assertEqual(answer["served_by"], "job_get")
        self.assertEqual(lookup_only.asked, ["job_get"])


class BuiltinDispatchBindingTests(unittest.TestCase):
    """The binding itself: what it must cover before an engine can serve."""

    def test_every_builtin_operation_is_served_by_a_bound_handler(self) -> None:
        dispatch = EngineDispatch(
            _ListModelsStub(),
            SimpleNamespace(
                engine_instance_id="550e8400-e29b-41d4-a716-446655440001",
                instance_nonce="nonce-binding",
            ),
            _EnrollmentStub(),
        )
        bound = dispatch._builtin_binding.bound_operations()
        self.assertEqual(bound, frozenset(builtin_operation_ids()))
        for operation_id in builtin_operation_ids():
            with self.subTest(operation_id=operation_id):
                self.assertIsNotNone(dispatch._builtin_binding.dispatch_handler(operation_id))

    def test_a_binding_serves_one_dispatch_only(self) -> None:
        binding = BuiltinDispatchBinding()
        self.assertFalse(binding.is_bound())
        self.assertEqual(binding.bound_operations(), frozenset())

    def test_a_composed_builtin_answers_only_on_the_dispatch_bound_path(self) -> None:
        """The bound path carries the context; the generic one has none to carry."""
        binding = BuiltinDispatchBinding()
        features = (
            builtin_descriptor(FEATURE_CORE),
            builtin_descriptor(FEATURE_MODELS_LIBRARY),
        )
        registry = builtin_handler_registry(binding)
        capabilities = {CAPABILITY_MODELS_LIBRARY: CAPABILITY_SUPPORTED}
        composition = KernelComposition(
            compose(features, capabilities, registry.build_handlers(features, {})),
            dispatch_bound_features=BUILTIN_FEATURE_IDS,
        )
        EngineDispatch(
            _ListModelsStub(),
            SimpleNamespace(
                engine_instance_id="550e8400-e29b-41d4-a716-446655440001",
                instance_nonce="nonce-binding",
            ),
            _EnrollmentStub(),
            builtin_binding=binding,
        )
        context = DispatchInvocationContext(connection_id=1, request_id=7)

        self.assertIn("engine.v1.health", composition.dispatch_bound_operations())
        self.assertEqual(
            composition.invoke_dispatch_bound("engine.v1.health", {}, context),
            {"status": "ok"},
        )

        # No register-model use case reached this dispatch, so its own method
        # answers unsupported_capability, and that answer travels whole.
        with self.assertRaises(KernelPassthroughError) as passthrough:
            composition.invoke_dispatch_bound(
                "engine.v1.models.register",
                {
                    "connection_id": CONNECTION_ID,
                    "provider_model_id": "fixture/model",
                    "expected_revision": 0,
                    "idempotency_key": "reg",
                },
                context,
            )
        error = passthrough.exception.envelope["error"]
        self.assertEqual(error["data"]["code"], "unsupported_capability")
        self.assertEqual(error["data"]["message"], "method not configured")

        # The generic path cannot serve a built-in: it carries no connection.
        with self.assertRaises(KernelInvocationError):
            composition.invoke("engine.v1.health", {})


class BoundBuiltinSocketTests(unittest.TestCase):
    """The same operations over the real socket, through the composed kernel."""

    def setUp(self) -> None:
        self._temporary: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for directory in self._temporary:
            directory.cleanup()

    def directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="builtin-binding-")
        self._temporary.append(directory)
        return Path(directory.name).resolve()

    def build(self, **overrides):
        runtime = build_engine_server(
            state_root=self.directory(),
            artifact_root=self.directory(),
            socket_root=self.directory(),
            legacy_agents_dir=FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION_ID,
            source_root=repo_root(),
            enable_application_state=True,
            enable_fixture_runs=True,
            **overrides,
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    @contextmanager
    def session(self, runtime, *, request_id_base: int = 1):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        with client.session() as session:
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": request_id_base,
                    "method": "engine.v1.hello",
                    "params": {
                        "client_name": CLIENT_NAME,
                        "offered_api": {"major": 1, "minor": 0},
                        "authentication": {
                            "engine_instance_id": descriptor.engine_instance_id,
                            "instance_nonce": descriptor.instance_nonce,
                            "credential": credential,
                        },
                    },
                }
            )
            self.assertTrue(response["result"]["authenticated"])
            yield session

    @staticmethod
    def call(session, request_id: int, method: str, params=None):
        return session.call(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )

    def result(self, session, request_id: int, method: str, params=None):
        response = self.call(session, request_id, method, params)
        self.assertIn("result", response, response)
        return response["result"]

    def start_fixture_run(self, session, *, request_id_base: int) -> str:
        self.result(
            session,
            request_id_base,
            "engine.v1.connections.save",
            {
                "expected_revision": 0,
                "idempotency_key": "conn-create",
                "connection": {
                    "connection_id": CONNECTION_ID,
                    "provider_id": DETERMINISTIC_PROVIDER_ID,
                },
            },
        )
        registered = self.result(
            session,
            request_id_base + 1,
            "engine.v1.models.register",
            {
                "connection_id": CONNECTION_ID,
                "provider_model_id": "fixture/model",
                "display_name": "Fixture Model",
                "expected_revision": 0,
                "idempotency_key": "model-reg",
            },
        )
        registration_id = registered["model"]["registration_id"]
        created = self.result(
            session,
            request_id_base + 2,
            "engine.v1.sessions.create",
            {"registration_id": registration_id},
        )
        started = self.result(
            session,
            request_id_base + 3,
            "engine.v1.runs.start",
            {
                "session_id": created["session_id"],
                "client_request_id": "binding-request",
                "idempotency_key": "binding-run",
                "registration_id": registration_id,
            },
        )
        return started["run"]["run_id"]

    def test_every_advertised_builtin_operation_answers_over_the_socket(self) -> None:
        """Discovery and routing agree: nothing is listed that nothing serves."""
        runtime = self.build()
        with self.session(runtime) as session:
            listing = self.result(session, 2, "engine.v1.operations.list")["operations"]
            advertised = {row["operation_id"] for row in listing}
            self.assertTrue(advertised >= {"engine.v1.jobs.get", "engine.v1.runs.start"})
            for operation_id in sorted(advertised & set(builtin_operation_ids())):
                if operation_id == "engine.v1.hello":
                    continue
                with self.subTest(operation_id=operation_id):
                    # Empty params are wrong for most of these; what matters is
                    # that none of them reports the method as unimplemented.
                    response = self.call(session, 3, operation_id)
                    error = response.get("error", {}).get("data", {})
                    self.assertNotEqual(error.get("code"), "unsupported_capability")
                    self.assertNotEqual(error.get("message"), "unhandled method")

    def test_a_subscription_still_belongs_to_the_connection_that_made_it(self) -> None:
        """Connection-scoped state survived the trip through the kernel."""
        runtime = self.build()
        with self.session(runtime, request_id_base=1) as owner:
            run_id = self.start_fixture_run(owner, request_id_base=2)
            subscribed = self.result(
                owner,
                10,
                "engine.v1.events.subscribe",
                {"topics": [f"run:{run_id}"], "initial_credit": 32},
            )
            subscription_id = subscribed["subscription_id"]
            notification = owner.read_notification()
            self.assertEqual(
                notification["params"]["subscription_id"], subscription_id
            )

            # The same client name authenticates to the same principal, so only
            # the connection tells these two apart.
            with self.session(runtime, request_id_base=20) as other:
                for method in ("engine.v1.events.ack", "engine.v1.events.unsubscribe"):
                    with self.subTest(method=method):
                        params = {"subscription_id": subscription_id}
                        if method.endswith("ack"):
                            params["sequence"] = 1
                        denied = self.call(other, 21, method, params)
                        self.assertEqual(
                            denied["error"]["data"]["code"], "capability_denied"
                        )

            # The owning connection still holds it.
            released = self.result(
                owner,
                30,
                "engine.v1.events.unsubscribe",
                {"subscription_id": subscription_id},
            )
            self.assertEqual(released, {"unsubscribed": True})

    def test_a_bound_handler_keeps_its_domain_code_and_message(self) -> None:
        """The kernel used to flatten these to one internal error."""
        runtime = self.build()
        with self.session(runtime) as session:
            missing = self.call(
                session, 2, "engine.v1.jobs.get", {"job_id": UNKNOWN_JOB_ID}
            )
            self.assertEqual(missing["error"]["code"], -32000)
            self.assertEqual(missing["error"]["data"]["code"], "not_found")
            self.assertEqual(missing["error"]["data"]["message"], "job not found")

            # A JSON-RPC-level error from the same body passes through too.
            invalid = self.call(session, 4, "engine.v1.sessions.get", {"session_id": 7})
            self.assertEqual(invalid["error"]["code"], -32602)
            self.assertNotIn("data", invalid["error"])

    def test_a_foreign_handler_failure_is_still_redacted_to_internal(self) -> None:
        runtime = self.build(
            additional_features=_foreign_feature(lambda: RuntimeError(FOREIGN_SECRET))
        )
        with self.session(runtime) as session:
            response = self.call(session, 2, FOREIGN_OPERATION)
            self.assertEqual(response["error"]["data"]["code"], "internal")
            self.assertEqual(response["error"]["message"], "operation failed")
            self.assertNotIn(FOREIGN_SECRET, str(response))

    def test_a_foreign_handler_names_a_code_but_never_writes_the_message(self) -> None:
        """Naming a public code is not a way around the redaction.

        ``KernelDomainError`` lets a composed feature classify its failure, which
        is why the evidence reads can say ``resource_exhausted`` instead of
        ``internal``. The sentence is still the engine's: a handler that puts a
        path, an id or a provider response into the exception publishes nothing.
        """
        runtime = self.build(
            additional_features=_foreign_feature(_domain_error_carrying_text)
        )
        with self.session(runtime) as session:
            response = self.call(session, 2, FOREIGN_OPERATION)
            error = response["error"]
            self.assertEqual(error["code"], -32000)
            self.assertEqual(error["data"]["code"], "resource_exhausted")
            self.assertEqual(
                error["data"]["message"],
                _COMPOSED_DOMAIN_ERROR_MESSAGES["resource_exhausted"],
            )
            self.assertEqual(error["message"], error["data"]["message"])
            self.assertNotIn(FOREIGN_SECRET, str(response))


class ComposedDomainErrorVocabularyTests(unittest.TestCase):
    """The code a feature may name, and the text only the engine may write."""

    def test_a_feature_cannot_supply_a_message_at_all(self) -> None:
        with self.assertRaises(TypeError):
            KernelDomainError("resource_exhausted", FOREIGN_SECRET)

    def test_an_unknown_code_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            KernelDomainError("teapot")

    def test_every_public_code_has_an_engine_written_sentence(self) -> None:
        """A code with no entry would fall back to a message that misleads."""
        self.assertEqual(
            set(_COMPOSED_DOMAIN_ERROR_MESSAGES), set(domain_error_codes())
        )
        for code, message in _COMPOSED_DOMAIN_ERROR_MESSAGES.items():
            with self.subTest(code=code):
                self.assertTrue(message and message.strip() == message)


if __name__ == "__main__":
    unittest.main()
