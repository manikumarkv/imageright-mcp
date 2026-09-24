"""SOAP transport gate (plan §4.1, §4.4; M5): token rotation, the serialized queue, session
expiry and re-login, AvailableConnections, logoff, dry-run previews, envelope snapshots and
redaction. A scripted in-process ASMX server stands in for ``irwebservice40.asmx``; nothing here
touches the network.

Regenerate the envelope snapshots with ``UPDATE_SNAPSHOTS=1 pytest tests/test_client_soap.py``
and review the diff: they are the wire contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client import MockReply, PreparedRequest, RestClient
from imageright_mcp.client.soap import logoff_all
from imageright_mcp.client.soap_envelope import EnvelopeBuilder, SoapTable
from tests.conftest import FakeClock

SOAP_URL = "https://ir.example.test/ImageRight/irwebservice40.asmx"
SOAP_PASSWORD = "Pa55-w0rd-SECRET-value"
NS = get_catalog().soap_table["namespace"]
SNAPSHOTS = Path(__file__).parent / "snapshots" / "soap"
FAULTS = Path(__file__).parent / "fixtures" / "soap" / "faults"
ENV = {"IMAGERIGHT_SOAP_URL": SOAP_URL, "IMAGERIGHT_SOAP_CONNECTION": "Production"}
EXPIRED = (FAULTS / "session_expired.xml").read_text()

Reply = Callable[[str, str | None], "tuple[int, str] | str"]


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


def _token_of(envelope: str) -> str | None:
    match = re.search(r"<securityToken>(.*?)</securityToken>", envelope, re.S)
    return match.group(1) if match else None


def _response(op: str, result: str | None, token: str | None) -> str:
    inner = "" if result is None else f"<{op}Result>{result}</{op}Result>"
    echo = "" if token is None else f"<securityToken>{token}</securityToken>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{op}Response xmlns="{NS}">{inner}{echo}</{op}Response></soap:Body>'
        "</soap:Envelope>"
    )


def _fault(message: str, token: str | None = None) -> str:
    detail = "" if token is None else f"<securityToken>{token}</securityToken>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><soap:Fault><faultcode>soap:Server</faultcode>"
        f"<faultstring>{message}</faultstring><detail>{detail}</detail>"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )


class FakeAsmx:
    """A stateful ASMX endpoint. UserLogin mints ``login-N``; every token-bearing call rotates:
    the response echoes ``<sent>+`` unless a scripted reply says otherwise. Scripted replies are
    queued per operation and receive (operation, sent token)."""

    def __init__(self, connections: list[str] | None = None) -> None:
        self.connections = connections or ["Production"]
        self.logins = 0
        self.log: list[tuple[str, str | None]] = []
        self.bodies: list[str] = []
        self.scripts: dict[str, list[Reply]] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay_steps = 0

    def script(self, op: str, *replies: Reply) -> None:
        self.scripts.setdefault(op, []).extend(replies)

    def calls(self, op: str) -> list[str | None]:
        return [token for name, token in self.log if name == op]

    def ops(self) -> list[str]:
        return [name for name, _ in self.log]

    async def __call__(self, request: PreparedRequest) -> MockReply:
        assert request.url == SOAP_URL
        assert request.headers["Content-Type"] == "text/xml; charset=utf-8"
        op = request.headers["SOAPAction"].strip('"').rsplit("/", 1)[-1]
        body = request.text or ""
        token = _token_of(body)
        self.log.append((op, token))
        self.bodies.append(body)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(self.delay_steps):
                await asyncio.sleep(0)
            status, text = self._answer(op, body, token)
        finally:
            self.in_flight -= 1
        return MockReply(status=status, body=text, headers={"Content-Type": "text/xml"})

    def _answer(self, op: str, body: str, token: str | None) -> tuple[int, str]:
        queue = self.scripts.get(op)
        if queue:
            scripted = queue.pop(0)(op, token)
            return scripted if isinstance(scripted, tuple) else (200, scripted)
        if op == "UserLogin":
            assert f"<password>{SOAP_PASSWORD}</password>" in body
            self.logins += 1
            return 200, _response(op, f"login-{self.logins}", None)
        if op == "AvailableConnections":
            names = "".join(f"<string>{n}</string>" for n in self.connections)
            return 200, _response(op, names, None)
        if op in {"IsLoggedIn", "UserLogoff"}:
            return 200, _response(op, "true", None)
        return 200, _response(
            op, "<Description>doc</Description>" if op == "GetObject" else "", f"{token}+"
        )


def rotate_to(new: str | None) -> Reply:
    return lambda op, _t: _response(op, "", new)


def fault_with(new: str | None, message: str = "Object 42 was not found.") -> Reply:
    return lambda _op, _t: (500, _fault(message, new))


def expired(_op: str, _t: str | None) -> tuple[int, str]:
    return 500, EXPIRED


ClientMaker = Callable[..., tuple[RestClient, FakeAsmx]]


@pytest.fixture
def soap_client(make_client: Callable[..., Any]) -> ClientMaker:
    def factory(
        env: dict[str, str] | None = None, server: FakeAsmx | None = None, **kwargs: Any
    ) -> tuple[RestClient, FakeAsmx]:
        asmx = server or FakeAsmx()
        client, mock = make_client({**ENV, **(env or {})}, **kwargs)
        mock.handler = asmx
        return client, asmx

    return factory


# ---------------------------------------------------------------------------- token rotation


async def test_token_rotation_chain_including_faults(soap_client: ClientMaker) -> None:
    """t0 -> t1 -> t2: each request carries the token echoed by the previous response, a fault
    that carries a token rotates it too, and a response without an echo keeps the old one."""
    client, asmx = soap_client()
    asmx.script(
        "AddNote",
        rotate_to("t1"),  # sent t0 = login-1
        fault_with("t2"),  # sent t1; a fault that still echoes a token
        rotate_to(None),  # sent t2; no echo
        rotate_to("t3"),  # sent t2 again
    )
    args = {"objId": 5, "note": "hi"}
    first = await client.call("soap.AddNote", args)
    assert first.ok, first.error
    faulted = await client.call("soap.AddNote", args)
    assert faulted.error is not None
    assert faulted.error["code"] == "IR-4001"
    assert (await client.call("soap.AddNote", args)).ok
    assert (await client.call("soap.AddNote", args)).ok
    assert await client.call("soap.GetObject", {"objectId": 1})
    assert asmx.calls("AddNote") == ["login-1", "t1", "t2", "t2"]
    assert asmx.calls("GetObject") == ["t3"]
    assert asmx.logins == 1  # rotation never needed a new login


async def test_token_stays_in_memory(soap_client: ClientMaker, tmp_path: Path) -> None:
    client, _ = soap_client()
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    assert _files_containing(tmp_path, b"login-1") == []


# ---------------------------------------------------------------------------- serialization


async def test_parallel_calls_are_serialized(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.delay_steps = 5  # each exchange yields to the loop several times mid-flight
    outcomes = await asyncio.gather(
        *(client.call("soap.GetObject", {"objectId": i}) for i in range(8))
    )
    assert all(o.ok for o in outcomes), [o.error for o in outcomes]
    assert asmx.max_in_flight == 1
    assert asmx.logins == 1
    tokens = asmx.calls("GetObject")
    # Every call used the token the previous one received: one unbroken rotation chain.
    assert tokens == ["login-1" + "+" * i for i in range(8)]


# ---------------------------------------------------------------------------- session expiry


async def test_expired_read_logs_in_once_and_replays(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.script("GetObject", expired)
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.ok, outcome.error
    assert outcome.data == {"Description": "doc", "ObjectPermissions": []}
    assert outcome.meta["replayedAfterRelogin"] is True
    assert asmx.logins == 2
    assert asmx.calls("GetObject") == ["login-1", "login-2"]


async def test_expired_write_returns_ir_2007_and_is_not_replayed(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.script("AddNote", expired)
    outcome = await client.call("soap.AddNote", {"objId": 5, "note": "hi"})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2007"
    assert "never replayed" in outcome.error["hint"]
    assert asmx.calls("AddNote") == ["login-1"]
    assert asmx.logins == 2  # the session is renewed for the next call
    assert (await client.call("soap.AddNote", {"objId": 5, "note": "hi"})).ok
    assert asmx.calls("AddNote")[-1] == "login-2"


async def test_relogin_happens_only_once(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.script("GetObject", expired, expired)
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2007"
    assert asmx.logins == 2
    assert len(asmx.calls("GetObject")) == 2


async def test_idle_session_checks_is_logged_in(soap_client: ClientMaker) -> None:
    clock = FakeClock()
    client, asmx = soap_client(clock=clock)
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    clock.now += 19 * 60
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    assert asmx.calls("IsLoggedIn") == []
    clock.now += 21 * 60
    asmx.script("IsLoggedIn", lambda op, _t: _response(op, "false", None))
    assert (await client.call("soap.AddNote", {"objId": 5, "note": "hi"})).ok
    assert asmx.ops()[-3:] == ["IsLoggedIn", "UserLogin", "AddNote"]
    assert asmx.calls("AddNote") == ["login-2"]


async def test_idle_timeout_is_configurable(soap_client: ClientMaker) -> None:
    clock = FakeClock()
    client, asmx = soap_client({"IMAGERIGHT_SOAP_INACTIVITY_MINUTES": "1"}, clock=clock)
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    clock.now += 61
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    assert asmx.calls("IsLoggedIn") == ["login-1+"]
    assert asmx.logins == 1  # IsLoggedIn said true


# ---------------------------------------------------------------------------- login / logoff


async def test_single_available_connection_is_used(soap_client: ClientMaker) -> None:
    client, asmx = soap_client({"IMAGERIGHT_SOAP_CONNECTION": ""}, FakeAsmx(["OnlyOne"]))
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.ok, outcome.error
    assert asmx.ops()[:2] == ["AvailableConnections", "UserLogin"]
    assert "OnlyOne" in client.soap.session.status()["notes"][0]


async def test_several_connections_need_configuration(soap_client: ClientMaker) -> None:
    client, asmx = soap_client({"IMAGERIGHT_SOAP_CONNECTION": ""}, FakeAsmx(["Prod", "Test"]))
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1001"
    assert outcome.error["native"]["availableConnections"] == ["Prod", "Test"]
    assert "UserLogin" not in asmx.ops()


async def test_bad_credentials_map_to_ir_2001(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    bad = (FAULTS / "bad_credentials.xml").read_text()
    asmx.script("UserLogin", lambda _op, _t: (500, bad))
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-2001"
    assert "GetObject" not in asmx.ops()


async def test_session_ops_are_not_callable(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    for op in ("soap.UserLogin", "soap.UserLogoff"):
        outcome = await client.call(op, {})
        assert outcome.error is not None
        assert outcome.error["code"] == "IR-3006"
    assert asmx.log == []


async def test_logoff_on_close_is_best_effort(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    await client.aclose()
    assert asmx.log == []  # never logged in: nothing to log off
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    await client.aclose()
    assert asmx.ops()[-1] == "UserLogoff"
    assert asmx.calls("UserLogoff") == ["login-1+"]
    assert not client.soap.session.active

    broken, asmx2 = soap_client()
    assert (await broken.call("soap.GetObject", {"objectId": 1})).ok
    asmx2.script("UserLogoff", lambda _op, _t: (503, "gateway down"))
    await broken.aclose()  # must not raise
    assert not broken.soap.session.active


async def test_logoff_all_covers_live_sessions(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    assert (await client.call("soap.GetObject", {"objectId": 1})).ok
    assert await logoff_all() >= 1
    assert "UserLogoff" in asmx.ops()


# ---------------------------------------------------------------------------- policy / dry-run


async def test_write_dry_run_previews_envelope_without_sending(soap_client: ClientMaker) -> None:
    client, asmx = soap_client({"IMAGERIGHT_WRITE_MODE": "dry-run"})
    outcome = await client.call("soap.AddNote", {"objId": 5, "note": "a < b"})
    assert outcome.ok, outcome.error
    assert outcome.meta["dryRun"] is True
    preview = outcome.data["preview"]
    assert preview["request"]["soapAction"] == "http://imageright.com/imageright.webservice/AddNote"
    envelope = preview["request"]["envelope"]
    assert "<securityToken>***</securityToken>" in envelope
    assert "<note>a &lt; b</note>" in envelope
    assert "IR-2007" in preview["possibleErrors"]
    assert asmx.log == []


async def test_destructive_soap_needs_confirm(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    args = {"docId": 9, "killTasks": False}
    asmx.script(
        "DeleteDocument",
        lambda op, t: _response(op, "<Succeeded>true</Succeeded>", f"{t}+"),
    )
    first = await client.call("soap.DeleteDocument", args)
    assert first.ok, first.error
    assert first.meta["confirmRequired"] is True
    assert asmx.log == []
    pid = first.data["preview"]["previewId"]
    confirmed = await client.call("soap.DeleteDocument", args, confirm=pid)
    assert confirmed.ok, confirmed.error
    assert asmx.calls("DeleteDocument") == ["login-1"]


async def test_soap_validation_uses_the_operation_table(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    missing = await client.call("soap.GetDocumentByRef", {})
    assert missing.error is not None
    assert missing.error["code"] == "IR-3005"
    token = await client.call("soap.GetObject", {"objectId": 1, "securityToken": "x"})
    assert token.error is not None
    assert token.error["code"] == "IR-3006"
    wrong = await client.call("soap.GetObject", {"objectId": "one"})
    assert wrong.error is not None
    assert wrong.error["code"] == "IR-3006"
    bad_enum = await client.call(
        "soap.GetNotes", {"objectId": 1, "collectionId": 2, "noteType": "Nope"}
    )
    assert bad_enum.error is not None
    assert asmx.log == []


async def test_soap_needs_a_url(make_client: Callable[..., Any]) -> None:
    client, mock = make_client({"IMAGERIGHT_WRITE_MODE": "allow"})
    outcome = await client.call("soap.GetObject", {"objectId": 1})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1003"
    assert not mock.requests


async def test_result_level_failure_is_mapped(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.script(
        "DeleteDocument",
        lambda op, t: _response(
            op, "<Succeeded>false</Succeeded><ErrorMessage>locked</ErrorMessage>", t
        ),
    )
    first = await client.call("soap.DeleteDocument", {"docId": 9, "killTasks": False})
    pid = first.data["preview"]["previewId"]
    outcome = await client.call(
        "soap.DeleteDocument", {"docId": 9, "killTasks": False}, confirm=pid
    )
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-5008"


# ---------------------------------------------------------------------------- redaction


async def test_soap_secrets_never_leak(
    soap_client: ClientMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Password, current and rotated tokens stay out of results, errors, previews and logs,
    even when the server echoes them back in fault text and result data."""
    caplog.set_level(logging.DEBUG)
    client, asmx = soap_client({"IMAGERIGHT_WRITE_MODE": "dry-run"})
    asmx.script(
        "GetObject",
        lambda op, t: _response(
            op, f"<Description>{t} {SOAP_PASSWORD}</Description>", "rotated-SECRET-1"
        ),
        lambda _op, t: (500, _fault(f"Object not found for token {t} pw {SOAP_PASSWORD}")),
    )
    rendered = []
    for _ in range(2):
        outcome = await client.call("soap.GetObject", {"objectId": 1})
        rendered.append(json.dumps(outcome.envelope().structured_content))
    preview = await client.call("soap.AddNote", {"objId": 5, "note": "x"})
    rendered.append(json.dumps(preview.envelope().structured_content))
    await client.aclose()
    text = "\n".join(rendered) + caplog.text
    for secret in (SOAP_PASSWORD, "login-1", "rotated-SECRET-1"):
        assert secret not in text, secret
    assert asmx.calls("GetObject") == ["login-1", "rotated-SECRET-1"]


# ---------------------------------------------------------------------------- envelopes

SNAPSHOT_CALLS: dict[str, dict[str, Any]] = {
    "Version": {},
    "GetDocumentByRef": {
        "docRef": {"RefId": 1001, "Id": "D-1"},
        "getContent": True,
        "includeDeleted": False,
    },
    "CreateDocument": {
        "containerId": 42,
        "data": {"ObjTypeId": 7, "Name": "Policy & Terms", "Description": "Q3 <draft>"},
        "batchId": 0,
        "attributes": [
            {"Type": "atString", "Name": "PolicyNo", "Val": "P-1"},
            {"Type": "atInt", "Id": {"RefId": 3}, "Val": 12},
            None,
        ],
    },
    "FindDocumentsEx": {
        "searchConditions": {
            "Operation": "And",
            "DocumentConditions": [
                {
                    "ConditionName": "dsaDocType",
                    "AType": "atInt",
                    "ATarget": "catSelf",
                    "CompOp": "coEqual",
                    "Id": 0,
                    "Value": 55,
                },
                {
                    "ConditionName": "dsaDateCreated",
                    "AType": "atDate",
                    "ATarget": "catSelf",
                    "CompOp": "coBetween",
                    "Id": 0,
                    "Value": "2026-01-01T00:00:00",
                    "Value2": "2026-06-30T00:00:00",
                },
            ],
        },
        "includeDeleted": False,
        "includePageData": True,
    },
    "GetMapping": {"val": "ACME", "val2": 3.5, "lookupType": "Drawer"},
    "GetMultiPageImageFileUsingPages": {
        "pageRefs": {"PageRef": [{"RefId": 1}, {"RefId": 2, "Id": "p2"}]},
        "outputType": "PDF",
    },
    "KillTasks": {"taskRefs": [{"RefId": 900}]},
    "RouteTask": {
        "taskRef": {"RefId": 77},
        "stepRef": {"RefId": 0, "FlowProgrammaticName": "UW", "StepProgrammaticName": "Review"},
        "newAvailableDate": "2026-10-01T09:00:00",
        "userId": {"Id": 12, "HasId": True},
        "commit": True,
    },
    "AddPage": {
        "documentRef": {"RefId": 1001},
        "description": "",
        "imageList": {
            "Version": 1,
            "PreRotation": 0,
            "Rotation": 0,
            "Images": [
                {"Id": 0, "Rotation": 0, "ImageType": 1, "Extension": "tif", "Data": "SUkq"}
            ],
        },
        "batchId": 0,
        "archiveImmediately": True,
    },
    "SetTaskAttribute": {
        "taskRef": {"RefId": 77},
        "attributeRef": {"RefId": 5},
        "attribute": True,
    },
}


@pytest.mark.parametrize("operation", sorted(SNAPSHOT_CALLS))
async def test_envelope_snapshots(soap_client: ClientMaker, operation: str) -> None:
    client, asmx = soap_client({"IMAGERIGHT_WRITE_MODE": "dry-run", "IMAGERIGHT_DRY_RUN": "true"})
    outcome = await client.call(f"soap.{operation}", SNAPSHOT_CALLS[operation])
    assert outcome.ok, outcome.error
    assert outcome.data["preview"]["validation"]["ok"] is True
    envelope = outcome.data["preview"]["request"]["envelope"]
    path = SNAPSHOTS / f"{operation}.xml"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(envelope)
    assert envelope == path.read_text()
    assert asmx.log == []


async def test_live_envelope_matches_preview(soap_client: ClientMaker) -> None:
    """The bytes sent differ from the preview only by the token value."""
    client, asmx = soap_client()
    assert (await client.call("soap.GetDocumentByRef", SNAPSHOT_CALLS["GetDocumentByRef"])).ok
    sent = asmx.bodies[-1].replace("<securityToken>login-1<", "<securityToken>***<")
    assert sent == (SNAPSHOTS / "GetDocumentByRef.xml").read_text()


async def test_complex_response_is_json(soap_client: ClientMaker) -> None:
    client, asmx = soap_client()
    asmx.script(
        "GetAttributes",
        lambda op, t: _response(
            op,
            "<AttributeData><Type>atInt</Type><Name>Count</Name>"
            '<Val xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xsi:type="xsd:int">3</Val>'
            "</AttributeData>",
            t,
        ),
        lambda op, t: _response(op, "", t),
    )
    outcome = await client.call("soap.GetAttributes", {"objectId": 1})
    assert outcome.ok, outcome.error
    assert outcome.data == [{"Type": "atInt", "Name": "Count", "Val": 3}]
    assert outcome.meta["shape"] == "soap:ArrayOfAttributeData"
    empty = await client.call("soap.GetAttributes", {"objectId": 1})
    assert empty.data == []


def test_every_soap_operation_builds() -> None:
    """Each table entry yields a well-formed envelope from an empty call."""
    catalog = get_catalog()
    builder = EnvelopeBuilder(SoapTable(catalog.soap_table))
    ops = [op for op in catalog.ops.values() if op["surface"] == "soap"]
    assert len(ops) == len(catalog.soap_table["operations"])
    for op in ops:
        call = builder.build(op, {}, SOAP_URL)
        root = ET.fromstring(call.envelope("tok"))
        assert root.tag.endswith("Envelope"), op["id"]
