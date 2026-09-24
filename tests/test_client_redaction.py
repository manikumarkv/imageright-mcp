"""REDACTION gate (plan §4.4, M4): no secret appears in any preview, error, envelope or log line.

Runs through the real RestTransport (httpx2 with an in-process mock transport, so the wire path
and httpx2's own logging are exercised) with DEBUG logging switched on for every logger. The
server fixtures deliberately echo secrets back, as misbehaving servers do.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx2
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from imageright_mcp.client import BodySink, RestClient, RestTransport
from imageright_mcp.client.models import TransportFailure
from imageright_mcp.config import load_config
from tests.conftest import FakeClock, no_sleep

PASSWORD = "Hunter2-REDACT-ME-password"
URL_PASSWORD = "UrlPw-REDACT-ME-inline"
TOKEN = "AccessTok-REDACT-ME-0001"
TOKEN2 = "AccessTok-REDACT-ME-0002"
API_KEY = "ApiKey-REDACT-ME-42"
SAML = "U0FNTC1SRURBQ1QtTUUtdG9rZW4="
STATIC_JWT = pyjwt.encode({"sub": "svc", "exp": 4_000_000_000}, "k" * 32, algorithm="HS256")


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


PEM = _pem()
PEM_BODY = PEM.splitlines()[5]  # a line from the middle of the key


def _server(request: httpx2.Request) -> httpx2.Response:
    """A server that leaks everything it can into its responses."""
    path = request.url.path
    body = request.content.decode("utf-8", "replace")
    if path.endswith("/api/authenticate"):
        if PASSWORD not in body:
            return httpx2.Response(400, json={"ErrorCode": 1, "Message": f"bad login: {body}"})
        return httpx2.Response(200, json=TOKEN)
    if path.endswith("/api/validto"):
        return httpx2.Response(200, json="2099-01-01T00:00:00Z")
    auth = request.headers.get("authorization", "")
    echo = f"rejected {auth} key={request.headers.get('x-api-key')} body={body}"
    if path.endswith("/api/documents/401"):
        return httpx2.Response(401, json={"Message": echo})
    if path.endswith("/api/documents/500"):
        return httpx2.Response(500, json={"ErrorCode": 99999, "Message": echo})
    if path.endswith("/api/documents"):
        return httpx2.Response(400, json={"ErrorCode": 600, "Message": echo})
    return httpx2.Response(200, json={"Echo": echo})


def _client(tmp_path: Path, env: dict[str, str], handler: Any = _server) -> RestClient:
    config = load_config(
        {
            "IMAGERIGHT_REST_BASE_URL": f"https://svc:{URL_PASSWORD}@ir.example.test/IR",
            "IMAGERIGHT_USERNAME": "svc",
            "IMAGERIGHT_PASSWORD": PASSWORD,
            "IMAGERIGHT_WRITE_MODE": "allow",
            "IMAGERIGHT_FILE_ROOTS": str(tmp_path),
            "IMAGERIGHT_EXTRA_HEADERS": "X-Api-Key=IR_KEY",
            "IR_KEY": API_KEY,
            **env,
        }
    )
    transport = RestTransport(
        http_transport=httpx2.MockTransport(handler), sink=BodySink(tmp_path / "out")
    )
    return RestClient(config, transport=transport, clock=FakeClock(), sleep=no_sleep)


SECRETS = [PASSWORD, URL_PASSWORD, TOKEN, TOKEN2, API_KEY, SAML, STATIC_JWT, PEM_BODY]


def _assert_clean(label: str, text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{label} leaked {secret[:12]}…"


async def _exercise(client: RestClient, tmp_path: Path) -> list[str]:
    scan = tmp_path / "scan.tif"
    scan.write_bytes(b"II*\x00")
    doc = {"ParentId": 1, "DocumentTypeId": 2, "Description": "d", "DocumentDate": "2026-09-24"}
    outcomes = [
        await client.call("rest.v1.documents.getDocumentById", {"docId": 5}),
        await client.call("rest.v1.documents.getDocumentById", {"docId": 401}),
        await client.call("rest.v1.documents.getDocumentById", {"docId": 500}),
        await client.call("rest.v1.documents.createDocument", doc),
        await client.call("rest.v1.documents.createDocument", doc, dry_run=True),
        await client.call(
            "rest.v1.pages.createPage", {"DocId": 1}, {"image0": str(scan)}, dry_run=True
        ),
        await client.call("rest.v2.documents.deleteDocument", {"documentId": 9}),
        await client.call("rest.v1.documents.getDocumentById", {}),  # validation error
        await client.call("rest.v1.authentication.validTo", {"body": "x"}, dry_run=True),
    ]
    rendered = []
    for outcome in outcomes:
        envelope = outcome.envelope()
        rendered.append(json.dumps(envelope.structured_content))
        rendered.extend(c.text for c in envelope.content if hasattr(c, "text"))
    return rendered


@pytest.mark.parametrize(
    "env",
    [
        {"IMAGERIGHT_AUTH_MODE": "password"},
        {"IMAGERIGHT_AUTH_MODE": "jwt", "IMAGERIGHT_JWT": STATIC_JWT},
        {
            "IMAGERIGHT_AUTH_MODE": "jwt",
            "IMAGERIGHT_JWT_PRIVATE_KEY": PEM,
            "IMAGERIGHT_JWT_ISSUER": "iss",
            "IMAGERIGHT_JWT_AUDIENCE": "aud",
        },
        {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN": SAML},
    ],
    ids=["password", "jwt-static", "jwt-signed", "saml"],
)
async def test_no_secret_in_previews_errors_envelopes_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, env: dict[str, str]
) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client(tmp_path, env)
    rendered = await _exercise(client, tmp_path)
    await client.aclose()
    for i, text in enumerate(rendered):
        _assert_clean(f"envelope #{i}", text)
    _assert_clean("logs", caplog.text)
    for record in caplog.records:
        _assert_clean(f"log record {record.name}", record.getMessage())
    # Self-signed JWTs are secrets too, even though they are minted at runtime.
    if env.get("IMAGERIGHT_JWT_PRIVATE_KEY"):
        assert client.auth.logins >= 1
        minted = client.auth._token  # the live token itself must not appear anywhere
        assert minted
        for text in rendered:
            assert minted not in text
        assert minted not in caplog.text


async def test_secret_echoed_in_a_failed_login_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def rude(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={"ErrorCode": 1, "Message": f"wrong: {request.content.decode()}"},
        )

    client = _client(tmp_path, {}, rude)
    outcome = await client.call("rest.v1.documents.getDocumentById", {"docId": 5})
    assert outcome.error is not None
    text = json.dumps(outcome.envelope().structured_content)
    _assert_clean("login error", text)
    _assert_clean("logs", caplog.text)


async def test_transport_error_text_is_redacted(tmp_path: Path) -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot reach {request.url} as {PASSWORD}", request=request)

    client = _client(tmp_path, {}, broken)
    outcome = await client.call("rest.v1.documents.getDocumentById", {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-5004"
    _assert_clean("transport error", json.dumps(outcome.envelope().structured_content))


def test_transport_failure_carries_no_request_object() -> None:
    failure = TransportFailure("x")
    assert failure.__cause__ is None


async def test_url_password_never_reaches_the_wire_or_preview(tmp_path: Path) -> None:
    """M0 regression: a password embedded in restBaseUrl once leaked through the config view."""
    seen: list[httpx2.Request] = []

    def spy(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _server(request)

    client = _client(tmp_path, {"IMAGERIGHT_AUTH_MODE": "saml", "IMAGERIGHT_SAML_TOKEN": SAML}, spy)
    preview = await client.call("rest.v1.documents.getDocumentById", {"docId": 5}, dry_run=True)
    assert URL_PASSWORD not in json.dumps(preview.envelope().structured_content)
    assert (await client.call("rest.v1.documents.getDocumentById", {"docId": 5})).ok
    for request in seen:
        assert URL_PASSWORD not in str(request.url)
        assert request.headers["authorization"] == f"SecurityToken {SAML}"
    assert client.config.redacted()["restBaseUrl"].count("***") == 1
