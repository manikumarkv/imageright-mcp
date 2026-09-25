"""ir_test_connection, ir_session, ir_configure and shutdown (plan §3.3, §4.2, §4.4; M6).

Includes the version-mismatch gate: a server reporting a version whose catalog profile differs
from the configured one raises IR-1004 (a warning, or an error with strictVersion). Everything
runs against MockTransport / FakeAsmx; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import jwt as pyjwt
import pytest
from mcp.client.client import Client
from mcp.types import TextContent

from imageright_mcp.client import MockReply, MockTransport, TransportFailure
from imageright_mcp.runtime import Runtime
from imageright_mcp.server import create_server, serve
from tests.conftest import (
    BASE,
    PASSWORD,
    TOKEN,
    USER,
    ClientFactory,
    mock_runtime,
    script_rest_login,
)
from tests.test_client_soap import ENV as SOAP_ENV
from tests.test_client_soap import NS, SOAP_URL, FakeAsmx

REST_ENV = {
    "IMAGERIGHT_REST_BASE_URL": BASE,
    "IMAGERIGHT_USERNAME": USER,
    "IMAGERIGHT_PASSWORD": PASSWORD,
    "IMAGERIGHT_VERSION": "24.x",
}
V24 = {"Build": 115, "Major": 24, "MajorRevision": 0, "Minor": 2, "MinorRevision": 0, "Revision": 0}


def healthy(mock: MockTransport, version: dict[str, Any] | None = None) -> None:
    script_rest_login(mock)
    mock.add("GET", "/api/health", MockReply(body=b"", headers={"Content-Type": "text/plain"}))
    mock.add("GET", "/api/integration/version", MockReply(json=version or V24))


async def call(runtime: Runtime, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(create_server(runtime=runtime)) as client:
        result = await client.call_tool(tool, args)
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert result.is_error is (not payload["ok"])
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    for secret in (PASSWORD, TOKEN):
        assert secret not in text
    return payload


# ---------------------------------------------------------------------------- test connection


async def test_rest_connection_is_healthy_when_versions_match() -> None:
    mock = MockTransport()
    healthy(mock)
    payload = await call(mock_runtime(REST_ENV, mock), "ir_test_connection", {})
    assert payload["ok"] is True, payload["error"]
    data = payload["data"]
    assert data["healthy"] is True
    (rest,) = data["surfaces"]
    assert rest["surface"] == "rest"
    assert rest["reachable"] is rest["authenticated"] is rest["ok"] is True
    assert rest["version"]["serverVersion"] == "24.2.115"
    assert rest["version"]["serverProfile"] == rest["version"]["configuredProfile"] == "24.2"
    assert rest["version"]["match"] is True
    assert rest["auth"]["mode"] == "password"
    assert rest["latencyMs"] >= 0
    assert payload["meta"]["warnings"] == []
    health = mock.calls("GET", "/api/health")[0]
    assert "Authorization" not in health.headers  # anonymous probe
    assert mock.calls("GET", "/api/integration/version")[0].headers["Authorization"] == (
        f"AccessToken {TOKEN}"
    )


async def test_version_mismatch_is_ir_1004_warning() -> None:
    mock = MockTransport()
    healthy(mock, {**V24, "Major": 25, "Minor": 1, "Build": 340})
    payload = await call(mock_runtime(REST_ENV, mock), "ir_test_connection", {})
    assert payload["ok"] is True
    assert payload["data"]["healthy"] is False
    (warning,) = payload["meta"]["warnings"]
    assert warning["code"] == "IR-1004"
    assert warning["name"] == "ServerVersionMismatch"
    assert "25.1.340" in warning["message"]
    assert "profile 24.2" in warning["message"]


async def test_version_mismatch_is_an_error_with_strict_version() -> None:
    mock = MockTransport()
    healthy(mock, {**V24, "Major": 7, "Minor": 2, "Build": 1181})
    env = {**REST_ENV, "IMAGERIGHT_STRICT_VERSION": "true"}
    payload = await call(mock_runtime(env, mock), "ir_test_connection", {})
    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "IR-1004"
    assert error["native"]["serverProfile"] == "7.2"
    assert error["surfaces"][0]["version"]["serverVersion"] == "7.2.1181"


async def test_unsupported_server_version_is_a_mismatch() -> None:
    mock = MockTransport()
    healthy(mock, {**V24, "Major": 9, "Minor": 0})
    payload = await call(mock_runtime(REST_ENV, mock), "ir_test_connection", {})
    assert [w["code"] for w in payload["meta"]["warnings"]] == ["IR-1004"]
    assert payload["data"]["surfaces"][0]["version"]["serverProfile"] is None


async def test_unreachable_server_reports_the_mapped_error() -> None:
    mock = MockTransport()
    mock.add("GET", "/api/health", MockReply(fail=TransportFailure("connection refused")))
    payload = await call(mock_runtime(REST_ENV, mock), "ir_test_connection", {})
    assert payload["ok"] is True
    (rest,) = payload["data"]["surfaces"]
    assert rest["reachable"] is False
    assert rest["ok"] is False
    assert rest["error"]["code"] == "IR-5004"
    assert payload["data"]["healthy"] is False


async def test_bad_credentials_show_as_auth_failure() -> None:
    mock = MockTransport()
    mock.add("GET", "/api/health", MockReply(body=b"", headers={"Content-Type": "text/plain"}))
    mock.add("POST", "/api/authenticate", MockReply(status=401))
    payload = await call(mock_runtime(REST_ENV, mock), "ir_test_connection", {})
    (rest,) = payload["data"]["surfaces"]
    assert rest["reachable"] is True
    assert rest["authenticated"] is False
    assert rest["auth"]["ok"] is False
    assert rest["error"]["code"] == "IR-2001"


async def test_jwt_mode_also_validates_the_token() -> None:
    static = pyjwt.encode({"sub": "svc", "exp": 4_000_000_000}, "k" * 32, algorithm="HS256")
    env = {**REST_ENV, "IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT": static}
    mock = MockTransport()
    mock.add("GET", "/api/health", MockReply(body=b"", headers={"Content-Type": "text/plain"}))
    mock.add("GET", "/api/integration/version", MockReply(json=V24))
    mock.add("POST", "/api/jwtTokens/validate", MockReply(json="2096-10-02T07:06:40Z"))
    payload = await call(mock_runtime(env, mock), "ir_test_connection", {})
    assert payload["ok"] is True, payload["error"]
    rest = payload["data"]["surfaces"][0]
    assert rest["jwtValidation"] == {
        "ok": True,
        "validTo": "2096-10-02T07:06:40Z",
        "latencyMs": rest["jwtValidation"]["latencyMs"],
    }
    (validate,) = mock.calls("POST", "/api/jwtTokens/validate")
    assert validate.json == static
    assert static not in json.dumps(payload)


async def test_soap_connection_lists_connections_and_logs_in() -> None:
    asmx = FakeAsmx(connections=["Production", "Training"])
    asmx.script(
        "Version",
        lambda op, _t: (
            200,
            f'<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><VersionResponse xmlns="{NS}">'
            "<VersionResult><WebService>4.0.1.2</WebService><Library>24.2.115</Library>"
            "<Session>1</Session></VersionResult></VersionResponse></soap:Body></soap:Envelope>",
        ),
    )
    mock = MockTransport(handler=asmx)
    env = {
        "IMAGERIGHT_SOAP_URL": SOAP_URL,
        "IMAGERIGHT_SOAP_CONNECTION": "Production",
        "IMAGERIGHT_USERNAME": USER,
        "IMAGERIGHT_PASSWORD": PASSWORD,
    }
    payload = await call(mock_runtime(env, mock), "ir_test_connection", {"surfaces": ["soap"]})
    assert payload["ok"] is True, payload["error"]
    (soap,) = payload["data"]["surfaces"]
    assert soap["reachable"] is soap["authenticated"] is True
    assert soap["version"]["webService"] == "4.0.1.2"
    assert "availableConnections" not in soap  # soapConnection is configured
    assert soap["auth"]["connection"] == "Production"
    assert asmx.ops() == ["Version", "UserLogin"]

    del env["IMAGERIGHT_SOAP_CONNECTION"]
    asmx.script("Version", lambda op, _t: (500, "not xml"))
    payload = await call(mock_runtime(env, mock), "ir_test_connection", {"surfaces": ["soap"]})
    (soap,) = payload["data"]["surfaces"]
    assert soap["ok"] is False


async def test_soap_without_connection_lists_them() -> None:
    asmx = FakeAsmx(connections=["Production", "Training"])
    mock = MockTransport(handler=asmx)
    env = {
        "IMAGERIGHT_SOAP_URL": SOAP_URL,
        "IMAGERIGHT_USERNAME": USER,
        "IMAGERIGHT_PASSWORD": PASSWORD,
    }
    payload = await call(mock_runtime(env, mock), "ir_test_connection", {})
    (soap,) = payload["data"]["surfaces"]
    assert soap["availableConnections"] == ["Production", "Training"]
    assert soap["authenticated"] is False  # two connections and none configured
    assert soap["error"]["code"] == "IR-1001"


async def test_unconfigured_surface_is_reported() -> None:
    mock = MockTransport()
    healthy(mock)
    payload = await call(
        mock_runtime(REST_ENV, mock), "ir_test_connection", {"surfaces": ["rest", "soap"]}
    )
    assert [s["surface"] for s in payload["data"]["surfaces"]] == ["rest", "soap"]
    assert payload["data"]["surfaces"][1]["error"]["code"] == "IR-1003"


# ---------------------------------------------------------------------------- session


async def test_session_status_refresh_and_logout() -> None:
    asmx = FakeAsmx()
    mock = MockTransport(handler=asmx)
    script_rest_login(mock)
    env = {**REST_ENV, **SOAP_ENV}
    runtime = mock_runtime(env, mock)
    status = await call(runtime, "ir_session", {})
    assert status["data"]["sessions"]["rest"]["authenticated"] is False
    assert status["data"]["sessions"]["soap"]["authenticated"] is False

    refreshed = await call(runtime, "ir_session", {"action": "refresh"})
    assert refreshed["ok"] is True, refreshed["error"]
    sessions = refreshed["data"]["sessions"]
    assert sessions["rest"]["authenticated"] is True
    assert sessions["rest"]["expiresAt"].startswith("2099-01-01")
    assert sessions["soap"]["authenticated"] is True
    assert sessions["soap"]["logins"] == 1

    again = await call(runtime, "ir_session", {"action": "login", "surface": "soap"})
    assert again["data"]["sessions"]["soap"]["logins"] == 2
    assert asmx.ops()[-2:] == ["UserLogoff", "UserLogin"]  # the old session is ended first

    out = await call(runtime, "ir_session", {"action": "logout"})
    assert out["data"]["result"]["soap"] == {"ok": True, "userLogoff": True}
    assert out["data"]["sessions"]["rest"]["authenticated"] is False
    assert out["data"]["sessions"]["soap"]["authenticated"] is False
    assert asmx.ops()[-1] == "UserLogoff"


async def test_session_counts_soap_token_rotations(make_client: ClientFactory) -> None:
    client, mock = make_client(SOAP_ENV)
    mock.handler = FakeAsmx()
    for _ in range(3):
        assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    status = client.soap.session.status()
    assert status["tokenRotations"] == 3
    assert status["logins"] == 1


# ---------------------------------------------------------------------------- configure


async def test_configure_overrides_are_session_scoped_and_visible() -> None:
    runtime = Runtime({"IMAGERIGHT_VERSION": "24.x"})
    payload = await call(
        runtime, "ir_configure", {"settings": {"writeMode": "allow", "irVersion": "25.x"}}
    )
    assert payload["ok"] is True, payload["error"]
    data = payload["data"]
    assert data["changed"] == ["irVersion", "writeMode"]
    assert data["config"]["writeMode"] == "allow"
    assert data["config"]["profile"]["profile"] == "25.1"
    assert data["config"]["sources"]["writeMode"] == "runtime"
    config = await call(runtime, "ir_get_config", {})
    assert config["data"]["writeMode"] == "allow"
    # Explorer tools follow the overridden version.
    listed = await call(
        runtime, "ir_check_availability", {"operationId": "rest.v1.pages.createPage"}
    )
    assert listed["ok"] is True
    reset = await call(runtime, "ir_configure", {"reset": True})
    assert reset["data"]["config"]["writeMode"] == "dry-run"
    assert reset["data"]["overrides"] == []


@pytest.mark.parametrize("key", ["password", "jwt", "jwtPrivateKey", "samlToken", "apiKey"])
async def test_configure_rejects_secrets_without_echoing_them(key: str) -> None:
    secret = "do-not-echo-9f8e7d"
    payload = await call(Runtime({}), "ir_configure", {"settings": {key: secret}})
    assert payload["error"]["code"] == "IR-3005"
    assert "never accepted" in payload["error"]["message"]
    assert "IMAGERIGHT_PASSWORD" in payload["error"]["hint"]
    assert secret not in json.dumps(payload)


@pytest.mark.parametrize(
    ("settings", "fragment"),
    [
        ({"samlTokenCommand": "curl evil"}, "environment or the config file"),
        ({"extraHeaders": {"X-Key": "HOME"}}, "environment or the config file"),
        ({"verifyTls": False}, "environment or the config file"),
        ({"colour": "blue"}, "Unknown setting"),
        ({"writeMode": "yolo-secret-value"}, "Invalid setting value"),
        ({"maxRetries": 99}, "Invalid setting value"),
    ],
)
async def test_configure_rejects_env_only_unknown_and_invalid(
    settings: dict[str, Any], fragment: str
) -> None:
    payload = await call(Runtime({}), "ir_configure", {"settings": settings})
    assert payload["error"]["code"] == "IR-3006"
    assert fragment in payload["error"]["message"]
    assert "yolo-secret-value" not in json.dumps(payload)


async def test_moving_an_endpoint_withholds_env_credentials() -> None:
    mock = MockTransport()
    healthy(mock)
    runtime = mock_runtime(REST_ENV, mock)
    moved = await call(
        runtime, "ir_configure", {"settings": {"restBaseUrl": "https://attacker.example.test"}}
    )
    assert moved["ok"] is True
    (warning,) = moved["meta"]["warnings"]
    assert warning["code"] == "IR-1001"
    assert "withheld" in warning["message"]
    assert moved["data"]["config"]["password"] is None
    login = await call(runtime, "ir_session", {"action": "login", "surface": "rest"})
    assert login["error"]["code"] == "IR-1001"
    assert mock.calls("POST", "/api/authenticate") == []  # the password never left

    same_host = await call(
        runtime,
        "ir_configure",
        {"reset": True, "settings": {"restBaseUrl": BASE + "/", "writeMode": "allow"}},
    )
    assert same_host["meta"]["warnings"] == []
    assert same_host["data"]["config"]["password"] == "***"


async def test_configure_rebuilds_the_client_only_for_connection_settings() -> None:
    asmx = FakeAsmx()
    mock = MockTransport(handler=asmx)
    runtime = mock_runtime({**REST_ENV, **SOAP_ENV}, mock)
    first = await runtime.client()
    await first.soap.session.login()
    runtime.configure({"writeMode": "allow", "fileRoots": ["/tmp"]})
    same = await runtime.client()
    assert same is first
    assert same.config.writeMode == "allow"
    assert same.builder.file_roots[0].name == "tmp"
    runtime.configure({"soapConnection": "Training"})
    rebuilt = await runtime.client()
    assert rebuilt is not first
    assert asmx.ops()[-1] == "UserLogoff"  # the old SOAP session was ended


# ---------------------------------------------------------------------------- shutdown


class FakeStdio:
    def __init__(self, *, ends: bool) -> None:
        self.ends = ends

    async def run_stdio_async(self) -> None:
        if self.ends:
            return  # stdin closed
        await asyncio.Event().wait()


@pytest.mark.parametrize("ends", [True, False], ids=["stdin-closed", "signal"])
async def test_shutdown_logs_off_soap_sessions(ends: bool) -> None:
    asmx = FakeAsmx()
    mock = MockTransport(handler=asmx)
    runtime = mock_runtime(
        SOAP_ENV | {"IMAGERIGHT_USERNAME": USER, "IMAGERIGHT_PASSWORD": PASSWORD}, mock
    )
    client = await runtime.client()
    await client.soap.session.login()
    stop = asyncio.Event()
    if not ends:
        asyncio.get_running_loop().call_later(0.01, stop.set)  # what SIGTERM's handler does
    reason = await serve(FakeStdio(ends=ends), runtime, stop)
    assert reason == ("stdin-closed" if ends else "stopped")
    assert asmx.ops() == ["UserLogin", "UserLogoff"]
    assert client.soap.session.active is False
    assert runtime.peek_client() is None
