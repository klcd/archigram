"""Performance benchmarks for AG-03: the committed Python quickstart `apply --check` is CI-cheap."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from archigram.entrypoints.cli.app import ApplyOptions, execute_apply

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "python-quickstart"
_CI_OVERHEAD_BUDGET_SECONDS = 5.0


def _options() -> ApplyOptions:
    return ApplyOptions(
        input_path=_EXAMPLE / "shop",
        spec_path=_EXAMPLE / "diagram-views.yaml",
        out_dir=_EXAMPLE / "diagrams",
        check_only=True,
    )


def test_perf_quickstart_apply_check(benchmark: Any) -> None:
    """Benchmark `execute_apply --check` (AST ingest + 5 gates + byte compare) on the example."""
    options = _options()

    outcome = benchmark(lambda: execute_apply(options))

    assert outcome.is_clean
    assert outcome.compilation.lint_report.passed
    assert len(outcome.compilation.views) == 1


def test_perf_quickstart_apply_check_within_ci_budget(benchmark: Any) -> None:
    """A single cold-ish check stays far below the 5s CI overhead target."""
    options = _options()

    def _timed() -> float:
        start = time.perf_counter()
        outcome = execute_apply(options)
        elapsed = time.perf_counter() - start
        assert outcome.is_clean
        return elapsed

    elapsed = benchmark(_timed)

    assert elapsed < _CI_OVERHEAD_BUDGET_SECONDS
