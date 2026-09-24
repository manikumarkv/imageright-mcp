"""Search-quality eval (plan §8, M2 gate): expected operations must rank in the top N.

Every reported bad query gets a case in tests/eval/cases.json before the ranker is changed.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.catalog import get_catalog

CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "eval" / "cases.json").read_text(encoding="utf-8")
)
PASS_RATE = 0.9


def _top(case: dict[str, Any]) -> list[str]:
    catalog = get_catalog()
    profile, _ = catalog.resolve_version(case.get("version"), catalog.baseline)
    answer = catalog.search(
        case["query"],
        profile,
        surface=case.get("surface"),
        limit=case.get("top", 3),
    )
    return [r["operationId"] for r in answer.data["results"]]


def _passes(case: dict[str, Any]) -> bool:
    top = _top(case)
    if not case["expect"]:
        return top == []
    return any(op in top for op in case["expect"])


def test_eval_cases_reference_real_operations() -> None:
    ops = get_catalog().ops
    for case in CASES:
        for op_id in case["expect"]:
            assert op_id in ops, f"{case['query']!r} expects unknown {op_id}"


def test_search_eval_pass_rate() -> None:
    failures = [c["query"] for c in CASES if not _passes(c)]
    rate = 1 - len(failures) / len(CASES)
    print(
        f"search eval: {len(CASES) - len(failures)}/{len(CASES)} ({rate:.0%}); failing: {failures}"
    )
    assert rate >= PASS_RATE, f"pass rate {rate:.0%} < {PASS_RATE:.0%}; failing: {failures}"


@pytest.mark.parametrize("case", CASES, ids=[c["query"] for c in CASES])
def test_search_eval_case(case: dict[str, Any]) -> None:
    """Per-case view for debugging; strict failures are only enforced by the pass-rate gate."""
    if not _passes(case):
        pytest.xfail(f"top {case.get('top', 3)} = {_top(case)}")
