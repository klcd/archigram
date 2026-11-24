"""Performance benchmark suite for Gate 4 and spanning-tree fallback on 300-node/1200-edge IR."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import EdgeRelation, IREdge, IRNode, NodeKind, StructuralGraphIR
from archigram.domain.spec import (
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.lint_budget import (
    CognitiveBudgetAndOrphanGate,
    apply_spanning_tree_edge_fallback,
)


def _build_dense_graph(node_count: int = 300, edge_count: int = 1200) -> StructuralGraphIR:
    prov = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_lint_budget",
        input_sha256="b" * 64,
    )
    nodes = {
        f"node_{idx:03d}": IRNode(
            symbol_id=f"node_{idx:03d}",
            label=f"Service {idx:03d}",
            kind=NodeKind.SERVICE,
            provenance=prov,
        )
        for idx in range(node_count)
    }
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        src_idx = e_idx % node_count
        stride = 1 + (e_idx // node_count)
        dst_idx = (src_idx + stride) % node_count
        edges.append(
            IREdge(
                edge_id=f"edge_{e_idx:04d}",
                source=f"node_{src_idx:03d}",
                target=f"node_{dst_idx:03d}",
                relation=EdgeRelation.CALLS,
                weight=float((e_idx % 50) + 1),
                provenance=prov,
            )
        )
    return StructuralGraphIR(graph_id="dense_300_1200", nodes=nodes, edges=tuple(edges))


_DENSE_GRAPH = _build_dense_graph(300, 1200)
_GATE = CognitiveBudgetAndOrphanGate()
_SPEC_SPANNING = CurationSpecV1(
    views=(
        DiagramViewSpec(
            id="dense_view",
            title="Dense View",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=TargetRenderer.MERMAID,
            invariants=ViewInvariants(
                node_budget=BudgetRange(min_count=1, max_count=350),
                edge_budget_max=320,
                overflow_strategy="spanning_tree_table",
            ),
        ),
    )
)


def _run_gate_and_fallback() -> tuple[bool, int, int]:
    report = _GATE.check(_DENSE_GRAPH, _SPEC_SPANNING)
    reduced, table = apply_spanning_tree_edge_fallback(_DENSE_GRAPH, 320)
    return report.passed, len(reduced.edges), len(table)


def test_gate4_and_spanning_tree_300_nodes_1200_edges(benchmark: Any) -> None:
    passed, kept_edges, table_len = benchmark(_run_gate_and_fallback)
    assert passed
    assert kept_edges == 320
    assert table_len > 0


def test_spanning_tree_fallback_300_nodes_1200_edges(benchmark: Any) -> None:
    reduced, table = benchmark(apply_spanning_tree_edge_fallback, _DENSE_GRAPH, 320)
    assert len(reduced.edges) == 320
    assert len(table.splitlines()) == 2 + (1200 - 320)
