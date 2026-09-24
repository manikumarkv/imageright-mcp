"""Stdio MCP server exposing the ImageRight tools."""

from __future__ import annotations

from collections.abc import Mapping

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from imageright_mcp import __version__
from imageright_mcp.config import ConfigError, load_config
from imageright_mcp.envelope import to_envelope
from imageright_mcp.tools import register_catalog_tools

SERVER_NAME = "imageright-mcp"

INSTRUCTIONS = (
    "Unofficial helper for the ImageRight document-management APIs (REST v1, REST v2, SOAP). "
    "Call ir_get_config first to see which product version and surfaces are configured. "
    "To find an API, start with ir_search_apis, then ir_describe_api; ir_list_flows shows "
    "multi-step recipes. Explorer and version tools work offline."
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
            return to_envelope(
                error={
                    "code": "IR-1001",
                    "name": "ConfigMissing",
                    "category": "config",
                    "message": str(exc),
                    "retryable": False,
                    "hint": "Fix the IMAGERIGHT_* environment variables or the config file.",
                }
            )
        return to_envelope(data=config.redacted())

    register_catalog_tools(server, env)
    return server
