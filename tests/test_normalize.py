"""Normalizer gate (plan §4.5; M6): Level-1 rules, and Level-2 canonical entities from fixtures
per surface per entity (REST v1, REST v2, SOAP).

Every fixture describes the same underlying object (``fixtures/normalize/canonical.json``
``truth``), so each surface must produce the same canonical entity; fields a surface does not
report are listed under ``absent`` and must come back ``null``. REST fixtures are checked against
the catalog schema so they cannot drift from the vendored OAS. SOAP fixtures go through the real
response parser, so XML -> JSON is covered too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client.normalize import ENTITY_FIELDS, MAPPERS, level1, normalize
from imageright_mcp.client.soap_envelope import EnvelopeBuilder, ResponseParser, SoapTable
from tests.test_client_soap import SOAP_URL, _response

FIXTURES = Path(__file__).parent / "fixtures" / "normalize"
CANONICAL: dict[str, dict[str, Any]] = json.loads((FIXTURES / "canonical.json").read_text())
CATALOG = get_catalog()
TABLE = SoapTable(CATALOG.soap_table)

# Entity -> SOAP operation whose result carries it (from the capability table).
SOAP_OPS = {
    "Document": "soap.GetDocumentByRef",
    "File": "soap.GetFileByRef",
    "Folder": "soap.GetFolderByRef",
    "Page": "soap.GetPageByRef",
    "Task": "soap.GetTaskByRef",
    "Workflow": "soap.GetFlows",
    "Step": "soap.GetStepByRef",
    "User": "soap.GetUser",
    "AuthorizedStepUser": "soap.GetAuthorizedUsersForStep",
}
REST_CASES = sorted(
    (surface, path.stem)
    for surface in ("rest-v1", "rest-v2")
    for path in (FIXTURES / surface).glob("*.json")
)
ENTITY_OF_FILE = {"UserAccount": "User", "AuthorizedStepUser": "User"}


def expected(entity: str, *absent_keys: str) -> dict[str, Any]:
    spec = CANONICAL[entity]
    missing = {f for key in absent_keys for f in spec["absent"].get(key, [])}
    return {f: None if f in missing else spec["truth"][f] for f in ENTITY_FIELDS[entity]}


def schema_fields(surface: str, type_name: str) -> set[str]:
    schema = CATALOG.schemas[surface][type_name]
    fields = set(schema.get("fields") or {})
    for parent in schema.get("extends") or []:
        fields |= schema_fields(surface, parent)
    return fields


def parse_soap(op_id: str, inner: str) -> tuple[Any, str]:
    op = CATALOG.ops[op_id]
    call = EnvelopeBuilder(TABLE).build(op, {}, SOAP_URL)
    parser = ResponseParser(TABLE, lambda data, stem: {"bytes": len(data)})
    parsed = parser.parse(call, _response(str(op["operation"]), inner, "t1").encode())
    assert parsed.shape is not None
    return parsed.result, parsed.shape.split(":", 1)[1]


# ---------------------------------------------------------------------------- level 2


@pytest.mark.parametrize(("surface", "name"), REST_CASES)
def test_rest_fixture_normalizes_to_the_canonical_entity(surface: str, name: str) -> None:
    fixture = json.loads((FIXTURES / surface / f"{name}.json").read_text())
    type_name, body = fixture["type"], fixture["body"]
    assert set(body) <= schema_fields(surface, type_name), "fixture drifted from the catalog"
    entity = ENTITY_OF_FILE.get(name, name)
    data, meta = normalize(
        body, surface=surface, type_name=type_name, canonical=True, include_raw=False
    )
    assert meta == {"normalization": "level2", "shape": f"{surface}:{type_name}", "entity": entity}
    assert data == expected(entity, surface, f"{surface}:{type_name}")


@pytest.mark.parametrize("name", sorted(SOAP_OPS))
def test_soap_fixture_normalizes_to_the_canonical_entity(name: str) -> None:
    inner = (FIXTURES / "soap" / f"{name}.xml").read_text()
    result, type_name = parse_soap(SOAP_OPS[name], inner)
    entity = ENTITY_OF_FILE.get(name, name)
    data, meta = normalize(
        result, surface="soap", type_name=type_name, canonical=True, include_raw=False
    )
    assert meta["entity"] == entity
    assert meta["shape"] == f"soap:{type_name}"
    if type_name.startswith("ArrayOf"):
        assert isinstance(data, list)
        (data,) = data
    assert data == expected(entity, "soap")


def test_every_entity_has_fixtures_for_every_surface_that_has_its_model() -> None:
    rest = {(s, ENTITY_OF_FILE.get(n, n)) for s, n in REST_CASES}
    for entity in ENTITY_FIELDS:
        assert ("rest-v1", entity) in rest, entity
        assert (FIXTURES / "soap" / f"{entity}.xml").exists(), entity
        models = [t for (fam, t), (e, _) in MAPPERS.items() if e == entity and fam != "soap"]
        in_v2 = [t for t in models if t in CATALOG.schemas["rest-v2"]]
        if in_v2:
            assert ("rest-v2", entity) in rest, entity
        else:
            # REST v2 has no workflow or step models: v1 serves them (plan §4.3 table).
            assert entity in {"Workflow", "Step"}, entity


def test_mappers_only_name_models_the_catalog_knows() -> None:
    for (family, type_name), _ in MAPPERS.items():
        if family == "soap":
            assert type_name in TABLE.types
        else:
            surfaces = ["rest-v1", "rest-v2"] if family == "rest" else [family]
            assert any(type_name in CATALOG.schemas[s] for s in surfaces), type_name


def test_include_raw_keeps_the_upstream_object_per_entity() -> None:
    fixture = json.loads((FIXTURES / "rest-v1" / "Step.json").read_text())
    body = fixture["body"]
    data, _ = normalize(
        [body, body], surface="rest-v1", type_name="StepData[]", canonical=True, include_raw=True
    )
    assert [d["raw"] for d in data] == [body, body]
    plain, _ = normalize(
        body, surface="rest-v1", type_name="StepData", canonical=True, include_raw=False
    )
    assert "raw" not in plain


def test_task_page_result_becomes_a_list_with_paging_in_meta() -> None:
    body = json.loads((FIXTURES / "rest-v1" / "Task.json").read_text())["body"]
    page = {"Count": 41, "Items": [body], "NextPageLink": "https://ir.example.test/next"}
    data, meta = normalize(
        page,
        surface="rest-v1",
        type_name="PageResultOfTaskModel",
        canonical=True,
        include_raw=False,
    )
    assert data == [expected("Task", "rest-v1")]
    assert meta["paging"] == {"count": 41, "nextPageLink": "https://ir.example.test/next"}


def test_soap_refs_become_plain_ids_like_rest_int64() -> None:
    data, meta = normalize(
        {"Id": "File", "RefId": 4001},
        surface="soap",
        type_name="FileRef",
        canonical=True,
        include_raw=False,
    )
    assert data == 4001
    assert meta["normalization"] == "level2"


def test_operation_calls_stay_level_1() -> None:
    body = json.loads((FIXTURES / "rest-v1" / "Page.json").read_text())["body"]
    data, meta = normalize(
        body, surface="rest-v1", type_name="PageModel", canonical=False, include_raw=True
    )
    assert data == body
    assert meta["normalization"] == "level1"
    assert "Level 1" in meta["rawNote"]


# ---------------------------------------------------------------------------- level 1


def test_dotnet_dates_become_iso_8601() -> None:
    value = {"A": "/Date(1709374500000)/", "B": ["/Date(0+0200)/"], "C": "2024-03-01T00:00:00"}
    assert level1(value) == {
        "A": "2024-03-02T10:15:00+00:00",
        "B": ["1970-01-01T02:00:00+02:00"],
        "C": "2024-03-01T00:00:00",
    }


def test_int64_stays_native_and_null_arrays_become_empty() -> None:
    big = 2**63 - 1
    data, meta = normalize(
        {"Id": big}, surface="rest-v1", type_name="DocumentDataResult", canonical=False,
        include_raw=False,
    )  # fmt: skip
    assert data["Id"] == big
    assert meta["shape"] == "rest-v1:DocumentDataResult"
    empty, _ = normalize(
        None, surface="rest-v1", type_name="PageModel[]", canonical=True, include_raw=False
    )
    assert empty == []


def test_empty_notes_container_is_kept_with_a_note() -> None:
    data, meta = normalize(
        [{"Id": -1, "Notes": []}],
        surface="rest-v1",
        type_name="NotesContainer[]",
        canonical=True,
        include_raw=False,
    )
    assert data == [{"Id": -1, "Notes": []}]
    assert "Id -1" in meta["note"]
