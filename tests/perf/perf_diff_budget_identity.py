"""Performance benchmark for budget-aware diagnostic diffing over large diagnostic sets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archigram.domain.diagnostics import DiagnosticSeverity, GateId, LintDiagnostic
from archigram.entrypoints.cli.diff_report import diff_diagnostics

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture


def _make(count: int, shift: int) -> tuple[LintDiagnostic, ...]:
    return tuple(
        LintDiagnostic(
            gate=GateId.GATE_4_BUDGET_ORPHAN,
            severity=DiagnosticSeverity.ERROR,
            code="EDGE_BUDGET_EXCEEDED",
            message=f"edge count ({100 + shift + i}) exceeds budget",
            view_id=f"view_{i}",
            offending_symbols=(f"e{i}_{shift}",),
            repair_hint="reduce",
        )
        for i in range(count)
    )


def test_perf_budget_diff_1000_views(benchmark: BenchmarkFixture) -> None:
    base = _make(1000, 0)
    head = _make(1000, 3)
    added, resolved = benchmark(diff_diagnostics, base, head, DiagnosticSeverity.ERROR)
    assert added == ()
    assert resolved == ()
