"""Bootstrap the Codex Desktop attachment with the public Unix engine client."""

from __future__ import annotations

import os
import sys

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.integrations.hosts.codex.bridge import EngineRPC
from model_deck.integrations.hosts.codex.desktop_attachment import (
    DesktopAttachmentConfiguration,
    main as run_desktop_attachment,
)


def main(arguments: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if "app-server" not in argv:
        return run_desktop_attachment(argv)
    configuration = DesktopAttachmentConfiguration.load(os.environ)
    engine = EngineRPC(
        configuration.engine_rendezvous_path,
        configuration.engine_credential_path,
        rendezvous_loader=load_rendezvous_file,
        client_factory=UnixSocketEngineClient,
    )
    return run_desktop_attachment(argv, engine=engine)


if __name__ == "__main__":
    raise SystemExit(main())
