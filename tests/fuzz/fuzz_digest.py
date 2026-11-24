"""Hypothesis property-based fuzz suite for Compact Graph & Concurrent Trace Digest Generator."""

from __future__ import annotations

import string
from collections import defaultdict

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
from archigram.features.digest import (
    GraphDigestGenerator,
    StructuralDigest,
    TraceDigest,
)

_ID_ALPHABET = string.ascii_lowercase + string.digits + "_."
_SAFE_ID = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=14)
_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_digest",
    input_sha256="f" * 64,
)
_GENERATOR = GraphDigestGenerator()


@st.composite
def structural_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate arbitrary StructuralGraphIR topologies (empty, dense, and modular graphs)."""
    node_count = draw(st.integers(min_value=0, max_value=24))
    node_ids = [f"pkg_{idx % 4}.sym_{idx:02d}" for idx in range(node_count)]
    nodes: dict[str, IRNode] = {}
    for idx, sym_id in enumerate(node_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Label {sym_id}",
            kind=draw(st.sampled_from(list(NodeKind))),
            parent_id=parent_id,
            provenance=_STAMP,
        )

    edges: list[IREdge] = []
    if node_count > 0:
        edge_count = draw(st.integers(min_value=0, max_value=min(48, node_count * 3)))
        for e_idx in range(edge_count):
            edges.append(
                IREdge(
                    edge_id=f"e_{e_idx:03d}",
                    source=draw(st.sampled_from(node_ids)),
                    target=draw(st.sampled_from(node_ids)),
                    relation=draw(st.sampled_from(list(EdgeRelation))),
                    sync_kind=draw(st.sampled_from(list(SyncKind))),
                    weight=draw(st.floats(min_value=0.1, max_value=50.0, allow_nan=False)),
                    provenance=_STAMP,
                )
            )
    return StructuralGraphIR(
        graph_id=draw(_SAFE_ID),
        nodes=nodes,
        edges=tuple(edges),
    )


@st.composite
def interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate arbitrary InteractionTraceIR instances with fanout, loops, and recursion."""
    p_count = draw(st.integers(min_value=1, max_value=8))
    p_ids = [f"p_{idx}" for idx in range(p_count)]
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=f"Service {pid}",
            kind=draw(st.sampled_from(list(NodeKind))),
            provenance=_STAMP,
        )
        for pid in p_ids
    }

    event_count = draw(st.integers(min_value=0, max_value=30))
    e_ids = [f"evt_{idx:02d}" for idx in range(event_count)]
    events: dict[str, TraceEvent] = {}
    for idx, eid in enumerate(e_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = e_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        start_ns = draw(st.integers(min_value=0, max_value=500))
        duration_ns = draw(st.integers(min_value=10, max_value=300))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=draw(st.sampled_from(p_ids)),
            callee_id=draw(st.sampled_from(p_ids)),
            signature=draw(st.sampled_from(["call_a()", "call_b()", "poll_state()"])),
            parent_event_id=parent_id,
            clock_domain_id=draw(st.sampled_from(["clk_0", "clk_1"])),
            start_ns=start_ns,
            end_ns=start_ns + duration_ns,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=draw(st.sampled_from(list(EventStatus))),
            provenance=_STAMP,
        )

    return InteractionTraceIR(
        trace_id=draw(_SAFE_ID),
        participants=participants,
        events=events,
    )


@settings(max_examples=75, deadline=None)
@given(ir=structural_graph_strategy())
def test_structural_digest_determinism_and_permutation_invariance(
    ir: StructuralGraphIR,
) -> None:
    """StructuralDigest is strictly deterministic regardless of node/edge insertion order."""
    digest_a = _GENERATOR.digest_structural(ir)
    reversed_ir = StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=dict(reversed(list(ir.nodes.items()))),
        edges=tuple(reversed(ir.edges)),
        metadata=ir.metadata,
    )
    digest_b = _GENERATOR.digest_structural(reversed_ir)
    assert digest_a == digest_b
    assert _GENERATOR.to_compact_markdown(ir) == digest_a.to_compact_markdown()


@settings(max_examples=75, deadline=None)
@given(ir=structural_graph_strategy())
def test_structural_digest_leaf_sink_and_wrapper_invariants(
    ir: StructuralGraphIR,
) -> None:
    """Every reported leaf sink has out_degree == 0 and every wrapper has 1 <= in*out <= 16."""
    digest = _GENERATOR.digest_structural(ir)
    assert isinstance(digest, StructuralDigest)
    assert digest.node_count == len(ir.nodes)
    assert digest.edge_count == len(ir.edges)

    in_deg: dict[str, int] = dict.fromkeys(ir.nodes, 0)
    out_deg: dict[str, int] = dict.fromkeys(ir.nodes, 0)
    for edge in ir.edges:
        out_deg[edge.source] += 1
        in_deg[edge.target] += 1

    expected_leaves = {sym for sym, d in out_deg.items() if d == 0}
    reported_leaves = {leaf.symbol_id for leaf in digest.candidate_leaf_sinks}
    assert reported_leaves == expected_leaves

    leaf_keys = [(-leaf.in_degree, leaf.symbol_id) for leaf in digest.candidate_leaf_sinks]
    assert leaf_keys == sorted(leaf_keys)
    for leaf in digest.candidate_leaf_sinks:
        assert leaf.out_degree == 0
        assert leaf.in_degree == in_deg[leaf.symbol_id]

    expected_wrappers = {
        sym
        for sym in ir.nodes
        if in_deg[sym] >= 1 and out_deg[sym] >= 1 and in_deg[sym] * out_deg[sym] <= 16
    }
    reported_wrappers = {w.symbol_id for w in digest.candidate_wrappers}
    assert reported_wrappers == expected_wrappers

    wrapper_keys = [
        (w.fanout_product, w.in_degree + w.out_degree, w.symbol_id)
        for w in digest.candidate_wrappers
    ]
    assert wrapper_keys == sorted(wrapper_keys)
    for w in digest.candidate_wrappers:
        assert w.in_degree == in_deg[w.symbol_id]
        assert w.out_degree == out_deg[w.symbol_id]
        assert w.fanout_product == w.in_degree * w.out_degree
        assert 1 <= w.fanout_product <= 16


@settings(max_examples=75, deadline=None)
@given(ir=structural_graph_strategy())
def test_structural_digest_hubs_and_communities_partition_invariants(
    ir: StructuralGraphIR,
) -> None:
    """PageRank hubs are sorted descending and communities form an exact partition of nodes."""
    digest = _GENERATOR.digest_structural(ir)
    assert len(digest.top_hubs) == len(ir.nodes)
    hub_keys = [
        (
            -round(h.pagerank, 8),
            -round(h.betweenness, 8),
            -(h.in_degree + h.out_degree),
            h.symbol_id,
        )
        for h in digest.top_hubs
    ]
    assert hub_keys == sorted(hub_keys)

    all_community_members: list[str] = []
    for comm in digest.communities:
        assert comm.node_count == len(comm.member_symbols)
        assert list(comm.member_symbols) == sorted(comm.member_symbols)
        assert 0.0 <= comm.cohesion_ratio <= 1.0
        all_community_members.extend(comm.member_symbols)

    assert sorted(all_community_members) == sorted(ir.nodes)

    total_containment = sum(c.member_count for c in digest.containment_summary)
    assert total_containment == len(ir.nodes)


@settings(max_examples=75, deadline=None)
@given(ir=interaction_trace_strategy())
def test_trace_digest_invariants_and_determinism(
    ir: InteractionTraceIR,
) -> None:
    """TraceDigest accurately reflects call depth, fanout hotspots, loops, and recursion."""
    digest_a = _GENERATOR.digest(ir)
    assert isinstance(digest_a, TraceDigest)

    reversed_ir = InteractionTraceIR(
        trace_id=ir.trace_id,
        participants=dict(reversed(list(ir.participants.items()))),
        events=dict(reversed(list(ir.events.items()))),
        metadata=ir.metadata,
    )
    digest_b = _GENERATOR.digest_trace(reversed_ir)
    assert digest_a == digest_b

    assert digest_a.participant_count == len(ir.participants)
    assert digest_a.event_count == len(ir.events)
    assert sum(p.calls_initiated for p in digest_a.participants) == len(ir.events)
    assert sum(p.calls_received for p in digest_a.participants) == len(ir.events)

    for fanout in digest_a.concurrent_fanout_hotspots:
        assert fanout.concurrent_degree >= 2
        assert len(fanout.overlapping_event_ids) == fanout.concurrent_degree
        spans = [ir.events[eid] for eid in fanout.overlapping_event_ids]
        max_start = max(s.start_ns for s in spans)
        min_end = min(s.end_ns for s in spans)
        assert max_start <= min_end

    for loop in digest_a.repeating_loop_hotspots:
        assert loop.repeat_count >= 2
        assert len(loop.event_ids) == loop.repeat_count

    for rec in digest_a.recursive_call_hotspots:
        assert rec.max_recursion_depth >= 1
        assert rec.occurrence_count >= 1


@settings(max_examples=40, deadline=None)
@given(
    ir=st.one_of(structural_graph_strategy(), interaction_trace_strategy()),
    max_items=st.integers(min_value=1, max_value=60),
)
def test_compact_markdown_token_budget_bounded(
    ir: StructuralGraphIR | InteractionTraceIR,
    max_items: int,
) -> None:
    """Rendered compact Markdown always stays within the <4,000-token (12,000-char) budget."""
    md = _GENERATOR.to_compact_markdown(ir, max_items_per_section=max_items)
    assert len(md) > 0
    assert len(md) <= 12_000
    by_section = defaultdict(int)
    for line in md.splitlines():
        if line.startswith("## "):
            by_section[line] += 1
    assert len(by_section) >= 5
