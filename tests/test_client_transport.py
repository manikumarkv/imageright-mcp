"""RestTransport over httpx2's in-process MockTransport (wire format, no network), plus the
retry policy and request builder."""

from __future__ import annotations

import hashlib
import json
import ssl
from pathlib import Path
from typing import Any

import httpx2
import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client import (
    BodySink,
    JsonPart,
    MockReply,
    MockTransport,
    PreparedRequest,
    RequestBuilder,
    RestTransport,
    RetryPolicy,
    TransportFailure,
    send_with_retries,
)
from imageright_mcp.client.builder import BuildError
from tests.conftest import BASE, no_sleep

BIG = 2**53 + 1  # not representable as a float64
MAX_INT64 = 2**63 - 1


def _op(op_id: str) -> dict[str, Any]:
    return get_catalog().ops[op_id]


def _parts(request: httpx2.Request) -> list[tuple[str, str | None, bytes]]:
    """(name, filename, payload) of each multipart part, parsed from the raw body."""
    content_type = request.headers["content-type"]
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = request.content
    parts = []
    for chunk in body.split(b"--" + boundary)[1:-1]:
        head, _, payload = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
        disposition = head.decode().split("\r\n")[0]
        name = disposition.split('name="', 1)[1].split('"', 1)[0]
        filename = (
            disposition.split('filename="', 1)[1].split('"', 1)[0]
            if "filename=" in disposition
            else None
        )
        parts.append((name, filename, payload))
    return parts


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


class Recorder:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.requests: list[httpx2.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _transport(recorder: Recorder, tmp_path: Path) -> RestTransport:
    return RestTransport(
        http_transport=httpx2.MockTransport(recorder), sink=BodySink(tmp_path / "out")
    )


def _builder(tmp_path: Path) -> RequestBuilder:
    return RequestBuilder(BASE, [str(tmp_path)])


# ---------------------------------------------------------------------------- multipart


@pytest.mark.parametrize(
    ("op_id", "files", "expected"),
    [
        ("rest.v1.pages.createPage", {"image0": "scan.tif"}, ["PageCreateData", "image0"]),
        (
            "rest.v2.pages.createPageV2",
            {"Image": "scan.tif", "Annotation": "notes.xml"},
            ["PageCreateData", "Image", "Annotation"],
        ),
        ("rest.v2.pages.createPageV2", {"Image": "scan.tif"}, ["PageCreateData", "Image"]),
        (
            "rest.v1.pages.updatePageContent",
            {"image0": "scan.tif", "image2": "c.tif", "image1": "b.tif"},
            ["PageUpdateData", "image0", "image1", "image2"],
        ),
        (
            "rest.v2.pages.updatePageContentV2",
            {"image": "scan.tif", "annotation": "notes.xml"},
            ["PageUpdateData", "image", "annotation"],
        ),
    ],
)
async def test_multipart_part_names_come_from_the_catalog(
    tmp_path: Path, op_id: str, files: dict[str, str], expected: list[str]
) -> None:
    for name in files.values():
        (tmp_path / name).write_bytes(b"II*\x00" + name.encode() * 100)
    params: dict[str, Any] = {"pageId": 7} if "update" in op_id else {"DocId": BIG, "BatchId": 5}
    if "update" in op_id:
        params["PageUpdateData"] = {"DocId": BIG}
    request = _builder(tmp_path).build(
        _op(op_id), params, {k: str(tmp_path / v) for k, v in files.items()}
    )
    recorder = Recorder(httpx2.Response(201, json={"Id": 1}))
    transport = _transport(recorder, tmp_path)
    response = await transport.send(request)
    await transport.aclose()
    assert response.status == 201
    wire = _parts(recorder.requests[0])
    assert [name for name, _, _ in wire] == expected
    settings = json.loads(wire[0][2])
    assert settings["DocId"] == BIG  # int64 above 2^53 goes out exactly
    for name, filename, payload in wire[1:]:
        assert filename == files[name]
        assert payload == (tmp_path / files[name]).read_bytes()
    expected_path = "/ImageRight" + _op(op_id)["path"].replace("{pageId}", "7")
    assert recorder.requests[0].url.path == expected_path


def test_v1_and_v2_image_part_names_differ() -> None:
    """Guard against someone hardcoding one name for both versions."""

    def names(op_id: str) -> list[str]:
        return [p["name"] for p in _op(op_id)["params"] if p["in"] == "multipart"]

    assert names("rest.v1.pages.createPage") == ["PageCreateData", "image0"]
    assert names("rest.v2.pages.createPageV2") == ["PageCreateData", "Image", "Annotation"]


def test_multipart_file_outside_allowed_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-root.tif"
    outside.write_bytes(b"x")
    builder = RequestBuilder(BASE, [str(tmp_path / "inbox")])
    (tmp_path / "inbox").mkdir()
    with pytest.raises(BuildError) as info:
        builder.build(_op("rest.v1.pages.createPage"), {"DocId": 1}, {"image0": str(outside)})
    assert info.value.error["code"] == "IR-1007"


def test_multipart_files_are_described_not_buffered(tmp_path: Path) -> None:
    (tmp_path / "scan.tif").write_bytes(b"\x00" * 5000)
    request = _builder(tmp_path).build(
        _op("rest.v1.pages.createPage"), {"DocId": 1}, {"image0": str(tmp_path / "scan.tif")}
    )
    file_part = request.multipart[1]
    assert not isinstance(file_part, JsonPart)
    assert file_part.size == 5000
    assert file_part.sha256 == hashlib.sha256(b"\x00" * 5000).hexdigest()


# ---------------------------------------------------------------------------- bodies and verbs


async def test_json_body_int64_round_trip(tmp_path: Path) -> None:
    request = _builder(tmp_path).build(
        _op("rest.v1.folders.findFolders"), {"FileId": BIG, "ParentId": MAX_INT64}, {}
    )
    recorder = Recorder(httpx2.Response(200, json=[{"Id": BIG, "ParentId": MAX_INT64}]))
    transport = _transport(recorder, tmp_path)
    response = await transport.send(request)
    sent = recorder.requests[0]
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content) == {"FileId": BIG, "ParentId": MAX_INT64}
    assert str(BIG).encode() in sent.content
    assert json.loads(response.content) == [{"Id": BIG, "ParentId": MAX_INT64}]


async def test_text_plain_body(tmp_path: Path) -> None:
    request = _builder(tmp_path).build(
        _op("rest.v1.authentication.validTo"), {"body": "some-token"}, {}
    )
    recorder = Recorder(httpx2.Response(200, text='"2030-01-01T00:00:00Z"'))
    await _transport(recorder, tmp_path).send(request)
    sent = recorder.requests[0]
    assert sent.headers["content-type"].startswith("text/plain")
    assert sent.content == b"some-token"


async def test_delete_with_body_and_head(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    delete = builder.build(_op("rest.v1.ocrfolders.removeOcrFolder"), {"folderId": 42}, {})
    head = builder.build(_op("rest.v1.documents.getDocumentHeadById"), {"docId": BIG}, {})
    recorder = Recorder(httpx2.Response(200), httpx2.Response(404))
    transport = _transport(recorder, tmp_path)
    await transport.send(delete)
    response = await transport.send(head)
    assert recorder.requests[0].method == "DELETE"
    assert recorder.requests[0].content == b"42"
    assert recorder.requests[1].method == "HEAD"
    assert recorder.requests[1].url.path == f"/ImageRight/api/documents/{BIG}"
    assert response.status == 404


async def test_query_params_and_v2_paths_are_verbatim(tmp_path: Path) -> None:
    request = _builder(tmp_path).build(
        _op("rest.v2.documents.deleteDocument"), {"documentId": 9, "force": True}, {}
    )
    assert request.full_url == f"{BASE}/api/v2/documents/9?force=true"


async def test_request_id_header_and_ca_bundle(tmp_path: Path) -> None:
    request = _builder(tmp_path).build(_op("rest.v1.accounts.getAllAccounts"), {}, {})
    recorder = Recorder(httpx2.Response(200, json=[]))
    transport = RestTransport(
        http_transport=httpx2.MockTransport(recorder), request_id_header="X-Trace"
    )
    await transport.send(request)
    assert recorder.requests[0].headers["X-Trace"] == request.request_id
    # A custom CA bundle path reaches the TLS layer (it must exist to be loaded).
    with pytest.raises((FileNotFoundError, ssl.SSLError, OSError)):
        RestTransport(verify=str(tmp_path / "missing-ca.pem"))


# ---------------------------------------------------------------------------- binary responses


async def test_binary_response_is_written_to_file(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 400
    request = _builder(tmp_path).build(
        _op("rest.v1.reports.getReportChunk"), {"reportid": 1, "chunkNumber": 0}, {}
    )
    recorder = Recorder(
        httpx2.Response(200, content=payload, headers={"Content-Type": "application/pdf"})
    )
    response = await _transport(recorder, tmp_path).send(request)
    assert response.content == b""
    assert response.file is not None
    ref = response.file.to_dict()
    assert ref["bytes"] == len(payload)
    assert ref["contentType"] == "application/pdf"
    assert ref["sha256"] == hashlib.sha256(payload).hexdigest()
    assert _read(ref["path"]) == payload
    assert Path(ref["path"]).parent == tmp_path / "out"


async def test_octet_stream_scalars_stay_in_memory(tmp_path: Path) -> None:
    """authenticate is typed octet-stream but returns a token string, not a file."""
    request = _builder(tmp_path).build(
        _op("rest.v1.authentication.authenticate"), {"UserName": "u", "Password": "p"}, {}
    )
    recorder = Recorder(
        httpx2.Response(200, content=b'"abc"', headers={"Content-Type": "application/octet-stream"})
    )
    response = await _transport(recorder, tmp_path).send(request)
    assert response.file is None
    assert response.content == b'"abc"'


# ---------------------------------------------------------------------------- failures and retries


async def test_network_errors_become_transport_failures(tmp_path: Path) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("read timed out", request=request)

    request = _builder(tmp_path).build(_op("rest.v1.accounts.getAllAccounts"), {}, {})
    with pytest.raises(TransportFailure) as info:
        await RestTransport(http_transport=httpx2.MockTransport(boom)).send(request)
    assert not info.value.timeout
    with pytest.raises(TransportFailure) as info:
        await RestTransport(http_transport=httpx2.MockTransport(slow)).send(request)
    assert info.value.timeout


def _get(**kwargs: Any) -> PreparedRequest:
    return PreparedRequest(
        "GET", f"{BASE}/api/accounts", "rest.v1.accounts.getAllAccounts", idempotent=True, **kwargs
    )


def _post() -> PreparedRequest:
    return PreparedRequest("POST", f"{BASE}/api/documents", "rest.v1.documents.createDocument")


@pytest.mark.parametrize(
    "first",
    [
        MockReply(status=503),
        MockReply(status=202, json={"ErrorCode": 15, "Message": "DataNotReady"}),
        MockReply(fail=TransportFailure("reset")),
    ],
)
async def test_idempotent_requests_retry(first: MockReply) -> None:
    mock = MockTransport()
    mock.add("GET", "/api/accounts", first, MockReply(json=[{"Id": 1}]))
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    response = await send_with_retries(mock, _get(), RetryPolicy(max_retries=2), sleep)
    assert response.status == 200
    assert response.attempts == 2
    assert delays == [0.5]


async def test_retries_are_bounded() -> None:
    mock = MockTransport()
    mock.add("GET", "/api/accounts", MockReply(status=503), sticky=True)
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    response = await send_with_retries(mock, _get(), RetryPolicy(max_retries=3), sleep)
    assert response.status == 503
    assert len(mock.requests) == 4
    assert delays == [0.5, 1.0, 2.0]


@pytest.mark.parametrize(
    "reply", [MockReply(status=503), MockReply(fail=TransportFailure("reset"))]
)
async def test_writes_are_never_retried(reply: MockReply) -> None:
    mock = MockTransport()
    mock.add("POST", "/api/documents", reply, MockReply(status=201, json=1))
    try:
        response = await send_with_retries(mock, _post(), RetryPolicy(max_retries=3), no_sleep)
    except TransportFailure:
        pass
    else:
        assert response.status == 503
    assert len(mock.requests) == 1


async def test_plain_202_on_a_read_is_not_retried() -> None:
    mock = MockTransport()
    mock.add("GET", "/api/accounts", MockReply(status=202, json=[]), MockReply(json=[1]))
    response = await send_with_retries(mock, _get(), RetryPolicy(), no_sleep)
    assert response.status == 202
    assert len(mock.requests) == 1
