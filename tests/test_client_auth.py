"""AuthManager through RestClient + MockTransport: header per mode, expiry/renewal, single-flight
re-auth on 401 with replay only for idempotent requests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from imageright_mcp.client import MockReply, PreparedRequest
from tests.conftest import (
    FAR_FUTURE,
    PASSWORD,
    TOKEN,
    TOKEN2,
    USER,
    ClientFactory,
    FakeClock,
)

DOC = "rest.v1.documents.getDocumentById"
DOC_PATH = "/api/documents/5"
NEW_DOC = {"ParentId": 1, "DocumentTypeId": 2, "Description": "d", "DocumentDate": "2026-09-24"}


def _auth(request: PreparedRequest) -> str | None:
    return request.headers.get("Authorization")


# ---------------------------------------------------------------------------- password


async def test_password_mode_logs_in_and_sends_access_token(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("GET", DOC_PATH, MockReply(json={"Id": 5}))
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.ok, outcome.error
    assert outcome.data == {"Id": 5}
    login = mock.calls("POST", "/api/authenticate")[0]
    assert login.json == {"UserName": USER, "Password": PASSWORD}
    assert _auth(login) is None
    valid_to = mock.calls("POST", "/api/validto")[0]
    assert valid_to.text == TOKEN
    assert _auth(mock.calls("GET", DOC_PATH)[0]) == f"AccessToken {TOKEN}"
    assert client.auth.status()["expiresAt"].startswith("2099-01-01")


async def test_token_is_reused_then_renewed_60s_before_expiry(make_client: ClientFactory) -> None:
    clock = FakeClock(1_000_000.0)
    client, mock = make_client(clock=clock, login=False)
    expiry = "1970-01-12T13:50:00+00:00"  # 1_000_200.0: 200 s from "now"
    mock.add("POST", "/api/authenticate", MockReply(json=TOKEN), MockReply(json=TOKEN2))
    mock.add("POST", "/api/validto", MockReply(json=expiry), MockReply(json=FAR_FUTURE))
    mock.add("GET", DOC_PATH, MockReply(json={}), sticky=True)
    await client.call(DOC, {"docId": 5})
    clock.now += 100  # 100 s left: still fine
    await client.call(DOC, {"docId": 5})
    clock.now += 50  # 50 s left: inside the 60 s renewal window
    await client.call(DOC, {"docId": 5})
    assert [_auth(r) for r in mock.calls("GET", DOC_PATH)] == [
        f"AccessToken {TOKEN}",
        f"AccessToken {TOKEN}",
        f"AccessToken {TOKEN2}",
    ]
    assert client.auth.logins == 2


async def test_validto_failure_falls_back_to_short_lifetime(make_client: ClientFactory) -> None:
    client, mock = make_client(login=False)
    mock.add("POST", "/api/authenticate", MockReply(json=TOKEN))
    mock.add("POST", "/api/validto", MockReply(status=500))
    mock.add("GET", DOC_PATH, MockReply(json={}))
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.ok
    assert client.auth.status()["notes"]


async def test_concurrent_401s_share_one_login_and_reads_replay(
    make_client: ClientFactory,
) -> None:
    client, mock = make_client(login=False)
    logins = [TOKEN, TOKEN2]

    async def handler(request: PreparedRequest) -> MockReply:
        if request.url.endswith("/api/authenticate"):
            await asyncio.sleep(0.01)  # a slow login invites a thundering herd
            return MockReply(json=logins.pop(0))
        if request.url.endswith("/api/validto"):
            return MockReply(json=FAR_FUTURE)
        if _auth(request) == f"AccessToken {TOKEN}":
            await asyncio.sleep(0)
            return MockReply(status=401)
        return MockReply(json={"Id": 5})

    mock.handler = handler
    outcomes = await asyncio.gather(*(client.call(DOC, {"docId": 5}) for _ in range(8)))
    assert all(o.ok for o in outcomes), [o.error for o in outcomes]
    assert len(mock.calls("POST", "/api/authenticate")) == 2  # first login + ONE re-login
    assert client.auth.logins == 2
    replays = [r for r in mock.calls("GET", DOC_PATH) if _auth(r) == f"AccessToken {TOKEN2}"]
    assert len(replays) == 8


async def test_401_on_a_write_reauthenticates_but_does_not_replay(
    make_client: ClientFactory,
) -> None:
    client, mock = make_client(login=False)
    mock.add("POST", "/api/authenticate", MockReply(json=TOKEN), MockReply(json=TOKEN2))
    mock.add("POST", "/api/validto", MockReply(json=FAR_FUTURE), sticky=True)
    mock.add("POST", "/api/documents", MockReply(status=401), MockReply(status=201, json=77))
    outcome = await client.call("rest.v1.documents.createDocument", NEW_DOC)
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2004"
    assert "never replayed" in outcome.error["hint"]
    assert len(mock.calls("POST", "/api/documents")) == 1
    assert client.auth.logins == 2  # the session was renewed for the next call
    again = await client.call("rest.v1.documents.createDocument", NEW_DOC)
    assert again.ok
    assert again.data == 77
    assert _auth(mock.calls("POST", "/api/documents")[1]) == f"AccessToken {TOKEN2}"


async def test_replayed_read_that_still_gets_401_is_not_retried_again(
    make_client: ClientFactory,
) -> None:
    client, mock = make_client()
    mock.add("GET", DOC_PATH, MockReply(status=401), sticky=True)
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2004"
    assert len(mock.calls("GET", DOC_PATH)) == 2  # original + exactly one replay


async def test_bad_credentials_map_to_invalid_credentials(make_client: ClientFactory) -> None:
    client, mock = make_client(login=False)
    mock.add("POST", "/api/authenticate", MockReply(status=401))
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2001"
    assert not mock.calls("GET", DOC_PATH)


async def test_missing_password_is_a_config_error(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_PASSWORD": ""}, login=False)
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1001"
    assert "IMAGERIGHT_PASSWORD" in outcome.error["hint"]
    assert not mock.requests


async def test_password_can_come_from_a_renamed_env_var(make_client: ClientFactory) -> None:
    client, mock = make_client(
        {"IMAGERIGHT_PASSWORD": "", "IMAGERIGHT_SECRET_ENV": "password=CORP_PW", "CORP_PW": "x-pw"}
    )
    mock.add("GET", DOC_PATH, MockReply(json={}))
    assert (await client.call(DOC, {"docId": 5})).ok
    assert mock.calls("POST", "/api/authenticate")[0].json["Password"] == "x-pw"


async def test_extra_headers_go_on_every_request(make_client: ClientFactory) -> None:
    client, mock = make_client(
        {
            "IMAGERIGHT_EXTRA_HEADERS": "X-Tenant-Id=IR_TENANT,X-Api-Key=IR_API_KEY",
            "IR_TENANT": "tenant-42",
            "IR_API_KEY": "api-key-secret-9",
        }
    )
    mock.add("GET", DOC_PATH, MockReply(json={}))
    await client.call(DOC, {"docId": 5})
    for request in mock.requests:
        assert request.headers["X-Tenant-Id"] == "tenant-42"
        assert request.headers["X-Api-Key"] == "api-key-secret-9"


# ---------------------------------------------------------------------------- jwt


def _rsa_pem() -> tuple[str, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


async def test_static_jwt_header(make_client: ClientFactory) -> None:
    static = pyjwt.encode({"sub": "svc", "exp": 4_000_000_000}, "k" * 32, algorithm="HS256")
    client, mock = make_client({"IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT": static})
    mock.add("GET", DOC_PATH, MockReply(json={}))
    assert (await client.call(DOC, {"docId": 5})).ok
    assert [_auth(r) for r in mock.requests] == [f"JWT {static}"]


async def test_expired_static_jwt_is_token_expired(make_client: ClientFactory) -> None:
    static = pyjwt.encode({"sub": "svc", "exp": 1_000}, "k" * 32, algorithm="HS256")
    client, mock = make_client({"IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT": static})
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2004"
    assert "IMAGERIGHT_JWT_PRIVATE_KEY" in outcome.error["hint"]
    assert not mock.requests


async def test_static_jwt_401_is_not_refreshable(make_client: ClientFactory) -> None:
    static = pyjwt.encode({"sub": "svc"}, "k" * 32, algorithm="HS256")
    client, mock = make_client({"IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT": static})
    mock.add("GET", DOC_PATH, MockReply(status=401), sticky=True)
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2004"
    assert len(mock.requests) == 1


async def test_self_signed_rs256_jwt(make_client: ClientFactory, tmp_path: Path) -> None:
    pem, public = _rsa_pem()
    key_file = tmp_path / "ir-key.pem"
    key_file.write_text(pem)
    clock = FakeClock(1_900_000_000.0)
    client, mock = make_client(
        {
            "IMAGERIGHT_AUTH_MODE": "jwt",
            "IMAGERIGHT_JWT_PRIVATE_KEY_FILE": str(key_file),
            "IMAGERIGHT_JWT_ISSUER": "imageright-mcp",
            "IMAGERIGHT_JWT_AUDIENCE": "imageright",
            "IMAGERIGHT_JWT_TTL_SECONDS": "120",
        },
        clock=clock,
    )
    mock.add("GET", DOC_PATH, MockReply(json={}), sticky=True)
    await client.call(DOC, {"docId": 5})
    clock.now += 30
    await client.call(DOC, {"docId": 5})  # still valid: reused
    clock.now += 40  # 50 s left of 120 -> inside min(60, ttl/2) = 60 s window: re-signed
    await client.call(DOC, {"docId": 5})
    headers = [_auth(r) or "" for r in mock.requests]
    assert headers[0] == headers[1] != headers[2]
    token = headers[0].removeprefix("JWT ")
    claims = pyjwt.decode(
        token,
        public,
        algorithms=["RS256"],
        audience="imageright",
        issuer="imageright-mcp",
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert claims["sub"] == USER
    assert claims["iat"] == 1_900_000_000
    assert claims["exp"] - claims["iat"] == 120
    assert claims["nbf"] <= claims["iat"]
    assert client.auth.logins == 2


async def test_self_signed_jwt_needs_issuer_and_audience(make_client: ClientFactory) -> None:
    pem, _ = _rsa_pem()
    client, _mock = make_client({"IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT_PRIVATE_KEY": pem})
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1001"


# ---------------------------------------------------------------------------- saml


async def test_saml_token_from_env(make_client: ClientFactory) -> None:
    client, mock = make_client(
        {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN": "PHNhbWw+c2VjcmV0PC9zYW1sPg=="}
    )
    mock.add("GET", DOC_PATH, MockReply(json={}))
    assert (await client.call(DOC, {"docId": 5})).ok
    assert _auth(mock.requests[0]) == "SecurityToken PHNhbWw+c2VjcmV0PC9zYW1sPg=="


async def test_saml_command_reruns_after_401(make_client: ClientFactory) -> None:
    issued = ["c2FtbC1vbmU=", "c2FtbC10d28="]
    commands: list[list[str]] = []

    async def runner(args: list[str]) -> str:
        commands.append(args)
        return issued.pop(0)

    client, mock = make_client(
        {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN_COMMAND": "get-token --adfs x"},
        command_runner=runner,
    )
    mock.add("GET", DOC_PATH, MockReply(status=401), MockReply(json={"Id": 5}))
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.ok
    assert commands == [["get-token", "--adfs", "x"]] * 2
    assert [_auth(r) for r in mock.requests] == [
        "SecurityToken c2FtbC1vbmU=",
        "SecurityToken c2FtbC10d28=",
    ]


async def test_real_saml_command_runs_without_a_shell(make_client: ClientFactory) -> None:
    client, mock = make_client(
        {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN_COMMAND": "echo dG9rZW4="}
    )
    mock.add("GET", DOC_PATH, MockReply(json={}))
    assert (await client.call(DOC, {"docId": 5})).ok
    assert _auth(mock.requests[0]) == "SecurityToken dG9rZW4="


async def test_failing_saml_command_is_a_config_error(make_client: ClientFactory) -> None:
    client, _ = make_client(
        {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN_COMMAND": "false"}
    )
    outcome = await client.call(DOC, {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1001"


@pytest.mark.parametrize("mode", ["password", "jwt", "saml"])
async def test_preview_shows_scheme_without_credentials(
    make_client: ClientFactory, mode: str
) -> None:
    client, mock = make_client(
        {"IMAGERIGHT_AUTH_MODE": mode, "IMAGERIGHT_PASSWORD": ""}, login=False
    )
    outcome = await client.call(DOC, {"docId": 5}, dry_run=True)
    assert outcome.ok
    scheme = {"password": "AccessToken", "jwt": "JWT", "saml": "SecurityToken"}[mode]
    assert outcome.data["preview"]["request"]["headers"]["Authorization"] == f"{scheme} ***"
    assert not mock.requests  # dry-run: no auth calls, no network
