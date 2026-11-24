"""Hypothesis property-based fuzz suite for structural topological reduction operators."""

from __future__ import annotations

from collections import deque
from typing import Literal

import pytest
import rustworkx as rx
from hypothesis import given, settings
from hypothesis import strategies as st

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
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    SimplifyConfig,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.reduce_structural import (
    CycleInducedError,
    StructuralTopologicalReducer,
    bypass_vertices,
    collapse_partition,
    condense_sccs,
    k_hop_neighborhood,
    prune_leaves,
    transitive_reduction,
)
from archigram.ports.contracts import StructuralReducerPort

_OBSERVED_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-oracle",
    input_sha256="a" * 64,
)

_ALL_RELATIONS = tuple(EdgeRelation)
_ALL_SYNC_KINDS = tuple(SyncKind)
_ALL_NODE_KINDS = (
    NodeKind.SERVICE,
    NodeKind.MODULE,
    NodeKind.PACKAGE,
    NodeKind.CLASS,
    NodeKind.FUNCTION,
    NodeKind.COMPONENT,
)


def _reachability_closure(ir: StructuralGraphIR) -> frozenset[tuple[str, str]]:
    """Independent BFS oracle computing all directed reachable pairs (u, v) of length >= 1."""
    adj: dict[str, set[str]] = {nid: set() for nid in ir.nodes}
    for edge in ir.edges:
        adj[edge.source].add(edge.target)

    pairs: set[tuple[str, str]] = set()
    for start in ir.nodes:
        visited: set[str] = set()
        queue: deque[str] = deque(adj[start])
        while queue:
            curr = queue.popleft()
            if curr in visited:
                continue
            visited.add(curr)
            pairs.add((start, curr))
            queue.extend(adj[curr] - visited)
    return frozenset(pairs)


def _is_strict_dag(ir: StructuralGraphIR) -> bool:
    graph: rx.PyDiGraph[str, None] = rx.PyDiGraph(multigraph=False)
    idx_of = {nid: graph.add_node(nid) for nid in sorted(ir.nodes)}
    seen: set[tuple[int, int]] = set()
    for edge in ir.edges:
        if edge.source == edge.target:
            return False
        pair = (idx_of[edge.source], idx_of[edge.target])
        if pair not in seen:
            seen.add(pair)
            graph.add_edge(pair[0], pair[1], None)
    return rx.is_directed_acyclic_graph(graph)


def _make_node(
    symbol_id: str,
    *,
    kind: NodeKind = NodeKind.SERVICE,
    parent_id: str | None = None,
) -> IRNode:
    return IRNode(
        symbol_id=symbol_id,
        label=symbol_id,
        kind=kind,
        parent_id=parent_id,
        provenance=_OBSERVED_STAMP,
    )


def _make_edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    relation: EdgeRelation = EdgeRelation.CALLS,
    sync_kind: SyncKind = SyncKind.SYNC,
    weight: float = 1.0,
) -> IREdge:
    return IREdge(
        edge_id=edge_id,
        source=source,
        target=target,
        relation=relation,
        sync_kind=sync_kind,
        weight=weight,
        attributes={"origin": edge_id},
        provenance=_OBSERVED_STAMP,
    )


@st.composite
def random_structural_graph(
    draw: st.DrawFn,
    *,
    min_nodes: int = 2,
    max_nodes: int = 14,
    allow_cycles: bool = True,
    allow_self_loops: bool = False,
) -> StructuralGraphIR:
    """Generate a valid StructuralGraphIR with containment hierarchy and mixed SyncKinds."""
    count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    node_ids = [f"n_{idx:02d}" for idx in range(count)]

    nodes: dict[str, IRNode] = {}
    for idx, nid in enumerate(node_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        kind = draw(st.sampled_from(_ALL_NODE_KINDS))
        nodes[nid] = _make_node(nid, kind=kind, parent_id=parent_id)

    edge_count = draw(st.integers(min_value=0, max_value=count * 2))
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        src_i = draw(st.integers(min_value=0, max_value=count - 1))
        dst_i = draw(st.integers(min_value=0, max_value=count - 1))
        if not allow_self_loops and src_i == dst_i:
            continue
        if not allow_cycles and src_i >= dst_i:
            continue
        edges.append(
            _make_edge(
                f"e_{e_idx:03d}",
                node_ids[src_i],
                node_ids[dst_i],
                relation=draw(st.sampled_from(_ALL_RELATIONS)),
                sync_kind=draw(st.sampled_from(_ALL_SYNC_KINDS)),
                weight=draw(st.floats(min_value=0.5, max_value=5.0, allow_nan=False)),
            )
        )

    return StructuralGraphIR(graph_id="fuzz_graph", nodes=nodes, edges=tuple(edges))


@st.composite
def random_bypass_scenario(
    draw: st.DrawFn,
) -> tuple[StructuralGraphIR, BypassVerticesRule, frozenset[str]]:
    """Generate a DAG with designated wrapper_* intermediate nodes and mixed SyncKind edges."""
    core_count = draw(st.integers(min_value=2, max_value=6))
    wrap_count = draw(st.integers(min_value=1, max_value=4))
    core_ids = [f"core_{idx}" for idx in range(core_count)]
    wrap_ids = [f"wrap_{idx}" for idx in range(wrap_count)]
    ordered = core_ids[: core_count // 2] + wrap_ids + core_ids[core_count // 2 :]

    nodes: dict[str, IRNode] = {}
    for idx, nid in enumerate(ordered):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = ordered[draw(st.integers(min_value=0, max_value=idx - 1))]
        nodes[nid] = _make_node(nid, parent_id=parent_id)

    edges: list[IREdge] = []
    edge_seq = 0
    for i, src in enumerate(ordered):
        for dst in ordered[i + 1 :]:
            if draw(st.booleans()):
                edges.append(
                    _make_edge(
                        f"e_{edge_seq:03d}",
                        src,
                        dst,
                        relation=draw(st.sampled_from((EdgeRelation.CALLS, EdgeRelation.IMPORTS))),
                        sync_kind=draw(st.sampled_from(_ALL_SYNC_KINDS)),
                    )
                )
                edge_seq += 1

    # Guarantee at least one async path through wrap_0 when possible
    if core_count >= 2 and wrap_ids:
        edges.append(
            _make_edge(
                f"e_{edge_seq:03d}",
                core_ids[0],
                wrap_ids[0],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.ASYNC_FIRE_FORGET,
            )
        )
        edge_seq += 1
        edges.append(
            _make_edge(
                f"e_{edge_seq:03d}",
                wrap_ids[0],
                core_ids[-1],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
            )
        )

    ir = StructuralGraphIR(graph_id="fuzz_bypass", nodes=nodes, edges=tuple(edges))
    rule = BypassVerticesRule(
        id="rule_bypass_wrap",
        node_id_globs=("wrap_*",),
        preserve_edge_relations=(),
        preserve_concurrency_kind=True,
        max_fanout_product=64,
    )
    return ir, rule, frozenset(wrap_ids)


@settings(max_examples=60, deadline=None)
@given(scenario=random_bypass_scenario())
def test_fuzz_bypass_vertices_preserves_reachability_sync_and_provenance(
    scenario: tuple[StructuralGraphIR, BypassVerticesRule, frozenset[str]],
) -> None:
    """Bypass preserves surviving reachability, never upgrades async, and stamps backing_ids."""
    ir, rule, _ = scenario
    original_reach = _reachability_closure(ir)
    reduced = bypass_vertices(ir, rule)
    reduced_reach = _reachability_closure(reduced)

    surviving = set(reduced.nodes)
    for src, dst in original_reach:
        if src in surviving and dst in surviving and src != dst:
            assert (src, dst) in reduced_reach

    for src, dst in reduced_reach:
        if src != dst:
            assert (src, dst) in original_reach

    # Verify every derived spliced edge carries DERIVED_DETERMINISTIC + non-empty backing_ids
    derived_edges = [
        edge
        for edge in reduced.edges
        if edge.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
    ]
    for edge in derived_edges:
        assert edge.provenance.rule_id == rule.id
        assert len(edge.provenance.backing_ids) >= 3
        assert tuple(sorted(edge.provenance.backing_ids)) == edge.provenance.backing_ids

    # Verify no duplicate parallel (source, target, relation, sync_kind) edges exist
    edge_keys = [(e.source, e.target, e.relation, e.sync_kind) for e in reduced.edges]
    assert len(edge_keys) == len(set(edge_keys))


@settings(max_examples=60, deadline=None)
@given(
    in_kind=st.sampled_from(_ALL_SYNC_KINDS),
    out_kind=st.sampled_from(_ALL_SYNC_KINDS),
    preserve_concurrency=st.booleans(),
    w1=st.floats(min_value=0.5, max_value=10.0, allow_nan=False),
    w2=st.floats(min_value=0.5, max_value=10.0, allow_nan=False),
)
def test_fuzz_bypass_never_upgrades_async_fire_forget_to_sync(
    in_kind: SyncKind,
    out_kind: SyncKind,
    preserve_concurrency: bool,
    w1: float,
    w2: float,
) -> None:
    """Splicing legs where either leg is ASYNC_FIRE_FORGET never upgrades to SYNC."""
    nodes = {nid: _make_node(nid) for nid in ("u", "mid", "w")}
    edges = (
        _make_edge("e_in", "u", "mid", sync_kind=in_kind, weight=w1),
        _make_edge("e_out", "mid", "w", sync_kind=out_kind, weight=w2),
    )
    ir = StructuralGraphIR(graph_id="sync_check", nodes=nodes, edges=edges)
    rule = BypassVerticesRule(
        id="bypass_mid",
        node_id_globs=("mid",),
        preserve_concurrency_kind=preserve_concurrency,
        max_fanout_product=4,
    )
    reduced = bypass_vertices(ir, rule)
    assert "mid" not in reduced.nodes
    assert len(reduced.edges) == 1
    spliced = reduced.edges[0]

    if in_kind is SyncKind.ASYNC_FIRE_FORGET or out_kind is SyncKind.ASYNC_FIRE_FORGET:
        assert spliced.sync_kind is SyncKind.ASYNC_FIRE_FORGET
    elif preserve_concurrency and (
        in_kind is SyncKind.FORK_JOIN_BRANCH or out_kind is SyncKind.FORK_JOIN_BRANCH
    ):
        assert spliced.sync_kind is SyncKind.FORK_JOIN_BRANCH
    else:
        assert spliced.sync_kind is SyncKind.SYNC

    assert spliced.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
    assert spliced.provenance.rule_id == "bypass_mid"
    assert {"e_in", "mid", "e_out"}.issubset(set(spliced.provenance.backing_ids))


@settings(max_examples=50, deadline=None)
@given(
    extra_chain=st.integers(min_value=1, max_value=4),
    prefix_nodes=st.integers(min_value=0, max_value=3),
    suffix_nodes=st.integers(min_value=0, max_value=3),
)
def test_fuzz_collapse_partition_rejects_induced_cycle_a_x_b_y(
    extra_chain: int,
    prefix_nodes: int,
    suffix_nodes: int,
) -> None:
    """Contracting {X, Y} on an induced path A -> X -> B1 -> ... -> Bk -> Y raises cycle error."""
    chain_ids = [f"A_{i}" for i in range(prefix_nodes)]
    chain_ids.append("part_X")
    bridge_ids = [f"B_{i}" for i in range(extra_chain)]
    chain_ids.extend(bridge_ids)
    chain_ids.append("part_Y")
    chain_ids.extend(f"Z_{i}" for i in range(suffix_nodes))

    nodes = {nid: _make_node(nid) for nid in chain_ids}
    edges = tuple(
        _make_edge(f"e_{i}", chain_ids[i], chain_ids[i + 1]) for i in range(len(chain_ids) - 1)
    )
    ir = StructuralGraphIR(graph_id="induced_cycle", nodes=nodes, edges=edges)
    rule = CollapsePartitionRule(
        id="bad_collapse",
        target_node_id="super_XY",
        label="Super XY",
        member_globs=("part_*",),
    )

    with pytest.raises((CycleInducedError, rx.DAGWouldCycle)) as exc_info:
        collapse_partition(ir, rule, forbid_new_cycles=True)

    err = exc_info.value
    assert isinstance(err, CycleInducedError)
    assert err.rule_id == "bad_collapse"
    assert err.target_node_id == "super_XY"
    assert err.member_ids == ("part_X", "part_Y")
    assert err.witness_path == ("part_X", *bridge_ids, "part_Y")


@settings(max_examples=60, deadline=None)
@given(
    ir=random_structural_graph(min_nodes=3, max_nodes=12, allow_cycles=False),
    interval_start=st.integers(min_value=0, max_value=8),
    interval_len=st.integers(min_value=1, max_value=5),
)
def test_fuzz_collapse_partition_preserves_acyclic_reachability_and_provenance(
    ir: StructuralGraphIR,
    interval_start: int,
    interval_len: int,
) -> None:
    """Contracting a convex topological interval in a DAG stays acyclic and preserves reach."""
    ordered = sorted(ir.nodes)
    start = interval_start % len(ordered)
    members = ordered[start : min(len(ordered), start + interval_len)]
    rule = CollapsePartitionRule(
        id="convex_collapse",
        target_node_id="super_group",
        label="Grouped",
        summary="Convex cluster",
        member_globs=tuple(members),
    )

    collapsed = collapse_partition(ir, rule, forbid_new_cycles=True)
    assert _is_strict_dag(collapsed)
    assert "super_group" in collapsed.nodes
    supernode = collapsed.nodes["super_group"]
    assert supernode.kind is NodeKind.SUPERNODE
    assert supernode.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
    assert supernode.provenance.rule_id == "convex_collapse"
    assert set(members).issubset(set(supernode.provenance.backing_ids))

    # No self-loops on supernode
    assert all(
        not (e.source == "super_group" and e.target == "super_group") for e in collapsed.edges
    )

    # All boundary edges touching supernode are stamped DERIVED_DETERMINISTIC with backing_ids
    for edge in collapsed.edges:
        if edge.source == "super_group" or edge.target == "super_group":
            assert edge.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
            assert edge.provenance.rule_id == "convex_collapse"
            assert len(edge.provenance.backing_ids) >= 1

    # Reachability preservation between mapped endpoints (distinct reps)
    member_set = set(members)
    orig_reach = _reachability_closure(ir)
    new_reach = _reachability_closure(collapsed)
    for src, dst in orig_reach:
        mapped_src = "super_group" if src in member_set else src
        mapped_dst = "super_group" if dst in member_set else dst
        if mapped_src != mapped_dst:
            assert (mapped_src, mapped_dst) in new_reach


@settings(max_examples=60, deadline=None)
@given(
    ir=random_structural_graph(min_nodes=2, max_nodes=14, allow_cycles=True, allow_self_loops=True)
)
def test_fuzz_condense_sccs_and_transitive_reduction_preserve_dag_closure_and_provenance(
    ir: StructuralGraphIR,
) -> None:
    """condense_sccs + transitive_reduction yields a strict DAG with identical closure."""
    condensed = condense_sccs(ir)
    assert _is_strict_dag(condensed)

    for node in condensed.nodes.values():
        if node.kind is NodeKind.SUPERNODE:
            assert node.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
            assert len(node.provenance.backing_ids) >= 2

    reduced = transitive_reduction(condensed, condense_cycles=False)
    assert _is_strict_dag(reduced)
    assert _reachability_closure(reduced) == _reachability_closure(condensed)
    assert len(reduced.edges) <= len(condensed.edges)

    # Every surviving edge retains exact IREdge attributes, SyncKind, and ProvenanceStamp
    condensed_by_id = {edge.edge_id: edge for edge in condensed.edges}
    for edge in reduced.edges:
        assert edge == condensed_by_id[edge.edge_id]

    # Direct transitive_reduction on potentially cyclic IR matches condense_sccs + TR
    direct_tr = transitive_reduction(ir, condense_cycles=True)
    assert _is_strict_dag(direct_tr)
    assert direct_tr == reduced


@settings(max_examples=50, deadline=None)
@given(
    ir=random_structural_graph(min_nodes=2, max_nodes=12, allow_cycles=True),
    direction=st.sampled_from(("sink", "root", "both")),
    iterative=st.booleans(),
)
def test_fuzz_prune_leaves_never_deletes_internal_bridges_or_active_parents(
    ir: StructuralGraphIR,
    direction: Literal["sink", "root", "both"],
    iterative: bool,
) -> None:
    """prune_leaves removes only degree-0 leaves without children and keeps bridges intact."""
    rule = PruneLeavesRule(
        id="prune_all",
        node_id_globs=("n_*",),
        direction=direction,
    )
    pruned = prune_leaves(ir, rule, iterative=iterative)

    # Compute original degrees and child map for single-pass safety check
    in_deg = dict.fromkeys(ir.nodes, 0)
    out_deg = dict.fromkeys(ir.nodes, 0)
    for edge in ir.edges:
        out_deg[edge.source] += 1
        in_deg[edge.target] += 1

    if not iterative:
        parent_set = {n.parent_id for n in ir.nodes.values() if n.parent_id is not None}
        for nid in ir.nodes:
            if in_deg[nid] > 0 and out_deg[nid] > 0:
                assert nid in pruned.nodes
            if nid in parent_set:
                assert nid in pruned.nodes

    # Iterative prune is idempotent at fixed point
    if iterative:
        assert prune_leaves(pruned, rule, iterative=True) == pruned


@settings(max_examples=50, deadline=None)
@given(
    ir=random_structural_graph(min_nodes=2, max_nodes=12, allow_cycles=True),
    upstream_depth=st.integers(min_value=0, max_value=3),
    downstream_depth=st.integers(min_value=0, max_value=3),
    retain_ancestors=st.booleans(),
)
def test_fuzz_k_hop_neighborhood_valid_containment_and_reducer_port(
    ir: StructuralGraphIR,
    upstream_depth: int,
    downstream_depth: int,
    retain_ancestors: bool,
) -> None:
    """k_hop_neighborhood retains focal nodes and valid containment forest deterministically."""
    focal = sorted(ir.nodes)[0]
    scope = KHopNeighborhoodScope(
        focal_nodes=(focal,),
        upstream_depth=upstream_depth,
        downstream_depth=downstream_depth,
    )
    sliced = k_hop_neighborhood(ir, scope, retain_ancestors=retain_ancestors)
    assert focal in sliced.nodes
    assert set(sliced.nodes).issubset(set(ir.nodes))
    for edge in sliced.edges:
        assert edge.source in sliced.nodes
        assert edge.target in sliced.nodes

    # Verify StructuralTopologicalReducer conforms to StructuralReducerPort and is deterministic
    reducer = StructuralTopologicalReducer()
    assert isinstance(reducer, StructuralReducerPort)
    view = DiagramViewSpec(
        id="fuzz_view",
        title="Fuzz View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        scope=FullScope(),
        simplify=SimplifyConfig(condense_sccs=True, transitive_reduction=True),
        invariants=ViewInvariants(forbid_new_cycles=True),
    )
    first = reducer.reduce(ir, view)
    second = reducer.reduce(ir, view)
    assert first == second
    assert _is_strict_dag(first)


@settings(max_examples=50, deadline=None)
@given(
    branch_count=st.integers(min_value=2, max_value=5),
    edge_weight=st.floats(min_value=0.5, max_value=4.0, allow_nan=False),
    fanout_cap=st.integers(min_value=1, max_value=6),
)
def test_fuzz_bypass_deduplicates_parallel_weights_and_enforces_fanout_guard(
    branch_count: int,
    edge_weight: float,
    fanout_cap: int,
) -> None:
    """Bypass sums parallel path weights, merges backing_ids, and respects max_fanout_product."""
    nodes: dict[str, IRNode] = {"src": _make_node("src"), "dst": _make_node("dst")}
    edges: list[IREdge] = []
    for i in range(branch_count):
        mid = f"mid_{i}"
        nodes[mid] = _make_node(mid)
        edges.append(_make_edge(f"in_{i}", "src", mid, weight=edge_weight))
        edges.append(_make_edge(f"out_{i}", mid, "dst", weight=edge_weight))

    # Add a high-fanout hub with 3 incoming and 3 outgoing edges (product = 9)
    nodes["mid_hub"] = _make_node("mid_hub")
    for k in range(3):
        in_node = f"hub_in_{k}"
        out_node = f"hub_out_{k}"
        nodes[in_node] = _make_node(in_node)
        nodes[out_node] = _make_node(out_node)
        edges.append(_make_edge(f"hin_{k}", in_node, "mid_hub", weight=1.0))
        edges.append(_make_edge(f"hout_{k}", "mid_hub", out_node, weight=1.0))

    ir = StructuralGraphIR(graph_id="fanout_dedup", nodes=nodes, edges=tuple(edges))
    rule = BypassVerticesRule(
        id="bypass_mid_all",
        node_id_globs=("mid_*",),
        preserve_edge_relations=("calls",),
        max_fanout_product=fanout_cap,
    )
    reduced = bypass_vertices(ir, rule)

    # mid_hub has fanout 9 > fanout_cap (<= 6), so it MUST survive
    assert "mid_hub" in reduced.nodes
    for i in range(branch_count):
        assert f"mid_{i}" not in reduced.nodes

    spliced = [e for e in reduced.edges if e.source == "src" and e.target == "dst"]
    assert len(spliced) == 1
    assert abs(spliced[0].weight - branch_count * edge_weight) < 1e-6
    backing = set(spliced[0].provenance.backing_ids)
    for i in range(branch_count):
        assert {f"in_{i}", f"mid_{i}", f"out_{i}"}.issubset(backing)


@settings(max_examples=40, deadline=None)
@given(
    count=st.integers(min_value=3, max_value=8),
    exclude_idx=st.integers(min_value=0, max_value=7),
)
def test_fuzz_collapse_partition_honors_exclude_ids(count: int, exclude_idx: int) -> None:
    """Excluded IDs remain outside the contracted supernode and absent from its backing_ids."""
    member_ids = [f"svc_{i}" for i in range(count)]
    excluded_id = member_ids[exclude_idx % count]
    nodes = {nid: _make_node(nid) for nid in member_ids}
    edges = tuple(_make_edge(f"e_{i}", member_ids[i], member_ids[i + 1]) for i in range(count - 1))
    ir = StructuralGraphIR(graph_id="exclude_test", nodes=nodes, edges=edges)
    # Exclude only boundary index 0 or count - 1 to ensure no cycle is induced
    safe_excluded = member_ids[0] if exclude_idx % 2 == 0 else member_ids[-1]
    rule = CollapsePartitionRule(
        id="collapse_with_exclude",
        target_node_id="super_svc",
        label="Services",
        member_globs=("svc_*",),
        exclude_ids=(safe_excluded,),
    )
    collapsed = collapse_partition(ir, rule, forbid_new_cycles=True)
    assert safe_excluded in collapsed.nodes
    assert "super_svc" in collapsed.nodes
    assert safe_excluded not in collapsed.nodes["super_svc"].provenance.backing_ids
    assert excluded_id in ir.nodes
