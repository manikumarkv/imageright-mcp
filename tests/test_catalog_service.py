"""Catalog-service invariants that the explorer tools rely on."""

from imageright_mcp.catalog import get_catalog
from imageright_mcp.catalog.areas import _BY_NATIVE, _SOAP_SPLITS, AREAS


def test_every_native_area_is_mapped_explicitly() -> None:
    catalog = get_catalog()
    native = {str(op["area"]) for op in catalog.ops.values()}
    assert native <= set(_BY_NATIVE) | set(_SOAP_SPLITS)
    assert set(catalog.area.values()) <= set(AREAS)


def test_every_area_has_operations() -> None:
    assert set(get_catalog().area.values()) == set(AREAS)


def test_every_capability_and_flow_points_at_real_operations() -> None:
    catalog = get_catalog()
    for cap in catalog.capabilities.values():
        for impl in cap["implementations"].values():
            assert impl["operationId"] in catalog.ops
    for flow in catalog.flows.values():
        for step in flow["steps"]:
            assert step["operationId"] in catalog.ops


def test_search_is_deterministic() -> None:
    catalog = get_catalog()
    first = catalog.search("lock a page", "24.2", limit=20).data
    second = catalog.search("lock a page", "24.2", limit=20).data
    assert first == second
