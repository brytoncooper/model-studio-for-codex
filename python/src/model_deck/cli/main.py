from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from model_deck.adapters.transport.rendezvous import RendezvousDescriptor, RendezvousError, load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck_contracts.negotiation import rendezvous_matches
from model_deck_contracts.paths import repo_root


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


def _cmd_models_list(args: argparse.Namespace) -> int:
    try:
        descriptor = load_rendezvous_file(Path(args.rendezvous))
    except (RendezvousError, OSError, json.JSONDecodeError) as exc:
        _stderr(f"invalid rendezvous: {exc}")
        return 1
    client = UnixSocketEngineClient(descriptor.socket_path)
    try:
        with client.session() as session:
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
                _stderr(str(first["error"].get("message", "hello negotiation failed")))
                return 1
            challenge_error = _validate_hello_challenge(descriptor, first.get("result"))
            if challenge_error is not None:
                _stderr(challenge_error)
                return 1
            try:
                credential = Path(args.credential).read_text(encoding="utf-8").strip()
            except OSError as exc:
                _stderr(f"cannot read credential: {exc}")
                return 1
            auth_error = _authenticate_session(descriptor, credential, session)
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
        default="550e8400-e29b-41d4-a716-446655440002",
    )
    serve_cmd.add_argument(
        "--catalog-cache",
        default=None,
        help="optional absolute path to a JSON catalog cache fixture",
    )
    serve_cmd.set_defaults(func=_cmd_engine_serve)

    models = sub.add_parser("models", help="model library")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    list_cmd = models_sub.add_parser("list")
    list_cmd.add_argument("--rendezvous", required=True)
    list_cmd.add_argument("--credential", required=True)
    list_cmd.set_defaults(func=_cmd_models_list)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
