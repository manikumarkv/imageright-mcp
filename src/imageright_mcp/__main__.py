"""Console entry point: ``imageright-mcp`` or ``python -m imageright_mcp`` runs the stdio server."""

import asyncio
import os
import sys

from imageright_mcp.runtime import Runtime
from imageright_mcp.server import create_server, serve


def main() -> None:
    runtime = Runtime()
    loop = asyncio.new_event_loop()
    try:
        reason = loop.run_until_complete(serve(create_server(runtime=runtime), runtime))
    except KeyboardInterrupt:
        reason = "stopped"
    if reason == "stdin-closed":
        loop.close()
        return
    # Stopped by a signal: sessions are logged off, but the stdio reader thread is blocked on
    # stdin and cannot be interrupted, so do not wait for it.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
