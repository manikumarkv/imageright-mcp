"""Write policy and dry-run gate (plan §7): writeMode, per-call dryRun, confirm/previewId, and
what a preview contains. Previews never touch the transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.client import MockReply
from tests.conftest import BASE, ClientFactory

READ = ("rest.v1.documents.getDocumentById", {"docId": 5})
WRITE = (
    "rest.v1.documents.createDocument",
    {"ParentId": 1, "DocumentTypeId": 2, "Description": "d", "DocumentDate": "2026-09-24"},
)
DESTRUCTIVE = ("rest.v2.documents.deleteDocument", {"documentId": 9})


def _script(mock: Any) -> None:
    mock.add("GET", "/api/documents/5", MockReply(json={"Id": 5}), sticky=True)
    mock.add("POST", "/api/documents", MockReply(status=201, json=77), sticky=True)
    mock.add("DELETE", "/api/v2/documents/9", MockReply(status=200), sticky=True)


def _sent(mock: Any) -> list[str]:
    return [r.operation_id for r in mock.requests if "authentication" not in r.operation_id]


@pytest.mark.parametrize(
    ("mode", "call", "dry_run", "expected"),
    [
        # reads run unless a preview is asked for
        ("deny", READ, None, "execute"),
        ("dry-run", READ, None, "execute"),
        ("allow", READ, None, "execute"),
        ("allow", READ, True, "preview"),
        # writes: deny blocks, dry-run previews, allow runs
        ("deny", WRITE, None, "IR-3008"),
        ("deny", WRITE, True, "IR-3008"),
        ("dry-run", WRITE, None, "preview"),
        ("dry-run", WRITE, False, "preview"),  # dryRun=false is ignored outside allow
        ("allow", WRITE, None, "execute"),
        ("allow", WRITE, False, "execute"),
        ("allow", WRITE, True, "preview"),
        # destructive: blocked under deny, previewed until confirmed under allow
        ("deny", DESTRUCTIVE, None, "IR-3008"),
        ("dry-run", DESTRUCTIVE, None, "preview"),
        ("allow", DESTRUCTIVE, None, "preview"),
    ],
)
async def test_write_mode_matrix(
    make_client: ClientFactory,
    mode: str,
    call: tuple[str, dict[str, Any]],
    dry_run: bool | None,
    expected: str,
) -> None:
    client, mock = make_client({"IMAGERIGHT_WRITE_MODE": mode})
    _script(mock)
    outcome = await client.call(call[0], call[1], dry_run=dry_run)
    if expected == "execute":
        assert outcome.ok, outcome.error
        assert outcome.meta["dryRun"] is False
        assert _sent(mock) == [call[0]]
    elif expected == "preview":
        assert outcome.ok, outcome.error
        assert outcome.meta["dryRun"] is True
        assert "preview" in outcome.data
        assert not mock.requests
    else:
        assert outcome.error is not None
        assert outcome.error["code"] == expected
        assert "preview" in outcome.error
        assert not mock.requests


async def test_default_write_mode_is_dry_run(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_WRITE_MODE": ""})
    assert client.config.writeMode == "dry-run"
    outcome = await client.call(*WRITE)
    assert outcome.meta["dryRun"] is True
    assert not mock.requests


async def test_ignored_dry_run_false_is_warned(make_client: ClientFactory) -> None:
    client, _ = make_client({"IMAGERIGHT_WRITE_MODE": "dry-run"})
    outcome = await client.call(*WRITE, dry_run=False)
    assert any("ignored" in w["message"] for w in outcome.meta["warnings"])


async def test_global_dry_run_previews_reads_too(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_DRY_RUN": "true"})
    outcome = await client.call(*READ)
    assert outcome.meta["dryRun"] is True
    assert not mock.requests
    # ... unless the call opts out under writeMode allow
    _script(mock)
    live = await client.call(*READ, dry_run=False)
    assert live.meta["dryRun"] is False


async def test_destructive_confirm_flow(make_client: ClientFactory) -> None:
    client, mock = make_client()
    _script(mock)
    first = await client.call(*DESTRUCTIVE)
    assert first.meta["confirmRequired"] is True
    pid = first.data["preview"]["previewId"]
    assert pid.startswith("pv_")
    assert not mock.requests
    confirmed = await client.call(*DESTRUCTIVE, confirm=pid)
    assert confirmed.ok, confirmed.error
    assert _sent(mock) == [DESTRUCTIVE[0]]


async def test_confirm_for_a_different_request_is_rejected(make_client: ClientFactory) -> None:
    client, mock = make_client()
    _script(mock)
    first = await client.call(*DESTRUCTIVE)
    pid = first.data["preview"]["previewId"]
    changed = await client.call(DESTRUCTIVE[0], {"documentId": 9, "force": True}, confirm=pid)
    assert changed.error is not None
    assert changed.error["code"] == "IR-3009"
    assert changed.error["preview"]["previewId"] != pid
    assert not mock.requests


async def test_forged_preview_id_is_rejected(make_client: ClientFactory) -> None:
    """A previewId must come from a dry-run in this process, not be computed elsewhere."""
    client, mock = make_client()
    other, _ = make_client()
    pid = (await other.call(*DESTRUCTIVE)).data["preview"]["previewId"]
    outcome = await client.call(*DESTRUCTIVE, confirm=pid)
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-3009"
    assert not mock.requests


async def test_confirm_can_be_disabled(make_client: ClientFactory) -> None:
    client, mock = make_client({"IMAGERIGHT_REQUIRE_CONFIRM": "false"})
    _script(mock)
    assert (await client.call(*DESTRUCTIVE)).ok
    assert _sent(mock) == [DESTRUCTIVE[0]]


async def test_preview_id_is_stable_and_sensitive_to_file_content(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    client, _ = make_client()
    scan = tmp_path / "scan.tif"
    scan.write_bytes(b"one")
    call = ("rest.v1.pages.createPage", {"DocId": 1, "BatchId": 2})
    files = {"image0": str(scan)}
    a = (await client.call(*call, files, dry_run=True)).data["preview"]["previewId"]
    b = (await client.call(*call, files, dry_run=True)).data["preview"]["previewId"]
    scan.write_bytes(b"two")
    c = (await client.call(*call, files, dry_run=True)).data["preview"]["previewId"]
    assert a == b != c


async def test_preview_contents(make_client: ClientFactory, tmp_path: Path) -> None:
    client, mock = make_client({"IMAGERIGHT_WRITE_MODE": "dry-run"}, login=False)
    scan = tmp_path / "scan.tif"
    scan.write_bytes(b"II*\x00" * 10)
    outcome = await client.call(
        "rest.v1.pages.createPage",
        {"DocId": 123, "BatchId": 456, "Description": "p1"},
        {"image0": str(scan)},
    )
    assert outcome.ok
    preview = outcome.data["preview"]
    assert preview["operationId"] == "rest.v1.pages.createPage"
    assert preview["capabilityId"] == "page.create"
    assert preview["safety"] == "write"
    assert preview["route"] == [
        {"surface": "rest-v1", "chosen": True, "reason": "explicit operationId"}
    ]
    request = preview["request"]
    assert request["method"] == "POST"
    assert request["url"] == f"{BASE}/api/pages"
    assert request["headers"]["Authorization"] == "AccessToken ***"
    assert request["headers"]["Content-Type"].startswith("multipart/form-data")
    assert request["multipart"][0] == {
        "name": "PageCreateData",
        "contentType": "application/json",
        "json": {"DocId": 123, "BatchId": 456, "Description": "p1"},
    }
    assert request["multipart"][1]["name"] == "image0"
    assert request["multipart"][1]["bytes"] == 40
    assert len(request["multipart"][1]["sha256"]) == 64
    assert preview["curl"].startswith(f"curl -X POST {BASE}/api/pages")
    assert "-F 'image0=@" in preview["curl"]
    assert "AccessToken ***" in preview["curl"]
    assert preview["validation"] == {"ok": True, "issues": []}
    assert any("real batch id" in p for p in preview["prerequisites"])
    assert "IR-2004" in preview["possibleErrors"]
    assert len(preview["possibleErrors"]) >= 3
    assert outcome.meta["dryRun"] is True
    assert not mock.requests


async def test_dry_run_works_without_base_url_or_credentials(make_client: ClientFactory) -> None:
    client, mock = make_client(
        {"IMAGERIGHT_REST_BASE_URL": "", "IMAGERIGHT_PASSWORD": "", "IMAGERIGHT_USERNAME": ""},
        login=False,
    )
    outcome = await client.call(*READ, dry_run=True)
    assert outcome.ok
    assert outcome.data["preview"]["request"]["url"].startswith("https://{restBaseUrl}/api/")
    assert any(w["code"] == "IR-1003" for w in outcome.meta["warnings"])
    live = await client.call(*READ)
    assert live.error is not None
    assert live.error["code"] == "IR-1003"
    assert not mock.requests


async def test_preview_envelope_shape(make_client: ClientFactory) -> None:
    client, _ = make_client()
    envelope = (await client.call(*READ, dry_run=True)).envelope()
    payload = envelope.structured_content
    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert payload["meta"]["dryRun"] is True
    assert payload["meta"]["profile"] == "24.2"
    assert payload["data"]["preview"]["previewId"].startswith("pv_")


async def test_deprecated_operation_warns(make_client: ClientFactory) -> None:
    client, _ = make_client()
    outcome = await client.call("rest.v1.files.updateFileProperties", {"fileId": 1}, dry_run=True)
    assert any(w["code"] == "IR-3003" for w in outcome.meta["warnings"])
