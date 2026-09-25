"""Router gate (plan §4.2, §4.3; M6): every capability x profile x surfacePreference resolves to
the expected surface, snapshotted in ``tests/snapshots/routing.json``.

Regenerate with ``UPDATE_SNAPSHOTS=1 pytest tests/test_router.py`` and review the diff: a changed
entry means a capability now goes to a different surface on some version.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client.capability import DERIVED
from imageright_mcp.client.router import SURFACES, Route, RouteError, Router

SNAPSHOT = Path(__file__).parent / "snapshots" / "routing.json"
CATALOG = get_catalog()
ROUTER = Router(CATALOG)
PROFILES = sorted(CATALOG.profiles)
# All orderings of the three surfaces, plus each surface alone.
PREFERENCES = [list(p) for p in itertools.permutations(SURFACES)] + [[s] for s in SURFACES]
ALL = {"rest-v1", "rest-v2", "soap"}


def pref_key(preference: list[str]) -> str:
    return ">".join(preference)


def route_or_none(capability: str, profile: str, preference: list[str], **kw: Any) -> str | None:
    try:
        return ROUTER.route(capability, profile=profile, preference=preference, **kw).surface
    except RouteError as exc:
        code = exc.error["code"]
    assert code == "IR-3004"
    return None


def routing_table() -> dict[str, dict[str, dict[str, str | None]]]:
    return {
        cap: {
            profile: {pref_key(p): route_or_none(cap, profile, p, enabled=ALL) for p in PREFERENCES}
            for profile in PROFILES
        }
        for cap in sorted(CATALOG.capabilities)
    }


def test_routing_table_snapshot() -> None:
    table = routing_table()
    rendered = json.dumps(table, indent=1, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.exists(), "run UPDATE_SNAPSHOTS=1 pytest tests/test_router.py"
    assert json.loads(SNAPSHOT.read_text()) == table


def expected_surface(capability: dict[str, Any], profile: str, preference: list[str]) -> str | None:
    """Independent restatement of the routing rules (plan §4.2 step 2)."""
    for surface in preference:
        impl = capability["implementations"].get(surface)
        if impl is None:
            continue
        op = CATALOG.ops[impl["operationId"]]
        if op["availability"].get(profile, "absent") == "absent":
            continue
        deprecation = op.get("deprecation")
        if deprecation and profile in deprecation.get("in", []):
            continue
        if set(impl.get("derived") or {}) - set(DERIVED):
            continue
        return surface
    return None


@pytest.mark.parametrize("capability_id", sorted(CATALOG.capabilities))
def test_routing_follows_the_rules(capability_id: str) -> None:
    capability = CATALOG.capabilities[capability_id]
    for profile in PROFILES:
        for preference in PREFERENCES:
            got = route_or_none(capability_id, profile, preference, enabled=ALL)
            assert got == expected_surface(capability, profile, preference), (
                capability_id,
                profile,
                preference,
            )


def test_every_capability_is_routable_somewhere() -> None:
    table = routing_table()
    stuck = [
        cap for cap, rows in table.items() if not any(v for r in rows.values() for v in r.values())
    ]
    assert stuck == []


def test_default_preference_prefers_v2_then_v1_then_soap() -> None:
    route = ROUTER.route("document.get", profile="24.2", preference=["rest-v2", "rest-v1", "soap"])
    assert route.surface == "rest-v2"
    assert route.operation_id == "rest.v2.documents.getDocumentByIdV2"
    assert route.trail[0] == {
        "surface": "rest-v2",
        "operationId": "rest.v2.documents.getDocumentByIdV2",
        "verified": False,
        "chosen": True,
    }
    assert route.trail[1]["rejected"] == "lower preference than rest-v2"
    assert [t["surface"] for t in route.trail] == ["rest-v2", "rest-v1", "soap"]


def test_trail_explains_a_fallback_to_v1() -> None:
    route = ROUTER.route("task.create", profile="24.2", preference=["rest-v2", "rest-v1", "soap"])
    assert route.surface == "rest-v1"
    assert route.trail[0] == {
        "surface": "rest-v2",
        "rejected": "no implementation of this capability",
    }
    assert route.trail[1]["chosen"] is True
    assert "Despite its name" in route.trail[1]["note"]


def patched_router(op_id: str, **changes: Any) -> Router:
    """A router over a copy of the catalog with one operation's record changed. The generated
    catalog has no deprecated or version-absent capability implementation today, so these
    rules are exercised on a patched copy."""
    catalog = copy.copy(CATALOG)
    catalog.ops = {**CATALOG.ops, op_id: {**CATALOG.ops[op_id], **changes}}
    return Router(catalog)


def test_deprecated_implementation_is_dropped_unless_forced() -> None:
    op_id = "rest.v2.documents.getDocumentByIdV2"
    router = patched_router(
        op_id, deprecation={"in": ["25.1"], "replacement": "rest.v1.documents.getDocumentById"}
    )
    route = router.route("document.get", profile="25.1", preference=list(SURFACES))
    assert route.surface == "rest-v1"
    assert route.trail[0]["rejected"] == (
        "deprecated in 25.1; replacement rest.v1.documents.getDocumentById"
    )
    assert router.route("document.get", profile="24.2", preference=list(SURFACES)).surface == (
        "rest-v2"
    )
    forced = router.route(
        "document.get", profile="25.1", preference=list(SURFACES), force="rest-v2"
    )
    assert forced.surface == "rest-v2"
    assert [w["code"] for w in forced.warnings] == ["IR-3003"]


def test_absent_in_profile_is_rejected_with_the_versions_that_have_it() -> None:
    op_id = "soap.GetTaskByRef"
    availability = {"7.2": "absent", "24.2": "available", "25.1": "available"}
    router = patched_router(op_id, availability=availability)
    with pytest.raises(RouteError) as info:
        router.route("task.get", profile="7.2", preference=list(SURFACES))
    assert info.value.error["code"] == "IR-3004"
    assert info.value.trail[-1]["rejected"] == "absent in 7.2 (available in 24.2, 25.1)"
    assert router.route("task.get", profile="24.2", preference=list(SURFACES)).surface == "soap"


def test_disabled_surface_is_skipped() -> None:
    with pytest.raises(RouteError) as info:
        ROUTER.route(
            "task.get", profile="24.2", preference=list(SURFACES), enabled={"rest-v1", "rest-v2"}
        )
    assert info.value.trail[-1]["rejected"] == "surface disabled: soapUrl is not configured"
    # No endpoint at all (enabled=None) routes by catalog and preference only (dry-run).
    offline = ROUTER.route("task.get", profile="24.2", preference=list(SURFACES), enabled=None)
    assert offline.surface == "soap"


def test_forcing_a_disabled_surface_still_routes_for_a_preview() -> None:
    route = ROUTER.route(
        "document.get",
        profile="24.2",
        preference=list(SURFACES),
        force="soap",
        enabled={"rest-v1", "rest-v2"},
    )
    assert route.surface == "soap"
    assert route.trail[0]["reason"] == "surface forced by the caller"
    assert all("rejected" in t for t in route.trail[1:])


def test_require_verified_mappings_blocks_unverified_unless_forced() -> None:
    with pytest.raises(RouteError) as info:
        ROUTER.route(
            "document.get", profile="24.2", preference=list(SURFACES), require_verified=True
        )
    assert {t["rejected"] for t in info.value.trail} == {
        "param mapping not verified against a fixture (requireVerifiedMappings)"
    }
    forced = ROUTER.route(
        "document.get",
        profile="24.2",
        preference=list(SURFACES),
        require_verified=True,
        force="rest-v1",
    )
    assert forced.surface == "rest-v1"


def test_underivable_arguments_reject_the_surface() -> None:
    route = ROUTER.route("file.find", profile="24.2", preference=["soap", "rest-v1"])
    assert route.surface == "rest-v1"
    assert route.trail[0]["rejected"] == (
        "needs derived argument(s) searchConditions that this server cannot build yet"
    )


def test_a_param_the_surface_cannot_express_rejects_it() -> None:
    kwargs: dict[str, Any] = {"profile": "24.2", "preference": ["soap", "rest-v1"]}
    assert ROUTER.route("folder.find", supplied={"fileId"}, **kwargs).surface == "soap"
    route = ROUTER.route("folder.find", supplied={"fileId", "description"}, **kwargs)
    assert route.surface == "rest-v1"
    assert route.trail[0]["rejected"] == "cannot express parameter(s) description on this surface"


def test_surfaces_missing_from_the_preference_are_not_used() -> None:
    with pytest.raises(RouteError) as info:
        ROUTER.route("task.get", profile="24.2", preference=["rest-v1", "rest-v2"])
    assert info.value.trail[-1] == {
        "surface": "soap",
        "operationId": "soap.GetTaskByRef",
        "verified": False,
        "rejected": "not in surfacePreference",
    }
    assert info.value.error["route"] == info.value.trail


def test_explicit_operation_id_keeps_its_surface() -> None:
    route: Route = ROUTER.route("GET /api/documents/{docId}", profile="24.2", preference=[])
    assert route.operation_id == "rest.v1.documents.getDocumentById"
    assert route.capability_id == "document.get"
    assert route.trail == [{"surface": "rest-v1", "chosen": True, "reason": "explicit operationId"}]
    with pytest.raises(RouteError) as info:
        ROUTER.route(
            "rest.v1.documents.getDocumentById", profile="24.2", preference=[], force="soap"
        )
    assert info.value.error["code"] == "IR-3006"


def test_unknown_identifiers() -> None:
    with pytest.raises(RouteError) as info:
        ROUTER.route("document.teleport", profile="24.2", preference=list(SURFACES))
    assert info.value.error["code"] == "IR-3001"
    with pytest.raises(RouteError) as info:
        ROUTER.route("document.get", profile="24.2", preference=[], force="rest-v3")
    assert info.value.error["code"] == "IR-3006"
