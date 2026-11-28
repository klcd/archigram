"""Hypothesis property-based fuzz suite for shared pure domain UML helpers."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    EventStatus,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.uml import (
    build_structural_hierarchy,
    number_trace_events,
    partition_use_case_structural,
    partition_use_case_trace,
    shorten_signature,
    structural_start_end_nodes,
)

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_uml_domain_helpers",
    input_sha256="b" * 64,
)


@st.composite
def _structural_ir_strategy(
    draw: st.DrawFn,
    *,
    min_nodes: int = 0,
    max_nodes: int = 10,
) -> tuple[StructuralGraphIR, list[int], list[int]]:
    node_count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    prefixes = ("pkg.svc", "cli.cmd", "mcp.srv", "entrypoint.api", "core.mod")
    kinds = (NodeKind.SERVICE, NodeKind.MODULE, NodeKind.EXTERNAL, NodeKind.PACKAGE)
    raw_nodes: list[IRNode] = []
    for idx in range(node_count):
        prefix = draw(st.sampled_from(prefixes))
        kind = draw(st.sampled_from(kinds))
        parent_id = (
            raw_nodes[draw(st.integers(min_value=0, max_value=idx - 1))].symbol_id
            if idx > 0 and draw(st.booleans())
            else None
        )
        sid = f"{prefix}_{idx}"
        raw_nodes.append(
            IRNode(
                symbol_id=sid,
                label=f"Node {idx}",
                kind=kind,
                parent_id=parent_id,
                provenance=_STAMP,
            )
        )
    node_perm = draw(st.permutations(range(node_count)))
    nodes = {raw_nodes[i].symbol_id: raw_nodes[i] for i in node_perm}

    edge_specs = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=max(0, node_count - 1)),
                st.integers(min_value=0, max_value=max(0, node_count - 1)),
                st.sampled_from(list(EdgeRelation)),
            ),
            max_size=18,
        )
    )
    edges: list[IREdge] = []
    if node_count > 0:
        for idx, (src_i, dst_i, rel) in enumerate(edge_specs):
            edges.append(
                IREdge(
                    edge_id=f"e_{idx}",
                    source=raw_nodes[src_i].symbol_id,
                    target=raw_nodes[dst_i].symbol_id,
                    relation=rel,
                    provenance=_STAMP,
                )
            )
    edge_perm = draw(st.permutations(range(len(edges))))
    return (
        StructuralGraphIR(nodes=nodes, edges=tuple(edges)),
        list(node_perm),
        list(edge_perm),
    )


@st.composite
def _trace_ir_strategy(
    draw: st.DrawFn,
    *,
    min_participants: int = 0,
    max_participants: int = 6,
    max_events: int = 12,
) -> tuple[InteractionTraceIR, list[int]]:
    p_count = draw(st.integers(min_value=min_participants, max_value=max_participants))
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"p_{idx}",
            label=f"Participant {idx}",
            kind=NodeKind.SERVICE,
            provenance=_STAMP,
        )
        for idx in range(p_count)
    }
    ev_count = draw(st.integers(min_value=0, max_value=max_events)) if p_count > 0 else 0
    raw_events: list[TraceEvent] = []
    for idx in range(ev_count):
        caller = f"p_{draw(st.integers(min_value=0, max_value=p_count - 1))}"
        callee = f"p_{draw(st.integers(min_value=0, max_value=p_count - 1))}"
        parent_id = (
            f"ev_{draw(st.integers(min_value=0, max_value=idx - 1))}"
            if idx > 0 and draw(st.booleans())
            else None
        )
        causal = (
            (f"ev_{draw(st.integers(min_value=0, max_value=idx - 1))}",)
            if idx > 0 and draw(st.booleans())
            else ()
        )
        start_ns = draw(st.integers(min_value=1, max_value=500))
        dur_ns = draw(st.integers(min_value=0, max_value=100))
        sig = draw(
            st.sampled_from(
                [
                    f"pkg.mod.op_{idx}(x: int) -> str",
                    f"run_{idx}()",
                    f"step_{idx}",
                ]
            )
        )
        raw_events.append(
            TraceEvent(
                event_id=f"ev_{idx}",
                parent_event_id=parent_id,
                causal_links=causal,
                caller_id=caller,
                callee_id=callee,
                signature=sig,
                sync_kind=draw(st.sampled_from(list(SyncKind))),
                clock_domain_id="host_0",
                start_ns=start_ns,
                end_ns=start_ns + dur_ns,
                status=EventStatus.OK,
                provenance=_STAMP,
            )
        )
    ev_perm = draw(st.permutations(range(ev_count)))
    events = {raw_events[i].event_id: raw_events[i] for i in ev_perm}
    return (
        InteractionTraceIR(trace_id="fuzz_uml_domain", participants=participants, events=events),
        list(ev_perm),
    )


@settings(max_examples=100, deadline=None)
@given(sig=st.text(max_size=120))
def test_shorten_signature_invariants(sig: str) -> None:
    """`shorten_signature` is total, deterministic, non-empty, trimmed, and bounded."""
    out = shorten_signature(sig)
    assert out == shorten_signature(sig)
    assert bool(out)
    assert out == out.strip()
    assert "->" not in out

    base = sig.split("->", 1)[0].strip()
    if not base:
        assert out == "call"
    elif "(" not in base:
        assert len(out) <= 36
    else:
        fn_name = base.split("(", 1)[0].strip().rsplit(".", 1)[-1]
        if fn_name:
            assert out == f"{fn_name}()"
            assert "." not in out
        else:
            assert out == base[:32]


@settings(max_examples=80, deadline=None)
@given(data=_trace_ir_strategy())
def test_number_trace_events_hierarchy_completeness_and_permutation_invariance(
    data: tuple[InteractionTraceIR, list[int]],
) -> None:
    """`number_trace_events` yields complete hierarchical numbering invariant to dict order."""
    trace, _ = data
    numbered = number_trace_events(trace)

    seq_nos = [seq for seq, _ in numbered]
    event_ids = [ev.event_id for _, ev in numbered]
    assert len(numbered) == len(trace.events)
    assert len(set(seq_nos)) == len(seq_nos)
    assert set(event_ids) == set(trace.events)

    seq_by_id = {ev.event_id: seq for seq, ev in numbered}
    pos_by_id = {ev.event_id: idx for idx, (_, ev) in enumerate(numbered)}
    sibling_counters: dict[str | None, int] = {}

    for seq, ev in numbered:
        parts = seq.split(".")
        assert all(part.isdigit() and int(part) >= 1 for part in parts)
        parent = ev.parent_event_id if ev.parent_event_id in trace.events else None
        sibling_counters[parent] = sibling_counters.get(parent, 0) + 1
        if parent is None:
            assert seq == str(sibling_counters[None])
        else:
            parent_seq = seq_by_id[parent]
            assert pos_by_id[parent] < pos_by_id[ev.event_id]
            assert seq == f"{parent_seq}.{sibling_counters[parent]}"

    # Sibling causal_links ordering is preserved within each parent group.
    for ev in trace.events.values():
        for pred_id in ev.causal_links:
            pred = trace.events.get(pred_id)
            if pred is not None and pred.parent_event_id == ev.parent_event_id:
                assert pos_by_id[pred_id] < pos_by_id[ev.event_id]

    # Permutation invariance: canonical sorted insertion order produces identical output.
    canonical_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants={k: trace.participants[k] for k in sorted(trace.participants)},
        events={k: trace.events[k] for k in sorted(trace.events)},
    )
    assert [(s, e.event_id) for s, e in numbered] == [
        (s, e.event_id) for s, e in number_trace_events(canonical_trace)
    ]


@settings(max_examples=80, deadline=None)
@given(data=_structural_ir_strategy(min_nodes=0, max_nodes=10))
def test_structural_start_end_nodes_invariants(
    data: tuple[StructuralGraphIR, list[int], list[int]],
) -> None:
    """`structural_start_end_nodes` deterministically selects valid start and end nodes."""
    ir, _, edge_perm = data
    starts, ends = structural_start_end_nodes(ir)

    if not ir.nodes:
        assert starts == []
        assert ends == []
        return

    sorted_ids = sorted(ir.nodes)
    assert starts
    assert ends
    assert starts == sorted(starts)
    assert ends == sorted(ends)
    assert set(starts) <= set(sorted_ids)
    assert set(ends) <= set(sorted_ids)

    targets = {edge.target for edge in ir.edges}
    sources = {edge.source for edge in ir.edges}
    zero_in = [sid for sid in sorted_ids if sid not in targets]
    zero_out = [sid for sid in sorted_ids if sid not in sources]
    assert starts == (zero_in or [sorted_ids[0]])
    assert ends == (zero_out or [sorted_ids[-1]])

    permuted_ir = StructuralGraphIR(
        nodes={k: ir.nodes[k] for k in sorted(ir.nodes, reverse=True)},
        edges=tuple(ir.edges[i] for i in edge_perm),
    )
    assert (starts, ends) == structural_start_end_nodes(permuted_ir)


@settings(max_examples=80, deadline=None)
@given(data=_structural_ir_strategy(min_nodes=0, max_nodes=10))
def test_partition_use_case_structural_invariants(
    data: tuple[StructuralGraphIR, list[int], list[int]],
) -> None:
    """`partition_use_case_structural` produces an exact, deterministic partition."""
    ir, _, edge_perm = data
    actors, use_cases = partition_use_case_structural(ir)

    sorted_ids = sorted(ir.nodes)
    assert actors == sorted(actors)
    assert use_cases == sorted(use_cases)
    assert set(actors).isdisjoint(use_cases)
    assert sorted([*actors, *use_cases]) == sorted_ids

    if len(sorted_ids) <= 1:
        assert actors == []
        assert use_cases == sorted_ids
    else:
        assert actors
        assert use_cases

    reordered_ir = StructuralGraphIR(
        nodes={k: ir.nodes[k] for k in sorted(ir.nodes, reverse=True)},
        edges=tuple(ir.edges[i] for i in edge_perm),
    )
    assert (actors, use_cases) == partition_use_case_structural(reordered_ir)


@settings(max_examples=80, deadline=None)
@given(data=_trace_ir_strategy(min_participants=0, max_participants=6))
def test_partition_use_case_trace_invariants(
    data: tuple[InteractionTraceIR, list[int]],
) -> None:
    """`partition_use_case_trace` deterministically partitions trace participants."""
    trace, _ = data
    actors, use_cases = partition_use_case_trace(trace)

    sorted_pids = sorted(trace.participants)
    assert actors == sorted(actors)
    assert use_cases == sorted(use_cases)
    assert set(actors) <= set(sorted_pids)
    assert set(use_cases) <= set(sorted_pids)
    assert set(actors) | set(use_cases) == set(sorted_pids)

    if len(sorted_pids) >= 2:
        assert actors
        assert use_cases
        assert set(actors).isdisjoint(use_cases)
    else:
        assert actors == sorted_pids
        assert use_cases == sorted_pids

    canonical_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants={k: trace.participants[k] for k in sorted_pids},
        events={k: trace.events[k] for k in sorted(trace.events)},
    )
    assert (actors, use_cases) == partition_use_case_trace(canonical_trace)


@settings(max_examples=80, deadline=None)
@given(data=_structural_ir_strategy(min_nodes=0, max_nodes=10))
def test_build_structural_hierarchy_conservation_and_order(
    data: tuple[StructuralGraphIR, list[int], list[int]],
) -> None:
    """`build_structural_hierarchy` conserves all nodes in sorted root/child lists."""
    ir, _, _ = data
    roots, children = build_structural_hierarchy(ir.nodes)

    sorted_ids = sorted(ir.nodes)
    assert roots == sorted(roots)
    assert list(children.keys()) == sorted_ids
    for child_list in children.values():
        assert child_list == sorted(child_list)

    all_children = [cid for child_list in children.values() for cid in child_list]
    assert len(roots) + len(all_children) == len(sorted_ids)
    assert sorted([*roots, *all_children]) == sorted_ids
