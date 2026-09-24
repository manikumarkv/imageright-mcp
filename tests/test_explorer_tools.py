"""Explorer and version-matrix tools (plan §3.1-3.2), exercised through the in-memory MCP client."""

from typing import Any

import pytest
from mcp.client.client import Client

from imageright_mcp.server import create_server

EXPLORER_TOOLS = {
    "ir_search_apis",
    "ir_list_areas",
    "ir_describe_api",
    "ir_describe_type",
    "ir_list_flows",
    "ir_describe_flow",
    "ir_check_availability",
    "ir_compare_versions",
    "ir_list_deprecations",
}
EMAIL_RECEIVER_OPS = {
    "rest.v1.emailreceiver.getAccountDetails",
    "rest.v1.emailreceiver.getAccountDetailsByEmail",
    "rest.v1.emailreceiver.getAccountStatusByEmail",
    "rest.v1.emailreceiver.getAccountsByConfiguration",
    "rest.v1.emailreceiver.getConfigurations",
    "rest.v1.emailreceiver.getErrorEmails",
    "rest.v1.emailreceiver.getErrorEmailsByEmail",
}


async def call(
    name: str, args: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, Any]:
    async with Client(create_server(env or {})) as client:
        result = await client.call_tool(name, args)
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert result.is_error is (not payload["ok"])
    return payload


async def ok(name: str, args: dict[str, Any], env: dict[str, str] | None = None) -> Any:
    payload = await call(name, args, env)
    assert payload["ok"] is True, payload["error"]
    return payload["data"]


async def error(name: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = await call(name, args)
    assert payload["ok"] is False
    assert payload["data"] is None
    err = payload["error"]
    assert set(err) >= {"code", "name", "category", "message", "retryable", "hint"}
    assert isinstance(err, dict)
    return err


def warning_codes(payload: dict[str, Any]) -> list[str]:
    return [w["code"] for w in payload["meta"]["warnings"]]


async def test_explorer_tools_are_listed_offline_and_read_only() -> None:
    async with Client(create_server({})) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= EXPLORER_TOOLS
    for name in EXPLORER_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True, name
        assert annotations.open_world_hint is False, name


# ---------------------------------------------------------------- ir_search_apis


async def test_search_ranks_page_upload_first() -> None:
    data = await ok("ir_search_apis", {"query": "upload a page", "limit": 3})
    ids = [r["operationId"] for r in data["results"]]
    assert "rest.v1.pages.createPage" in ids
    first = data["results"][0]
    assert first["score"] == 1.0
    assert {"operationId", "surface", "summary", "area", "availability", "deprecated"} <= set(first)


async def test_search_hides_deprecated_unless_asked() -> None:
    query = {"query": "batch delete pages", "surface": "rest-v1", "limit": 50}
    hidden = await ok("ir_search_apis", query)
    assert "rest.v1.pages.batchDeletePages" not in [r["operationId"] for r in hidden["results"]]
    shown = await ok("ir_search_apis", {**query, "includeDeprecated": True})
    hit = next(r for r in shown["results"] if r["operationId"] == "rest.v1.pages.batchDeletePages")
    assert hit["deprecated"] is True


async def test_search_filters_by_surface_and_area() -> None:
    data = await ok("ir_search_apis", {"query": "lock", "surface": "soap", "area": "tasks"})
    assert data["results"]
    assert {r["surface"] for r in data["results"]} == {"soap"}
    assert {r["area"] for r in data["results"]} == {"Tasks"}


async def test_search_reports_availability_in_the_chosen_version() -> None:
    data = await ok("ir_search_apis", {"query": "email receiver configurations", "version": "24.x"})
    hit = next(r for r in data["results"] if r["operationId"] in EMAIL_RECEIVER_OPS)
    assert hit["availability"] == "absent"
    data = await ok("ir_search_apis", {"query": "email receiver configurations", "version": "25.x"})
    assert data["results"][0]["operationId"] in EMAIL_RECEIVER_OPS
    assert data["results"][0]["availability"] == "available"


async def test_search_nonsense_returns_empty_with_hint() -> None:
    data = await ok("ir_search_apis", {"query": "zzzzqqq"})
    assert data["results"] == []
    assert "hint" in data


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"query": "page", "version": "9.x"}, "IR-1002"),
        ({"query": "page", "area": "Nowhere"}, "IR-3006"),
        ({"query": "  "}, "IR-3006"),
    ],
)
async def test_search_rejects_bad_input(args: dict[str, Any], code: str) -> None:
    assert (await error("ir_search_apis", args))["code"] == code


async def test_version_defaults_to_config_and_warns_when_approximated() -> None:
    data = await ok("ir_search_apis", {"query": "page"}, {"IMAGERIGHT_VERSION": "25.x"})
    assert data["version"] == "25.1"
    payload = await call("ir_search_apis", {"query": "page", "version": "24.1"})
    assert payload["data"]["version"] == "24.2"
    assert warning_codes(payload) == ["IR-1005"]


async def test_bad_config_falls_back_with_warning() -> None:
    payload = await call("ir_list_flows", {}, {"IMAGERIGHT_WRITE_MODE": "yolo"})
    assert payload["ok"] is True
    assert payload["data"]["version"] == "24.2"
    assert warning_codes(payload) == ["IR-1001"]


# ---------------------------------------------------------------- ir_list_areas


async def test_list_areas_counts_per_version() -> None:
    old = {a["area"]: a for a in (await ok("ir_list_areas", {"version": "24.x"}))["areas"]}
    new = {a["area"]: a for a in (await ok("ir_list_areas", {"version": "25.x"}))["areas"]}
    assert "Email receiver" not in old
    assert new["Email receiver"]["counts"] == {"rest-v1": 7, "rest-v2": 0, "soap": 0}
    assert new["Tasks"]["topOperations"]
    assert all(a["description"] for a in new.values())


async def test_list_areas_surface_filter() -> None:
    data = await ok("ir_list_areas", {"surface": "rest-v2"})
    for area in data["areas"]:
        assert area["counts"]["rest-v2"] > 0
        for op in area["topOperations"]:
            assert op["operationId"].startswith("rest.v2.")


# ---------------------------------------------------------------- ir_describe_api


async def test_describe_page_create_has_multipart_value_sources_and_gotchas() -> None:
    data = await ok("ir_describe_api", {"operationId": "rest.v1.pages.createPage"})
    assert data["request"] == {"method": "POST", "path": "/api/pages"}
    assert [p["name"] for p in data["multipartParts"]] == ["PageCreateData", "image0"]
    batch = next(p for p in data["params"] if p["name"] == "BatchId")
    assert batch["meaning"]
    assert batch["valueFrom"][0]["operationId"] == "rest.v1.batches.createBatch"
    assert any("batch" in g for g in data["gotchas"])
    assert {"nativeCode": 915, "irCode": "IR-4108", "irName": "ImageMissing"}.items() <= next(
        e for e in data["errors"] if e["nativeCode"] == 915
    ).items()
    assert data["flows"][0]["flowId"] == "F3"
    assert data["availability"]["row"] == {
        "24.2": "available",
        "25.1": "available",
        "7.2": "changed",
    }
    assert data["source"]["confidence"] == "annotated"
    assert data["response"]["201"]["type"] == "PageModel"
    assert "rest.v1.batches.createBatch" in [r["operationId"] for r in data["related"]]


async def test_describe_resolves_keys_and_soap_names() -> None:
    by_key = await ok("ir_describe_api", {"operationId": "POST /api/pages"})
    assert by_key["operationId"] == "rest.v1.pages.createPage"
    soap = await ok("ir_describe_api", {"operationId": "CreateTask"})
    assert soap["operationId"] == "soap.CreateTask"
    assert soap["request"]["argumentOrder"][0] == "securityToken"
    token = next(p for p in soap["params"] if p["name"] == "securityToken")
    assert "rotates" in token["meaning"]


async def test_describe_capability_follows_surface_preference() -> None:
    default = await ok("ir_describe_api", {"capabilityId": "task.create"})
    assert default["operationId"] == "rest.v1.tasks.createTask"
    assert {i["surface"] for i in default["capability"]["implementations"]} == {"rest-v1", "soap"}
    soap_first = await ok(
        "ir_describe_api",
        {"capabilityId": "task.create"},
        {"IMAGERIGHT_SURFACE_PREFERENCE": "soap,rest-v1"},
    )
    assert soap_first["operationId"] == "soap.CreateTask"


async def test_describe_marks_version_specific_params() -> None:
    data = await ok(
        "ir_describe_api", {"operationId": "rest.v1.tasks.getNextAvailableTask", "version": "7.2"}
    )
    file_id = next(p for p in data["params"] if p["name"] == "FileId")
    assert file_id["availableIn"] == ["24.2", "25.1"]
    assert file_id["availableInVersion"] is False
    assert data["availability"]["status"] == "changed"


async def test_describe_lists_enum_values_for_the_version() -> None:
    data = await ok("ir_describe_api", {"operationId": "rest.v1.workflow.getSteps"})
    flag = next(p for p in data["params"] if p["name"] == "flag")
    assert "Production" in flag["allowedValues"]


async def test_describe_deprecated_op_warns_and_names_replacement() -> None:
    payload = await call("ir_describe_api", {"operationId": "rest.v1.pages.batchDeletePages"})
    assert payload["data"]["deprecation"]["replacement"] == "rest.v1.instances.batchDeleteInstances"
    assert "IR-3003" in warning_codes(payload)


async def test_describe_absent_op_warns() -> None:
    args = {"operationId": "rest.v1.emailreceiver.getConfigurations", "version": "24.x"}
    payload = await call("ir_describe_api", args)
    assert payload["ok"] is True
    assert "IR-3002" in warning_codes(payload)


async def test_describe_unknown_op_suggests() -> None:
    err = await error("ir_describe_api", {"operationId": "rest.v1.pages.createPag"})
    assert err["code"] == "IR-3001"
    assert "rest.v1.pages.createPage" in err["suggestions"]
    assert (await error("ir_describe_api", {}))["code"] == "IR-3005"


# ---------------------------------------------------------------- ir_describe_type


async def test_describe_type_shows_version_differences() -> None:
    data = await ok("ir_describe_type", {"name": "TaskFilterV2", "version": "7.2"})
    (definition,) = data["definitions"]
    assert definition["surfaces"] == ["rest-v1", "rest-v2"]
    file_id = next(f for f in definition["fields"] if f["name"] == "FileId")
    assert file_id["availableInVersion"] is False
    assert any(d.startswith("FileId") for d in definition["versionDifferences"])
    algo = next(f for f in definition["fields"] if f["name"] == "AgeCalculationAlgorithm")
    assert "AvailableDate" not in algo["allowedValues"]


async def test_describe_type_enum_and_type_changes() -> None:
    enum = (await ok("ir_describe_type", {"name": "TaskAgeCalculationAlgorithm"}))["definitions"]
    values = {v["name"]: v for v in enum[0]["values"]}
    assert values["AvailableDate"]["availableIn"] == ["24.2", "25.1"]
    search = await ok(
        "ir_describe_type", {"name": "FolderSearchObject", "version": "7.2", "surface": "rest-v1"}
    )
    attrs = next(f for f in search["definitions"][0]["fields"] if f["name"] == "Attributes")
    assert attrs["type"] == "AttributeDTO[]"
    assert attrs["typeByVersion"]["24.2"] == "SearchAttributeDTO[]"


async def test_describe_type_borrows_field_meanings_and_lists_users() -> None:
    data = await ok("ir_describe_type", {"name": "PageCreateModel", "surface": "rest-v1"})
    definition = data["definitions"][0]
    doc_id = next(f for f in definition["fields"] if f["name"] == "DocId")
    assert doc_id["meaning"]
    assert "rest.v1.pages.createPage" in definition["usedBy"]


async def test_describe_type_unknown() -> None:
    err = await error("ir_describe_type", {"name": "TaskFiltrV2"})
    assert err["code"] == "IR-3006"
    assert "TaskFilterV2" in err["suggestions"]


# ---------------------------------------------------------------- flows


async def test_list_flows() -> None:
    data = await ok("ir_list_flows", {})
    ids = [f["flowId"] for f in data["flows"]]
    assert ids[:8] == ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"]
    assert all(f["usableInVersion"] for f in data["flows"])
    soap = await ok("ir_list_flows", {"surface": "soap"})
    assert [f["flowId"] for f in soap["flows"]] == ["F8", "F8b"]


async def test_describe_flow_is_documentation_only() -> None:
    data = await ok("ir_describe_flow", {"flowId": "f3", "version": "7.2"})
    assert data["flowId"] == "F3"
    assert data["executes"] is False
    steps = data["steps"]
    assert [s["n"] for s in steps] == list(range(1, len(steps) + 1))
    assert steps[-1]["operationId"] == "rest.v1.pages.createPage"
    assert any(ref.startswith("$step") for ref in steps[-1]["inputsFrom"])
    assert any("PageExtension" in c for c in data["versionCaveats"])
    assert data["failurePoints"]


async def test_describe_flow_warns_on_other_surface_and_rejects_unknown() -> None:
    payload = await call("ir_describe_flow", {"flowId": "F8", "surface": "rest-v1"})
    assert warning_codes(payload) == ["IR-3004"]
    err = await error("ir_describe_flow", {"flowId": "F99"})
    assert err["code"] == "IR-3006"
    assert "F1" in err["suggestions"]


# ---------------------------------------------------------------- ir_check_availability


async def test_availability_of_new_op() -> None:
    data = await ok(
        "ir_check_availability", {"operationId": "rest.v1.emailreceiver.getConfigurations"}
    )
    assert {r["version"]: r["status"] for r in data["rows"]} == {
        "7.2": "absent",
        "24.2": "absent",
        "25.1": "available",
    }


async def test_availability_by_path_and_method() -> None:
    data = await ok("ir_check_availability", {"path": "/api/steps/users", "method": "POST"})
    assert data["operationId"] == "rest.v1.workflow.getUsersToAssignSteps"
    assert data["rows"][0] == {"version": "7.2", "status": "absent"}
    concrete = await ok("ir_check_availability", {"path": "/api/tasks/123/lock", "method": "post"})
    assert concrete["operationId"] == "rest.v1.taskactions.lockTask"
    overload = await ok(
        "ir_check_availability", {"path": "/api/marks?fileTypeId=4", "method": "GET"}
    )
    assert overload["operationId"] == "rest.v1.marks.getFileMarkDefinitionsByFileTypeId"


async def test_availability_of_param_and_member() -> None:
    data = await ok(
        "ir_check_availability",
        {"operationId": "rest.v2.folders.getFolderByIdV2", "param": "excludehidden"},
    )
    assert data["param"]["name"] == "excludeHidden"
    assert [r["status"] for r in data["param"]["rows"]] == ["absent", "absent", "available"]
    member = await ok(
        "ir_check_availability", {"param": "TaskAgeCalculationAlgorithm.AvailableDate"}
    )
    rows = member["definitions"][0]["rows"]
    assert [r["status"] for r in rows] == ["absent", "available", "available"]


async def test_availability_of_deprecated_op_and_capability() -> None:
    data = await ok(
        "ir_check_availability",
        {"operationId": "rest.v1.files.updateFileProperties", "versions": ["25.x"]},
    )
    (row,) = data["rows"]
    assert row["status"] == "deprecated"
    assert row["replacement"] == "rest.v2.files.updateFilePropertiesV2"
    cap = await ok("ir_check_availability", {"capabilityId": "page.create"})
    assert {i["surface"] for i in cap["implementations"]} == {"rest-v1", "rest-v2", "soap"}


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, "IR-3005"),
        ({"path": "/api/nothing/here"}, "IR-3001"),
        ({"path": "/api/pages/{pageId}"}, "IR-3001"),
        ({"operationId": "rest.v1.pages.createPage", "param": "Nope"}, "IR-3006"),
        ({"param": "Nope.Nothing"}, "IR-3006"),
        ({"operationId": "soap.CreateTask", "versions": ["3.0"]}, "IR-1002"),
    ],
)
async def test_availability_rejects_bad_input(args: dict[str, Any], code: str) -> None:
    assert (await error("ir_check_availability", args))["code"] == code


# ---------------------------------------------------------------- ir_compare_versions


async def test_compare_24_to_25() -> None:
    data = await ok("ir_compare_versions", {"fromVersion": "24.x", "toVersion": "25.x"})
    v1 = data["surfaces"]["rest-v1"]
    assert {o["operationId"] for o in v1["added"]} >= EMAIL_RECEIVER_OPS
    assert v1["removed"] == []
    v2_params = {c["operationId"]: c for c in data["surfaces"]["rest-v2"]["changedParams"]}
    assert "excludeHidden (query)" in v2_params["rest.v2.folders.getFolderByIdV2"]["added"]
    assert [e["nativeCode"] for e in data["errorCodes"]["added"]] == [917]
    assert "note" in data["surfaces"]["soap"]


async def test_compare_runs_backwards_and_filters_by_area() -> None:
    data = await ok(
        "ir_compare_versions",
        {"fromVersion": "25.1", "toVersion": "7.2", "surface": "rest-v1", "area": "Email receiver"},
    )
    v1 = data["surfaces"]["rest-v1"]
    assert {o["operationId"] for o in v1["removed"]} == EMAIL_RECEIVER_OPS
    assert v1["added"] == []
    enums = {e["name"]: e["change"] for e in v1["changedEnums"]}
    assert enums["TaskAgeCalculationAlgorithm"] == "-value AvailableDate"
    assert list(data["surfaces"]) == ["rest-v1"]


async def test_compare_same_version_is_empty() -> None:
    data = await ok(
        "ir_compare_versions", {"fromVersion": "24.x", "toVersion": "24.2", "surface": "rest-v1"}
    )
    v1 = data["surfaces"]["rest-v1"]
    assert v1["added"] == v1["removed"] == v1["changedParams"] == []
    assert data["errorCodes"] == {"added": [], "removed": []}


# ---------------------------------------------------------------- ir_list_deprecations


async def test_list_deprecations() -> None:
    data = await ok("ir_list_deprecations", {})
    items = {d["operationId"]: d for d in data["deprecations"]}
    assert set(items) == {
        "rest.v1.files.updateFileProperties",
        "rest.v1.functionalityrights.getPermissionsBatchAll",
        "rest.v1.pages.batchDeletePages",
    }
    item = items["rest.v1.pages.batchDeletePages"]
    assert item["replacementKey"] == "POST /api/files/{fileId}/instances/delete"
    assert item["note"]
    assert len((await ok("ir_list_deprecations", {"version": "7.2"}))["deprecations"]) == 3
