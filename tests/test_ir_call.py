"""ir_call end to end (plan §3.3, §4; M6): capability routing through the full pipeline, the route
trail in meta and previews, capability param mapping per surface, Level-2 results that match
across surfaces, includeRaw, and the tool's annotations. MockTransport / FakeAsmx only.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from mcp.client.client import Client

from imageright_mcp.client import MockReply
from imageright_mcp.server import create_server
from tests.conftest import BASE, ClientFactory
from tests.test_client_soap import ENV as SOAP_ENV
from tests.test_client_soap import FakeAsmx, _response

FIXTURES = Path(__file__).parent / "fixtures" / "normalize"
DOC_JSON = json.loads((FIXTURES / "rest-v1" / "Document.json").read_text())["body"]
DOC_XML = (FIXTURES / "soap" / "Document.xml").read_text()
DOC_ID = 9007199254740993


def soap_server() -> FakeAsmx:
    asmx = FakeAsmx()
    asmx.script("GetDocumentByRef", lambda op, t: _response(op, DOC_XML, f"{t}+"))
    return asmx


async def test_capability_routes_to_v2_and_returns_a_canonical_entity(
    make_client: ClientFactory,
) -> None:
    client, mock = make_client()
    mock.add("GET", f"/api/v2/documents/{DOC_ID}", MockReply(json=DOC_JSON))
    outcome = await client.call("document.get", {"documentId": DOC_ID})
    assert outcome.ok, outcome.error
    assert outcome.data["id"] == DOC_ID
    assert outcome.data["documentTypeName"] == "Correspondence"
    assert "raw" not in outcome.data
    meta = outcome.meta
    assert meta["operationId"] == "rest.v2.documents.getDocumentByIdV2"
    assert meta["capabilityId"] == "document.get"
    assert meta["surface"] == "rest-v2"
    assert meta["entity"] == "Document"
    assert meta["shape"] == "rest-v2:DocumentDataResult"
    assert [t["surface"] for t in meta["route"]] == ["rest-v2", "rest-v1", "soap"]
    assert meta["route"][0]["chosen"] is True
    assert meta["route"][2]["rejected"] == "surface disabled: soapUrl is not configured"
    assert meta["durationMs"] >= 0


async def test_same_entity_whichever_surface_answers(make_client: ClientFactory) -> None:
    client, mock = make_client(SOAP_ENV)
    mock.handler = soap_server()
    mock.add("GET", f"/api/v2/documents/{DOC_ID}", MockReply(json=DOC_JSON))
    mock.add("GET", f"/api/documents/{DOC_ID}", MockReply(json=DOC_JSON))
    results = {}
    for surface in ("rest-v2", "rest-v1", "soap"):
        outcome = await client.call("document.get", {"documentId": DOC_ID}, surface=surface)
        assert outcome.ok, outcome.error
        assert outcome.meta["surface"] == surface
        results[surface] = outcome.data
    assert results["rest-v2"] == results["rest-v1"] == results["soap"]


async def test_soap_capability_maps_params_to_nested_arguments(make_client: ClientFactory) -> None:
    client, mock = make_client(SOAP_ENV)
    asmx = soap_server()
    mock.handler = asmx
    outcome = await client.call(
        "document.get", {"documentId": DOC_ID}, surface="soap", include_raw=True
    )
    assert outcome.ok, outcome.error
    body = asmx.bodies[-1]
    assert f"<docRef>\n        <RefId>{DOC_ID}</RefId>\n      </docRef>" in body
    assert "<getContent>false</getContent>" in body  # capability "fixed" value
    assert outcome.data["raw"]["Id"] == {"Id": "Document", "RefId": DOC_ID}
    assert outcome.meta["shape"] == "soap:Document"


async def test_surface_preference_from_config(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_SURFACE_PREFERENCE": "rest-v1,rest-v2"})
    mock.add("GET", f"/api/documents/{DOC_ID}", MockReply(json=DOC_JSON))
    outcome = await client.call("document.get", {"documentId": DOC_ID})
    assert outcome.ok, outcome.error
    assert outcome.meta["surface"] == "rest-v1"
    assert outcome.meta["route"][0]["surface"] == "rest-v1"
    assert outcome.meta["route"][1]["rejected"] == "lower preference than rest-v1"


async def test_dry_run_preview_shows_the_route(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_WRITE_MODE": "dry-run"}, login=False)
    outcome = await client.call(
        "task.create",
        {
            "objectId": 5,
            "stepId": 62,
            "priority": 3,
            "availableDate": "2024-03-04T08:00:00",
            "pageId": 7001,
        },
    )
    assert outcome.ok, outcome.error
    preview = outcome.data["preview"]
    assert preview["operationId"] == "rest.v1.tasks.createTask"
    assert preview["capabilityId"] == "task.create"
    assert preview["route"][0] == {
        "surface": "rest-v2",
        "rejected": "no implementation of this capability",
    }
    assert preview["route"][1]["chosen"] is True
    assert preview["request"]["json"]["PageNumber"] == 7001  # the page-id gotcha, mapped
    assert outcome.meta["route"] == preview["route"]
    assert mock.requests == []


async def test_missing_and_unknown_capability_params(make_client: ClientFactory) -> None:
    client, mock = make_client(login=False)
    outcome = await client.call("document.get", {"docId": 1}, dry_run=True)
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-3006"
    codes = [(i["code"], i["param"]) for i in outcome.error["issues"]]
    assert ("IR-3006", "docId") in codes
    assert ("IR-3005", "documentId") in codes
    assert "Did you mean documentId?" in outcome.error["issues"][0]["message"]
    assert "preview" in outcome.error  # a failed validation still shows the partial preview
    assert mock.requests == []


async def test_catalog_default_fills_document_date(make_client: ClientFactory) -> None:
    client, _ = make_client({"IMAGERIGHT_WRITE_MODE": "dry-run"}, login=False)
    outcome = await client.call(
        "document.create", {"parentId": 5001, "documentTypeId": 31, "description": "Letter"}
    )
    assert outcome.ok, outcome.error
    sent = outcome.data["preview"]["request"]["json"]
    assert sent["DocumentDate"]  # today's date
    assert "documentDate was not given" in outcome.meta["notes"][0]


async def test_no_surface_available(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_SURFACE_PREFERENCE": "rest-v2"})
    outcome = await client.call("task.get", {"taskId": 1})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-3004"
    assert outcome.meta["route"][-1]["rejected"] == "not in surfacePreference"
    assert mock.requests == []


async def test_soap_page_upload_previews_a_placeholder_and_sends_base64(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    scan = tmp_path / "scan.TIF"
    scan.write_bytes(b"II*\x00" + bytes(range(256)) * 4)
    client, mock = make_client({**SOAP_ENV, "IMAGERIGHT_WRITE_MODE": "dry-run"})
    asmx = FakeAsmx()
    mock.handler = asmx
    params = {"documentId": 9, "batchId": 3, "imagePath": str(scan)}
    preview = await client.call("page.create", params, surface="soap")
    assert preview.ok, preview.error
    envelope = preview.data["preview"]["request"]["envelope"]
    assert f"[base64 of scan.TIF: {scan.stat().st_size} bytes, sha256 " in envelope
    assert "<Extension>tif</Extension>" in envelope
    assert asmx.log == []

    client.reconfigure(client.config.model_copy(update={"writeMode": "allow"}))
    outcome = await client.call("page.create", params, surface="soap")
    assert outcome.ok, outcome.error
    sent = asmx.bodies[-1]
    assert base64.b64encode(scan.read_bytes()).decode() in sent
    assert "[base64 of" not in sent


async def test_upload_outside_the_allowed_roots_is_refused(make_client: ClientFactory) -> None:
    client, mock = make_client(SOAP_ENV)
    outcome = await client.call(
        "page.create",
        {"documentId": 9, "batchId": 3, "imagePath": "/etc/hostname"},
        surface="soap",
    )
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-1007"
    assert mock.requests == []


async def test_rest_page_upload_maps_image_path_to_the_catalog_part(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    scan = tmp_path / "scan.tif"
    scan.write_bytes(b"II*\x00")
    client, _ = make_client({"IMAGERIGHT_WRITE_MODE": "dry-run"}, login=False)
    for surface, part in (("rest-v1", "image0"), ("rest-v2", "Image")):
        outcome = await client.call(
            "page.create", {"documentId": 9}, {"imagePath": str(scan)}, surface=surface
        )
        assert outcome.ok, outcome.error
        parts = outcome.data["preview"]["request"]["multipart"]
        assert parts[0]["json"] == {"DocId": 9}
        assert parts[1]["name"] == part


async def test_level_1_for_operation_ids(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("GET", "/api/documents/7", MockReply(json={**DOC_JSON, "DateCreated": "/Date(0)/"}))
    outcome = await client.call("rest.v1.documents.getDocumentById", {"docId": 7})
    assert outcome.ok, outcome.error
    assert outcome.data["DateCreated"] == "1970-01-01T00:00:00+00:00"
    assert outcome.data["Id"] == DOC_ID
    assert outcome.meta["normalization"] == "level1"
    assert outcome.meta["shape"] == "rest-v1:DocumentDataResult"
    assert "entity" not in outcome.meta


async def test_unexpected_payload_shapes_do_not_crash(make_client: ClientFactory) -> None:
    client, mock = make_client()
    mock.add("GET", f"/api/v2/documents/{DOC_ID}", MockReply(json={"Folder": "not-an-object"}))
    outcome = await client.call("document.get", {"documentId": DOC_ID})
    assert outcome.ok  # a string where an object is expected is tolerated, not a crash
    mock.add("GET", f"/api/v2/documents/{DOC_ID}", MockReply(json=[1, 2]))
    listed = await client.call("document.get", {"documentId": DOC_ID})
    assert listed.ok
    assert listed.data == [1, 2]


# ---------------------------------------------------------------------------- the tool


def _payload(result: Any) -> dict[str, Any]:
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


async def test_ir_call_tool_annotations_and_arguments() -> None:
    async with Client(create_server({})) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        annotations = tools["ir_call"].annotations
        assert annotations is not None
        assert annotations.destructive_hint is True
        assert annotations.read_only_hint is False
        both = await client.call_tool(
            "ir_call", {"operationId": "rest.v1.documents.getDocumentById", "capabilityId": "x"}
        )
        neither = await client.call_tool("ir_call", {})
        preview = await client.call_tool(
            "ir_call",
            {"capabilityId": "document.get", "params": {"documentId": 7}, "surface": "soap"},
        )
    assert _payload(both)["error"]["code"] == "IR-3006"
    assert _payload(neither)["error"]["code"] == "IR-3005"
    data = _payload(preview)
    # No endpoint configured: reads are previewed too, with a placeholder URL and a warning.
    assert data["ok"] is False
    assert data["error"]["code"] == "IR-1003"
    assert data["meta"]["route"][0]["surface"] == "soap"


async def test_ir_call_tool_dry_run_needs_no_server() -> None:
    env = {"IMAGERIGHT_REST_BASE_URL": BASE}
    async with Client(create_server(env)) as client:
        result = await client.call_tool(
            "ir_call",
            {"capabilityId": "document.get", "params": {"documentId": 7}, "dryRun": True},
        )
    payload = _payload(result)
    assert payload["ok"] is True, payload["error"]
    assert payload["data"]["preview"]["request"]["url"] == f"{BASE}/api/v2/documents/7"
    assert payload["meta"]["dryRun"] is True
