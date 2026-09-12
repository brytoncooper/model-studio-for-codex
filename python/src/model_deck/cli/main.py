from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from model_deck.adapters.providers.deterministic import DETERMINISTIC_PROVIDER_ID
from model_deck.adapters.transport.rendezvous import RendezvousDescriptor, RendezvousError, load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck_contracts.negotiation import rendezvous_matches
from model_deck_contracts.paths import repo_root
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

DETERMINISTIC_CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
FIXTURE_CLI_CONNECTION_IDEMPOTENCY_KEY = "model-deck-cli-fixture-connection"
FIXTURE_CLI_MODEL_IDEMPOTENCY_KEY = "model-deck-cli-fixture-model"
EVENT_NOTIFICATION_SCHEMA = "contracts/engine.v1/notifications/event.schema.json"
MAX_EVENT_NOTIFICATION_READS = 256
FIXTURE_EVENT_INITIAL_CREDIT = 256
_TERMINAL_RUN_EVENT_KINDS = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _cmd_engine_serve(args: argparse.Namespace) -> int:
    catalog_cache_path = Path(args.catalog_cache) if args.catalog_cache else None
    runtime = build_engine_server(
        state_root=Path(args.state_root),
        artifact_root=Path(args.artifact_root),
        socket_root=Path(args.socket_root),
        legacy_agents_dir=Path(args.legacy_agents_dir),
        default_connection_id=args.default_connection_id,
        source_root=repo_root(),
        catalog_cache_path=catalog_cache_path,
        enable_application_state=args.enable_application_state,
        enable_fixture_runs=args.enable_fixture_runs,
    )
    runtime.server.serve_forever()
    return 0


def _api_profile_matches_client(api_profile: object) -> bool:
    if not isinstance(api_profile, dict):
        return False
    return api_profile.get("major") == 1 and api_profile.get("minor") == 0


def _validate_hello_challenge(
    descriptor: RendezvousDescriptor, hello_result: object
) -> str | None:
    if not isinstance(hello_result, dict):
        return "engine hello returned invalid result"
    if hello_result.get("authenticated") is not False:
        return "engine hello did not return unauthenticated challenge"
    if not _api_profile_matches_client(hello_result.get("api_profile")):
        return "engine api_profile incompatible with client 1.0"
    if not rendezvous_matches(
        str(hello_result.get("engine_instance_id", "")),
        str(hello_result.get("instance_nonce", "")),
        descriptor.engine_instance_id,
        descriptor.instance_nonce,
    ):
        return "rendezvous mismatch"
    return None


def _validate_authenticated_hello(
    descriptor: RendezvousDescriptor, hello_result: object
) -> str | None:
    if not isinstance(hello_result, dict):
        return "engine hello returned invalid result"
    if hello_result.get("authenticated") is not True:
        return "authentication failed"
    if not _api_profile_matches_client(hello_result.get("api_profile")):
        return "engine api_profile incompatible with client 1.0"
    if str(hello_result.get("engine_instance_id", "")) != descriptor.engine_instance_id:
        return "rendezvous mismatch"
    if str(hello_result.get("instance_nonce", "")) != descriptor.instance_nonce:
        return "rendezvous mismatch"
    return None


def _authenticate_session(
    descriptor: RendezvousDescriptor,
    credential: str,
    session,
) -> str | None:
    second = session.call(
        {
            "jsonrpc": "2.0",
            "id": "hello-2",
            "method": "engine.v1.hello",
            "params": {
                "client_name": "model-deck-cli",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        }
    )
    if "error" in second:
        err = second.get("error")
        if not isinstance(err, dict):
            return "authentication failed"
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        if isinstance(data, dict) and data.get("code") == "capability_denied":
            return "invalid enrollment credential"
        return str(err.get("message", "authentication failed"))
    result = second.get("result")
    validation_error = _validate_authenticated_hello(descriptor, result)
    if validation_error is not None:
        return validation_error
    return None


def _negotiate_and_authenticate(
    session,
    descriptor: RendezvousDescriptor,
    credential_path: Path,
) -> str | None:
    first = session.call(
        {
            "jsonrpc": "2.0",
            "id": "hello-1",
            "method": "engine.v1.hello",
            "params": {
                "client_name": "model-deck-cli",
                "offered_api": {"major": 1, "minor": 0},
            },
        }
    )
    if "error" in first:
        return str(first["error"].get("message", "hello negotiation failed"))
    challenge_error = _validate_hello_challenge(descriptor, first.get("result"))
    if challenge_error is not None:
        return challenge_error
    try:
        credential = credential_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"cannot read credential: {exc}"
    return _authenticate_session(descriptor, credential, session)


def _rpc_error_message(response: object) -> str:
    if not isinstance(response, dict):
        return "invalid engine response"
    error = response.get("error")
    if not isinstance(error, dict):
        return "engine request failed"
    return str(error.get("message", "engine request failed"))


def _result_string(response: object, *path: str) -> str | None:
    if not isinstance(response, dict):
        return None
    node = response.get("result")
    if not isinstance(node, dict):
        return None
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _validate_event_notification(
    notification: object,
    *,
    expected_subscription_id: str | None = None,
    expected_run_id: str | None = None,
) -> str | None:
    try:
        validate_schema_ref(EVENT_NOTIFICATION_SCHEMA, notification)
    except SchemaValidationError:
        return "invalid engine event notification"
    if not isinstance(notification, dict):
        return "invalid engine event notification"
    if notification.get("method") != "engine.v1.event":
        return "invalid engine event notification"
    params = notification.get("params")
    if not isinstance(params, dict):
        return "invalid engine event notification"
    if expected_subscription_id is not None:
        subscription_id = params.get("subscription_id")
        if subscription_id != expected_subscription_id:
            return "invalid engine event notification"
    event = params.get("event")
    if not isinstance(event, dict):
        return "invalid engine event notification"
    if expected_run_id is not None:
        event_run_id = event.get("run_id")
        if event_run_id != expected_run_id:
            return "invalid engine event notification"
    return None


def _ack_and_unsubscribe(session, subscription_id: str, sequence: int) -> str | None:
    acked = session.call(
        {
            "jsonrpc": "2.0",
            "id": "events-ack",
            "method": "engine.v1.events.ack",
            "params": {
                "subscription_id": subscription_id,
                "sequence": sequence,
            },
        }
    )
    if "error" in acked:
        return _rpc_error_message(acked)
    unsubscribed = session.call(
        {
            "jsonrpc": "2.0",
            "id": "events-unsubscribe",
            "method": "engine.v1.events.unsubscribe",
            "params": {"subscription_id": subscription_id},
        }
    )
    if "error" in unsubscribed:
        return _rpc_error_message(unsubscribed)
    return None


def _cmd_models_list(args: argparse.Namespace) -> int:
    try:
        descriptor = load_rendezvous_file(Path(args.rendezvous))
    except (RendezvousError, OSError, json.JSONDecodeError) as exc:
        _stderr(f"invalid rendezvous: {exc}")
        return 1
    client = UnixSocketEngineClient(descriptor.socket_path)
    try:
        with client.session() as session:
            auth_error = _negotiate_and_authenticate(
                session, descriptor, Path(args.credential)
            )
            if auth_error is not None:
                _stderr(auth_error)
                return 1
            response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "models-list",
                    "method": "engine.v1.models.list",
                    "params": {"collection": "registered"},
                }
            )
    except FileNotFoundError:
        _stderr("engine is not running")
        return 1
    except (ConnectionError, OSError):
        _stderr("cannot connect to engine")
        return 1
    if "error" in response:
        _stderr(json.dumps(response["error"], indent=2))
        return 1
    print(json.dumps(response["result"], indent=2))
    return 0


def _cmd_runs_fixture_text(args: argparse.Namespace) -> int:
    try:
        descriptor = load_rendezvous_file(Path(args.rendezvous))
    except (RendezvousError, OSError, json.JSONDecodeError) as exc:
        _stderr(f"invalid rendezvous: {exc}")
        return 1
    credential_path = Path(args.credential)
    client = UnixSocketEngineClient(descriptor.socket_path)
    try:
        with client.session() as session:
            auth_error = _negotiate_and_authenticate(session, descriptor, credential_path)
            if auth_error is not None:
                _stderr(auth_error)
                return 1
            run_idempotency = str(uuid.uuid4())
            client_request_id = str(uuid.uuid4())
            saved = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "connection-save",
                    "method": "engine.v1.connections.save",
                    "params": {
                        "expected_revision": 0,
                        "idempotency_key": FIXTURE_CLI_CONNECTION_IDEMPOTENCY_KEY,
                        "connection": {
                            "connection_id": DETERMINISTIC_CONNECTION_ID,
                            "provider_id": DETERMINISTIC_PROVIDER_ID,
                        },
                    },
                }
            )
            if "error" in saved:
                _stderr(_rpc_error_message(saved))
                return 1
            registered = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "model-register",
                    "method": "engine.v1.models.register",
                    "params": {
                        "connection_id": DETERMINISTIC_CONNECTION_ID,
                        "provider_model_id": "fixture/model",
                        "display_name": "Fixture Model",
                        "expected_revision": 0,
                        "idempotency_key": FIXTURE_CLI_MODEL_IDEMPOTENCY_KEY,
                    },
                }
            )
            if "error" in registered:
                _stderr(_rpc_error_message(registered))
                return 1
            registration_id = _result_string(registered, "model", "registration_id")
            if registration_id is None:
                _stderr("engine models.register returned invalid result")
                return 1
            created = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "session-create",
                    "method": "engine.v1.sessions.create",
                    "params": {"registration_id": registration_id},
                }
            )
            if "error" in created:
                _stderr(_rpc_error_message(created))
                return 1
            session_id = _result_string(created, "session_id")
            if session_id is None:
                _stderr("engine sessions.create returned invalid result")
                return 1
            started = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "run-start",
                    "method": "engine.v1.runs.start",
                    "params": {
                        "session_id": session_id,
                        "client_request_id": client_request_id,
                        "idempotency_key": run_idempotency,
                        "registration_id": registration_id,
                    },
                }
            )
            if "error" in started:
                _stderr(_rpc_error_message(started))
                return 1
            run_id = _result_string(started, "run", "run_id")
            if run_id is None:
                _stderr("engine runs.start returned invalid result")
                return 1
            subscribed = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "events-subscribe",
                    "method": "engine.v1.events.subscribe",
                    "params": {
                        "topics": [f"run:{run_id}"],
                        "initial_credit": FIXTURE_EVENT_INITIAL_CREDIT,
                    },
                }
            )
            if "error" in subscribed:
                _stderr(_rpc_error_message(subscribed))
                return 1
            subscription_id = _result_string(subscribed, "subscription_id")
            if subscription_id is None:
                _stderr("engine subscribe returned invalid result")
                return 1
            deltas: list[str] = []
            terminal_kind: str | None = None
            max_sequence: int | None = None
            for _ in range(MAX_EVENT_NOTIFICATION_READS):
                try:
                    notification = session.read_notification()
                except (ConnectionError, OSError, json.JSONDecodeError, KeyError, TypeError):
                    _stderr("invalid engine event notification")
                    return 1
                validation_error = _validate_event_notification(
                    notification,
                    expected_subscription_id=subscription_id,
                    expected_run_id=run_id,
                )
                if validation_error is not None:
                    _stderr(validation_error)
                    return 1
                params = notification["params"]
                event = params["event"]
                sequence = event.get("sequence")
                if not isinstance(sequence, int):
                    _stderr("invalid engine event notification")
                    return 1
                kind = event.get("kind")
                if kind == "content.delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        _stderr("invalid engine event notification")
                        return 1
                    deltas.append(delta)
                max_sequence = sequence if max_sequence is None else max(max_sequence, sequence)
                if isinstance(kind, str) and kind in _TERMINAL_RUN_EVENT_KINDS:
                    terminal_kind = kind
                    break
            else:
                _stderr("run did not reach a terminal event")
                return 1
            if max_sequence is None:
                _stderr("invalid engine event notification")
                return 1
            cleanup_error = _ack_and_unsubscribe(session, subscription_id, max_sequence)
            if cleanup_error is not None:
                _stderr(cleanup_error)
                return 1
            if terminal_kind != "run.completed":
                _stderr("run did not complete successfully")
                return 1
    except FileNotFoundError:
        _stderr("engine is not running")
        return 1
    except (ConnectionError, OSError):
        _stderr("cannot connect to engine")
        return 1
    print("".join(deltas) + "\n", end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-deck")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("engine", help="engine control")
    serve_sub = serve.add_subparsers(dest="engine_command", required=True)
    serve_cmd = serve_sub.add_parser("serve")
    serve_cmd.add_argument("--state-root", required=True)
    serve_cmd.add_argument("--artifact-root", required=True)
    serve_cmd.add_argument("--socket-root", required=True)
    serve_cmd.add_argument("--legacy-agents-dir", required=True)
    serve_cmd.add_argument(
        "--default-connection-id",
        default=DETERMINISTIC_CONNECTION_ID,
    )
    serve_cmd.add_argument(
        "--catalog-cache",
        default=None,
        help="optional absolute path to a JSON catalog cache fixture",
    )
    serve_cmd.add_argument(
        "--enable-application-state",
        action="store_true",
        help="enable persisted application state in the engine",
    )
    serve_cmd.add_argument(
        "--enable-fixture-runs",
        action="store_true",
        help="enable deterministic fixture run execution (requires application state)",
    )
    serve_cmd.set_defaults(func=_cmd_engine_serve)

    models = sub.add_parser("models", help="model library")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    list_cmd = models_sub.add_parser("list")
    list_cmd.add_argument("--rendezvous", required=True)
    list_cmd.add_argument("--credential", required=True)
    list_cmd.set_defaults(func=_cmd_models_list)

    runs = sub.add_parser("runs", help="run control")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    fixture_text_cmd = runs_sub.add_parser("fixture-text")
    fixture_text_cmd.add_argument("--rendezvous", required=True)
    fixture_text_cmd.add_argument("--credential", required=True)
    fixture_text_cmd.set_defaults(func=_cmd_runs_fixture_text)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
