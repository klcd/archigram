"""Performance benchmarks for AG-05 PR diff: high-churn structural diff and unavailable base."""

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
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.entrypoints.cli.diff_report import (
    PrDiffSummary,
    RelationshipStatus,
    compute_pr_regression_diff,
)

_NODE_COUNT = 320
_CHAIN_LEN = 6


def _stamp() -> ProvenanceStamp:
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_pr_diff",
        input_sha256="d" * 64,
        file_path="enterprise.json",
    )


def _build_graph(*, head: bool) -> StructuralGraphIR:
    """320 nodes / 630+ edges; head flips every bulk relation and severs one declared edge."""
    stamp = _stamp()
    nodes = {
        f"n{idx:03d}": IRNode(
            symbol_id=f"n{idx:03d}",
            label=f"Node {idx}",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        )
        for idx in range(_NODE_COUNT)
    }
    bulk_relation = EdgeRelation.IMPORTS if head else EdgeRelation.CALLS
    edges: list[IREdge] = []
    for idx in range(_NODE_COUNT - 1):
        is_chain = idx < _CHAIN_LEN - 1
        if is_chain and head and idx == _CHAIN_LEN - 2:
            continue  # the single broken declared dependency
        edges.append(
            IREdge(
                edge_id=f"a{idx}",
                source=f"n{idx:03d}",
                target=f"n{idx + 1:03d}",
                relation=EdgeRelation.CALLS if is_chain else bulk_relation,
                sync_kind=SyncKind.SYNC,
                provenance=stamp,
            )
        )
    for idx in range(_CHAIN_LEN, _NODE_COUNT - 2):
        edges.append(
            IREdge(
                edge_id=f"b{idx}",
                source=f"n{idx:03d}",
                target=f"n{idx + 2:03d}",
                relation=bulk_relation,
                sync_kind=SyncKind.SYNC,
                provenance=stamp,
            )
        )
    return StructuralGraphIR(graph_id="perf_pr_diff", nodes=nodes, edges=tuple(edges))


def _build_spec() -> CurationSpecV1:
    invariants = ViewInvariants(
        require_reachability=(
            ReachabilityAssertion(from_node="n000", to_node=f"n{_CHAIN_LEN - 1:03d}"),
        ),
        node_budget=BudgetRange(min_count=1, max_count=1000),
        edge_budget_max=2000,
    )
    return CurationSpecV1(
        views=tuple(
            DiagramViewSpec(
                id=view_id,
                title=view_id,
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderer,
                invariants=invariants,
            )
            for view_id, renderer in (
                ("arch_mermaid", TargetRenderer.MERMAID),
                ("arch_d2", TargetRenderer.D2),
            )
        )
    )


def test_perf_pr_diff_high_churn_300_nodes_600_edges(benchmark: Any) -> None:
    """Benchmark base-vs-head diff of a 320-node / 630-edge graph with ~1260 edge changes."""
    base = _build_graph(head=False)
    head = _build_graph(head=True)
    spec = _build_spec()
    assert len(base.nodes) >= 300
    assert len(base.edges) >= 600

    summary: PrDiffSummary = benchmark(compute_pr_regression_diff, base, head, spec)
    assert not summary.passed
    assert summary.base_available is True
    assert summary.edge_churn.bulk_edges_changed > 1000
    assert summary.declared_changes[0].status is RelationshipStatus.NEWLY_BROKEN
    assert summary.new_errors[0].code == "REACHABILITY_NOT_OBSERVED"
    assert set(summary.affected_view_ids) == {"arch_mermaid", "arch_d2"}
    assert len(summary.markdown_report.splitlines()) < 45


def test_perf_pr_diff_unavailable_base(benchmark: Any) -> None:
    """Benchmark head-only validation when the base input is unavailable."""
    head = _build_graph(head=True)
    spec = _build_spec()

    summary: PrDiffSummary = benchmark(
        compute_pr_regression_diff,
        None,
        head,
        spec,
        base_unavailable_reason="base ref not found",
    )
    assert summary.base_available is False
    assert summary.base_unavailable_reason == "base ref not found"
    assert not summary.passed
    assert summary.declared_changes[0].status is RelationshipStatus.FAILING
