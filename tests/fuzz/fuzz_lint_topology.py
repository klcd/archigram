"""Hypothesis property-based fuzz suite for Gate 3 Topological & Partial-Order Invariants."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import DiagnosticSeverity, GateId
from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.ports.contracts import LintGatePort

_OBS_PROVENANCE = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-ingestor",
    input_sha256="a" * 64,
)

_IDENT = st.from_regex(r"[a-z][a-z0-9_]{1,8}", fullmatch=True)


def _obs_node(symbol_id: str, kind: NodeKind = NodeKind.MODULE) -> IRNode:
    return IRNode(symbol_id=symbol_id, label=symbol_id, kind=kind, provenance=_OBS_PROVENANCE)


def _derived_supernode(
    symbol_id: str,
    rule_id: str,
    backing_ids: tuple[str, ...],
) -> IRNode:
    return IRNode(
        symbol_id=symbol_id,
        label=symbol_id,
        kind=NodeKind.SUPERNODE,
        provenance=ProvenanceStamp(
            grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
            rule_id=rule_id,
            backing_ids=backing_ids,
        ),
    )


def _obs_edge(
    edge_id: str,
    source: str,
    target: str,
    relation: EdgeRelation = EdgeRelation.CALLS,
    sync_kind: SyncKind = SyncKind.SYNC,
) -> IREdge:
    return IREdge(
        edge_id=edge_id,
        source=source,
        target=target,
        relation=relation,
        sync_kind=sync_kind,
        provenance=_OBS_PROVENANCE,
    )


def _derived_edge(
    edge_id: str,
    source: str,
    target: str,
    rule_id: str,
    backing_ids: tuple[str, ...],
    sync_kind: SyncKind = SyncKind.SYNC,
) -> IREdge:
    return IREdge(
        edge_id=edge_id,
        source=source,
        target=target,
        relation=EdgeRelation.CALLS,
        sync_kind=sync_kind,
        provenance=ProvenanceStamp(
            grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
            rule_id=rule_id,
            backing_ids=backing_ids,
        ),
    )


def _structural_spec(
    *,
    view_id: str = "arch-view",
    rules: tuple[BypassVerticesRule | CollapsePartitionRule, ...] = (),
    global_rules: tuple[BypassVerticesRule | CollapsePartitionRule, ...] = (),
    invariants: ViewInvariants | None = None,
) -> CurationSpecV1:
    view = DiagramViewSpec(
        id=view_id,
        title="Architecture View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        rules=rules,
        invariants=invariants or ViewInvariants(),
    )
    return CurationSpecV1(global_abstractions=global_rules, views=(view,))


def _trace_spec(
    *, view_id: str = "trace-view", preserve_partial_order: bool = True
) -> CurationSpecV1:
    view = DiagramViewSpec(
        id=view_id,
        title="Trace View",
        ir_mode=IRMode.INTERACTION_TRACE,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(preserve_partial_order=preserve_partial_order),
    )
    return CurationSpecV1(views=(view,))


def _make_participant(pid: str) -> TraceParticipant:
    return TraceParticipant(
        participant_id=pid,
        symbol_id=f"svc/{pid}",
        label=pid,
        provenance=_OBS_PROVENANCE,
    )


@settings(max_examples=60, deadline=None)
@given(
    chain_len=st.integers(min_value=3, max_value=12),
    cut_offset=st.integers(min_value=1, max_value=10),
    rel=st.sampled_from([EdgeRelation.CALLS, EdgeRelation.IMPORTS, EdgeRelation.WRITES_DATA]),
    filter_rel=st.booleans(),
)
def test_severing_bridge_always_fails_with_valid_observed_witness(
    chain_len: int,
    cut_offset: int,
    rel: EdgeRelation,
    filter_rel: bool,
) -> None:
    gate = TopologicalInvariantGate()
    assert isinstance(gate, LintGatePort)
    assert gate.gate_id is GateId.GATE_3_TOPOLOGY_POSET

    cut_idx = 1 + (cut_offset % (chain_len - 2))
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    obs_nodes = {nid: _obs_node(nid) for nid in node_ids}
    obs_edges = tuple(
        _obs_edge(f"e{idx}", node_ids[idx], node_ids[idx + 1], relation=rel)
        for idx in range(chain_len - 1)
    )
    observed = StructuralGraphIR(nodes=obs_nodes, edges=obs_edges)

    cut_node = node_ids[cut_idx]
    cur_nodes = {nid: node for nid, node in obs_nodes.items() if nid != cut_node}
    cur_edges = tuple(e for e in obs_edges if cut_node not in {e.source, e.target})
    curated = StructuralGraphIR(nodes=cur_nodes, edges=cur_edges)

    via = (rel.value,) if filter_rel else ()
    assertion = ReachabilityAssertion(
        from_node=node_ids[0],
        to_node=node_ids[-1],
        via_relations=via,
    )
    spec = _structural_spec(invariants=ViewInvariants(preserve_reachability=(assertion,)))

    report = gate.check(observed, spec, {"arch-view": curated})
    assert not report.passed
    sev_diags = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert len(sev_diags) == 1
    diag = sev_diags[0]
    assert diag.gate is GateId.GATE_3_TOPOLOGY_POSET
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.witness_path[0] == node_ids[0]
    assert diag.witness_path[-1] == node_ids[-1]
    assert cut_node in diag.offending_symbols

    obs_edge_pairs = {(e.source, e.target) for e in obs_edges}
    for u, v in pairwise(diag.witness_path):
        assert (u, v) in obs_edge_pairs

    # Determinism check
    assert gate.check(observed, spec, {"arch-view": curated}) == report


@settings(max_examples=50, deadline=None)
@given(
    prefix=st.integers(min_value=1, max_value=4),
    suffix=st.integers(min_value=1, max_value=4),
    use_rule_resolution=st.booleans(),
)
def test_preserved_reachability_through_supernode_passes(
    prefix: int,
    suffix: int,
    use_rule_resolution: bool,
) -> None:
    gate = TopologicalInvariantGate()
    total = prefix + suffix + 2
    node_ids = [f"app/n{idx}" for idx in range(total)]
    obs_nodes = {nid: _obs_node(nid) for nid in node_ids}
    obs_edges = tuple(
        _obs_edge(f"e{idx}", node_ids[idx], node_ids[idx + 1]) for idx in range(total - 1)
    )
    observed = StructuralGraphIR(nodes=obs_nodes, edges=obs_edges)

    collapsed = tuple(node_ids[prefix : prefix + 2])
    super_id = "app/Core"
    cur_nodes = {nid: _obs_node(nid) for nid in node_ids if nid not in collapsed}
    cur_nodes[super_id] = (
        IRNode(
            symbol_id=super_id,
            label=super_id,
            kind=NodeKind.SUPERNODE,
            provenance=ProvenanceStamp(grade=ProvenanceGrade.DECLARED, extractor="spec"),
        )
        if use_rule_resolution
        else _derived_supernode(super_id, "collapse-core", collapsed)
    )
    cur_edges: list[IREdge] = []
    for edge in obs_edges:
        src = super_id if edge.source in collapsed else edge.source
        dst = super_id if edge.target in collapsed else edge.target
        if src != dst:
            cur_edges.append(_obs_edge(edge.edge_id, src, dst))
    curated = StructuralGraphIR(nodes=cur_nodes, edges=tuple(cur_edges))

    rules: tuple[CollapsePartitionRule, ...] = (
        (
            CollapsePartitionRule(
                id="collapse-core",
                target_node_id=super_id,
                label="Core",
                member_globs=collapsed,
            ),
        )
        if use_rule_resolution
        else ()
    )
    assertion = ReachabilityAssertion(from_node=node_ids[0], to_node=node_ids[-1])
    spec = _structural_spec(
        rules=rules,
        invariants=ViewInvariants(preserve_reachability=(assertion,)),
    )
    report = gate.check(observed, spec, {"arch-view": curated})
    assert report.passed
    assert report.error_count == 0


@settings(max_examples=60, deadline=None)
@given(
    domain_a=_IDENT,
    domain_b=_IDENT,
    extra_hops=st.integers(min_value=0, max_value=3),
    two_cycle=st.booleans(),
)
def test_contracting_two_domain_utils_inducing_cycle_fails_with_closed_witness(
    domain_a: str,
    domain_b: str,
    extra_hops: int,
    two_cycle: bool,
) -> None:
    d_a = f"auth_{domain_a}"
    d_b = f"billing_{domain_b}"
    a_svc = f"{d_a}/service"
    a_utils = f"{d_a}/utils"
    b_svc = f"{d_b}/service"
    b_utils = f"{d_b}/utils"

    obs_nodes: dict[str, IRNode] = {
        a_svc: _obs_node(a_svc),
        a_utils: _obs_node(a_utils),
        b_utils: _obs_node(b_utils),
    }
    obs_edges: list[IREdge] = [_obs_edge("e_a", a_svc, a_utils)]
    if two_cycle:
        # Acyclic DAG: billing/utils -> auth/service -> auth/utils
        obs_edges.append(_obs_edge("e_ba", b_utils, a_svc))
    else:
        # Acyclic DAG: billing/utils -> billing/service -> h0 -> ... -> auth/service -> auth/utils
        obs_nodes[b_svc] = _obs_node(b_svc)
        obs_edges.append(_obs_edge("e_b", b_utils, b_svc))
        prev = b_svc
        for hop in range(extra_hops):
            hid = f"mid/h{hop}"
            obs_nodes[hid] = _obs_node(hid)
            obs_edges.append(_obs_edge(f"e_hop_{hop}", prev, hid))
            prev = hid
        obs_edges.append(_obs_edge("e_bridge", prev, a_svc))

    observed = StructuralGraphIR(nodes=obs_nodes, edges=tuple(obs_edges))

    super_id = "Utils"
    cur_nodes = {k: v for k, v in obs_nodes.items() if k not in {a_utils, b_utils}}
    cur_nodes[super_id] = _derived_supernode(super_id, "merge-utils", (a_utils, b_utils))
    cur_edges = tuple(
        _obs_edge(
            e.edge_id,
            super_id if e.source in {a_utils, b_utils} else e.source,
            super_id if e.target in {a_utils, b_utils} else e.target,
        )
        for e in obs_edges
    )
    curated = StructuralGraphIR(nodes=cur_nodes, edges=cur_edges)

    collapse_rule = CollapsePartitionRule(
        id="merge-utils",
        target_node_id=super_id,
        label="Shared Utils",
        member_globs=("*/utils",),
    )
    spec = _structural_spec(
        rules=(collapse_rule,),
        invariants=ViewInvariants(forbid_new_cycles=True),
    )

    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    assert not report.passed
    cycle_diags = [d for d in report.diagnostics if d.code == "INDUCED_PARTITION_CYCLE"]
    assert len(cycle_diags) == 1
    diag = cycle_diags[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert len(diag.witness_path) >= 3
    assert diag.witness_path[0] == diag.witness_path[-1]
    assert super_id in diag.witness_path
    cur_edge_pairs = {(e.source, e.target) for e in cur_edges}
    for u, v in pairwise(diag.witness_path):
        assert (u, v) in cur_edge_pairs
    assert "member_globs" in diag.repair_hint or "exclude_ids" in diag.repair_hint


@settings(max_examples=40, deadline=None)
@given(cycle_len=st.integers(min_value=2, max_value=8))
def test_preexisting_observed_cycle_is_not_flagged_as_induced(cycle_len: int) -> None:
    node_ids = [f"core/c{i}" for i in range(cycle_len)]
    nodes = {nid: _obs_node(nid) for nid in node_ids}
    edges = tuple(
        _obs_edge(f"e{i}", node_ids[i], node_ids[(i + 1) % cycle_len]) for i in range(cycle_len)
    )
    graph = StructuralGraphIR(nodes=nodes, edges=edges)
    spec = _structural_spec(invariants=ViewInvariants(forbid_new_cycles=True))

    report = TopologicalInvariantGate().check(graph, spec, {"arch-view": graph})
    assert report.passed
    assert not any(d.code == "INDUCED_PARTITION_CYCLE" for d in report.diagnostics)


@settings(max_examples=60, deadline=None)
@given(
    in_deg=st.integers(min_value=1, max_value=10),
    out_deg=st.integers(min_value=1, max_value=10),
    max_fanout=st.integers(min_value=1, max_value=40),
    use_global=st.booleans(),
)
def test_bypass_hub_biclique_fanout_guard_exact_threshold(
    in_deg: int,
    out_deg: int,
    max_fanout: int,
    use_global: bool,
) -> None:
    hub = "shared/hub"
    nodes: dict[str, IRNode] = {hub: _obs_node(hub)}
    edges: list[IREdge] = []
    for idx in range(in_deg):
        src = f"up/u{idx}"
        nodes[src] = _obs_node(src)
        edges.append(_obs_edge(f"in_{idx}", src, hub, relation=EdgeRelation.CALLS))
    for idx in range(out_deg):
        dst = f"down/d{idx}"
        nodes[dst] = _obs_node(dst)
        edges.append(_obs_edge(f"out_{idx}", hub, dst, relation=EdgeRelation.CALLS))
    # Unrelated non-preserved relation edges must not inflate active fanout product
    extra_src = "noise/n0"
    nodes[extra_src] = _obs_node(extra_src)
    edges.append(_obs_edge("noise_in", extra_src, hub, relation=EdgeRelation.IMPORTS))

    observed = StructuralGraphIR(nodes=nodes, edges=tuple(edges))
    rule = BypassVerticesRule(
        id="bypass-hub",
        node_id_globs=("shared/*",),
        preserve_edge_relations=(EdgeRelation.CALLS.value,),
        max_fanout_product=max_fanout,
    )
    spec = _structural_spec(
        rules=() if use_global else (rule,),
        global_rules=(rule,) if use_global else (),
    )

    report = TopologicalInvariantGate().check(observed, spec, None)
    fanout_diags = [d for d in report.diagnostics if d.code == "BICLIQUE_FANOUT_EXCEEDED"]
    product = in_deg * out_deg
    if product > max_fanout:
        assert not report.passed
        assert len(fanout_diags) == 1
        diag = fanout_diags[0]
        assert diag.rule_id == "bypass-hub"
        assert hub in diag.offending_symbols
        assert str(product) in diag.message
        assert "collapse_partition" in diag.repair_hint
    else:
        assert report.passed
        assert fanout_diags == []


@settings(max_examples=50, deadline=None)
@given(
    comp_a_size=st.integers(min_value=1, max_value=4),
    comp_b_size=st.integers(min_value=1, max_value=4),
    strict_cohesion=st.booleans(),
    connected=st.booleans(),
)
def test_semref_cohesion_check_flags_disconnected_partition_merge(
    comp_a_size: int,
    comp_b_size: int,
    strict_cohesion: bool,
    connected: bool,
) -> None:
    nodes: dict[str, IRNode] = {}
    edges: list[IREdge] = []
    a_ids = [f"mod_a/m{i}" for i in range(comp_a_size)]
    b_ids = [f"mod_b/m{i}" for i in range(comp_b_size)]
    for nid in (*a_ids, *b_ids):
        nodes[nid] = _obs_node(nid)
    for u, v in pairwise(a_ids):
        edges.append(_obs_edge(f"ea_{u}_{v}", u, v))
    for u, v in pairwise(b_ids):
        edges.append(_obs_edge(f"eb_{u}_{v}", u, v))
    if connected:
        edges.append(_obs_edge("e_link", a_ids[0], b_ids[0]))

    observed = StructuralGraphIR(nodes=nodes, edges=tuple(edges))
    rule = CollapsePartitionRule(
        id="merge-disjoint",
        target_node_id="Merged",
        label="Merged",
        member_globs=("mod_a/m0", "mod_b/m0"),
    )
    spec = _structural_spec(rules=(rule,))

    gate = TopologicalInvariantGate(strict_cohesion=strict_cohesion)
    report = gate.check(observed, spec, None)
    cohesion_diags = [d for d in report.diagnostics if d.code == "DISCONNECTED_PARTITION_MERGE"]
    if connected:
        assert cohesion_diags == []
        assert report.passed
    else:
        assert len(cohesion_diags) == 1
        expected_sev = DiagnosticSeverity.ERROR if strict_cohesion else DiagnosticSeverity.WARNING
        assert cohesion_diags[0].severity is expected_sev
        assert report.passed is (not strict_cohesion)


@settings(max_examples=50, deadline=None)
@given(
    curated_sync=st.sampled_from([SyncKind.SYNC, SyncKind.ASYNC_FIRE_FORGET]),
    has_async_backing=st.booleans(),
)
def test_structural_async_non_upgrade_invariant(
    curated_sync: SyncKind,
    has_async_backing: bool,
) -> None:
    nodes = {
        "api": _obs_node("api"),
        "worker": _obs_node("worker"),
        "db": _obs_node("db"),
    }
    e1 = _obs_edge(
        "e_spawn",
        "api",
        "worker",
        relation=EdgeRelation.ASYNC_SPAWN if has_async_backing else EdgeRelation.CALLS,
        sync_kind=SyncKind.ASYNC_FIRE_FORGET if has_async_backing else SyncKind.SYNC,
    )
    e2 = _obs_edge("e_write", "worker", "db", sync_kind=SyncKind.SYNC)
    observed = StructuralGraphIR(nodes=nodes, edges=(e1, e2))

    spliced = _derived_edge(
        "e_spliced",
        "api",
        "db",
        rule_id="bypass-worker",
        backing_ids=("e_spawn", "e_write"),
        sync_kind=curated_sync,
    )
    curated = StructuralGraphIR(
        nodes={"api": nodes["api"], "db": nodes["db"]},
        edges=(spliced,),
    )
    spec = _structural_spec(invariants=ViewInvariants(preserve_partial_order=True))

    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    async_diags = [d for d in report.diagnostics if d.code == "ASYNC_UPGRADED_TO_SYNC"]
    if has_async_backing and curated_sync is SyncKind.SYNC:
        assert not report.passed
        assert len(async_diags) == 1
        assert "e_spawn" in async_diags[0].offending_symbols
    else:
        assert report.passed
        assert async_diags == []


@settings(max_examples=60, deadline=None)
@given(
    chain_len=st.integers(min_value=2, max_value=8),
    invert_order=st.booleans(),
    upgrade_async=st.booleans(),
)
def test_trace_partial_order_and_async_non_upgrade(
    chain_len: int,
    invert_order: bool,
    upgrade_async: bool,
) -> None:
    participants = {"p0": _make_participant("p0"), "p1": _make_participant("p1")}
    obs_events: dict[str, TraceEvent] = {}
    for idx in range(chain_len):
        eid = f"ev{idx}"
        obs_events[eid] = TraceEvent(
            event_id=eid,
            caller_id="p0",
            callee_id="p1",
            signature=f"op_{idx}()",
            parent_event_id=f"ev{idx - 1}" if idx > 0 else None,
            clock_domain_id="clk0",
            start_ns=idx * 10,
            end_ns=idx * 10 + 5,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET if idx == chain_len - 1 else SyncKind.SYNC,
            provenance=_OBS_PROVENANCE,
        )
    observed = InteractionTraceIR(participants=participants, events=obs_events)

    first_id = "ev0"
    last_id = f"ev{chain_len - 1}"
    cur_first_sync = SyncKind.SYNC
    cur_last_sync = SyncKind.SYNC if upgrade_async else SyncKind.ASYNC_FIRE_FORGET

    if invert_order:
        # Reverse observed happens-before: last_id -> first_id in curated trace
        cur_events = {
            last_id: TraceEvent(
                event_id=last_id,
                caller_id="p0",
                callee_id="p1",
                signature="op_last()",
                parent_event_id=None,
                clock_domain_id="clk0",
                start_ns=0,
                end_ns=5,
                sync_kind=cur_last_sync,
                provenance=_OBS_PROVENANCE,
            ),
            first_id: TraceEvent(
                event_id=first_id,
                caller_id="p0",
                callee_id="p1",
                signature="op_first()",
                parent_event_id=None,
                causal_links=(last_id,),
                clock_domain_id="clk0",
                start_ns=10,
                end_ns=15,
                sync_kind=cur_first_sync,
                provenance=_OBS_PROVENANCE,
            ),
        }
    else:
        # Preserve observed happens-before: first_id -> last_id via derived representative
        cur_events = {
            first_id: TraceEvent(
                event_id=first_id,
                caller_id="p0",
                callee_id="p1",
                signature="op_first()",
                parent_event_id=None,
                clock_domain_id="clk0",
                start_ns=0,
                end_ns=5,
                sync_kind=cur_first_sync,
                provenance=_OBS_PROVENANCE,
            ),
            "rep_last": TraceEvent(
                event_id="rep_last",
                caller_id="p0",
                callee_id="p1",
                signature="op_last()",
                parent_event_id=first_id,
                clock_domain_id="clk0",
                start_ns=10,
                end_ns=15,
                sync_kind=cur_last_sync,
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                    rule_id="fold-tail",
                    backing_ids=(last_id,),
                ),
            ),
        }
    curated = InteractionTraceIR(participants=participants, events=cur_events)
    spec = _trace_spec(preserve_partial_order=True)

    report = TopologicalInvariantGate().check(observed, spec, {"trace-view": curated})
    po_diags = [d for d in report.diagnostics if d.code == "PARTIAL_ORDER_VIOLATED"]
    async_diags = [d for d in report.diagnostics if d.code == "ASYNC_UPGRADED_TO_SYNC"]

    assert bool(po_diags) is invert_order
    assert bool(async_diags) is upgrade_async
    assert report.passed is (not invert_order and not upgrade_async)


@settings(max_examples=30, deadline=None)
@given(
    chain_len=st.integers(min_value=3, max_value=8),
    dropped_edge_idx=st.integers(min_value=0, max_value=6),
)
def test_dropping_bridge_edge_with_surviving_nodes_reports_severed_endpoints(
    chain_len: int,
    dropped_edge_idx: int,
) -> None:
    edge_idx = dropped_edge_idx % (chain_len - 1)
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    obs_nodes = {nid: _obs_node(nid) for nid in node_ids}
    obs_edges = tuple(
        _obs_edge(f"e{idx}", node_ids[idx], node_ids[idx + 1]) for idx in range(chain_len - 1)
    )
    observed = StructuralGraphIR(nodes=obs_nodes, edges=obs_edges)

    cur_edges = tuple(e for idx, e in enumerate(obs_edges) if idx != edge_idx)
    curated = StructuralGraphIR(nodes=obs_nodes, edges=cur_edges)
    assertion = ReachabilityAssertion(from_node=node_ids[0], to_node=node_ids[-1])
    spec = _structural_spec(invariants=ViewInvariants(preserve_reachability=(assertion,)))

    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    assert not report.passed
    sev = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert len(sev) == 1
    assert sev[0].witness_path == tuple(node_ids)
    assert node_ids[edge_idx] in sev[0].offending_symbols
    assert node_ids[edge_idx + 1] in sev[0].offending_symbols


@settings(max_examples=30, deadline=None)
@given(tag=_IDENT)
def test_pairwise_intersecting_three_scc_triangle_induces_cycle_witness(tag: str) -> None:
    # Three disjoint observed 2-cycles: {a0,a1}, {b0,b1}, {c0,c1}
    obs_ids = [f"{tag}/a0", f"{tag}/a1", f"{tag}/b0", f"{tag}/b1", f"{tag}/c0", f"{tag}/c1"]
    obs_nodes = {nid: _obs_node(nid) for nid in obs_ids}
    obs_edges = (
        _obs_edge("ea1", obs_ids[0], obs_ids[1]),
        _obs_edge("ea2", obs_ids[1], obs_ids[0]),
        _obs_edge("eb1", obs_ids[2], obs_ids[3]),
        _obs_edge("eb2", obs_ids[3], obs_ids[2]),
        _obs_edge("ec1", obs_ids[4], obs_ids[5]),
        _obs_edge("ec2", obs_ids[5], obs_ids[4]),
    )
    observed = StructuralGraphIR(nodes=obs_nodes, edges=obs_edges)

    # Supernodes pairwise share an SCC ({S0,S1}, {S1,S2}, {S2,S0}) yet triple intersection is empty
    cur_nodes = {
        "S_ab": _derived_supernode("S_ab", "r_ab", (obs_ids[0], obs_ids[2])),
        "S_bc": _derived_supernode("S_bc", "r_bc", (obs_ids[3], obs_ids[4])),
        "S_ca": _derived_supernode("S_ca", "r_ca", (obs_ids[5], obs_ids[1])),
    }
    cur_edges = (
        _obs_edge("ce1", "S_ab", "S_bc"),
        _obs_edge("ce2", "S_bc", "S_ca"),
        _obs_edge("ce3", "S_ca", "S_ab"),
    )
    curated = StructuralGraphIR(nodes=cur_nodes, edges=cur_edges)
    spec = _structural_spec(invariants=ViewInvariants(forbid_new_cycles=True))

    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    assert not report.passed
    cycle_diags = [d for d in report.diagnostics if d.code == "INDUCED_PARTITION_CYCLE"]
    assert len(cycle_diags) == 1
    witness = cycle_diags[0].witness_path
    assert len(witness) >= 4
    assert witness[0] == witness[-1]
    cur_edge_pairs = {(e.source, e.target) for e in cur_edges}
    for u, v in pairwise(witness):
        assert (u, v) in cur_edge_pairs
