"""Catalog assertions for milestone M1 (plan §8), checked against the committed data/catalog."""

import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "data" / "catalog"
PROFILES = ("7.2", "24.2", "25.1")
SAFETY_LEVELS = {"read", "write", "destructive"}

EMAIL_RECEIVER_OPS = {
    "rest.v1.emailreceiver.getAccountDetails",
    "rest.v1.emailreceiver.getAccountDetailsByEmail",
    "rest.v1.emailreceiver.getAccountStatusByEmail",
    "rest.v1.emailreceiver.getAccountsByConfiguration",
    "rest.v1.emailreceiver.getConfigurations",
    "rest.v1.emailreceiver.getErrorEmails",
    "rest.v1.emailreceiver.getErrorEmailsByEmail",
}
# Found by the mechanical diff beyond the report's claims; human-reviewed (REVIEW.md C2).
V1_25_1_OTHER_ADDED = {
    "rest.v1.containers.getDeletedContent",
    "rest.v1.containers.restoreDeletedContent",
    "rest.v1.containers.restoreDeletedDocFolder",
    "rest.v1.drawers.getLocationTree",
    "rest.v1.notes.restoreDeletedNotesCollection",
    "rest.v1.pages.restoreDeletedPages",
}
V1_7_2_REPORTED_ABSENT = {
    "GET /api/steps/{stepId}/indexingsettings",
    "GET /api/workflows/rootbuddies",
    "POST /api/steps/users",
}
# Found by the mechanical diff beyond the report's claims; human-reviewed (REVIEW.md C4).
V1_7_2_OTHER_ABSENT = {
    "rest.v1.files.createFileRelationship",
    "rest.v1.files.deleteFilesRelationship",
    "rest.v1.globalconfig.createGlobalConfig",
    "rest.v1.globalconfig.deleteGlobalConfig",
    "rest.v1.globalconfig.deleteGlobalConfigSection",
    "rest.v1.instances.managePermissionsToInstance",
    "rest.v1.taskhistory.getWorkSummary",
}
DEPRECATED_V1 = {
    "POST /api/files/{fileId}/properties",
    "POST /api/functionalityrights/permissionsbatch",
    "POST /api/pages/delete",
}


@cache
def load(name: str) -> Any:
    return json.loads((CATALOG / name).read_text(encoding="utf-8"))


def operations() -> dict[str, Any]:
    ops: dict[str, Any] = load("operations.json")["operations"]
    return ops


def present(op: dict[str, Any], profile: str) -> bool:
    return bool(op["availability"][profile] != "absent")


def surface_ops(surface: str, profile: str) -> set[str]:
    return {
        k for k, op in operations().items() if op["surface"] == surface and present(op, profile)
    }


def by_key(key: str) -> dict[str, Any]:
    matches = [op for op in operations().values() if op["key"] == key]
    assert len(matches) == 1, key
    match: dict[str, Any] = matches[0]
    return match


def test_v1_25_1_is_24_2_plus_email_receiver_and_reviewed_extras() -> None:
    base, newer = surface_ops("rest-v1", "24.2"), surface_ops("rest-v1", "25.1")
    assert base <= newer
    assert newer - base == EMAIL_RECEIVER_OPS | V1_25_1_OTHER_ADDED


def test_v1_7_2_lacks_the_three_workflow_endpoints_and_reviewed_extras() -> None:
    missing = surface_ops("rest-v1", "24.2") - surface_ops("rest-v1", "7.2")
    reported = {by_key(key)["id"] for key in V1_7_2_REPORTED_ABSENT}
    assert missing == reported | V1_7_2_OTHER_ABSENT
    assert surface_ops("rest-v1", "7.2") <= surface_ops("rest-v1", "24.2")


def test_v2_25_1_adds_exclude_hidden() -> None:
    op = by_key("GET /api/v2/folders/{folderId}")
    assert op["paramAvailability"] == {"excludeHidden": ["25.1"]}
    assert op["availability"]["25.1"] == "changed"


def test_task_filter_v2_has_no_file_id_in_7_2() -> None:
    field = load("schemas.json")["schemas"]["rest-v2"]["TaskFilterV2"]["fields"]["FileId"]
    assert field["availableIn"] == ["24.2", "25.1"]


def test_age_algorithm_available_date_missing_in_7_2() -> None:
    enum = load("schemas.json")["schemas"]["rest-v1"]["TaskAgeCalculationAlgorithm"]
    assert enum["values"]["AvailableDate"]["availableIn"] == ["24.2", "25.1"]


@pytest.mark.parametrize("profile", PROFILES)
def test_three_deprecations_per_v1_profile(profile: str) -> None:
    deprecated = {
        op["key"]
        for op in operations().values()
        if op["surface"] == "rest-v1" and op["deprecation"] and profile in op["deprecation"]["in"]
    }
    assert deprecated == DEPRECATED_V1
    for key in deprecated:
        replacement = by_key(key)["deprecation"]["replacement"]
        assert replacement in operations()
        assert not operations()[replacement]["deprecation"]


def test_error_917_only_in_25_1() -> None:
    assert load("errors.json")["rest"]["917"]["profiles"] == ["25.1"]


def test_error_2500_only_in_7_2() -> None:
    assert load("errors.json")["rest"]["2500"]["profiles"] == ["7.2"]


def test_soap_has_114_operations() -> None:
    assert load("operations.json")["counts"]["soap"] == 114
    assert sum(1 for op in operations().values() if op["surface"] == "soap") == 114


def test_every_operation_has_safety() -> None:
    for op_id, op in operations().items():
        assert op["safety"] in SAFETY_LEVELS, op_id


def test_capabilities_route_only_to_live_operations() -> None:
    caps = load("capabilities.json")["capabilities"]
    assert len(caps) >= 35
    for cap_id, cap in caps.items():
        assert cap["safety"] in SAFETY_LEVELS
        for surface, impl in cap["implementations"].items():
            op = operations()[impl["operationId"]]
            assert op["surface"] == surface
            assert op["deprecation"] is None, cap_id
            assert op["capability"] == cap_id
            assert impl["verified"] is False  # nothing is fixture-verified before M6
            for native in impl["paramMap"].values():
                assert native.split(".", 1)[0] in {p["name"] for p in op["params"]}


def test_flows_f1_to_f8_reference_known_operations() -> None:
    flows = load("flows.json")["flows"]
    assert {f"F{n}" for n in range(1, 9)} <= set(flows)
    # F8b is the SOAP ingest variant of F8; F9 is the search_files composite
    assert set(flows) - {f"F{n}" for n in range(1, 9)} == {"F8b", "F9"}
    for flow in flows.values():
        for step in flow["steps"]:
            assert step["operationId"] in operations()
            assert step["safety"] == operations()[step["operationId"]]["safety"]


def test_matrix_rows_cover_every_operation() -> None:
    matrix = load("matrix.json")
    assert set(matrix["operations"]) == set(operations())
    assert set(matrix["capabilities"]) == set(load("capabilities.json")["capabilities"])


def test_no_vendor_openapi_documents_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout.split("\0")
    for rel in filter(None, tracked):
        path = REPO / rel
        if path.suffix in {".json", ".wsdl", ".html"} and path.is_file():
            head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
            assert '"openapi"' not in head, rel
            assert "<wsdl:" not in head, rel


def test_generated_catalog_is_current() -> None:
    """Rebuild from vendor snapshots (when available locally) and require no diff."""
    vendor = Path(os.environ.get("IMAGERIGHT_VENDOR_DIR", REPO.parent / "imageright-mcp-vendor"))
    if not vendor.is_dir():
        pytest.skip("vendor snapshots not available (they are never committed)")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_catalog.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
