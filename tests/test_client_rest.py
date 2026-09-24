"""RestClient end to end over MockTransport: int64 round-trip, binary to file, HEAD, error
mapping, retries and envelopes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from imageright_mcp.client import MockReply, TransportFailure
from tests.conftest import BASE, ClientFactory

BIG = 2**53 + 1
BIGGER = 2**63 - 1


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


async def test_int64_ids_round_trip_unchanged(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add(
        "POST",
        "/api/folders/find",
        MockReply(json=[{"Id": BIGGER, "FileId": BIG, "ParentId": BIG + 2}]),
    )
    mock.add("GET", f"/api/documents/{BIG}", MockReply(json={"Id": BIG, "ParentId": BIGGER}))
    found = await client.call("rest.v1.folders.findFolders", {"FileId": BIG})
    doc = await client.call("rest.v1.documents.getDocumentById", {"docId": BIG})
    assert found.data == [{"Id": BIGGER, "FileId": BIG, "ParentId": BIG + 2}]
    assert doc.data == {"Id": BIG, "ParentId": BIGGER}
    sent = mock.calls("POST", "/api/folders/find")[0]
    assert sent.json == {"FileId": BIG}
    assert mock.calls("GET")[0].url == f"{BASE}/api/documents/{BIG}"
    # ...and through the envelope's JSON text too
    payload = json.loads(doc.envelope().content[0].text)  # type: ignore[union-attr]
    assert payload["data"]["Id"] == BIG
    assert str(BIG) in doc.envelope().content[0].text  # type: ignore[union-attr]


async def test_binary_response_goes_to_a_file(make_client: ClientFactory, tmp_path: Path) -> None:
    client, mock = make_client()
    blob = b"%PDF-1.7\n" + bytes(range(256)) * 50
    mock.add(
        "GET",
        "/api/reports/3/chunks/0",
        MockReply(body=blob, headers={"Content-Type": "application/pdf"}),
    )
    outcome = await client.call("rest.v1.reports.getReportChunk", {"reportid": 3, "chunkNumber": 0})
    assert outcome.ok
    ref = outcome.data
    assert set(ref) == {"path", "bytes", "contentType", "sha256"}
    assert ref["bytes"] == len(blob)
    assert ref["sha256"] == hashlib.sha256(blob).hexdigest()
    assert _read(ref["path"]) == blob
    assert Path(ref["path"]).is_relative_to(tmp_path / "out")
    text = outcome.envelope().content[0].text  # type: ignore[union-attr]
    assert "%PDF" not in text


async def test_head_reports_existence(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("HEAD", "/api/documents/1", MockReply(status=200))
    mock.add("HEAD", "/api/documents/2", MockReply(status=404))
    present = await client.call("rest.v1.documents.getDocumentHeadById", {"docId": 1})
    deleted = await client.call("rest.v1.documents.getDocumentHeadById", {"docId": 2})
    assert present.data == {"exists": True}
    assert deleted.data == {"exists": False}


async def test_native_error_codes_map_through_the_error_mapper(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add(
        "GET", "/api/documents/5", MockReply(status=400, json={"ErrorCode": 2, "Message": "nope"})
    )
    outcome = await client.call("rest.v1.documents.getDocumentById", {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["native"]["code"] == 2
    assert outcome.error["native"]["httpStatus"] == 400
    assert outcome.meta["requestId"]
    assert outcome.meta["httpStatus"] == 400
    envelope = outcome.envelope()
    assert envelope.is_error


async def test_data_not_ready_is_retried_then_reported(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("GET", "/api/accounts", MockReply(status=202), MockReply(json=[{"Id": 1}]))
    outcome = await client.call("rest.v1.accounts.getAllAccounts")
    assert outcome.ok
    assert outcome.meta["attempts"] == 2

    mock.add("GET", "/api/accounts", MockReply(status=202), sticky=True)
    stuck = await client.call("rest.v1.accounts.getAllAccounts")
    assert stuck.error is not None
    assert stuck.error["code"] == "IR-5006"
    assert stuck.meta["attempts"] == 3  # 1 + maxRetries (default 2)


async def test_network_failure_on_a_write_is_not_retried(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("POST", "/api/documents", MockReply(fail=TransportFailure("reset")))
    outcome = await client.call(
        "rest.v1.documents.createDocument",
        {"ParentId": 1, "DocumentTypeId": 2, "Description": "d", "DocumentDate": "2026-09-24"},
    )
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-5004"
    assert len(mock.calls("POST", "/api/documents")) == 1


async def test_timeout_maps_to_ir_5005(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_MAX_RETRIES": "0"})
    mock.add("GET", "/api/documents/5", MockReply(fail=TransportFailure("slow", timeout=True)))
    outcome = await client.call("rest.v1.documents.getDocumentById", {"docId": 5})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-5005"


async def test_multipart_upload_end_to_end(make_client: ClientFactory, tmp_path: Path) -> None:
    client, mock = make_client()
    scan = tmp_path / "scan.tif"
    scan.write_bytes(b"II*\x00")
    mock.add("POST", "/api/v2/pages", MockReply(status=201, json={"Id": BIG}))
    outcome = await client.call(
        "rest.v2.pages.createPageV2", {"DocId": BIG, "before": True}, {"Image": str(scan)}
    )
    assert outcome.ok, outcome.error
    assert outcome.data == {"Id": BIG}
    sent = mock.calls("POST", "/api/v2/pages")[0]
    assert [p.name for p in sent.multipart] == ["PageCreateData", "Image"]
    assert sent.query == (("before", "true"),)
    assert not sent.idempotent
