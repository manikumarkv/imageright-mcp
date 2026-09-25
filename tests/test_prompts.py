"""MCP prompts: listed with their arguments, render, and only point at real tools."""

from __future__ import annotations

import re
from typing import Any

import pytest
from mcp.client.client import Client

from imageright_mcp.server import create_server

MINIMAL_ARGS: dict[str, dict[str, str]] = {
    "smoke_test": {},
    "create_task_guided": {"fileNumber": "F-1"},
    "find_documents_in_file": {"fileNumber": "F-1"},
    "upload_document_guided": {
        "fileNumber": "F-1",
        "drawerCode": "CLM",
        "folderTypeName": "Correspondence",
        "docTypeCode": "INV",
        "pdfFile": "/data/in.pdf",
    },
    "explain_error": {"errorCode": "IR-4003"},
}
WRITING = {"create_task_guided", "upload_document_guided"}


async def render(name: str, args: dict[str, str]) -> str:
    async with Client(create_server({})) as client:
        result = await client.get_prompt(name, args)
    [message] = result.messages
    assert message.role == "user"
    text: str = message.content.text  # type: ignore[union-attr]
    return text


async def tools() -> dict[str, Any]:
    async with Client(create_server({})) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


async def test_lists_every_prompt_with_its_arguments() -> None:
    async with Client(create_server({})) as client:
        prompts = (await client.list_prompts()).prompts
    required = {p.name: sorted(a.name for a in p.arguments or [] if a.required) for p in prompts}
    assert required == {name: sorted(args) for name, args in MINIMAL_ARGS.items()}
    assert all(p.title and p.description for p in prompts)


@pytest.mark.parametrize("name", sorted(MINIMAL_ARGS))
async def test_prompts_only_name_registered_tools(name: str) -> None:
    text = await render(name, MINIMAL_ARGS[name])
    named = set(re.findall(r"\bir_[a-z_]+\b", text))
    registered = await tools()
    assert named, name
    assert named <= set(registered), named - set(registered)
    writers = {t for t in named if not registered[t].annotations.read_only_hint}
    if name in WRITING:
        assert writers
        assert "dryRun: true" in text
        assert "confirm" in text
    else:
        assert not writers, writers


async def test_arguments_are_filled_in() -> None:
    text = await render(
        "create_task_guided",
        {"fileNumber": "F-9", "workflowName": "SELECT", "stepName": "Review"},
    )
    assert 'fileNumber "F-9"' in text
    assert 'ir_find_workflows with workflowName "SELECT"' in text
    assert 'stepName "Review"' in text
    bare = await render("create_task_guided", {"fileNumber": "F-9"})
    assert "workflowName: (not given)" in bare


async def test_smoke_test_searches_a_file_only_when_given() -> None:
    assert "ir_search_files" not in await render("smoke_test", {})
    assert 'ir_search_files with fileNumber "F-1"' in await render(
        "smoke_test", {"fileNumber": "F-1"}
    )
