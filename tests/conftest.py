"""Shared fixtures for the M4 client tests. Nothing here touches the network."""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from imageright_mcp.client import BodySink, MockReply, MockTransport, PreparedRequest, RestClient
from imageright_mcp.config import EffectiveConfig, load_config
from imageright_mcp.runtime import Runtime

BASE = "https://ir.example.test/ImageRight"
USER = "svc-imageright"
PASSWORD = "Pa55-w0rd-SECRET-value"
TOKEN = "tok-AAAA-first-access-token"
TOKEN2 = "tok-BBBB-second-access-token"
FAR_FUTURE = "2099-01-01T00:00:00Z"


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens an IP connection; local pipes and UNIX sockets are fine."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"test tried to reach the network: {address!r}")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


ClientFactory = Callable[..., tuple[RestClient, MockTransport]]


@pytest.fixture
def make_client(tmp_path: Path) -> ClientFactory:
    """Build a RestClient over a MockTransport. ``env`` entries override the defaults."""

    def factory(
        env: dict[str, str] | None = None,
        *,
        clock: FakeClock | None = None,
        login: bool = True,
        **kwargs: object,
    ) -> tuple[RestClient, MockTransport]:
        full_env = {
            "IMAGERIGHT_REST_BASE_URL": BASE,
            "IMAGERIGHT_USERNAME": USER,
            "IMAGERIGHT_PASSWORD": PASSWORD,
            "IMAGERIGHT_WRITE_MODE": "allow",
            "IMAGERIGHT_FILE_ROOTS": str(tmp_path),
            "IMAGERIGHT_OUTPUT_DIR": str(tmp_path / "out"),
            **(env or {}),
        }
        config = load_config(full_env)
        mock = MockTransport(sink=BodySink(tmp_path / "out"))
        if login and config.authMode == "password":
            mock.add("POST", "/api/authenticate", MockReply(json=TOKEN), sticky=True)
            mock.add("POST", "/api/validto", MockReply(json=FAR_FUTURE), sticky=True)
        client = RestClient(
            config,
            transport=mock,
            clock=clock or FakeClock(),
            sleep=no_sleep,
            **kwargs,  # type: ignore[arg-type]
        )
        return client, mock

    return factory


def mock_runtime(
    env: dict[str, str], mock: MockTransport, *, clock: FakeClock | None = None
) -> Runtime:
    """A Runtime whose clients all talk to ``mock`` (the tools' view of ``make_client``)."""

    def factory(config: EffectiveConfig, previous: RestClient | None) -> RestClient:
        return RestClient(
            config,
            transport=mock,
            clock=clock or FakeClock(),
            sleep=no_sleep,
            redactor=previous.redactor if previous else None,
        )

    return Runtime(env, client_factory=factory)


def script_rest_login(mock: MockTransport) -> None:
    mock.add("POST", "/api/authenticate", MockReply(json=TOKEN), sticky=True)
    mock.add("POST", "/api/validto", MockReply(json=FAR_FUTURE), sticky=True)


# ---------------------------------------------------------------- fake ImageRight (composites)


class FakeImageRight:
    """A small in-memory ImageRight REST v1 server for the composite tools; plug ``handler`` into
    a ``MockTransport``. Holds drawers, types, files, folders, documents and workflows, applies
    creates, and records every write in ``writes`` as ``(operationId, body)``."""

    def __init__(self) -> None:
        self.next_id = 9000
        self.drawers: list[dict[str, Any]] = [{"Id": 7, "Name": "CLM"}, {"Id": 8, "Name": "POL"}]
        self.file_types: list[dict[str, Any]] = [
            {"Id": 70, "Name": "CLM", "Description": "Claim file"},
            {"Id": 80, "Name": "POL", "Description": "Policy file"},
        ]
        self.document_types: list[dict[str, Any]] = [
            {"Id": 501, "Name": "INV", "Description": "Invoice", "AutomationId": "doc.inv"},
            {"Id": 502, "Name": "LTR", "Description": "Letter"},
            {"Id": 503, "Name": "PHOTO", "Description": "Photo"},
        ]
        # Allowed types per container id: folder types plus document types (as the API mixes them).
        self.folder_types: list[dict[str, Any]] = [
            {"Id": 601, "Name": "Correspondence", "ClassId": 3},
            {"Id": 602, "Name": "Claims", "ClassId": 3},
        ]
        self.files: list[dict[str, Any]] = [
            self.file(101, "F-1", 7),
            self.file(102, "F-2", 7),
            self.file(103, "DUP", 7),
            self.file(104, "DUP", 8),
            self.file(105, "P-9", 8),
        ]
        self.folders: list[dict[str, Any]] = [
            {"Id": 201, "FileId": 101, "Description": "Correspondence", "FolderTypeId": 601},
            {"Id": 202, "FileId": 102, "Description": "Claims", "FolderTypeId": 602},
            {"Id": 203, "FileId": 102, "Description": "Claims Archive", "FolderTypeId": 602},
            {"Id": 204, "FileId": 105, "Description": "Twin", "FolderTypeId": 601},
            {"Id": 205, "FileId": 105, "Description": "Twin", "FolderTypeId": 601},
        ]
        self.documents: list[dict[str, Any]] = [
            self.document(301, 201, 101, 501, "Invoice March"),
            self.document(302, 201, 101, 501, "Invoice April"),
            self.document(303, 201, 101, 502, "Letter to client"),
            self.document(304, 201, 101, 503, "Old photo", deleted=True),
        ]
        self.workflows: list[dict[str, Any]] = [
            {"Id": 11, "Name": "Claims Intake"},
            {"Id": 12, "Name": "Underwriting"},
        ]
        self.steps: dict[int, list[dict[str, Any]]] = {
            11: [{"Id": 21, "Name": "Review"}, {"Id": 22, "Name": "Approve"}]
        }
        self.users: dict[int, list[dict[str, Any]]] = {21: [{"Id": 31, "Name": "alice"}]}
        self.priorities: dict[int, list[int]] = {21: [1, 3, 5], 22: [5]}
        self.writes: list[tuple[str, Any]] = []
        self.fail_page: int | None = None
        self.pages_created = 0
        self.move_failures: list[int] = []

    @staticmethod
    def file(file_id: int, number: str, drawer_id: int) -> dict[str, Any]:
        return {
            "Id": file_id,
            "FileNumberPart1": number,
            "DrawerId": drawer_id,
            "DrawerName": "CLM" if drawer_id == 7 else "POL",
            "Description": f"File {number}",
            "IsTemporary": False,
        }

    @staticmethod
    def document(
        doc_id: int, folder_id: int, file_id: int, type_id: int, text: str, *, deleted: bool = False
    ) -> dict[str, Any]:
        return {
            "Id": doc_id,
            "ParentId": folder_id,
            "FileId": file_id,
            "DocumentTypeId": type_id,
            "Description": text,
            "Deleted": deleted,
        }

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def ops(self) -> list[str]:
        return [op for op, _ in self.writes]

    def handler(self, request: PreparedRequest) -> MockReply:
        path = httpx2.URL(request.url).path.split("/ImageRight", 1)[-1]
        body = request.json if isinstance(request.json, dict) else {}
        parts = path.strip("/").split("/")
        method = request.method
        if method == "GET":
            return self._get(parts)
        if path == "/api/files/find":
            number = str(body.get("FileNumberPart1", ""))
            hits = [f for f in self.files if f["FileNumberPart1"].lower() == number.lower()]
            if "ParentId" in body:
                hits = [f for f in hits if f["DrawerId"] == body["ParentId"]]
            return MockReply(json=hits)
        if path == "/api/folders/find":
            # Substring match, as a lenient server might do; the composite re-checks equality.
            text = str(body.get("Description", "")).lower()
            hits = [
                f
                for f in self.folders
                if f["FileId"] == body.get("FileId") and text in f["Description"].lower()
            ]
            return MockReply(json=hits)
        if path == "/api/documents/find":
            hits = [
                d
                for d in self.documents
                if d["FileId"] == body.get("FileId")
                and ("ParentId" not in body or d["ParentId"] == body["ParentId"])
                and (
                    "DocumentTypeIds" not in body or d["DocumentTypeId"] in body["DocumentTypeIds"]
                )
                and (body.get("Deleted") is None or d["Deleted"] == body["Deleted"])
            ]
            return MockReply(json=hits)
        self.writes.append((request.operation_id, request.json))
        return self._write(request, body)

    def _get(self, parts: list[str]) -> MockReply:
        if parts[1:] == ["drawers"]:
            return MockReply(json=self.drawers)
        if parts[1] == "objecttypes":
            return MockReply(json=self.file_types if parts[2] == "File" else self.document_types)
        if parts[1] == "containers":
            return MockReply(json=self.folder_types + self.document_types)
        if parts[1:] == ["workflows"]:
            return MockReply(json=self.workflows)
        if parts[1] == "workflows":
            return MockReply(json=self.steps.get(int(parts[2]), []))
        if parts[1] == "steps" and parts[3] == "users":
            return MockReply(json=self.users.get(int(parts[2]), []))
        if parts[1] == "steps" and parts[3] == "priorities":
            return MockReply(json=self.priorities.get(int(parts[2]), []))
        raise AssertionError(f"FakeImageRight: no GET {'/'.join(parts)}")

    def _write(self, request: PreparedRequest, body: dict[str, Any]) -> MockReply:
        op = request.operation_id
        if op == "rest.v1.files.createFile":
            new = self.file(self.new_id(), body.get("FileNumberPart1", "GEN-1"), body["ParentId"])
            self.files.append(new)
            return MockReply(status=201, json=new["Id"])
        if op == "rest.v1.folders.createFolder":
            new_id = self.new_id()
            self.folders.append(
                {"Id": new_id, "FileId": body["ParentId"], "Description": body["Description"]}
            )
            return MockReply(status=201, json=new_id)
        if op in {"rest.v1.documents.createDocument", "rest.v1.batches.createBatch"}:
            return MockReply(status=201, json=self.new_id())
        if op == "rest.v1.pages.createPage":
            self.pages_created += 1
            if self.pages_created == self.fail_page:
                return MockReply(status=400, json={"ErrorCode": 1, "Message": "bad image"})
            return MockReply(
                status=201, json={"Id": self.new_id(), "Pagenumber": self.pages_created}
            )
        if op == "rest.v1.tasks.createTask":
            # The created task echoes a Code of its own; a 200 body is never an error model.
            new_id = self.new_id()
            return MockReply(json={"Id": new_id, "Code": f"T-{new_id}", **body})
        if op == "rest.v1.documents.moveDocument":
            return MockReply(json={"FailedDocumentMoves": self.move_failures, "DocumentIdMap": {}})
        if op == "rest.v2.documents.copyDocumentV2":
            mapping = {str(i): i + 1000 for i in body["DocumentIds"]}
            return MockReply(json={"DocumentIdMap": mapping, "FailedObjects": {}})
        if op in {"rest.v1.files.mergeFiles", "rest.v1.files.updateFileProperties"}:
            return MockReply(status=200, body=b"")
        raise AssertionError(f"FakeImageRight: no write {op}")


def fake_ir_mock(fake: FakeImageRight) -> MockTransport:
    mock = MockTransport(handler=fake.handler)
    script_rest_login(mock)
    return mock
