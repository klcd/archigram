"""Performance benchmark suite for Gates 1 & 2 over a 500-node IR and 20-rule CurationSpecV1."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.lint_grounding import GroundingAndBoundaryGate

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf-lint-grounding",
    input_sha256="b" * 64,
)


def _build_500_node_structural_ir() -> StructuralGraphIR:
    """Build a 500-node StructuralGraphIR with 100 roots, 300 bridges, and 100 leaf sinks."""
    nodes: dict[str, IRNode] = {}
    edges: list[IREdge] = []
    edge_idx = 0

    for idx in range(100):
        sym = f"domain.roots.root_{idx:03d}"
        nodes[sym] = IRNode(
            symbol_id=sym,
            label=f"Root {idx}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )

    for idx in range(300):
        grp = idx // 30
        sym = f"domain.bridges.g{grp:02d}.bridge_{idx:03d}"
        nodes[sym] = IRNode(
            symbol_id=sym,
            label=f"Bridge {idx}",
            kind=NodeKind.COMPONENT,
            provenance=_OBS_PROV,
        )
        root_sym = f"domain.roots.root_{idx % 100:03d}"
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=root_sym,
                target=sym,
                relation=EdgeRelation.CALLS,
                provenance=_OBS_PROV,
            )
        )
        edge_idx += 1

    for idx in range(100):
        grp = idx // 10
        sym = f"domain.sinks.s{grp:02d}.sink_{idx:03d}"
        nodes[sym] = IRNode(
            symbol_id=sym,
            label=f"Sink {idx}",
            kind=NodeKind.DATABASE,
            provenance=_OBS_PROV,
        )
        bridge_sym = f"domain.bridges.g{(idx % 300) // 30:02d}.bridge_{idx % 300:03d}"
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=bridge_sym,
                target=sym,
                relation=EdgeRelation.WRITES_DATA,
                provenance=_OBS_PROV,
            )
        )
        edge_idx += 1

    return StructuralGraphIR(
        graph_id="perf_500_nodes",
        nodes=nodes,
        edges=tuple(edges),
    )


def _build_20_rule_curation_spec() -> CurationSpecV1:
    """Build a 20-rule CurationSpecV1 exercising globs, leaf pruning, bypass, and collapse."""
    global_rules = tuple(
        PruneLeavesRule(
            id=f"global_prune_s{grp:02d}",
            node_id_globs=(f"domain.sinks.s{grp:02d}.*",),
            direction="sink",
        )
        for grp in range(5)
    )

    view_0_rules = tuple(
        BypassVerticesRule(
            id=f"v0_bypass_g{grp:02d}",
            node_id_globs=(f"domain.bridges.g{grp:02d}.*",),
            preserve_edge_relations=("calls", "writes_data"),
        )
        for grp in range(5)
    )

    view_1_rules = (
        *(
            CollapsePartitionRule(
                id=f"v1_collapse_g{grp:02d}",
                target_node_id=f"super.bridge_group_{grp:02d}",
                label=f"Bridge Group {grp}",
                member_globs=(f"domain.bridges.g{grp:02d}.**",),
            )
            for grp in range(5, 10)
        ),
        *(
            PruneLeavesRule(
                id=f"v1_prune_s{grp:02d}",
                node_id_globs=(f"domain.sinks.s{grp:02d}.*",),
                direction="sink",
            )
            for grp in range(5, 10)
        ),
    )

    views = (
        DiagramViewSpec(
            id="perf_view_0",
            title="Perf View 0",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=TargetRenderer.MERMAID,
            rules=view_0_rules,
            invariants=ViewInvariants(
                protected_nodes=("domain.roots.root_000", "domain.roots.root_001"),
                preserve_reachability=(
                    ReachabilityAssertion(
                        from_node="domain.roots.root_000",
                        to_node="domain.bridges.g05.bridge_150",
                        via_relations=("calls",),
                    ),
                ),
            ),
        ),
        DiagramViewSpec(
            id="perf_view_1",
            title="Perf View 1",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=TargetRenderer.D2,
            rules=view_1_rules,
            scope=KHopNeighborhoodScope(
                focal_nodes=("domain.roots.root_000", "super.bridge_group_05"),
                upstream_depth=2,
                downstream_depth=2,
            ),
            invariants=ViewInvariants(
                protected_nodes=("domain.roots.root_000", "super.bridge_group_05"),
            ),
        ),
    )

    return CurationSpecV1(
        global_abstractions=global_rules,
        views=views,
    )


_IR_500 = _build_500_node_structural_ir()
_SPEC_20 = _build_20_rule_curation_spec()
_GATE = GroundingAndBoundaryGate()


def test_gates_1_and_2_500_nodes_20_rules_benchmark(benchmark: Any) -> None:
    """Benchmark Gates 1 & 2 validation over a 500-node StructuralGraphIR and 20-rule spec."""
    assert len(_IR_500.nodes) == 500
    total_rules = len(_SPEC_20.global_abstractions) + sum(len(v.rules) for v in _SPEC_20.views)
    assert total_rules == 20

    report = benchmark(_GATE.check, _IR_500, _SPEC_20)
    assert report.passed
    assert report.error_count == 0
