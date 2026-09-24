"""Catalog-driven validation: required params, types, enums, per-version availability."""

from __future__ import annotations

from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client import Validator
from tests.conftest import ClientFactory

BIG = 2**53 + 1


def _validate(
    op_id: str, params: dict[str, Any], profile: str = "24.2", files: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    catalog = get_catalog()
    result = Validator(catalog.schemas).validate(catalog.ops[op_id], profile, params, files or {})
    return [(i.code, i.param) for i in result.issues]


def test_valid_request_has_no_issues() -> None:
    assert _validate("rest.v1.documents.getDocumentById", {"docId": BIG}) == []
    assert _validate("rest.v1.documents.getDocumentById", {"docId": "123"}) == []


def test_missing_required_params() -> None:
    assert _validate("rest.v1.documents.getDocumentById", {}) == [("IR-3005", "docId")]
    issues = _validate("rest.v1.documents.createDocument", {"ParentId": 1})
    assert ("IR-3005", "DocumentTypeId") in issues
    assert all(code == "IR-3005" for code, _ in issues)


def test_missing_file_part() -> None:
    assert _validate("rest.v1.pages.createPage", {"DocId": 1}) == [("IR-3005", "image0")]
    assert _validate("rest.v2.pages.createPageV2", {"DocId": 1}) == [("IR-3005", "Image")]


def test_wrong_file_part_name_suggests_the_catalog_name() -> None:
    catalog = get_catalog()
    result = Validator(catalog.schemas).validate(
        catalog.ops["rest.v2.pages.createPageV2"], "24.2", {"DocId": 1}, {"image0": "/x"}
    )
    codes = [(i.code, i.param) for i in result.issues]
    assert ("IR-3006", "image0") in codes
    assert ("IR-3005", "Image") in codes


def test_numbered_image_parts_follow_the_catalog_pattern() -> None:
    files = {"image0": "/a", "image1": "/b", "image7": "/c"}
    params = {"pageId": 1, "PageUpdateData": {"DocId": 1}}
    assert _validate("rest.v1.pages.updatePageContent", params, files=files) == []


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"docId": "abc"}, "docId"),
        ({"docId": True}, "docId"),
        ({"docId": 2**63}, "docId"),
        ({"docId": 1.5}, "docId"),
    ],
)
def test_type_errors(params: dict[str, Any], param: str) -> None:
    assert _validate("rest.v1.documents.getDocumentById", params) == [("IR-3006", param)]


def test_body_types_are_strict() -> None:
    issues = _validate(
        "rest.v1.folders.findFolders",
        {"FileId": "12", "IsDeleted": "yes", "FolderTypeIds": [1, "x"]},
    )
    assert issues == [
        ("IR-3006", "IsDeleted"),
        ("IR-3006", "FileId"),
        ("IR-3006", "FolderTypeIds[1]"),
    ]


def test_int32_range() -> None:
    issues = _validate("rest.v1.reports.getReportChunk", {"reportid": 1, "chunkNumber": 2**31})
    assert issues == [("IR-3006", "chunkNumber")]


def test_unknown_param_gets_a_suggestion() -> None:
    catalog = get_catalog()
    result = Validator(catalog.schemas).validate(
        catalog.ops["rest.v1.documents.getDocumentById"], "24.2", {"docid": 1}, {}
    )
    messages = [i.message for i in result.issues]
    assert any("Did you mean docId" in m for m in messages)


def test_enum_values() -> None:
    op = "rest.v1.tasks.getNextAvailableTask"
    assert _validate(op, {"AgeCalculationAlgorithm": "StepDuration"}) == []
    assert _validate(op, {"AgeCalculationAlgorithm": "Bogus"}) == [
        ("IR-3006", "AgeCalculationAlgorithm")
    ]
    assert _validate(op, {"AgeCalculationAlgorithm": "stepduration"}) == [
        ("IR-3006", "AgeCalculationAlgorithm")
    ]


def test_enum_value_not_in_version() -> None:
    op = "rest.v1.tasks.getNextAvailableTask"
    assert _validate(op, {"AgeCalculationAlgorithm": "AvailableDate"}, "24.2") == []
    assert _validate(op, {"AgeCalculationAlgorithm": "AvailableDate"}, "7.2") == [
        ("IR-3007", "AgeCalculationAlgorithm")
    ]


def test_param_not_in_version() -> None:
    assert _validate("rest.v1.folders.findFolders", {"ExcludeHidden": True}, "25.1") == []
    assert _validate("rest.v1.folders.findFolders", {"ExcludeHidden": True}, "24.2") == [
        ("IR-3007", "ExcludeHidden")
    ]
    assert _validate("rest.v1.tasks.getNextAvailableTask", {"FileId": 3}, "7.2") == [
        ("IR-3007", "FileId")
    ]


def test_nested_schema_fields() -> None:
    op = "rest.v1.folders.findFolders"
    good = {"CreatedDateRange": {"StartDate": "2024-01-01T00:00:00Z"}}
    assert _validate(op, good) == []
    assert _validate(op, {"CreatedDateRange": {"StartDate": "yesterday"}}) == [
        ("IR-3006", "CreatedDateRange.StartDate")
    ]
    assert _validate(op, {"CreatedDateRange": {"Start": "2024-01-01"}}) == [
        ("IR-3006", "CreatedDateRange.Start")
    ]


def test_operation_absent_from_profile() -> None:
    assert _validate("rest.v1.containers.getDeletedContent", {"containerId": 1}, "24.2") == [
        ("IR-3002", "rest.v1.containers.getDeletedContent")
    ]
    assert _validate("rest.v1.containers.getDeletedContent", {"containerId": 1}, "25.1") == []


def test_multipart_body_params_may_be_given_as_the_json_part() -> None:
    op = "rest.v1.pages.createPage"
    files = {"image0": "/x"}
    assert _validate(op, {"PageCreateData": {"DocId": 1}}, files=files) == []
    assert _validate(op, {"PageCreateData": {"BatchId": 1}}, files=files) == [
        ("IR-3005", "PageCreateData.DocId")
    ]
    assert ("IR-3006", "DocId") in _validate(
        op, {"PageCreateData": {"DocId": 1}, "DocId": 1}, files=files
    )


async def test_validation_failure_returns_error_and_partial_preview(
    make_client: ClientFactory,
) -> None:
    client, mock = make_client({"IMAGERIGHT_VERSION": "7.2"})
    outcome = await client.call(
        "rest.v1.tasks.getNextAvailableTask", {"FileId": 3, "Steps": "x"}, dry_run=True
    )
    assert outcome.error is not None
    issues = outcome.error["issues"]
    assert {i["code"] for i in issues} == {"IR-3007", "IR-3006"}
    assert outcome.error["code"] == issues[0]["code"]
    preview = outcome.error["preview"]
    assert preview["validation"]["ok"] is False
    assert preview["request"]["json"] == {"FileId": 3, "Steps": "x"}
    envelope = outcome.envelope()
    assert envelope.is_error
    assert not mock.requests


async def test_live_call_with_invalid_params_never_sends(make_client: ClientFactory) -> None:
    client, mock = make_client()
    outcome = await client.call("rest.v1.documents.getDocumentById", {})
    assert outcome.error is not None
    assert outcome.error["code"] == "IR-3005"
    assert not mock.requests


async def test_unknown_and_soap_operations(make_client: ClientFactory) -> None:
    client, mock = make_client()
    unknown = await client.call("rest.v1.documents.nope")
    assert unknown.error is not None
    assert unknown.error["code"] == "IR-3001"
    # SOAP operations are live (M5) and validated against the operation table: the missing
    # required arguments are reported and nothing is sent. The token is never a caller argument.
    soap = await client.call("soap.GetDocumentByRef")
    assert soap.error is not None
    assert soap.error["code"] == "IR-3005"
    missing = {issue["param"] for issue in soap.error["issues"]}
    assert missing == {"docRef", "getContent", "includeDeleted"}
    assert "request" in soap.error["preview"]
    assert not mock.requests
