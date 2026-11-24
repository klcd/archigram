"""Performance benchmark suite for the 3-Turn MCP Digest + Propose + 5-Gate Lint-Repair Loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    FullScope,
    IRMode,
    PruneLeavesRule,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.mcp import (
    ProposeResult,
    repair_curation_spec_from_diagnostics,
    run_propose_loop,
)

if TYPE_CHECKING:
    from archigram.domain.diagnostics import LintDiagnostic

_BENCH_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_mcp_repair_loop",
    input_sha256="d" * 64,
    file_path="src/service.py",
    line_span=(1, 120),
)


def _build_250_node_structural_graph(node_count: int = 250) -> StructuralGraphIR:
    """Build a 250-node layered structural graph with roots, internal bridges, and leaf sinks."""
    nodes: dict[str, IRNode] = {}
    for idx in range(node_count):
        cluster_idx = idx // 25
        role = "sink" if idx % 5 == 4 else ("root" if idx % 5 == 0 else "bridge")
        sym_id = f"pkg.c{cluster_idx:02d}.{role}_{idx:03d}"
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Node {idx}",
            kind=NodeKind.SERVICE if role != "sink" else NodeKind.FUNCTION,
            provenance=_BENCH_STAMP,
        )

    sym_ids = list(nodes)
    edges: list[IREdge] = []
    edge_idx = 0
    for idx in range(node_count):
        if idx % 5 == 4:
            # Keep every 5th node as a pure leaf sink (out_degree == 0)
            continue
        next_idx = idx + 1 if (idx + 1) < node_count else idx - 1
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=sym_ids[idx],
                target=sym_ids[next_idx],
                relation=EdgeRelation.CALLS,
                weight=2.0,
                provenance=_BENCH_STAMP,
            )
        )
        edge_idx += 1

        sink_idx = (idx // 5) * 5 + 4
        if sink_idx < node_count and sink_idx != next_idx:
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=sym_ids[idx],
                    target=sym_ids[sink_idx],
                    relation=EdgeRelation.CALLS,
                    weight=1.0,
                    provenance=_BENCH_STAMP,
                )
            )
            edge_idx += 1

    return StructuralGraphIR(
        graph_id="bench_mcp_250",
        nodes=nodes,
        edges=tuple(edges),
    )


class _ThreeTurnBenchProposer:
    """Fault-injecting proposer exercising all 3 turns on the 250-node StructuralGraphIR."""

    def __init__(self, ir: StructuralGraphIR) -> None:
        self._ir = ir
        self._root = "pkg.c00.root_000"
        self._bridge = "pkg.c00.bridge_001"
        self._sink = "pkg.c00.sink_004"

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        _ = digest_markdown
        if turn == 1:
            spec_t1 = CurationSpecV1(
                views=(
                    DiagramViewSpec(
                        id="bench_view",
                        title="Benchmark View",
                        ir_mode=IRMode.STRUCTURAL_GRAPH,
                        target_renderer=TargetRenderer.MERMAID,
                        invariants=ViewInvariants(
                            protected_nodes=(f"{self._root}_typoz",),
                            node_budget=BudgetRange(min_count=1, max_count=300),
                            edge_budget_max=600,
                        ),
                    ),
                ),
            )
            return dump_curation_spec_yaml(spec_t1)

        if turn == 2:
            spec_t2 = CurationSpecV1(
                views=(
                    DiagramViewSpec(
                        id="bench_view",
                        title="Benchmark View",
                        ir_mode=IRMode.STRUCTURAL_GRAPH,
                        target_renderer=TargetRenderer.MERMAID,
                        rules=(
                            PruneLeavesRule(
                                id="prune_internal_bridge_err",
                                node_id_globs=(self._bridge,),
                                direction="sink",
                            ),
                        ),
                        scope=FullScope(),
                        invariants=ViewInvariants(
                            protected_nodes=(self._root,),
                            node_budget=BudgetRange(min_count=1, max_count=300),
                            edge_budget_max=600,
                        ),
                    ),
                ),
            )
            return dump_curation_spec_yaml(spec_t2)

        repaired = repair_curation_spec_from_diagnostics(
            prior_spec_yaml,
            diagnostics,
            self._ir,
        )
        return dump_curation_spec_yaml(repaired)


_GRAPH_250 = _build_250_node_structural_graph(250)
_BENCH_PROPOSER = _ThreeTurnBenchProposer(_GRAPH_250)


def test_3_turn_mcp_repair_loop_250_nodes_benchmark(benchmark: Any) -> None:
    """Benchmark a full 3-turn digest + propose + 5-gate lint-repair cycle on a 250-node graph."""

    def _run() -> ProposeResult:
        return run_propose_loop(_GRAPH_250, _BENCH_PROPOSER, max_turns=3)

    result = benchmark(_run)
    assert result.succeeded
    assert result.turns_used == 3
    assert result.final_report.passed
