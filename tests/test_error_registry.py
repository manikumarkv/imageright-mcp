"""Error-registry guarantees (plan §6.4): coverage, append-only stability, internal consistency."""

import json
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog
from imageright_mcp.errors import get_registry, render
from imageright_mcp.errors.registry import TEMPLATE_DEFAULTS

REPO = Path(__file__).resolve().parent.parent
ERRORS = REPO / "data" / "errors"
PROFILES = ("7.2", "24.2", "25.1")
SNAPSHOT = Path(__file__).parent / "snapshots" / "ir-codes.json"

# Family ranges from plan §6.1 -> category.
FAMILIES = (
    (1000, 1999, "config"),
    (2000, 2999, "auth"),
    (3000, 3999, "catalog"),
    (4000, 4099, "not-found"),
    (4100, 4199, "invalid"),
    (4200, 4299, "permission"),
    (4300, 4399, "concurrency"),
    (4400, 4499, "state"),
    (5000, 5999, "upstream"),
    (9000, 9999, "internal"),
)


@cache
def load(name: str) -> Any:
    return json.loads((ERRORS / name).read_text(encoding="utf-8"))


def registry_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = load("registry.json")["codes"]
    return entries


def native_codes(profile: str) -> dict[str, dict[str, str]]:
    codes: dict[str, dict[str, str]] = load(f"native-rest.{profile}.json")["codes"]
    return codes


# ---------------------------------------------------------------- coverage


@pytest.mark.parametrize("profile", PROFILES)
def test_every_native_code_maps_to_exactly_one_ir_code(profile: str) -> None:
    owners = Counter(
        int(code) for e in registry_entries() for code in e["mappings"]["rest"]["errorCodes"]
    )
    codes = native_codes(profile)
    assert codes, profile
    unmapped = sorted(int(c) for c in codes if owners[int(c)] == 0)
    duplicated = sorted(int(c) for c in codes if owners[int(c)] > 1)
    assert unmapped == [], f"{profile}: native codes without an IR code"
    assert duplicated == [], f"{profile}: native codes mapped more than once"


def test_registry_maps_only_real_native_codes() -> None:
    known = {int(c) for p in PROFILES for c in native_codes(p)}
    mapped = {int(c) for e in registry_entries() for c in e["mappings"]["rest"]["errorCodes"]}
    assert mapped - known == set()


def test_native_files_match_the_catalog_dictionary() -> None:
    catalog = json.loads((REPO / "data" / "catalog" / "errors.json").read_text())["rest"]
    for profile in PROFILES:
        codes = native_codes(profile)
        expected = {k for k, v in catalog.items() if profile in v["profiles"]}
        assert set(codes) == expected
        assert load(f"native-rest.{profile}.json")["count"] == len(codes)
    assert "917" in native_codes("25.1")
    assert "917" not in native_codes("24.2")
    assert "917" not in native_codes("7.2")


def test_collapse_ratio_matches_plan() -> None:
    native = {c for p in PROFILES for c in native_codes(p)}
    assert len(native) > 200
    assert 55 <= len(registry_entries()) <= 90


# ---------------------------------------------------------------- stability (append-only)


def test_registry_is_append_only() -> None:
    snapshot: dict[str, str] = json.loads(SNAPSHOT.read_text())["codes"]
    current = {e["code"]: e["name"] for e in registry_entries()}
    removed = sorted(set(snapshot) - set(current))
    renamed = sorted(c for c in snapshot if c in current and current[c] != snapshot[c])
    assert removed == [], "IR codes may be deprecated but never removed"
    assert renamed == [], "IR codes may never be renamed or reused"
    added = sorted(set(current) - set(snapshot))
    assert added == [], f"new IR codes {added}: append them to {SNAPSHOT.name}"


def test_names_are_unique_and_codes_well_formed() -> None:
    entries = registry_entries()
    codes = [e["code"] for e in entries]
    names = [e["name"] for e in entries]
    assert len(set(codes)) == len(codes)
    assert len({n.lower() for n in names}) == len(names)
    assert codes == sorted(codes)
    for code in codes:
        assert re.fullmatch(r"IR-\d{4}", code)
        assert not code.startswith("IR-6"), "IR-6xxx is reserved for phase 2"


def test_deprecated_codes_keep_their_slot() -> None:
    for entry in registry_entries():
        if entry.get("deprecated"):
            assert entry["mappings"]["rest"]["errorCodes"] == [], entry["code"]


# ---------------------------------------------------------------- shape and text


@pytest.mark.parametrize("entry", registry_entries(), ids=lambda e: e["code"])
def test_entry_shape_and_family(entry: dict[str, Any]) -> None:
    assert set(entry) >= {
        "code",
        "name",
        "category",
        "retryable",
        "message",
        "hint",
        "mappings",
        "since",
    }
    number = int(entry["code"][3:])
    family = next(cat for low, high, cat in FAMILIES if low <= number <= high)
    assert entry["category"] == family
    assert isinstance(entry["retryable"], bool)
    assert set(entry["mappings"]) == {"rest", "http", "soap"}
    for text in (entry["message"], entry["hint"]):
        assert text.strip()
        assert text == text.strip()
        placeholders = set(re.findall(r"\{(\w+)\}", text))
        assert placeholders <= set(TEMPLATE_DEFAULTS), placeholders
        assert "{" not in render(text)


def test_warning_codes_are_marked() -> None:
    warnings = {e["code"] for e in registry_entries() if e.get("severity") == "warning"}
    assert warnings == {"IR-1005", "IR-3003"}


def test_every_ir_literal_in_the_source_is_registered() -> None:
    registry = get_registry()
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for code, name in re.findall(r'"(IR-\d{4})",\s*"([A-Z]\w+)"', text):
            assert code in registry.entries, f"{path.name}: {code}"
            assert registry.entry(code)["name"] == name, f"{path.name}: {code} {name}"
        for code in re.findall(r'"(IR-\d{4})"', text):
            assert code in registry.entries, f"{path.name}: {code}"


# ---------------------------------------------------------------- SOAP data


def test_soap_patterns_agree_with_registry() -> None:
    faults = load("soap-faults.json")
    from_file = [(p["pattern"], p["code"]) for p in faults["patterns"]]
    from_registry = {
        (pattern, e["code"])
        for e in registry_entries()
        for pattern in e["mappings"]["soap"]["faultPatterns"]
    }
    assert len(from_file) == len(set(from_file))
    assert set(from_file) == from_registry
    for pattern, _ in from_file:
        re.compile(pattern)


def test_result_failures_point_at_real_soap_operations() -> None:
    catalog = get_catalog()
    registry = get_registry()
    for op_id, rule in registry.result_failures.items():
        assert catalog.ops[op_id]["surface"] == "soap"
        assert rule["code"] in registry.entries
        assert rule["when"] in {"null", "false", "falseField"}
    assert registry.result_failures["soap.FindUserByName"]["code"] == "IR-4002"
    assert registry.result_failures["soap.DeleteDocument"]["field"] == "Succeeded"


def test_http_statuses_map_as_planned() -> None:
    registry = get_registry()
    assert registry.for_http(401) == ["IR-2004", "IR-2005"]
    assert registry.for_http(403) == ["IR-4201"]
    assert registry.for_http(404) == ["IR-4001"]
    assert registry.for_http(500) == ["IR-5001"]
    assert registry.for_http(202) == ["IR-5006"]
