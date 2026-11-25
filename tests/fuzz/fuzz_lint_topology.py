"""Hypothesis property-based fuzz suite for Gate 3 Topological & Partial-Order Invariants."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import DiagnosticSeverity, GateId, LintDiagnostic, LintReport
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
    FullScope,
    GlobalAbstractionRule,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
    ViewScope,
)
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.lint_topology.gate import (
    _build_curated_resolver,  # pyright: ignore[reportPrivateUsage]
)
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


def _hub_graph(hub: str, in_deg: int, out_deg: int) -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {hub: _obs_node(hub)}
    edges: list[IREdge] = []
    for idx in range(in_deg):
        nodes[f"up/u{idx}"] = _obs_node(f"up/u{idx}")
        edges.append(_obs_edge(f"in_{idx}", f"up/u{idx}", hub))
    for idx in range(out_deg):
        nodes[f"down/d{idx}"] = _obs_node(f"down/d{idx}")
        edges.append(_obs_edge(f"out_{idx}", hub, f"down/d{idx}"))
    return StructuralGraphIR(nodes=nodes, edges=tuple(edges))


@settings(max_examples=50, deadline=None)
@given(
    obs_in=st.integers(min_value=1, max_value=4),
    obs_out=st.integers(min_value=1, max_value=4),
    extra_in=st.integers(min_value=0, max_value=8),
    extra_out=st.integers(min_value=0, max_value=8),
    slack=st.integers(min_value=0, max_value=6),
)
def test_post_collapse_bypass_fanout_is_enforced_on_curated_nodes(
    obs_in: int,
    obs_out: int,
    extra_in: int,
    extra_out: int,
    slack: int,
) -> None:
    hub = "shared/hub"
    budget = obs_in * obs_out + slack
    observed = _hub_graph(hub, obs_in, obs_out)
    curated = _hub_graph(hub, obs_in + extra_in, obs_out + extra_out)
    rule = BypassVerticesRule(
        id="bypass-hub", node_id_globs=("shared/*",), max_fanout_product=budget
    )
    spec = _structural_spec(rules=(rule,))

    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    codes = [d.code for d in report.diagnostics if d.code == "BICLIQUE_FANOUT_EXCEEDED"]
    product = (obs_in + extra_in) * (obs_out + extra_out)
    assert len(codes) == (1 if product > budget else 0)
    if product > budget:
        assert not report.passed


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


@settings(max_examples=40, deadline=None)
@given(
    child_count=st.integers(min_value=2, max_value=6),
    include_parent_in_partition=st.booleans(),
)
def test_semref_cohesion_respects_in_partition_parent_containment(
    child_count: int,
    include_parent_in_partition: bool,
) -> None:
    parent_id = "pkg.slice"
    nodes: dict[str, IRNode] = {parent_id: _obs_node(parent_id, NodeKind.PACKAGE)}
    for idx in range(child_count):
        cid = f"{parent_id}.cls_{idx}"
        nodes[cid] = IRNode(
            symbol_id=cid,
            label=cid,
            kind=NodeKind.CLASS,
            parent_id=parent_id,
            provenance=_OBS_PROVENANCE,
        )
    observed = StructuralGraphIR(nodes=nodes, edges=())
    globs = ("pkg.slice", "pkg.slice.*") if include_parent_in_partition else ("pkg.slice.*",)
    rule = CollapsePartitionRule(
        id="collapse-lexical",
        target_node_id="SliceSupernode",
        label="Slice Supernode",
        member_globs=globs,
    )
    spec = _structural_spec(rules=(rule,))
    report = TopologicalInvariantGate(strict_cohesion=True).check(observed, spec, None)
    cohesion_diags = [d for d in report.diagnostics if d.code == "DISCONNECTED_PARTITION_MERGE"]
    if include_parent_in_partition:
        assert cohesion_diags == []
        assert report.passed
    else:
        assert len(cohesion_diags) == 1
        assert not report.passed


# ---------------------------------------------------------------------------
# require_reachability (opt-in) vs preserve_reachability (vacuous if unobserved)
# ---------------------------------------------------------------------------

_REL_CHOICES = st.sampled_from([EdgeRelation.CALLS, EdgeRelation.IMPORTS, EdgeRelation.WRITES_DATA])


def _chain_ir(node_ids: list[str], rel: EdgeRelation) -> StructuralGraphIR:
    return StructuralGraphIR(
        nodes={nid: _obs_node(nid) for nid in node_ids},
        edges=tuple(
            _obs_edge(f"e{idx}", node_ids[idx], node_ids[idx + 1], relation=rel)
            for idx in range(len(node_ids) - 1)
        ),
    )


def _without_node(ir: StructuralGraphIR, cut_node: str) -> StructuralGraphIR:
    return StructuralGraphIR(
        nodes={nid: node for nid, node in ir.nodes.items() if nid != cut_node},
        edges=tuple(e for e in ir.edges if cut_node not in {e.source, e.target}),
    )


def _inv_spec(
    *,
    preserve: tuple[ReachabilityAssertion, ...] = (),
    require: tuple[ReachabilityAssertion, ...] = (),
) -> CurationSpecV1:
    return _structural_spec(
        invariants=ViewInvariants(preserve_reachability=preserve, require_reachability=require)
    )


def _assert_not_observed(report: LintReport, src: str, dst: str, via: tuple[str, ...]) -> None:
    assert not report.passed
    diags = [d for d in report.diagnostics if d.code == "REACHABILITY_NOT_OBSERVED"]
    assert len(diags) == 1
    diag = diags[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.witness_path == ()  # never a fabricated witness
    assert diag.view_id == "arch-view"
    assert src in diag.message
    assert dst in diag.message
    assert set(diag.offending_symbols) == {src, dst}
    if via:
        assert via[0] in diag.message
    assert not [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]


def _assert_severed(report: LintReport, node_ids: list[str], cut_node: str) -> None:
    assert not report.passed
    sev = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert len(sev) == 1
    assert sev[0].witness_path == tuple(node_ids)
    assert cut_node in sev[0].offending_symbols
    assert not [d for d in report.diagnostics if d.code == "REACHABILITY_NOT_OBSERVED"]


@settings(max_examples=120, deadline=None)
@given(
    chain_len=st.integers(min_value=3, max_value=10),
    rel=_REL_CHOICES,
    other_rel=_REL_CHOICES,
    reverse=st.booleans(),
    filter_mode=st.sampled_from(["none", "match", "mismatch"]),
    cut=st.booleans(),
)
def test_require_reachability_matrix_vs_preserve(
    chain_len: int,
    rel: EdgeRelation,
    other_rel: EdgeRelation,
    reverse: bool,
    filter_mode: str,
    cut: bool,
) -> None:
    gate = TopologicalInvariantGate()
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    observed = _chain_ir(node_ids, rel)
    cut_node = node_ids[chain_len // 2]
    curated = _without_node(observed, cut_node) if cut else observed

    src, dst = (node_ids[-1], node_ids[0]) if reverse else (node_ids[0], node_ids[-1])
    via: tuple[str, ...] = {
        "none": (),
        "match": (rel.value,),
        "mismatch": (other_rel.value,),
    }[filter_mode]
    if filter_mode == "mismatch" and other_rel is rel:
        via = (rel.value,)
        filter_mode = "match"
    assertion = ReachabilityAssertion(from_node=src, to_node=dst, via_relations=via)
    path_observed = not reverse and filter_mode != "mismatch"

    preserve_report = gate.check(observed, _inv_spec(preserve=(assertion,)), {"arch-view": curated})
    require_report = gate.check(observed, _inv_spec(require=(assertion,)), {"arch-view": curated})

    # preserve_reachability semantics: vacuous pass when no observed path exists.
    if path_observed and cut:
        assert not preserve_report.passed
    else:
        assert preserve_report.passed

    if not path_observed:
        _assert_not_observed(require_report, src, dst, via)
    elif cut:
        _assert_severed(require_report, node_ids, cut_node)
    else:
        assert require_report.passed
        assert require_report.error_count == 0
    # Determinism.
    assert gate.check(observed, _inv_spec(require=(assertion,)), {"arch-view": curated}) == (
        require_report
    )


@settings(max_examples=60, deadline=None)
@given(
    branch_len=st.integers(min_value=1, max_value=4),
    cut_a=st.booleans(),
    cut_b=st.booleans(),
)
def test_require_reachability_survives_alternative_routes(
    branch_len: int,
    cut_a: bool,
    cut_b: bool,
) -> None:
    gate = TopologicalInvariantGate()
    branch_a = [f"alt/a{idx}" for idx in range(branch_len)]
    branch_b = [f"alt/b{idx}" for idx in range(branch_len)]
    ids = ["alt/src", *branch_a, *branch_b, "alt/dst"]
    edges = [
        _obs_edge("sa", "alt/src", branch_a[0]),
        _obs_edge("sb", "alt/src", branch_b[0]),
        _obs_edge("ea", branch_a[-1], "alt/dst"),
        _obs_edge("eb", branch_b[-1], "alt/dst"),
    ]
    for idx in range(branch_len - 1):
        edges.append(_obs_edge(f"a{idx}", branch_a[idx], branch_a[idx + 1]))
        edges.append(_obs_edge(f"b{idx}", branch_b[idx], branch_b[idx + 1]))
    observed = StructuralGraphIR(nodes={n: _obs_node(n) for n in ids}, edges=tuple(edges))

    curated = observed
    if cut_a:
        curated = _without_node(curated, branch_a[0])
    if cut_b:
        curated = _without_node(curated, branch_b[0])

    assertion = ReachabilityAssertion(from_node="alt/src", to_node="alt/dst")
    report = gate.check(observed, _inv_spec(require=(assertion,)), {"arch-view": curated})
    if cut_a and cut_b:
        assert not report.passed
        sev = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
        assert len(sev) == 1
        assert sev[0].witness_path[0] == "alt/src"
        assert sev[0].witness_path[-1] == "alt/dst"
    else:
        assert report.passed


@settings(max_examples=60, deadline=None)
@given(
    a=_IDENT,
    b=_IDENT,
    ghost=_IDENT,
    connected=st.booleans(),
    unknown_side=st.sampled_from(["from", "to", "none"]),
)
def test_require_reachability_unknown_vs_disconnected_endpoints(
    a: str,
    b: str,
    ghost: str,
    connected: bool,
    unknown_side: str,
) -> None:
    gate = TopologicalInvariantGate()
    a_id, b_id = f"mod/{a}_a", f"mod/{b}_b"
    ghost_id = f"ghost/{ghost}"
    edges = (_obs_edge("e0", a_id, b_id),) if connected else ()
    observed = StructuralGraphIR(
        nodes={a_id: _obs_node(a_id), b_id: _obs_node(b_id)},
        edges=edges,
    )
    src = ghost_id if unknown_side == "from" else a_id
    dst = ghost_id if unknown_side == "to" else b_id
    assertion = ReachabilityAssertion(from_node=src, to_node=dst)
    report = gate.check(observed, _inv_spec(require=(assertion,)), {"arch-view": observed})

    codes = {d.code for d in report.diagnostics}
    if unknown_side != "none":
        # Gate 3 never fabricates a disconnected-path error for unknown endpoints.
        assert codes == set()
    elif connected:
        assert report.passed
    else:
        assert codes == {"REACHABILITY_NOT_OBSERVED"}
        assert not report.passed


@settings(max_examples=60, deadline=None)
@given(
    prefix=st.integers(min_value=0, max_value=3),
    suffix=st.integers(min_value=0, max_value=3),
    reverse=st.booleans(),
    use_rule_resolution=st.booleans(),
)
def test_require_reachability_grouped_endpoints_need_observed_path(
    prefix: int,
    suffix: int,
    reverse: bool,
    use_rule_resolution: bool,
) -> None:
    gate = TopologicalInvariantGate()
    total = prefix + 2 + suffix
    node_ids = [f"grp/n{idx}" for idx in range(total)]
    observed = _chain_ir(node_ids, EdgeRelation.CALLS)
    collapsed = (node_ids[prefix], node_ids[prefix + 1])
    super_id = "grp/Core"

    cur_nodes = {nid: _obs_node(nid) for nid in node_ids if nid not in collapsed}
    cur_nodes[super_id] = _derived_supernode(super_id, "collapse-core", collapsed)
    cur_edges: list[IREdge] = []
    for edge in observed.edges:
        s = super_id if edge.source in collapsed else edge.source
        t = super_id if edge.target in collapsed else edge.target
        if s != t:
            cur_edges.append(_obs_edge(edge.edge_id, s, t))
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

    src, dst = collapsed if not reverse else (collapsed[1], collapsed[0])
    assertion = ReachabilityAssertion(from_node=src, to_node=dst)
    spec = _structural_spec(
        rules=rules,
        invariants=ViewInvariants(require_reachability=(assertion,)),
    )
    report = gate.check(observed, spec, {"arch-view": curated})
    if reverse:
        # Same curated node, but the observed directed path does not exist.
        assert [d.code for d in report.diagnostics if d.code.startswith("REACHABILITY")] == [
            "REACHABILITY_NOT_OBSERVED"
        ]
        assert not report.passed
    else:
        assert not [d for d in report.diagnostics if d.code.startswith("REACHABILITY")]

    # Cross-group endpoints still pass through the supernode projection.
    wide = ReachabilityAssertion(from_node=node_ids[0], to_node=node_ids[-1])
    wide_spec = _structural_spec(
        rules=rules, invariants=ViewInvariants(require_reachability=(wide,))
    )
    wide_report = gate.check(observed, wide_spec, {"arch-view": curated})
    assert not [d for d in wide_report.diagnostics if d.code.startswith("REACHABILITY")]


# ---------------------------------------------------------------------------
# AG-02: severed-path rule / excluded-node attribution (view, endpoints, witness, cause)
# ---------------------------------------------------------------------------


def _attribution_spec(
    assertion: ReachabilityAssertion,
    *,
    rules: tuple[GlobalAbstractionRule, ...] = (),
    scope: ViewScope | None = None,
) -> CurationSpecV1:
    view = DiagramViewSpec(
        id="arch-view",
        title="Architecture View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        rules=rules,
        scope=scope or FullScope(),
        invariants=ViewInvariants(preserve_reachability=(assertion,)),
    )
    return CurationSpecV1(views=(view,))


def _severed_only(report: LintReport) -> LintDiagnostic:
    sev = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert len(sev) == 1
    return sev[0]


def _assert_severed_core(diag: LintDiagnostic, node_ids: list[str], cut_node: str) -> None:
    assert diag.view_id == "arch-view"
    assert diag.witness_path == tuple(node_ids)
    assert cut_node in diag.offending_symbols
    assert "arch-view" in diag.message
    assert node_ids[0] in diag.message
    assert node_ids[-1] in diag.message
    assert " -> ".join(node_ids) in diag.message
    assert cut_node in diag.message


@settings(max_examples=80, deadline=None)
@given(
    chain_len=st.integers(min_value=3, max_value=9),
    cut_offset=st.integers(min_value=0, max_value=100),
    first_is_bypass=st.booleans(),
    second_rule=st.booleans(),
    preserve_rel=st.booleans(),
)
def test_severed_path_attributes_removing_rule_ids(
    chain_len: int,
    cut_offset: int,
    first_is_bypass: bool,
    second_rule: bool,
    preserve_rel: bool,
) -> None:
    """A single matching rule sets rule_id; several are all named without picking one."""
    cut_idx = 1 + (cut_offset % (chain_len - 2))
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    cut_node = node_ids[cut_idx]
    observed = _chain_ir(node_ids, EdgeRelation.CALLS)
    curated = _without_node(observed, cut_node)

    bypass = BypassVerticesRule(
        id="rule-bypass",
        node_id_globs=(cut_node,),
        preserve_edge_relations=("imports",) if preserve_rel else (),
    )
    prune = PruneLeavesRule(id="rule-prune", node_id_globs=(cut_node,))
    first: GlobalAbstractionRule = bypass if first_is_bypass else prune
    rules: tuple[GlobalAbstractionRule, ...] = (first,)
    if second_rule:
        rules = (*rules, prune if first_is_bypass else bypass)

    assertion = ReachabilityAssertion(from_node=node_ids[0], to_node=node_ids[-1])
    spec = _attribution_spec(assertion, rules=rules)
    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    diag = _severed_only(report)

    _assert_severed_core(diag, node_ids, cut_node)
    if second_rule:
        assert diag.rule_id is None  # ambiguous: never attribute to an arbitrary rule
        assert "multiple attributable" in diag.message
    else:
        assert diag.rule_id == first.id
    for rule in rules:
        assert f"'{rule.id}'" in diag.message
        assert rule.id in diag.repair_hint
    if preserve_rel and first_is_bypass:
        assert "splices only relations ['imports']" in diag.message
    assert not [d for d in report.diagnostics if d.code == "REACHABILITY_NOT_OBSERVED"]


@settings(max_examples=60, deadline=None)
@given(
    chain_len=st.integers(min_value=3, max_value=9),
    cut_offset=st.integers(min_value=0, max_value=100),
    use_khop_scope=st.booleans(),
)
def test_severed_path_attributes_scope_exclusion_or_none(
    chain_len: int,
    cut_offset: int,
    use_khop_scope: bool,
) -> None:
    """k-hop scope exclusion names the excluded node; no evidence means no fabricated cause."""
    cut_idx = 1 + (cut_offset % (chain_len - 2))
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    cut_node = node_ids[cut_idx]
    observed = _chain_ir(node_ids, EdgeRelation.CALLS)
    curated = _without_node(observed, cut_node)
    scope: ViewScope = (
        KHopNeighborhoodScope(focal_nodes=(node_ids[0],), upstream_depth=0, downstream_depth=1)
        if use_khop_scope
        else FullScope()
    )
    assertion = ReachabilityAssertion(from_node=node_ids[0], to_node=node_ids[-1])
    spec = _attribution_spec(assertion, scope=scope)
    report = TopologicalInvariantGate().check(observed, spec, {"arch-view": curated})
    diag = _severed_only(report)

    _assert_severed_core(diag, node_ids, cut_node)
    assert diag.rule_id is None
    if use_khop_scope:
        assert "k_hop_neighborhood" in diag.message
        assert "excluded node(s)" in diag.message
        assert "focal_nodes" in diag.repair_hint
    else:
        assert "Cause:" not in diag.message


@settings(max_examples=60, deadline=None)
@given(chain_len=st.integers(min_value=4, max_value=9), via_filter=st.booleans())
def test_severed_hop_attributes_collapse_partition_rule(chain_len: int, via_filter: bool) -> None:
    """A collapse_partition whose supernode lost its outgoing hop is attributable by id."""
    node_ids = [f"svc/n{idx}" for idx in range(chain_len)]
    members = tuple(node_ids[1:-1])
    super_id = "svc/Core"
    observed = _chain_ir(node_ids, EdgeRelation.CALLS)
    cur_nodes = {node_ids[0]: _obs_node(node_ids[0]), node_ids[-1]: _obs_node(node_ids[-1])}
    cur_nodes[super_id] = _derived_supernode(super_id, "collapse-core", members)
    curated = StructuralGraphIR(
        nodes=cur_nodes,
        edges=(_derived_edge("d0", node_ids[0], super_id, "collapse-core", ("e0",)),),
    )
    rule = CollapsePartitionRule(
        id="collapse-core", target_node_id=super_id, label="Core", member_globs=members
    )
    via = ("calls",) if via_filter else ()
    assertion = ReachabilityAssertion(
        from_node=node_ids[0], to_node=node_ids[-1], via_relations=via
    )
    spec = _attribution_spec(assertion, rules=(rule,))
    diag = _severed_only(TopologicalInvariantGate().check(observed, spec, {"arch-view": curated}))

    assert diag.view_id == "arch-view"
    assert diag.witness_path == tuple(node_ids)
    assert diag.rule_id == "collapse-core"
    assert "collapse_partition 'collapse-core'" in diag.message
    assert node_ids[-1] in diag.offending_symbols
    assert ("via relations ['calls']" in diag.message) is via_filter
    assert ("over any relation" in diag.message) is (not via_filter)


@settings(max_examples=30, deadline=None)
@given(tag=_IDENT, bypass_first=st.booleans())
def test_resolver_keeps_bypass_drop_over_later_collapse_glob(tag: str, bypass_first: bool) -> None:
    src, mid, dst, sup = f"{tag}/src", f"{tag}/mid", f"{tag}/dst", f"{tag}/sup"
    observed = StructuralGraphIR(
        nodes={nid: _obs_node(nid) for nid in (src, mid, dst)},
        edges=(_obs_edge("e0", src, mid), _obs_edge("e1", mid, dst)),
    )
    curated = StructuralGraphIR(
        nodes={
            src: _obs_node(src),
            dst: _obs_node(dst),
            sup: _derived_supernode(sup, "collapse-mid", (src,)),
        },
        edges=(),
    )
    bypass = BypassVerticesRule(id="bypass-mid", node_id_globs=(mid,))
    collapse = CollapsePartitionRule(
        id="collapse-mid", label="mid", target_node_id=sup, member_globs=(f"{tag}/m*",)
    )
    rules = (bypass, collapse) if bypass_first else (collapse, bypass)

    resolver = _build_curated_resolver(observed, curated, rules)

    assert resolver[src] == src
    assert resolver[dst] == dst
    if bypass_first:
        assert mid not in resolver
    else:
        assert resolver[mid] == sup
