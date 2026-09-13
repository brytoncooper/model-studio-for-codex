from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path

from model_deck.adapters.providers.deterministic import DETERMINISTIC_PROVIDER_ID
from model_deck.adapters.transport.framing import (
    MAX_FRAME_BYTES,
    FrameError,
    encode_frame,
)
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

# ---------------------------------------------------------------------------
# B23 generic kernel operation invocation
# ---------------------------------------------------------------------------


class _BoundedJsonError(ValueError):
    """Raised when a JSON value fails strict bounded validation."""


_OPERATION_LIST_RESULT_SCHEMA = (
    "contracts/engine.v1/methods/operations.list.result.schema.json"
)
_INPUT_MAX_DEPTH = 64
_INPUT_MAX_NODES = 200_000


def _walk_bounded_json_value(value, depth, counter):
    """Walk and validate a JSON value against the strict bounded contract.

    The same depth, node-count, finite-number and string-key guarantees used
    elsewhere in the engine. ``false``, ``0``, ``""``, empty arrays/objects
    and ``null`` are preserved as legitimate values. The function never
    mutates its input.
    """
    counter[0] += 1
    if counter[0] > _INPUT_MAX_NODES:
        raise _BoundedJsonError("value exceeds node limit")
    if depth > _INPUT_MAX_DEPTH:
        raise _BoundedJsonError("value exceeds depth limit")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _BoundedJsonError("non-finite numbers are not allowed")
        return
    if value is None or isinstance(value, (bool, int)) or isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _walk_bounded_json_value(item, depth + 1, counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _BoundedJsonError("value object keys must be strings")
            _walk_bounded_json_value(item, depth + 1, counter)
        return
    raise _BoundedJsonError("value contains a non-JSON value")


def _validate_bounded_json_value(value: object) -> None:
    """Validate that ``value`` is a strict bounded JSON value.

    Rejects:
    - non-finite numbers (``NaN``, ``±Inf``)
    - lone surrogates in strings (via UTF-8 encoding)
    - non-string object keys
    - depth > 64
    - total nodes > 200,000
    - non-JSON values (tuple, set, bytes, custom objects)

    Used for both the input file and the engine result so the engine and CLI
    share the same strict contract.
    """
    counter = [0]
    _walk_bounded_json_value(value, 0, counter)
    try:
        # ``ensure_ascii=False`` keeps the output valid Python; the actual
        # UTF-8 encode is what rejects lone surrogates (Python's json.dumps
        # silently emits an unescaped surrogate, which the encode step then
        # rejects). ``allow_nan=False`` rejects ``NaN`` / ``±Inf`` and any
        # non-JSON value the walker let through.
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise _BoundedJsonError("value contains lone surrogates") from exc
    except (TypeError, ValueError) as exc:
        raise _BoundedJsonError(
            f"value is not strict bounded JSON: {exc}"
        ) from exc


def _reject_duplicate_object_pairs(pairs):
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _BoundedJsonError("duplicate key in input object")
        seen.add(key)
    return dict(pairs)


def _read_bounded_bytes(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes from ``path``.

    Bounded reads avoid loading arbitrarily large input files into memory
    before the size check.
    """
    with path.open("rb") as handle:
        return handle.read(limit)


def _read_bounded_input_object(path: Path) -> dict[str, object]:
    """Read a strict bounded JSON object from ``path`` before any network work.

    Reuses :data:`MAX_FRAME_BYTES` for the raw byte budget so the engine's
    frame cap, duplicate keys, trailing data, non-finite numbers, lone
    surrogates and parser overflow are caught without ever contacting the
    engine.
    """
    raw_bytes = _read_bounded_bytes(path, MAX_FRAME_BYTES + 1)
    if len(raw_bytes) > MAX_FRAME_BYTES:
        raise _BoundedJsonError("input file exceeds frame budget")
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BoundedJsonError("input file is not valid UTF-8") from exc
    try:
        _, end = json.JSONDecoder().raw_decode(raw_text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise _BoundedJsonError("input file is not valid JSON") from exc
    if raw_text[end:].strip():
        raise _BoundedJsonError("input file has trailing data")
    try:
        decoded = json.loads(
            raw_text,
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise _BoundedJsonError("input file is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise _BoundedJsonError("input file must contain a JSON object")
    _validate_bounded_json_value(decoded)
    return decoded


def _find_listed_operation(
    listing: object, operation_id: str,
) -> dict[str, object] | None:
    """Find an operation advertised by ``engine.v1.operations.list``.

    Returns the operation descriptor dict if ``operation_id`` is advertised
    exactly once; returns ``None`` for zero or multiple matches.
    """
    if not isinstance(listing, dict):
        return None
    operations = listing.get("operations")
    if not isinstance(operations, list):
        return None
    matches = [
        entry
        for entry in operations
        if isinstance(entry, dict) and entry.get("operation_id") == operation_id
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _is_envelope_response(response: object) -> bool:
    """Validate that a JSON-RPC response has exactly one of ``error``/``result``.

    An envelope with neither branch (the engine forgot to populate the
    response), both branches (a malformed envelope), or a non-dict body
    is rejected.
    """
    if not isinstance(response, dict):
        return False
    return ("error" in response) ^ ("result" in response)


def _cmd_invoke(args: argparse.Namespace) -> int:
    operation_id = str(args.operation)

    try:
        descriptor = load_rendezvous_file(Path(args.rendezvous))
    except (RendezvousError, OSError, json.JSONDecodeError) as exc:
        _stderr(f"invalid rendezvous: {exc}")
        return 1
    credential_path = Path(args.credential)

    params: dict[str, object]
    if args.input_file is not None:
        try:
            params = _read_bounded_input_object(Path(args.input_file))
        except (OSError, _BoundedJsonError) as exc:
            _stderr(f"invalid input file: {exc}")
            return 1
    else:
        params = {}

    # Preflight the full invoke frame BEFORE any client construction so the
    # envelope overhead (jsonrpc / id / method wrapper) is included in the
    # frame budget check. ``encode_frame`` itself enforces the byte budget
    # and emits the wire format used by the engine; ``FrameError`` is
    # converted to a fixed CLI error rather than reaching the socket.
    invoke_payload = {
        "jsonrpc": "2.0",
        "id": "invoke-call",
        "method": operation_id,
        "params": params,
    }
    try:
        encode_frame(invoke_payload)
    except FrameError as exc:
        _stderr(f"invoke frame exceeds budget: {exc}")
        return 1

    client = UnixSocketEngineClient(descriptor.socket_path)
    try:
        with client.session() as session:
            auth_error = _negotiate_and_authenticate(
                session, descriptor, credential_path,
            )
            if auth_error is not None:
                _stderr(auth_error)
                return 1

            listing_response = session.call(
                {
                    "jsonrpc": "2.0",
                    "id": "invoke-operations-list",
                    "method": "engine.v1.operations.list",
                    "params": {},
                }
            )
            if not _is_envelope_response(listing_response):
                _stderr("invalid engine response")
                return 1
            if "error" in listing_response:
                _stderr(_rpc_error_message(listing_response))
                return 1
            listing = listing_response["result"]
            try:
                _validate_bounded_json_value(listing)
            except _BoundedJsonError:
                _stderr(
                    "engine operations listing failed strict bounded validation"
                )
                return 1
            try:
                validate_schema_ref(_OPERATION_LIST_RESULT_SCHEMA, listing)
            except SchemaValidationError:
                _stderr(
                    "engine operations listing failed bundled contract validation"
                )
                return 1
            operation = _find_listed_operation(listing, operation_id)
            if operation is None:
                _stderr(f"operation not advertised: {operation_id}")
                return 1

            invoke_response = session.call(invoke_payload)
    except FileNotFoundError:
        _stderr("engine is not running")
        return 1
    except (ConnectionError, OSError):
        _stderr("cannot connect to engine")
        return 1

    if not _is_envelope_response(invoke_response):
        _stderr("invalid engine response")
        return 1
    if "error" in invoke_response:
        _stderr(_rpc_error_message(invoke_response))
        return 1
    result = invoke_response["result"]
    try:
        _validate_bounded_json_value(result)
    except _BoundedJsonError as exc:
        _stderr(f"engine returned an invalid result: {exc}")
        return 1
    print(json.dumps(result, indent=2, allow_nan=False, ensure_ascii=False))
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

    invoke_cmd = sub.add_parser(
        "invoke",
        help="invoke a generic kernel operation via engine discovery",
    )
    invoke_cmd.add_argument("operation")
    invoke_cmd.add_argument("--rendezvous", required=True)
    invoke_cmd.add_argument("--credential", required=True)
    invoke_cmd.add_argument(
        "--input-file",
        default=None,
        help="absolute path to a strict bounded JSON-object params file",
    )
    invoke_cmd.set_defaults(func=_cmd_invoke)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
