"""Stdio MCP server exposing the ImageRight tools."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Mapping
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from imageright_mcp import __version__
from imageright_mcp.client_tools import register_client_tools
from imageright_mcp.composites import register_composite_tools
from imageright_mcp.config import ConfigError
from imageright_mcp.envelope import internal_error, to_envelope
from imageright_mcp.errors import get_registry
from imageright_mcp.prompts import register_prompts
from imageright_mcp.runtime import ConfigureError, Runtime
from imageright_mcp.tools import register_catalog_tools, register_error_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "imageright-mcp"

INSTRUCTIONS = (
    "Unofficial helper for the ImageRight document-management APIs (REST v1, REST v2, SOAP). "
    "Call ir_get_config first to see which product version and surfaces are configured. "
    "To find an API, start with ir_search_apis, then ir_describe_api; ir_list_flows shows "
    "multi-step recipes. ir_call executes one operation or capability (writes are previewed "
    "unless writeMode allows them); ir_test_connection checks the server. Composite tools "
    "(ir_create_task, ir_find_workflows, ir_find_steps, ir_search_files, ir_create_file, "
    "ir_update_file, ir_merge_files, ir_move_file_content, ir_find_documents, "
    "ir_create_document, ir_upload_document) run a whole flow from names and numbers; when one "
    'needs more from the user it returns data.status "needs-input" with the question. Every '
    "tool returns the same envelope; when a call fails, ir_explain_error explains its IR code. "
    "Explorer, version and error tools work offline. Prompts (smoke_test, create_task_guided, "
    "find_documents_in_file, upload_document_guided, explain_error) are ready-made test "
    "scenarios."
)


def create_server(
    env: Mapping[str, str] | None = None, *, runtime: Runtime | None = None
) -> MCPServer:
    """Build the server. ``env`` overrides ``os.environ`` (used by tests)."""
    runtime = runtime or Runtime(env)
    server = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        name="ir_get_config",
        title="Get effective configuration",
        description=(
            "Show the configuration this server is using right now: ImageRight version and the "
            "catalog profile it maps to, REST and SOAP endpoints, auth mode, surface preference, "
            "write mode, and dry-run default. Each field reports where its value came from "
            "(env, file, default, or runtime for an ir_configure override). Secrets are always "
            "redacted."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, open_world_hint=False, idempotent_hint=True
        ),
    )
    def ir_get_config() -> CallToolResult:
        try:
            config = runtime.config()
            warnings = runtime.config_warnings()
        except ConfigError as exc:
            return to_envelope(error=get_registry().error("IR-1001", message=str(exc)))
        except ConfigureError as exc:
            return to_envelope(error=exc.error)
        except Exception as exc:
            return to_envelope(error=internal_error(exc))
        return to_envelope(data=config.redacted(), meta={"warnings": warnings})

    register_catalog_tools(server, env, load=runtime.config)
    register_error_tools(server)
    register_client_tools(server, runtime)
    register_composite_tools(server, runtime)
    register_prompts(server)
    return server


class _Serves(Protocol):
    async def run_stdio_async(self) -> None: ...


async def serve(server: _Serves, runtime: Runtime, stop: asyncio.Event | None = None) -> str:
    """Run the stdio server until stdin closes, SIGINT / SIGTERM arrives, or ``stop`` is set;
    then log off every session (plan §4.4: ``UserLogoff`` at shutdown, best effort).

    Returns ``"stdin-closed"`` or ``"stopped"``. After a stop the stdio reader may still be parked
    in a worker thread on a blocking read, which nothing can interrupt, so the caller should exit
    the process rather than wait for it (see ``__main__``).
    """
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):  # not main thread / Windows
            pass
    running = asyncio.ensure_future(server.run_stdio_async())
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        stopping.cancel()
        # Log off before tearing the server down, so a hung reader cannot prevent it.
        await shutdown(runtime)
        if not running.done():
            running.cancel()
            await asyncio.wait({running}, timeout=STOP_GRACE_SECONDS)
    if running.done() and not running.cancelled() and running.exception() is None:
        return "stdin-closed"
    return "stopped"


STOP_GRACE_SECONDS = 1.0


async def shutdown(runtime: Runtime) -> None:
    with contextlib.suppress(Exception):
        await runtime.aclose()
    logger.info("imageright-mcp stopped")
