"""Performance benchmarks for the full 500-node structural topological reduction pipeline."""

from __future__ import annotations

from typing import Any

import rustworkx as rx

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
    BypassVerticesRule,
    CollapsePartitionRule,
    DiagramViewSpec,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    SimplifyConfig,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.reduce_structural import StructuralTopologicalReducer

_OBSERVED = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf-fixture",
    input_sha256="b" * 64,
)


def _build_500_nodes() -> dict[str, IRNode]:
    """Create 10 packages, 90 services, 150 wrappers, 100 leaf sinks, and 150 storage nodes."""
    nodes: dict[str, IRNode] = {}
    for pkg_idx in range(10):
        pkg_id = f"pkg_{pkg_idx:02d}"
        nodes[pkg_id] = IRNode(
            symbol_id=pkg_id,
            label=pkg_id,
            kind=NodeKind.PACKAGE,
            provenance=_OBSERVED,
        )

    groups: tuple[tuple[str, int, NodeKind], ...] = (
        ("svc", 90, NodeKind.SERVICE),
        ("wrap", 150, NodeKind.FUNCTION),
        ("leaf", 100, NodeKind.MODULE),
        ("store", 150, NodeKind.TABLE),
    )
    for prefix, count, kind in groups:
        for idx in range(count):
            nid = f"{prefix}_{idx:03d}"
            nodes[nid] = IRNode(
                symbol_id=nid,
                label=nid,
                kind=kind,
                parent_id=f"pkg_{idx % 10:02d}",
                provenance=_OBSERVED,
            )
    return nodes


def _edge(
    seq: int,
    source: str,
    target: str,
    relation: EdgeRelation = EdgeRelation.CALLS,
    sync_kind: SyncKind = SyncKind.SYNC,
) -> IREdge:
    return IREdge(
        edge_id=f"e_{seq:05d}",
        source=source,
        target=target,
        relation=relation,
        sync_kind=sync_kind,
        provenance=_OBSERVED,
    )


def _add_backbone_and_scc_edges(edges: list[IREdge]) -> None:
    for idx in range(89):
        edges.append(_edge(len(edges), f"svc_{idx:03d}", f"svc_{idx + 1:03d}"))
        if idx + 2 < 90:
            edges.append(_edge(len(edges), f"svc_{idx:03d}", f"svc_{idx + 2:03d}"))
    for base in (10, 25, 40, 55, 70):
        edges.append(_edge(len(edges), f"svc_{base + 2:03d}", f"svc_{base:03d}"))


def _add_wrapper_and_leaf_edges(edges: list[IREdge]) -> None:
    sync_cycle = (SyncKind.SYNC, SyncKind.ASYNC_FIRE_FORGET, SyncKind.FORK_JOIN_BRANCH)
    for idx in range(150):
        src_svc = f"svc_{idx % 85:03d}"
        dst_svc = f"svc_{(idx % 85) + 3:03d}"
        wid = f"wrap_{idx:03d}"
        edges.append(_edge(len(edges), src_svc, wid, sync_kind=sync_cycle[idx % 3]))
        edges.append(_edge(len(edges), wid, dst_svc))
    for idx in range(100):
        edges.append(
            _edge(len(edges), f"svc_{idx % 90:03d}", f"leaf_{idx:03d}", EdgeRelation.IMPORTS)
        )


def _add_storage_edges(edges: list[IREdge]) -> None:
    for idx in range(150):
        edges.append(
            _edge(len(edges), f"svc_{idx % 90:03d}", f"store_{idx:03d}", EdgeRelation.WRITES_DATA)
        )
        if idx + 1 < 150:
            edges.append(
                _edge(len(edges), f"store_{idx:03d}", f"store_{idx + 1:03d}", EdgeRelation.IMPORTS)
            )


def _build_500_node_graph() -> StructuralGraphIR:
    nodes = _build_500_nodes()
    edges: list[IREdge] = []
    _add_backbone_and_scc_edges(edges)
    _add_wrapper_and_leaf_edges(edges)
    _add_storage_edges(edges)
    return StructuralGraphIR(graph_id="perf_500", nodes=nodes, edges=tuple(edges))


def _build_full_pipeline_view() -> DiagramViewSpec:
    return DiagramViewSpec(
        id="perf_structural_pipeline",
        title="Full 500-Node Structural Reduction Pipeline",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        scope=KHopNeighborhoodScope(
            focal_nodes=("svc_000", "svc_045"),
            upstream_depth=20,
            downstream_depth=25,
        ),
        rules=(
            PruneLeavesRule(
                id="prune_leaf_modules",
                node_id_globs=("leaf_*",),
                direction="sink",
            ),
            BypassVerticesRule(
                id="bypass_wrappers",
                node_id_globs=("wrap_*",),
                preserve_concurrency_kind=True,
                max_fanout_product=16,
            ),
            CollapsePartitionRule(
                id="collapse_storage_cluster",
                target_node_id="storage_supernode",
                label="Storage Cluster",
                summary="Rolled-up storage tables",
                member_globs=("store_*",),
            ),
        ),
        simplify=SimplifyConfig(condense_sccs=True, transitive_reduction=True),
        invariants=ViewInvariants(forbid_new_cycles=True),
    )


def _is_dag(ir: StructuralGraphIR) -> bool:
    graph: rx.PyDiGraph[str, None] = rx.PyDiGraph(multigraph=False)
    idx_of = {nid: graph.add_node(nid) for nid in sorted(ir.nodes)}
    seen: set[tuple[int, int]] = set()
    for edge in ir.edges:
        pair = (idx_of[edge.source], idx_of[edge.target])
        if pair not in seen:
            seen.add(pair)
            graph.add_edge(pair[0], pair[1], None)
    return rx.is_directed_acyclic_graph(graph)


def test_perf_full_structural_reduction_pipeline_500_nodes(benchmark: Any) -> None:
    """Benchmark k-hop + prune + bypass + collapse + SCC + TR on a 500-node graph."""
    ir = _build_500_node_graph()
    assert len(ir.nodes) == 500

    view = _build_full_pipeline_view()
    reducer = StructuralTopologicalReducer()

    reduced: StructuralGraphIR = benchmark(reducer.reduce, ir, view)

    assert len(reduced.nodes) < len(ir.nodes)
    assert len(reduced.edges) < len(ir.edges)
    assert "storage_supernode" in reduced.nodes
    assert reduced.nodes["storage_supernode"].kind is NodeKind.SUPERNODE
    assert not any(nid.startswith("leaf_") for nid in reduced.nodes)
    assert not any(nid.startswith("wrap_") for nid in reduced.nodes)
    assert _is_dag(reduced)
