"""Phase-2 composite tools (flows F1, F9-F16) against the in-memory FakeImageRight."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from mcp.client.client import Client

from imageright_mcp.client import FilePart, JsonPart, MockReply, PreparedRequest
from imageright_mcp.server import create_server
from tests.conftest import (
    BASE,
    PASSWORD,
    USER,
    FakeImageRight,
    fake_ir_mock,
    mock_runtime,
)

WRITE_OPS = {
    "task": "rest.v1.tasks.createTask",
    "file": "rest.v1.files.createFile",
    "update": "rest.v1.files.updateFileProperties",
    "merge": "rest.v1.files.mergeFiles",
    "move": "rest.v1.documents.moveDocument",
    "copy": "rest.v2.documents.copyDocumentV2",
    "folder": "rest.v1.folders.createFolder",
    "document": "rest.v1.documents.createDocument",
    "batch": "rest.v1.batches.createBatch",
    "page": "rest.v1.pages.createPage",
}


def camel_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [camel_keys(item) for item in value]
    if isinstance(value, dict):
        return {k[:1].lower() + k[1:]: camel_keys(v) for k, v in value.items()}
    return value


class Harness:
    def __init__(
        self, tmp_path: Path, write_mode: str = "allow", *, camel: bool = False, **env: str
    ) -> None:
        self.fake = FakeImageRight()
        self.mock = fake_ir_mock(self.fake)
        if camel:
            # A server configured for camelCase JSON: same data, lower-case first letters.
            self.mock.handler = lambda request: camel_reply(self.fake.handler(request))
        self.env = {
            "IMAGERIGHT_REST_BASE_URL": BASE,
            "IMAGERIGHT_USERNAME": USER,
            "IMAGERIGHT_PASSWORD": PASSWORD,
            "IMAGERIGHT_VERSION": "24.x",
            "IMAGERIGHT_WRITE_MODE": write_mode,
            "IMAGERIGHT_FILE_ROOTS": str(tmp_path),
            **env,
        }
        self.server = create_server(runtime=mock_runtime(self.env, self.mock))

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        async with Client(self.server) as client:
            result = await client.call_tool(name, args)
        payload = result.structured_content
        assert isinstance(payload, dict)
        assert result.is_error is (not payload["ok"])
        return payload

    def sent(self, method: str, path: str) -> list[Any]:
        return [r.json for r in self.mock.calls(method, path)]


def camel_reply(reply: MockReply) -> MockReply:
    return replace(reply, json=camel_keys(reply.json))


@pytest.fixture(params=["PascalCase", "camelCase"])
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Harness:
    return Harness(tmp_path, camel=request.param == "camelCase")


@pytest.fixture
def preview(tmp_path: Path) -> Harness:
    return Harness(tmp_path, write_mode="dry-run")


def needs(payload: dict[str, Any], input_name: str) -> dict[str, Any]:
    assert payload["ok"] is True, payload["error"]
    assert payload["data"]["status"] == "needs-input", payload["data"]
    question: dict[str, Any] = payload["data"]["needsInput"]
    assert question["input"] == input_name
    assert question["question"]
    return question


def error(payload: dict[str, Any], code: str) -> dict[str, Any]:
    assert payload["ok"] is False, payload["data"]
    err: dict[str, Any] = payload["error"]
    assert err["code"] == code, err
    assert err["flowId"] == payload["meta"]["flowId"]
    return err


def done(payload: dict[str, Any], status: str = "done") -> dict[str, Any]:
    assert payload["ok"] is True, payload["error"]
    assert payload["data"]["status"] == status, payload["data"]
    outputs: dict[str, Any] = payload["data"]["outputs"]
    return outputs


def statuses(payload: dict[str, Any]) -> list[tuple[int, str, str]]:
    steps = (payload["data"] or payload["error"])["steps"]
    return [(s["step"], s["op"].rsplit(".", 1)[-1], s["status"]) for s in steps]


def make_pdf(path: Path, pages: int) -> Path:
    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    for number in range(1, pages + 1):
        pdf.new_page().insert_text((72, 72), f"page {number}")
    pdf.save(path)  # type: ignore[no-untyped-call]
    pdf.close()  # type: ignore[no-untyped-call]
    return path


# ------------------------------------------------------------------------------ F1


TASK = {"workflowName": "Claims Intake", "stepName": "Review", "fileNumber": "F-1"}


async def test_create_task_on_a_document(h: Harness) -> None:
    payload = await h.call(
        "ir_create_task",
        **TASK,
        assigneeUsername="Alice",
        targetDocumentType="INV",
        folderName="Correspondence",
        identifier="invoice april",
        priority=3,
        description="check it",
        availableDate="2026-10-01T09:00:00+00:00",
    )
    outputs = done(payload)
    assert outputs["stepId"] == 21
    assert isinstance(outputs["taskId"], int)
    [body] = h.sent("POST", "/api/tasks")
    assert body == {
        "ObjectId": 302,
        "StepId": 21,
        "Priority": 3,
        "AvailableDate": "2026-10-01T09:00:00+00:00",
        "UserId": 31,
        "Description": "check it",
    }
    assert [s[0] for s in statuses(payload)] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    [steps_call] = h.mock.calls("GET", "/api/workflows/11/steps")
    assert ("flag", "Production") in steps_call.query


async def test_create_task_on_the_file_with_defaults(h: Harness) -> None:
    done(await h.call("ir_create_task", **TASK))
    [body] = h.sent("POST", "/api/tasks")
    assert body["ObjectId"] == 101
    assert body["Priority"] == 5
    assert "UserId" not in body
    assert "Description" not in body
    assert body["AvailableDate"].endswith("+00:00")
    # folderName is ignored without targetDocumentType
    assert not h.mock.calls("POST", "/api/folders/find")


@pytest.mark.parametrize(
    ("args", "input_name", "option"),
    [
        ({"workflowName": "Nope"}, "workflowName", "Underwriting"),
        ({"stepName": "Nope"}, "stepName", "Approve"),
        ({"assigneeUsername": "bob"}, "assigneeUsername", "alice"),
        ({"priority": 2}, "priority", 3),
    ],
)
async def test_create_task_asks_for_unresolvable_names(
    h: Harness, args: dict[str, Any], input_name: str, option: Any
) -> None:
    question = needs(await h.call("ir_create_task", **{**TASK, **args}), input_name)
    assert option in question["options"]
    assert not h.fake.writes


async def test_create_task_asks_for_unknown_or_ambiguous_file(h: Harness) -> None:
    needs(await h.call("ir_create_task", **{**TASK, "fileNumber": "NOPE"}), "fileNumber")
    question = needs(await h.call("ir_create_task", **{**TASK, "fileNumber": "DUP"}), "fileNumber")
    assert {o["Id"] for o in question["options"]} == {103, 104}
    assert not h.fake.writes


async def test_create_task_document_target_branches(h: Harness) -> None:
    doc = {**TASK, "targetDocumentType": "INV"}
    needs(await h.call("ir_create_task", **doc), "folderName")
    assert not h.mock.calls("POST", "/api/folders/find")
    err = error(await h.call("ir_create_task", **doc, folderName="Nowhere"), "IR-4001")
    assert err["message"] == "File F-1 has no folder Nowhere."
    assert err["failedStep"] == 6
    question = needs(
        await h.call("ir_create_task", **doc, folderName="Correspondence"), "identifier"
    )
    assert {o["Id"] for o in question["options"]} == {301, 302}
    needs(
        await h.call("ir_create_task", **doc, folderName="Correspondence", identifier="x"),
        "identifier",
    )
    question = needs(
        await h.call(
            "ir_create_task", **{**doc, "targetDocumentType": "Nope"}, folderName="Correspondence"
        ),
        "targetDocumentType",
    )
    assert "Claims" in question["options"]
    assert not h.fake.writes


async def test_create_task_ambiguous_folder_is_an_error(h: Harness) -> None:
    payload = await h.call(
        "ir_create_task",
        **{**TASK, "fileNumber": "P-9"},
        targetDocumentType="INV",
        folderName="Twin",
    )
    err = error(payload, "IR-4001")
    assert err["message"] == "Folder Twin matches more than one folder in file P-9."


async def test_create_task_dry_run_previews_the_write(preview: Harness) -> None:
    payload = await preview.call("ir_create_task", **TASK)
    outputs = done(payload, "preview")
    assert outputs["taskId"] == "$step9.Id"
    assert payload["meta"]["dryRun"] is True
    last = payload["data"]["steps"][-1]
    assert last["status"] == "preview"
    assert last["preview"]["request"]["json"]["ObjectId"] == 101
    assert not preview.fake.writes


# ------------------------------------------------------------------------------ F9


async def test_search_files_needs_a_number_or_pattern(h: Harness) -> None:
    needs(await h.call("ir_search_files"), "fileNumber")
    assert not h.mock.requests


async def test_search_files_with_drawer_and_filters(h: Harness) -> None:
    outputs = done(await h.call("ir_search_files", fileNumber="DUP", drawerCode="pol"))
    assert [f["Id"] for f in outputs["files"]] == [104]
    assert h.sent("POST", "/api/files/find") == [
        {"FileNumberPart1": "DUP", "ParentId": 8, "IsTemporary": False, "IsDeleted": False}
    ]


async def test_search_files_null_filters_are_left_out(h: Harness) -> None:
    outputs = done(
        await h.call("ir_search_files", filePattern="NONE%", isTemp=None, isDeleted=None)
    )
    assert outputs["files"] == []
    assert h.sent("POST", "/api/files/find") == [{"FileNumberPart1": "NONE%"}]


async def test_search_files_asks_for_unknown_drawer(h: Harness) -> None:
    question = needs(
        await h.call("ir_search_files", fileNumber="F-1", drawerCode="X"), "drawerCode"
    )
    assert question["options"] == ["CLM", "POL"]
    assert not h.mock.calls("POST", "/api/files/find")


# ------------------------------------------------------------------------------ F10


NEW_FILE = {
    "drawerCode": "CLM",
    "description": "New claim",
    "fileType": "Claim file",
    "createdByApplication": "agent",
}


async def test_create_file(h: Harness) -> None:
    h.fake.file_types.append({"Id": 71, "Name": "Claim file"})
    outputs = done(await h.call("ir_create_file", **NEW_FILE, fileNumber="C-77"))
    assert isinstance(outputs["fileId"], int)
    assert h.fake.writes == [
        (
            WRITE_OPS["file"],
            {
                "FileTypeId": 71,
                "ParentId": 7,
                "Name": "New claim",
                "IsTemporary": False,
                "CreatedByApplication": "agent",
                "FileNumberPart1": "C-77",
            },
        )
    ]


async def test_create_file_without_number_skips_the_duplicate_check(h: Harness) -> None:
    done(await h.call("ir_create_file", **{**NEW_FILE, "fileType": "CLM"}, isTemporary=True))
    assert not h.mock.calls("POST", "/api/files/find")
    assert "FileNumberPart1" not in h.fake.writes[0][1]
    assert h.fake.writes[0][1]["IsTemporary"] is True


async def test_create_file_rejects_a_duplicate_number(h: Harness) -> None:
    err = error(
        await h.call("ir_create_file", **{**NEW_FILE, "fileType": "CLM"}, fileNumber="F-1"),
        "IR-4110",
    )
    assert err["existing"][0]["Id"] == 101
    assert not h.fake.writes


async def test_create_file_asks_for_unknown_type_and_drawer(h: Harness) -> None:
    question = needs(await h.call("ir_create_file", **NEW_FILE), "fileType")
    assert question["options"] == ["CLM", "POL"]
    needs(await h.call("ir_create_file", **{**NEW_FILE, "drawerCode": "ZZ"}), "drawerCode")
    assert not h.fake.writes


async def test_create_file_follows_write_mode(tmp_path: Path) -> None:
    preview = Harness(tmp_path, write_mode="dry-run")
    payload = await preview.call("ir_create_file", **{**NEW_FILE, "fileType": "CLM"})
    assert done(payload, "preview") == {"fileId": "$step4.value"}
    assert not preview.fake.writes
    deny = Harness(tmp_path, write_mode="deny")
    error(await deny.call("ir_create_file", **{**NEW_FILE, "fileType": "CLM"}), "IR-3008")
    assert not deny.fake.writes


# ------------------------------------------------------------------------------ F11


async def test_update_file(h: Harness) -> None:
    payload = await h.call(
        "ir_update_file", fileNumber="F-1", newFileNumber="F-1B", newDescription="Renamed"
    )
    assert done(payload) == {"fileId": 101}
    assert h.fake.writes == [(WRITE_OPS["update"], {"FileNumberPart1": "F-1B", "Name": "Renamed"})]
    assert h.mock.calls("POST", "/api/files/101/properties")


async def test_update_file_branches(h: Harness) -> None:
    needs(await h.call("ir_update_file", fileNumber="F-1"), "newFileNumber")
    assert not h.mock.requests
    needs(await h.call("ir_update_file", fileNumber="NOPE", newDescription="x"), "fileNumber")
    question = needs(
        await h.call("ir_update_file", fileNumber="DUP", newDescription="x"), "fileNumber"
    )
    assert len(question["options"]) == 2
    err = error(await h.call("ir_update_file", fileNumber="F-1", newFileNumber="F-2"), "IR-4110")
    assert err["existing"][0]["Id"] == 102
    assert not h.fake.writes
    # Keeping its own number is not a duplicate.
    done(await h.call("ir_update_file", fileNumber="F-1", newFileNumber="f-1"))
    assert h.fake.writes == [(WRITE_OPS["update"], {"FileNumberPart1": "f-1"})]


# ------------------------------------------------------------------------------ F12


async def test_merge_files_needs_confirmation(h: Harness) -> None:
    args = {"sourceFileNumber": "F-2", "targetFileNumber": "F-1"}
    first = await h.call("ir_merge_files", **args)
    assert done(first, "preview") == {"fileId": 101}
    assert first["meta"]["confirmRequired"] is True
    assert any(w["code"] == "IR-3009" for w in first["meta"]["warnings"])
    assert not h.fake.writes
    preview_id = first["data"]["steps"][-1]["preview"]["previewId"]
    second = await h.call("ir_merge_files", **args, confirm=preview_id)
    assert done(second) == {"fileId": 101}
    assert h.fake.writes == [(WRITE_OPS["merge"], 101)]
    assert h.mock.calls("POST", "/api/files/102/merge")


async def test_merge_files_errors(h: Harness) -> None:
    err = error(
        await h.call("ir_merge_files", sourceFileNumber="NOPE", targetFileNumber="F-1"), "IR-4001"
    )
    assert err["message"] == "No file has number NOPE."
    assert err["failedStep"] == 1
    err = error(
        await h.call("ir_merge_files", sourceFileNumber="F-1", targetFileNumber="DUP"), "IR-4001"
    )
    assert err["message"] == "File number DUP matches more than one file."
    error(await h.call("ir_merge_files", sourceFileNumber="F-1", targetFileNumber="F-1"), "IR-4107")
    assert not h.fake.writes


# ------------------------------------------------------------------------------ F13


MOVE = {"sourceFileNumber": "F-1", "targetFileNumber": "F-2", "targetFolderName": "Claims"}


async def test_copy_selected_document_types(h: Harness) -> None:
    payload = await h.call("ir_move_file_content", **MOVE, documentTypes=["INV"])
    outputs = done(payload)
    assert outputs["documentIds"] == [301, 302]
    assert outputs["transferred"] == [301, 302]
    assert outputs["moveResult"] is None
    assert outputs["copyResult"]["DocumentIdMap"] == {"301": 1301, "302": 1302}
    assert h.fake.writes == [(WRITE_OPS["copy"], {"DocumentIds": [301, 302], "TargetId": 202})]
    # "Claims" matched exactly, although the server also returned "Claims Archive".
    assert h.sent("POST", "/api/documents/find") == [{"FileId": 101, "Deleted": False}]


async def test_move_everything_reports_partial_failures(h: Harness) -> None:
    h.fake.move_failures = [303]
    outputs = done(await h.call("ir_move_file_content", **MOVE, mode="move"), "partial")
    assert outputs["transferred"] == [301, 302]
    assert outputs["failed"] == [303]
    assert h.fake.writes == [
        (WRITE_OPS["move"], {"DocumentIds": [301, 302, 303], "TargetParentId": 202})
    ]


async def test_move_file_content_invalid_input_sends_nothing(h: Harness) -> None:
    error(await h.call("ir_move_file_content", **MOVE, mode="teleport"), "IR-3006")
    error(await h.call("ir_move_file_content", **MOVE, documentTypes=["All", "INV"]), "IR-3006")
    error(await h.call("ir_move_file_content", **MOVE, documentTypes=[]), "IR-3006")
    assert not h.mock.requests


async def test_move_file_content_lookup_errors(h: Harness) -> None:
    error(await h.call("ir_move_file_content", **{**MOVE, "sourceFileNumber": "X"}), "IR-4001")
    error(await h.call("ir_move_file_content", **{**MOVE, "targetFileNumber": "DUP"}), "IR-4001")
    err = error(
        await h.call("ir_move_file_content", **{**MOVE, "targetFolderName": "Nope"}), "IR-4001"
    )
    assert err["message"] == "File F-2 has no folder Nope."
    err = error(await h.call("ir_move_file_content", **MOVE, documentTypes=["ZZZ"]), "IR-4006")
    assert err["unknownCodes"] == ["ZZZ"]
    assert not h.fake.writes


async def test_move_file_content_nothing_to_transfer(h: Harness) -> None:
    outputs = done(await h.call("ir_move_file_content", **MOVE, documentTypes=["PHOTO"]))
    assert outputs["documentIds"] == []
    assert not h.fake.writes


async def test_move_file_content_dry_run(preview: Harness) -> None:
    payload = await preview.call("ir_move_file_content", **MOVE, mode="move")
    outputs = done(payload, "preview")
    assert outputs["moveResult"] == "$step5.value"
    assert statuses(payload)[-1] == (5, "moveDocument", "preview")
    assert not preview.fake.writes


# ------------------------------------------------------------------------------ F14


async def test_find_documents_with_every_filter(h: Harness) -> None:
    outputs = done(
        await h.call(
            "ir_find_documents",
            fileNumber="F-1",
            drawerCode="CLM",
            folderName="correspondence",
            docTypeCodes=["doc.inv", "LTR"],
            identifier="APRIL",
        )
    )
    assert [d["Id"] for d in outputs["documents"]] == [302]
    assert h.sent("POST", "/api/documents/find") == [
        {"FileId": 101, "Deleted": False, "ParentId": 201, "DocumentTypeIds": [501, 502]}
    ]
    assert not h.fake.writes


async def test_find_documents_whole_file(h: Harness) -> None:
    outputs = done(await h.call("ir_find_documents", fileNumber="F-1"))
    assert [d["Id"] for d in outputs["documents"]] == [301, 302, 303]


async def test_find_documents_errors(h: Harness) -> None:
    err = error(await h.call("ir_find_documents", fileNumber="F-1", drawerCode="POL"), "IR-4107")
    assert err["message"] == "File F-1 is not in drawer POL."
    assert not h.mock.calls("POST", "/api/documents/find")
    err = error(await h.call("ir_find_documents", fileNumber="F-1", drawerCode="X"), "IR-4001")
    assert err["message"] == "No drawer has code X."
    err = error(
        await h.call("ir_find_documents", fileNumber="F-1", docTypeCodes=["A", "INV", "B"]),
        "IR-4006",
    )
    assert err["unknownCodes"] == ["A", "B"]
    assert "A, B" in err["message"]
    error(await h.call("ir_find_documents", fileNumber="DUP"), "IR-4001")
    error(await h.call("ir_find_documents", fileNumber="P-9", folderName="Twin"), "IR-4001")
    error(await h.call("ir_find_documents", fileNumber="F-1", folderName="Nope"), "IR-4001")


# ------------------------------------------------------------------------------ F15


DOC = {
    "fileNumber": "F-1",
    "folderName": "Correspondence",
    "docTypeCode": "INV",
    "description": "Invoice May",
}


async def test_create_document_in_existing_folder(h: Harness) -> None:
    payload = await h.call("ir_create_document", **DOC, drawerCode="CLM", documentDate="2026-05-01")
    outputs = done(payload)
    assert isinstance(outputs["documentId"], int)
    assert h.fake.writes == [
        (
            WRITE_OPS["document"],
            {
                "ParentId": 201,
                "DocumentTypeId": 501,
                "Description": "Invoice May",
                "DocumentDate": "2026-05-01",
            },
        )
    ]
    assert [s[0] for s in statuses(payload)] == [1, 2, 5, 8, 9]


async def test_create_document_without_force_create(h: Harness) -> None:
    err = error(await h.call("ir_create_document", **{**DOC, "fileNumber": "NEW"}), "IR-4001")
    assert err["message"] == "No file has number NEW."
    error(await h.call("ir_create_document", **{**DOC, "folderName": "Nope"}), "IR-4001")
    error(await h.call("ir_create_document", **{**DOC, "fileNumber": "DUP"}), "IR-4001")
    error(await h.call("ir_create_document", **DOC, drawerCode="POL"), "IR-4107")
    err = error(await h.call("ir_create_document", **{**DOC, "docTypeCode": "ZZ"}), "IR-4006")
    assert err["message"] == "No document type has code ZZ."
    assert not h.fake.writes


async def test_create_document_file_creation_path_asks_first(h: Harness) -> None:
    new = {**DOC, "fileNumber": "NEW", "forceCreate": True}
    needs(await h.call("ir_create_document", **new), "drawerCode")
    needs(await h.call("ir_create_document", **new, drawerCode="CLM"), "createdByApplication")
    needs(
        await h.call("ir_create_document", **new, drawerCode="CLM", createdByApplication="a"),
        "folderTypeName",
    )
    assert not h.mock.calls("GET", "/api/drawers")
    assert not h.fake.writes


async def test_create_document_creates_file_folder_and_document(h: Harness) -> None:
    payload = await h.call(
        "ir_create_document",
        **{**DOC, "fileNumber": "NEW"},
        forceCreate=True,
        drawerCode="POL",
        createdByApplication="agent",
        fileDescription="Brand new",
        folderTypeName="Correspondence",
    )
    done(payload)
    assert h.fake.ops() == [WRITE_OPS["file"], WRITE_OPS["folder"], WRITE_OPS["document"]]
    file_body, folder_body, doc_body = (body for _, body in h.fake.writes)
    assert file_body == {
        "FileTypeId": 80,
        "ParentId": 8,
        "Name": "Brand new",
        "FileNumberPart1": "NEW",
        "IsTemporary": False,
        "CreatedByApplication": "agent",
    }
    new_file = h.fake.files[-1]["Id"]
    assert folder_body == {
        "FolderTypeId": 601,
        "ParentId": new_file,
        "Description": "Correspondence",
    }
    assert doc_body["ParentId"] == h.fake.folders[-1]["Id"]
    assert [s[0] for s in statuses(payload)] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


async def test_create_document_creates_a_missing_folder(h: Harness) -> None:
    args = {**DOC, "folderName": "Claims", "forceCreate": True}
    needs(await h.call("ir_create_document", **args), "folderTypeName")
    err = error(await h.call("ir_create_document", **args, folderTypeName="Nope"), "IR-4006")
    assert err["message"] == "File F-1 allows no folder type named Nope."
    assert not h.fake.writes
    done(await h.call("ir_create_document", **args, folderTypeName="Claims"))
    assert h.fake.ops() == [WRITE_OPS["folder"], WRITE_OPS["document"]]
    assert h.fake.writes[0][1] == {"FolderTypeId": 602, "ParentId": 101, "Description": "Claims"}


async def test_create_document_file_type_code_override(h: Harness) -> None:
    args = {**DOC, "fileNumber": "NEW", "forceCreate": True, "createdByApplication": "a"}
    err = error(
        await h.call(
            "ir_create_document",
            **args,
            drawerCode="CLM",
            fileTypeCode="NOPE",
            folderTypeName="Claims",
        ),
        "IR-4006",
    )
    assert err["message"] == "No file type has code NOPE."
    error(
        await h.call("ir_create_document", **args, drawerCode="ZZ", folderTypeName="Claims"),
        "IR-4001",
    )
    assert not h.fake.writes


async def test_create_document_dry_run_plans_the_full_sequence(preview: Harness) -> None:
    payload = await preview.call(
        "ir_create_document",
        **{**DOC, "fileNumber": "NEW"},
        forceCreate=True,
        drawerCode="CLM",
        createdByApplication="agent",
        folderTypeName="Claims",
    )
    outputs = done(payload, "preview")
    assert outputs == {"documentId": "$step9.value"}
    assert statuses(payload) == [
        (1, "findFiles", "done"),
        (2, "getDrawers", "done"),
        (3, "getTypesForClass", "done"),
        (4, "createFile", "preview"),
        (5, "findFolders", "planned"),
        (6, "getAllowedTypesForContainer", "planned"),
        (7, "createFolder", "planned"),
        (8, "getTypesForClass", "done"),
        (9, "createDocument", "planned"),
    ]
    planned = payload["data"]["steps"][6]
    assert planned["params"] == {
        "FolderTypeId": "$step6.Id",
        "ParentId": "$step4.value",
        "Description": "Correspondence",
    }
    assert not preview.fake.writes


# ------------------------------------------------------------------------------ F16


def upload_args(pdf: Path, **extra: Any) -> dict[str, Any]:
    return {
        "fileNumber": "F-1",
        "drawerCode": "CLM",
        "folderTypeName": "Correspondence",
        "docTypeCode": "INV",
        "identifier": "Scan",
        "pdfFile": str(pdf),
        **extra,
    }


async def test_upload_document_uploads_every_page_in_order(h: Harness, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", 3)
    payload = await h.call("ir_upload_document", **upload_args(pdf))
    outputs = done(payload)
    assert outputs["pageCount"] == 3
    assert len(outputs["pageIds"]) == 3
    assert h.fake.ops() == [
        WRITE_OPS["document"],
        WRITE_OPS["batch"],
        WRITE_OPS["page"],
        WRITE_OPS["page"],
        WRITE_OPS["page"],
    ]
    assert h.fake.writes[1][1] == {"Application": "WebSdk"}
    images: list[FilePart] = []
    for number, request in enumerate(h.mock.calls("POST", "/api/pages"), start=1):
        data_part, image = request.multipart
        assert isinstance(data_part, JsonPart)
        assert isinstance(image, FilePart)
        assert data_part.name == "PageCreateData"
        assert data_part.value == {"DocId": outputs["documentId"], "BatchId": outputs["batchId"]}
        assert image.name == "image0"
        assert image.path.name == f"scan-page{number:04d}.png"
        assert image.content_type == "image/png"
        assert image.size > 0
        images.append(image)
    assert len({i.sha256 for i in images}) == 3  # one distinct image per page
    # The rendered images are temporary.
    assert not any(i.path.exists() for i in images)


async def test_upload_document_reports_a_failed_page(h: Harness, tmp_path: Path) -> None:
    h.fake.fail_page = 2
    pdf = make_pdf(tmp_path / "scan.pdf", 3)
    payload = await h.call("ir_upload_document", **upload_args(pdf))
    assert payload["ok"] is False
    err = payload["error"]
    assert err["failedStep"] == 11
    assert err["pages"]["uploaded"][0]["page"] == 1
    assert err["pages"]["failed"]["page"] == 2
    assert err["pages"]["notAttempted"] == [3]
    assert err["outputs"]["pageIds"] == [err["pages"]["uploaded"][0]["pageId"]]
    assert err["message"].startswith("Page 2 of 3 failed to upload; pages 1-1")
    assert h.fake.pages_created == 2


async def test_upload_document_rejects_bad_pdfs_before_any_request(
    h: Harness, tmp_path: Path
) -> None:
    outside = make_pdf(tmp_path.parent / f"{tmp_path.name}-outside.pdf", 1)
    error(await h.call("ir_upload_document", **upload_args(outside)), "IR-1007")
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    error(await h.call("ir_upload_document", **upload_args(junk)), "IR-3006")
    error(await h.call("ir_upload_document", **upload_args(tmp_path / "missing.pdf")), "IR-3006")
    assert not h.mock.requests


async def test_upload_document_force_create_path(h: Harness, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", 1)
    args = upload_args(pdf, fileNumber="NEW")
    needs(await h.call("ir_upload_document", **args), "createdByApplication")
    assert not h.fake.writes
    done(await h.call("ir_upload_document", **args, createdByApplication="agent"))
    assert h.fake.ops()[:3] == [WRITE_OPS["file"], WRITE_OPS["folder"], WRITE_OPS["document"]]
    assert h.fake.writes[0][1]["Name"] == ""
    assert h.fake.writes[0][1]["FileTypeId"] == 70
    assert h.fake.writes[1][1]["Description"] == "Correspondence"


async def test_upload_document_without_force_create(h: Harness, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", 1)
    err = error(
        await h.call("ir_upload_document", **upload_args(pdf, fileNumber="NEW", forceCreate=False)),
        "IR-4001",
    )
    assert err["message"] == "No file has number NEW."
    error(
        await h.call("ir_upload_document", **upload_args(pdf, drawerCode="POL")),
        "IR-4107",
    )
    assert not h.fake.writes


async def test_upload_document_dry_run_plans_every_page(preview: Harness, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", 2)
    payload = await preview.call("ir_upload_document", **upload_args(pdf))
    outputs = done(payload, "preview")
    assert outputs["pageIds"] == "$step11.Id"
    assert outputs["pageCount"] == 2
    assert statuses(payload)[-4:] == [
        (9, "createDocument", "preview"),
        (10, "createBatch", "preview"),
        (11, "createPage", "planned"),
        (11, "createPage", "planned"),
    ]
    assert [s.get("page") for s in payload["data"]["steps"][-2:]] == [1, 2]
    assert not preview.fake.writes


# ------------------------------------------------------------------------------ engine


async def test_dry_run_setting_stops_at_the_first_lookup(tmp_path: Path) -> None:
    harness = Harness(tmp_path, write_mode="dry-run", IMAGERIGHT_DRY_RUN="true")
    payload = await harness.call("ir_find_documents", fileNumber="F-1")
    assert payload["ok"] is True
    assert payload["data"]["status"] == "stopped"
    assert statuses(payload) == [(1, "findFiles", "preview")]
    assert not harness.mock.calls("POST", "/api/files/find")


async def test_upstream_error_carries_the_step_trail(h: Harness) -> None:
    def broken(request: PreparedRequest) -> MockReply:
        if request.url.endswith("/api/folders/find"):
            return MockReply(status=500, body=b"boom")
        return h.fake.handler(request)

    h.mock.handler = broken
    payload = await h.call("ir_find_documents", fileNumber="F-1", folderName="Correspondence")
    assert payload["ok"] is False
    err = payload["error"]
    assert err["code"].startswith("IR-5")
    assert err["failedStep"] == 3
    assert statuses(payload) == [(1, "findFiles", "done"), (3, "findFolders", "failed")]
