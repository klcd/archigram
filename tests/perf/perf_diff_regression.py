"""Performance benchmark for baseline-vs-head regression diff with pre-existing errors."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
)
from archigram.domain.spec import (
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    ViewInvariants,
)
from archigram.entrypoints.cli.diff_report import compute_pr_regression_diff


def _chain(node_count: int) -> StructuralGraphIR:
    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_diff_regression",
        input_sha256="d" * 64,
        file_path="arch.dot",
    )
    ids = [f"svc_{idx:03d}" for idx in range(node_count)]
    return StructuralGraphIR(
        graph_id="perf_diff",
        nodes={
            nid: IRNode(symbol_id=nid, label=nid, kind=NodeKind.SERVICE, provenance=stamp)
            for nid in ids
        },
        edges=tuple(
            IREdge(
                edge_id=f"e_{idx}",
                source=ids[idx],
                target=ids[idx + 1],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                weight=1.0,
                provenance=stamp,
            )
            for idx in range(node_count - 1)
        ),
    )


def test_perf_diff_preexisting_errors_not_regression(benchmark: Any) -> None:
    """Benchmark diff of a 24-node graph carrying an error on both base and head."""
    base = _chain(24)
    head = _chain(24)  # zero-churn: base errors persist unchanged
    spec = CurationSpecV1(
        global_abstractions=(),
        views=(
            DiagramViewSpec(
                id="arch_overview",
                title="Architecture Overview",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                invariants=ViewInvariants(
                    node_budget=BudgetRange(min_count=30, max_count=40),
                    edge_budget_max=60,
                ),
            ),
        ),
    )
    summary = benchmark(compute_pr_regression_diff, base, head, spec)
    assert summary.head_error_count > 0
    assert summary.has_regressions is False
