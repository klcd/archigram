"""Performance benchmarks for Zero-LLM CLI Data Plane end-to-end 200-node compilation + linting."""

from __future__ import annotations

import tempfile
from pathlib import Path
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
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    sync_compiled_artifacts,
)


def _build_200_node_structural_graph() -> tuple[StructuralGraphIR, CurationSpecV1]:
    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_cli_dataplane",
        input_sha256="b" * 64,
        file_path="enterprise.dot",
    )
    nodes: dict[str, IRNode] = {}
    edges: list[IREdge] = []

    tier_count = 10
    nodes_per_tier = 20
    for tier in range(tier_count):
        for idx in range(nodes_per_tier):
            sym_id = f"tier_{tier:02d}.svc_{idx:02d}"
            nodes[sym_id] = IRNode(
                symbol_id=sym_id,
                label=f"Tier {tier} Service {idx}",
                kind=NodeKind.SERVICE,
                provenance=stamp,
            )
            if idx > 0:
                prev_sym = f"tier_{tier:02d}.svc_{idx - 1:02d}"
                edges.append(
                    IREdge(
                        edge_id=f"intra_{tier:02d}_{idx:02d}",
                        source=prev_sym,
                        target=sym_id,
                        relation=EdgeRelation.CALLS,
                        sync_kind=SyncKind.SYNC,
                        weight=1.0,
                        provenance=stamp,
                    )
                )
        if tier > 0:
            prev_hub = f"tier_{tier - 1:02d}.svc_00"
            curr_hub = f"tier_{tier:02d}.svc_00"
            edges.append(
                IREdge(
                    edge_id=f"inter_{tier - 1:02d}_to_{tier:02d}",
                    source=prev_hub,
                    target=curr_hub,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=5.0,
                    provenance=stamp,
                )
            )

    graph = StructuralGraphIR(
        graph_id="perf_200_node_graph",
        nodes=nodes,
        edges=tuple(edges),
    )

    collapse_rules = tuple(
        CollapsePartitionRule(
            id=f"collapse_tier_{tier:02d}",
            target_node_id=f"tier_{tier:02d}.supernode",
            label=f"Tier {tier} Subsystem",
            member_globs=(f"tier_{tier:02d}.*",),
        )
        for tier in range(tier_count)
    )
    invariants = ViewInvariants(
        protected_nodes=("tier_00.supernode", f"tier_{tier_count - 1:02d}.supernode"),
        preserve_reachability=(
            ReachabilityAssertion(
                from_node="tier_00.supernode",
                to_node=f"tier_{tier_count - 1:02d}.supernode",
            ),
        ),
        node_budget=BudgetRange(min_count=1, max_count=25),
        edge_budget_max=40,
    )
    spec = CurationSpecV1(
        global_abstractions=(
            PruneLeavesRule(
                id="prune_leaf_tail",
                node_id_globs=("tier_09.svc_19",),
                direction="sink",
            ),
            *collapse_rules,
        ),
        views=(
            DiagramViewSpec(
                id="arch_mermaid",
                title="200-Node Curated Mermaid View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="arch_d2",
                title="200-Node Curated D2 View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                invariants=invariants,
            ),
        ),
    )
    return graph, spec


def test_perf_cli_apply_and_lint_200_node_mmd_and_d2(benchmark: Any) -> None:
    """Benchmark end-to-end zero-LLM `apply + lint` compilation of 200 nodes into .mmd & .d2."""
    graph, spec = _build_200_node_structural_graph()
    assert len(graph.nodes) == 200

    with tempfile.TemporaryDirectory() as tmp_str:
        out_dir = Path(tmp_str)

        def _run_pipeline() -> bool:
            comp = compile_and_lint_pipeline(graph, spec)
            outcome = sync_compiled_artifacts(comp, out_dir, check_only=False)
            check_outcome = sync_compiled_artifacts(comp, out_dir, check_only=True)
            return outcome.is_clean and check_outcome.is_clean

        is_clean = benchmark(_run_pipeline)
        assert is_clean
        assert (out_dir / "arch_mermaid.mmd").is_file()
        assert (out_dir / "arch_d2.d2").is_file()
