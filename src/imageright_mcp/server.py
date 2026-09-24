"""Stdio MCP server exposing the ImageRight tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from imageright_mcp import __version__
from imageright_mcp.config import ConfigError, load_config

SERVER_NAME = "imageright-mcp"

INSTRUCTIONS = (
    "Unofficial helper for the ImageRight document-management APIs (REST v1, REST v2, SOAP). "
    "Call ir_get_config first to see which product version and surfaces are configured."
)


def _envelope(data: Any = None, error: dict[str, Any] | None = None) -> CallToolResult:
    """Wrap a result in the standard envelope (plan §5.4); ``isError`` mirrors ``ok``."""
    envelope = {"ok": error is None, "data": data, "error": error, "meta": {"warnings": []}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
        structured_content=envelope,
        is_error=error is not None,
    )


def create_server(env: Mapping[str, str] | None = None) -> MCPServer:
    """Build the server. ``env`` overrides ``os.environ`` (used by tests)."""
    server = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        name="ir_get_config",
        title="Get effective configuration",
        description=(
            "Show the configuration this server is using right now: ImageRight version and the "
            "catalog profile it maps to, REST and SOAP endpoints, auth mode, surface preference, "
            "write mode, and dry-run default. Each field reports where its value came from "
            "(env, file, or default). Secrets are always redacted."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, open_world_hint=False, idempotent_hint=True
        ),
    )
    def ir_get_config() -> CallToolResult:
        try:
            config = load_config(env)
        except ConfigError as exc:
            return _envelope(
                error={
                    "code": "IR-1001",
                    "name": "ConfigMissing",
                    "category": "config",
                    "message": str(exc),
                    "retryable": False,
                    "hint": "Fix the IMAGERIGHT_* environment variables or the config file.",
                }
            )
        return _envelope(data=config.redacted())

    return server
