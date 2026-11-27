"""Performance benchmarks: linting every agent-move example is cheap enough to run on each push."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    load_canonical_ir,
    load_curation_spec,
)

if TYPE_CHECKING:
    from archigram.domain.spec import CurationSpecV1

_ROOT = Path(__file__).resolve().parents[2]
_MOVES = _ROOT / "examples" / "agent-moves"
_SHOP = _ROOT / "examples" / "python-quickstart" / "shop"
_CI_OVERHEAD_BUDGET_SECONDS = 5.0


def _loadable_specs() -> list[CurationSpecV1]:
    specs: list[CurationSpecV1] = []
    for path in sorted(_MOVES.glob("*.yaml")):
        try:
            specs.append(load_curation_spec(path))
        except ValidationError:
            continue  # the "draw an arrow" move: refused by the schema before any gate
    return specs


def _verdicts() -> list[bool]:
    observed = load_canonical_ir(_SHOP)
    return [
        compile_and_lint_pipeline(observed, spec).lint_report.passed for spec in _loadable_specs()
    ]


def test_perf_agent_moves_lint_all(benchmark: Any) -> None:
    """Benchmark ingest + 5 gates for every loadable move: 4 pass (start + 3 allowed), 3 fail."""
    verdicts = benchmark(_verdicts)

    assert verdicts == [True, True, True, True, False, False, False]


def test_perf_agent_moves_within_ci_budget(benchmark: Any) -> None:
    """Linting all moves, schema load included, stays far below the 5s CI overhead target."""

    def _timed() -> float:
        start = time.perf_counter()
        _verdicts()
        return time.perf_counter() - start

    elapsed = benchmark(_timed)

    assert elapsed < _CI_OVERHEAD_BUDGET_SECONDS
