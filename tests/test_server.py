import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult, TextContent

from imageright_mcp.server import create_server

SECRET = "s3cr3t-password-value"


def _structured(result: CallToolResult) -> dict[str, Any]:
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


async def test_lists_tools_in_memory() -> None:
    async with Client(create_server({})) as client:
        tools = (await client.list_tools()).tools
    names = [tool.name for tool in tools]
    assert names[0] == "ir_get_config"
    assert len(names) == 15
    assert {"ir_call", "ir_configure", "ir_session", "ir_test_connection"} <= set(names)
    annotations = tools[0].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is False


async def test_ir_get_config_redacts_secrets() -> None:
    env = {"IMAGERIGHT_PASSWORD": SECRET, "IMAGERIGHT_VERSION": "25.x"}
    async with Client(create_server(env)) as client:
        result = await client.call_tool("ir_get_config", {})
    assert not result.is_error
    payload = _structured(result)
    assert payload["ok"] is True
    assert payload["data"]["password"] == "***"
    assert payload["data"]["profile"]["profile"] == "25.1"
    assert payload["data"]["sources"]["irVersion"] == "env"
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    assert SECRET not in text
    assert SECRET not in json.dumps(payload)


async def test_ir_get_config_reports_config_errors() -> None:
    async with Client(create_server({"IMAGERIGHT_WRITE_MODE": "yolo"})) as client:
        result = await client.call_tool("ir_get_config", {})
    assert result.is_error
    payload = _structured(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "IR-1001"


def _stdio_command() -> tuple[str, list[str]]:
    script = shutil.which("imageright-mcp", path=str(Path(sys.executable).parent))
    if script:
        return script, []
    return sys.executable, ["-m", "imageright_mcp"]


async def test_stdio_smoke_lists_and_calls_ir_get_config() -> None:
    command, args = _stdio_command()
    env = {**os.environ, "IMAGERIGHT_PASSWORD": SECRET}
    params = StdioServerParameters(command=command, args=args, env=env)
    async with Client(params) as client:
        assert client.server_info is not None
        assert client.server_info.name == "imageright-mcp"
        tools = (await client.list_tools()).tools
        assert "ir_get_config" in [tool.name for tool in tools]
        result = await client.call_tool("ir_get_config", {})
        search = await client.call_tool("ir_search_apis", {"query": "upload a page"})
    payload = _structured(result)
    assert payload["ok"] is True
    assert payload["data"]["password"] == "***"
    assert SECRET not in json.dumps(payload)
    found = _structured(search)
    assert found["ok"] is True
    assert found["data"]["results"]
