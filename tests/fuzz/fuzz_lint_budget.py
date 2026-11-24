"""Hypothesis property-based fuzz suite for Gate 4 cognitive budgets, MST fallback & orphan gate."""

from __future__ import annotations

from collections import deque

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
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
)
from archigram.features.lint_budget import (
    CognitiveBudgetAndOrphanGate,
    apply_spanning_tree_edge_fallback,
)
from archigram.ports.contracts import LintGatePort

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_lint_budget",
    input_sha256="a" * 64,
)


def _make_structural_spec(
    *,
    min_count: int = 1,
    max_count: int = 25,
    edge_budget_max: int = 40,
    overflow_strategy: str = "fail",
) -> CurationSpecV1:
    strategy = "spanning_tree_table" if overflow_strategy == "spanning_tree_table" else "fail"
    view = DiagramViewSpec(
        id="structural_view",
        title="Structural View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(
            node_budget=BudgetRange(min_count=min_count, max_count=max_count),
            edge_budget_max=edge_budget_max,
            overflow_strategy=strategy,
        ),
    )
    return CurationSpecV1(views=(view,))


def _make_trace_spec(
    *,
    min_count: int = 1,
    max_count: int = 25,
    edge_budget_max: int = 40,
    max_par_branches: int = 6,
    parallel_epsilon_ns: int = 0,
) -> CurationSpecV1:
    view = DiagramViewSpec(
        id="trace_view",
        title="Trace View",
        ir_mode=IRMode.INTERACTION_TRACE,
        target_renderer=TargetRenderer.MERMAID,
        trace_compaction=TraceCompactionConfig(
            max_par_branches=max_par_branches,
            parallel_epsilon_ns=parallel_epsilon_ns,
        ),
        invariants=ViewInvariants(
            node_budget=BudgetRange(min_count=min_count, max_count=max_count),
            edge_budget_max=edge_budget_max,
        ),
    )
    return CurationSpecV1(views=(view,))


def _weakly_connected_components(graph: StructuralGraphIR) -> list[frozenset[str]]:
    """Independent BFS oracle computing weakly connected components."""
    adj: dict[str, set[str]] = {sym: set() for sym in graph.nodes}
    for edge in graph.edges:
        adj[edge.source].add(edge.target)
        adj[edge.target].add(edge.source)

    visited: set[str] = set()
    components: list[frozenset[str]] = []
    for sym in sorted(graph.nodes):
        if sym in visited:
            continue
        comp: set[str] = set()
        queue: deque[str] = deque([sym])
        while queue:
            curr = queue.popleft()
            if curr in comp:
                continue
            comp.add(curr)
            queue.extend(adj[curr] - comp)
        visited.update(comp)
        components.append(frozenset(comp))
    return components


@st.composite
def structural_graph_strategy(
    draw: st.DrawFn,
    *,
    min_nodes: int = 1,
    max_nodes: int = 18,
    min_edges: int = 0,
    max_edges: int = 45,
) -> StructuralGraphIR:
    """Generate a valid StructuralGraphIR with varied weights and component structure."""
    node_count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    node_ids = [f"n_{idx:02d}" for idx in range(node_count)]
    nodes: dict[str, IRNode] = {}
    for idx, sym_id in enumerate(node_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Node {idx}",
            kind=draw(st.sampled_from([NodeKind.SERVICE, NodeKind.MODULE, NodeKind.CLASS])),
            parent_id=parent_id,
            provenance=_OBS_PROV,
        )

    edge_count = draw(st.integers(min_value=min_edges, max_value=max_edges))
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        src = draw(st.sampled_from(node_ids))
        dst = draw(st.sampled_from(node_ids))
        weight = draw(
            st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False)
        )
        edges.append(
            IREdge(
                edge_id=f"e_{e_idx:03d}",
                source=src,
                target=dst,
                relation=draw(st.sampled_from(list(EdgeRelation))),
                weight=round(weight, 4),
                provenance=_OBS_PROV,
            )
        )
    return StructuralGraphIR(graph_id="fuzz_graph", nodes=nodes, edges=tuple(edges))


@settings(max_examples=80, deadline=None)
@given(
    graph=structural_graph_strategy(min_nodes=2, max_nodes=20, min_edges=0, max_edges=40),
    max_nodes_budget=st.integers(min_value=1, max_value=25),
    edge_budget_max=st.integers(min_value=0, max_value=45),
)
def test_gate4_fail_mode_enforces_node_and_edge_budgets(
    graph: StructuralGraphIR,
    max_nodes_budget: int,
    edge_budget_max: int,
) -> None:
    gate = CognitiveBudgetAndOrphanGate()
    assert isinstance(gate, LintGatePort)
    assert gate.gate_id is GateId.GATE_4_BUDGET_ORPHAN

    spec = _make_structural_spec(
        min_count=1,
        max_count=max_nodes_budget,
        edge_budget_max=edge_budget_max,
        overflow_strategy="fail",
    )
    report = gate.check(graph, spec)

    node_over = len(graph.nodes) > max_nodes_budget
    edge_over = len(graph.edges) > edge_budget_max
    codes = {diag.code for diag in report.diagnostics}

    assert report.passed == (not node_over and not edge_over)
    assert ("NODE_BUDGET_EXCEEDED" in codes) == node_over
    assert ("EDGE_BUDGET_EXCEEDED" in codes) == edge_over


@settings(max_examples=60, deadline=None)
@given(
    min_count=st.integers(min_value=2, max_value=10),
    node_count=st.integers(min_value=0, max_value=4),
)
def test_gate4_node_budget_underrun_emits_error(min_count: int, node_count: int) -> None:
    gate = CognitiveBudgetAndOrphanGate()
    nodes = {
        f"n_{idx}": IRNode(
            symbol_id=f"n_{idx}",
            label=f"N{idx}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )
        for idx in range(node_count)
    }
    graph = StructuralGraphIR(nodes=nodes)
    spec = _make_structural_spec(min_count=min_count, max_count=max(min_count, 20))
    report = gate.check(graph, spec)

    if node_count < min_count:
        assert not report.passed
        assert any(d.code == "NODE_BUDGET_UNDERRUN" for d in report.diagnostics)
    else:
        assert report.passed


@settings(max_examples=100, deadline=None)
@given(
    graph=structural_graph_strategy(min_nodes=2, max_nodes=16, min_edges=1, max_edges=50),
    edge_budget_max=st.integers(min_value=0, max_value=55),
    perm=st.data(),
)
def test_spanning_tree_fallback_preserves_connectivity_budget_and_table(
    graph: StructuralGraphIR,
    edge_budget_max: int,
    perm: st.DataObject,
) -> None:
    orig_components = _weakly_connected_components(graph)
    min_forest_edges = len(graph.nodes) - len(orig_components)

    reduced, table = apply_spanning_tree_edge_fallback(graph, edge_budget_max)
    assert len(reduced.edges) <= edge_budget_max
    assert reduced.nodes == graph.nodes

    if len(graph.edges) <= edge_budget_max:
        assert reduced == graph
        assert table == ""
        return

    assert len(reduced.edges) == edge_budget_max

    if edge_budget_max >= min_forest_edges:
        reduced_components = _weakly_connected_components(reduced)
        assert set(reduced_components) == set(orig_components)

    kept_ids = {edge.edge_id for edge in reduced.edges}
    all_ids = {edge.edge_id for edge in graph.edges}
    omitted_ids = all_ids - kept_ids
    assert len(kept_ids) + len(omitted_ids) == len(graph.edges)

    lines = [line for line in table.splitlines() if line.strip()]
    assert lines[0] == "| Source | Target | Relation | Weight | Provenance |"
    assert lines[1] == "| --- | --- | --- | --- | --- |"
    data_rows = lines[2:]
    assert len(data_rows) == len(omitted_ids)
    for edge in graph.edges:
        if edge.edge_id in omitted_ids:
            assert any(
                f"edge_id={edge.edge_id}" in row and edge.relation.value in row for row in data_rows
            )

    shuffled_edges = tuple(perm.draw(st.permutations(graph.edges)))
    shuffled_graph = graph.model_copy(update={"edges": shuffled_edges})
    reduced_shuffled, table_shuffled = apply_spanning_tree_edge_fallback(
        shuffled_graph,
        edge_budget_max,
    )
    assert reduced_shuffled.edges == reduced.edges
    assert table_shuffled == table

    gate = CognitiveBudgetAndOrphanGate()
    spec = _make_structural_spec(
        min_count=1,
        max_count=max(25, len(graph.nodes)),
        edge_budget_max=edge_budget_max,
        overflow_strategy="spanning_tree_table",
    )
    report = gate.check(graph, spec)
    assert report.passed
    assert report.warning_count == 1
    warn = report.diagnostics[0]
    assert warn.severity is DiagnosticSeverity.WARNING
    assert str(len(omitted_ids)) in warn.message


@settings(max_examples=80, deadline=None)
@given(
    node_count=st.integers(min_value=3, max_value=14),
    target_idx=st.integers(min_value=0, max_value=13),
    extra_edges=st.integers(min_value=0, max_value=12),
)
def test_stripping_edges_from_connected_non_container_triggers_accidental_orphan(
    node_count: int,
    target_idx: int,
    extra_edges: int,
) -> None:
    victim = f"n_{target_idx % node_count:02d}"
    node_ids = [f"n_{idx:02d}" for idx in range(node_count)]
    nodes = {
        sym: IRNode(symbol_id=sym, label=sym, kind=NodeKind.SERVICE, provenance=_OBS_PROV)
        for sym in node_ids
    }

    # Build a ring so every node is guaranteed connected in observed_graph.
    observed_edges = [
        IREdge(
            edge_id=f"ring_{idx}",
            source=node_ids[idx],
            target=node_ids[(idx + 1) % node_count],
            relation=EdgeRelation.CALLS,
            provenance=_OBS_PROV,
        )
        for idx in range(node_count)
    ]
    for e_idx in range(extra_edges):
        observed_edges.append(
            IREdge(
                edge_id=f"extra_{e_idx}",
                source=node_ids[e_idx % node_count],
                target=node_ids[(e_idx + 2) % node_count],
                relation=EdgeRelation.IMPORTS,
                provenance=_OBS_PROV,
            )
        )
    observed = StructuralGraphIR(nodes=nodes, edges=tuple(observed_edges))

    curated_edges = tuple(
        edge for edge in observed_edges if victim not in {edge.source, edge.target}
    )
    curated = StructuralGraphIR(nodes=nodes, edges=curated_edges)

    gate = CognitiveBudgetAndOrphanGate()
    spec = _make_structural_spec(min_count=1, max_count=node_count + 5, edge_budget_max=100)
    report = gate.check(observed, spec, curated_views={"structural_view": curated})

    assert not report.passed
    orphan_diags = [d for d in report.diagnostics if d.code == "ACCIDENTAL_ORPHAN_NODE"]
    assert any(victim in d.offending_symbols and victim in d.message for d in orphan_diags)
    assert all("prune_leaves" in d.repair_hint for d in orphan_diags)


@settings(max_examples=50, deadline=None)
@given(child_count=st.integers(min_value=1, max_value=8))
def test_parent_container_with_zero_incident_edges_not_flagged_as_orphan(
    child_count: int,
) -> None:
    nodes: dict[str, IRNode] = {
        "pkg": IRNode(symbol_id="pkg", label="Package", kind=NodeKind.PACKAGE, provenance=_OBS_PROV)
    }
    for idx in range(child_count):
        cid = f"c_{idx}"
        nodes[cid] = IRNode(
            symbol_id=cid,
            label=cid,
            kind=NodeKind.COMPONENT,
            parent_id="pkg",
            provenance=_OBS_PROV,
        )
    obs_edges = (
        IREdge(
            edge_id="obs_pkg_edge",
            source="pkg",
            target="c_0",
            relation=EdgeRelation.IMPORTS,
            provenance=_OBS_PROV,
        ),
    )
    observed = StructuralGraphIR(nodes=nodes, edges=obs_edges)
    curated = StructuralGraphIR(nodes=nodes, edges=())

    gate = CognitiveBudgetAndOrphanGate()
    spec = _make_structural_spec(min_count=1, max_count=25, edge_budget_max=40)
    report = gate.check(observed, spec, curated_views={"structural_view": curated})
    orphan_symbols = {
        sym
        for d in report.diagnostics
        if d.code == "ACCIDENTAL_ORPHAN_NODE"
        for sym in d.offending_symbols
    }
    assert "pkg" not in orphan_symbols


@settings(max_examples=80, deadline=None)
@given(
    branch_count=st.integers(min_value=2, max_value=12),
    max_par_branches=st.integers(min_value=2, max_value=8),
    use_explicit_attr=st.booleans(),
    attr_key=st.sampled_from(["par_group", "parallel_group_id"]),
)
def test_trace_parallel_branch_budget_enforced(
    branch_count: int,
    max_par_branches: int,
    use_explicit_attr: bool,
    attr_key: str,
) -> None:
    participants = {
        "caller": TraceParticipant(
            participant_id="caller",
            symbol_id="sym_caller",
            label="Caller",
            provenance=_OBS_PROV,
        ),
        "callee": TraceParticipant(
            participant_id="callee",
            symbol_id="sym_callee",
            label="Callee",
            provenance=_OBS_PROV,
        ),
    }
    events: dict[str, TraceEvent] = {}
    for idx in range(branch_count):
        attrs = {attr_key: "fanout_grp"} if use_explicit_attr else {}
        start_ns = 1_000_000 * idx if use_explicit_attr else 100
        end_ns = start_ns + 500
        events[f"ev_{idx:02d}"] = TraceEvent(
            event_id=f"ev_{idx:02d}",
            caller_id="caller",
            callee_id="callee",
            signature=f"call_{idx}()",
            clock_domain_id="host_0",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            attributes=attrs,
            provenance=_OBS_PROV,
        )

    trace = InteractionTraceIR(participants=participants, events=events)
    spec = _make_trace_spec(
        min_count=1,
        max_count=10,
        edge_budget_max=50,
        max_par_branches=max_par_branches,
        parallel_epsilon_ns=0,
    )
    gate = CognitiveBudgetAndOrphanGate()
    report = gate.check(trace, spec)

    exceeded = branch_count > max_par_branches
    par_diags = [d for d in report.diagnostics if d.code == "PAR_BRANCH_BUDGET_EXCEEDED"]
    assert report.passed == (not exceeded)
    assert bool(par_diags) == exceeded
    if exceeded:
        assert "fold_parallel_map" in par_diags[0].repair_hint
        assert set(par_diags[0].offending_symbols) == set(events.keys())


@settings(max_examples=60, deadline=None)
@given(
    participant_count=st.integers(min_value=1, max_value=12),
    event_count=st.integers(min_value=0, max_value=16),
    max_participants=st.integers(min_value=1, max_value=10),
    edge_budget_max=st.integers(min_value=0, max_value=14),
)
def test_trace_participant_and_event_budgets_enforced(
    participant_count: int,
    event_count: int,
    max_participants: int,
    edge_budget_max: int,
) -> None:
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"sym_{idx}",
            label=f"P{idx}",
            provenance=_OBS_PROV,
        )
        for idx in range(participant_count)
    }
    events = {
        f"e_{idx}": TraceEvent(
            event_id=f"e_{idx}",
            caller_id="p_0",
            callee_id=f"p_{idx % participant_count}",
            signature="step()",
            clock_domain_id="host_0",
            start_ns=idx * 100,
            end_ns=idx * 100 + 50,
            provenance=_OBS_PROV,
        )
        for idx in range(event_count)
    }
    trace = InteractionTraceIR(participants=participants, events=events)
    spec = _make_trace_spec(
        min_count=1,
        max_count=max_participants,
        edge_budget_max=edge_budget_max,
        max_par_branches=20,
        parallel_epsilon_ns=0,
    )
    report = CognitiveBudgetAndOrphanGate().check(trace, spec)
    codes = {d.code for d in report.diagnostics}
    p_over = participant_count > max_participants
    e_over = event_count > edge_budget_max

    assert report.passed == (not p_over and not e_over)
    assert ("NODE_BUDGET_EXCEEDED" in codes) == p_over
    assert ("EDGE_BUDGET_EXCEEDED" in codes) == e_over
