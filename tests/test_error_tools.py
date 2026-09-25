"""ir_explain_error and envelope uniformity across every registered tool (plan §6.4)."""

from typing import Any

import pytest
from mcp.client.client import Client
from mcp.types import TextContent

from imageright_mcp.catalog import Catalog
from imageright_mcp.client import MockReply, MockTransport
from imageright_mcp.envelope import to_envelope
from imageright_mcp.errors import get_registry
from imageright_mcp.server import create_server
from tests.conftest import BASE, PASSWORD, USER, mock_runtime, script_rest_login

ERROR_KEYS = {"code", "name", "category", "message", "retryable", "hint", "native"}

# One call per tool that fails inside our handler. A new tool must add an entry here.
BAD_CALLS: dict[str, tuple[dict[str, Any], dict[str, str]]] = {
    "ir_get_config": ({}, {"IMAGERIGHT_WRITE_MODE": "yolo"}),
    "ir_search_apis": ({"query": "upload a page", "version": "99.9"}, {}),
    "ir_list_areas": ({"version": "99.9"}, {}),
    "ir_describe_api": ({"operationId": "rest.v9.nothing.here"}, {}),
    "ir_describe_type": ({"name": "NoSuchTypeAnywhere"}, {}),
    "ir_list_flows": ({"version": "99.9"}, {}),
    "ir_describe_flow": ({"flowId": "F99"}, {}),
    "ir_check_availability": ({"operationId": "rest.v9.nothing.here"}, {}),
    "ir_compare_versions": ({"fromVersion": "99.9", "toVersion": "25.x"}, {}),
    "ir_list_deprecations": ({"version": "99.9"}, {}),
    "ir_explain_error": ({"query": "IR-7777"}, {}),
    "ir_call": ({"capabilityId": "no.such.capability"}, {}),
    "ir_test_connection": ({}, {}),
    "ir_session": (
        {"action": "login", "surface": "rest"},
        {"IMAGERIGHT_REST_BASE_URL": "https://ir.example.test"},
    ),
    "ir_configure": ({"settings": {"password": "hunter2-not-accepted"}}, {}),
}
GOOD_CALLS: dict[str, dict[str, Any]] = {
    "ir_get_config": {},
    "ir_search_apis": {"query": "upload a page"},
    "ir_list_areas": {},
    "ir_describe_api": {"operationId": "rest.v1.pages.createPage"},
    "ir_describe_type": {"name": "TaskFilterV2"},
    "ir_list_flows": {},
    "ir_describe_flow": {"flowId": "F3"},
    "ir_check_availability": {"operationId": "rest.v1.pages.createPage"},
    "ir_compare_versions": {"fromVersion": "24.x", "toVersion": "25.x"},
    "ir_list_deprecations": {},
    "ir_explain_error": {"query": "IR-4301"},
    "ir_call": {"capabilityId": "document.get", "params": {"documentId": 7}, "dryRun": True},
    "ir_test_connection": {},
    "ir_session": {"action": "status"},
    "ir_configure": {"settings": {"writeMode": "allow"}},
}
# Tools that need a server for a success case run against a MockTransport.
LIVE_ENV = {
    "IMAGERIGHT_REST_BASE_URL": BASE,
    "IMAGERIGHT_USERNAME": USER,
    "IMAGERIGHT_PASSWORD": PASSWORD,
    "IMAGERIGHT_VERSION": "24.x",
}


def live_server() -> Any:
    mock = MockTransport()
    script_rest_login(mock)
    mock.add("GET", "/api/health", MockReply(body=b"", headers={"Content-Type": "text/plain"}))
    mock.add(
        "GET", "/api/integration/version", MockReply(json={"Major": 24, "Minor": 2, "Build": 115})
    )
    return create_server(runtime=mock_runtime(LIVE_ENV, mock))


async def tool_names() -> list[str]:
    async with Client(create_server({})) as client:
        return [t.name for t in (await client.list_tools()).tools]


async def envelope(
    name: str, args: dict[str, Any], env: dict[str, str] | None = None, *, live: bool = False
) -> Any:
    server = live_server() if live else create_server(env or {})
    async with Client(server) as client:
        result = await client.call_tool(name, args)
    payload = result.structured_content
    assert isinstance(payload, dict), f"{name} did not return an envelope"
    assert set(payload) == {"ok", "data", "error", "meta"}
    assert result.is_error is (not payload["ok"])
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    assert text, f"{name} has no text rendering"
    registry = get_registry()
    for warning in payload["meta"]["warnings"]:
        assert warning["code"] in registry.entries
        assert warning["name"] == registry.entry(warning["code"])["name"]
    return payload


def assert_error_shape(error: dict[str, Any]) -> None:
    registry = get_registry()
    assert set(error) >= ERROR_KEYS
    entry = registry.entry(error["code"])
    assert error["name"] == entry["name"]
    assert error["category"] == entry["category"]
    assert error["retryable"] == entry["retryable"]
    assert error["message"]
    assert error["hint"]
    assert "{" not in error["message"] + error["hint"]


# ---------------------------------------------------------------- uniformity


async def test_every_tool_has_a_uniformity_case() -> None:
    names = await tool_names()
    assert set(names) == set(BAD_CALLS) == set(GOOD_CALLS)


@pytest.mark.parametrize("name", sorted(BAD_CALLS))
async def test_errors_share_one_shape(name: str) -> None:
    args, env = BAD_CALLS[name]
    payload = await envelope(name, args, env)
    assert payload["ok"] is False
    assert payload["data"] is None
    assert_error_shape(payload["error"])
    assert payload["error"]["code"] != "IR-9001", payload["error"]


@pytest.mark.parametrize("name", sorted(GOOD_CALLS))
async def test_successes_share_one_shape(name: str) -> None:
    payload = await envelope(name, GOOD_CALLS[name], live=name == "ir_test_connection")
    assert payload["ok"] is True, payload["error"]
    assert payload["error"] is None
    assert isinstance(payload["meta"]["warnings"], list)


@pytest.mark.parametrize(
    "args",
    [{"query": "x", "limit": 0}, {"query": 5}, {}],
    ids=["out-of-range", "wrong-type", "missing"],
)
async def test_schema_errors_are_rejected_by_the_sdk_before_our_handler(
    args: dict[str, Any],
) -> None:
    """Documented boundary (M2 finding): the MCP SDK validates arguments against the tool's
    input schema before our handler runs. Those failures come back as ``isError`` with plain
    text and no envelope; everything that reaches a handler goes through ``to_envelope``."""
    async with Client(create_server({})) as client:
        result = await client.call_tool("ir_search_apis", args)
    assert result.is_error is True
    assert result.structured_content is None
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    assert "validation error" in text


async def test_unexpected_exception_becomes_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("kaput")

    monkeypatch.setattr(Catalog, "search", boom)
    payload = await envelope("ir_search_apis", {"query": "upload a page"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "IR-9001"
    assert payload["error"]["native"] == {
        "surface": None,
        "exception": "RuntimeError",
        "message": "kaput",
    }


def test_to_envelope_rejects_unregistered_errors_and_fixes_warning_names() -> None:
    bogus = to_envelope(error={"code": "IR-7777", "name": "Made Up", "message": "x"})
    assert bogus.structured_content is not None
    assert bogus.structured_content["error"]["code"] == "IR-9001"
    renamed = to_envelope(error={"code": "IR-3001", "name": "WrongName"})
    assert renamed.structured_content is not None
    assert renamed.structured_content["error"]["code"] == "IR-9001"
    minimal = to_envelope(error={"code": "IR-3001"}, meta={"warnings": [{"code": "IR-3003"}]})
    payload = minimal.structured_content
    assert payload is not None
    assert payload["error"]["name"] == "UnknownOperation"
    assert payload["error"]["native"] is None
    assert payload["meta"]["warnings"][0]["name"] == "OperationDeprecated"
    assert minimal.is_error is True


# ---------------------------------------------------------------- ir_explain_error


async def explain(query: str, **extra: Any) -> Any:
    payload = await envelope("ir_explain_error", {"query": query, **extra})
    assert payload["ok"] is True, payload["error"]
    return payload["data"]


async def test_explain_is_annotated_offline_read_only() -> None:
    async with Client(create_server({})) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    annotations = tools["ir_explain_error"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is False


async def test_explain_ir_code() -> None:
    data = await explain("IR-4301")
    assert data["matchedAs"] == "ir-code"
    assert data["error"]["name"] == "LockedByAnotherUser"
    assert data["error"]["retryable"] is True
    rest = {m["code"]: m["name"] for m in data["mappings"]["rest"]}
    assert rest[201] == "TaskLockedByAnotherUser"
    assert data["mappings"]["soap"]["faultPatterns"]
    assert "rest.v1.taskactions.lockTask" in data["raisedBy"]
    assert data["raisedByCount"] == len(data["raisedBy"])
    assert (await explain("ir4301"))["error"]["code"] == "IR-4301"


async def test_explain_native_code_and_profiles() -> None:
    data = await explain("917")
    assert data["matchedAs"] == "native-rest-code"
    assert data["error"]["code"] == "IR-4405"
    assert data["native"]["profiles"] == ["25.1"]
    assert data["native"]["name"] == "DocFolderInDeletedContent"


async def test_explain_native_name_and_ir_name() -> None:
    native = await explain("TaskMandatoryAttributeIsNotSet")
    assert native["matchedAs"] == "native-rest-name"
    assert native["error"]["code"] == "IR-4102"
    ir_name = await explain("attributerequired")
    assert ir_name["matchedAs"] == "ir-name"
    assert ir_name["error"]["code"] == "IR-4102"


async def test_explain_name_shared_by_ir_and_native() -> None:
    data = await explain("TokenExpired")
    assert data["matchedAs"] == "ir-name"
    assert data["error"]["code"] == "IR-2004"
    assert data["alsoMatched"] == [{"code": "IR-2010", "name": "AccountRecoveryFailed"}]


async def test_explain_soap_fault_text() -> None:
    data = await explain("Task 12 is locked by another user")
    assert data["matchedAs"] == "soap-fault"
    assert data["error"]["code"] == "IR-4301"
    assert data["matchedPattern"]
    unmatched = await explain("Object reference not set to an instance of an object.")
    assert unmatched["matchedAs"] == "soap-fault-unmatched"
    assert unmatched["error"]["code"] == "IR-5003"


async def test_explain_http_status_and_unknown_native_code() -> None:
    forbidden = await explain("HTTP 403")
    assert forbidden["error"]["code"] == "IR-4201"
    unauthorized = await explain("http 401")
    assert unauthorized["error"]["code"] == "IR-2004"
    assert unauthorized["alsoMatched"][0]["code"] == "IR-2005"
    unknown = await explain("4242")
    assert unknown["matchedAs"] == "native-rest-code-unknown"
    assert unknown["error"]["code"] == "IR-5099"
    ir_number = await explain("5006")
    assert ir_number["error"]["code"] == "IR-5006"


async def test_explain_result_level_failures() -> None:
    data = await explain("IR-4002")
    ops = [r["operationId"] for r in data["mappings"]["soap"]["resultFailures"]]
    assert "soap.FindUserByName" in ops
    assert "soap.FindUserByName" in data["raisedBy"]


async def test_explain_templates_hint_with_operation() -> None:
    data = await explain("IR-4102", operationId="rest.v1.taskactions.releaseTask")
    assert "rest.v1.taskactions.releaseTask" in data["error"]["hint"]
    locked = await explain("IR-4301", operationId="soap.LockPage")
    assert locked["error"]["message"] == "The page is locked by another user."


async def test_explain_unknown_inputs() -> None:
    payload = await envelope("ir_explain_error", {"query": "TaskLockedByAnotherUsr"})
    assert payload["error"]["code"] == "IR-3010"
    assert "TaskLockedByAnotherUser" in payload["error"]["suggestions"]
    missing = await envelope("ir_explain_error", {"query": "IR-4999"})
    assert missing["error"]["code"] == "IR-3010"
    assert all(s.startswith("IR-4") for s in missing["error"]["suggestions"])
    bad_op = await envelope("ir_explain_error", {"query": "IR-4301", "operationId": "nope"})
    assert bad_op["error"]["code"] == "IR-3001"
    empty = await envelope("ir_explain_error", {"query": "  "})
    assert empty["error"]["code"] == "IR-3005"


# ---------------------------------------------------------------- M2 gaps now filled


async def test_describe_flow_failure_points_carry_ir_codes() -> None:
    payload = await envelope("ir_describe_flow", {"flowId": "F3"})
    errors = [e for point in payload["data"]["failurePoints"] for e in point["errors"]]
    assert errors
    registry = get_registry()
    for entry in errors:
        assert entry["irCode"] in registry.entries
        assert entry["irName"] == registry.entry(entry["irCode"])["name"]
