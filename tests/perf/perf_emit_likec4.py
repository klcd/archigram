"""Performance benchmarks for LikeC4Emitter on 300-node StructuralGraphIR & 400-event TraceIR."""

from __future__ import annotations

from typing import Any

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
from archigram.features.emit_likec4 import LikeC4Emitter

_BENCH_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_emit_likec4",
    input_sha256="c" * 64,
    file_path="src/architecture.py",
    line_span=(1, 50),
)


def _build_300_node_hierarchical_graph() -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(300):
        sym_id = f"sys.tier_{idx // 30}.svc_{idx}"
        parent_id: str | None = None
        if idx % 30 != 0:
            parent_id = f"sys.tier_{idx // 30}.svc_{(idx // 30) * 30}"
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Service {idx} (tier {idx // 30})",
            kind=NodeKind.CONTAINER if idx % 30 == 0 else NodeKind.SERVICE,
            parent_id=parent_id,
            provenance=_BENCH_STAMP,
        )

    sym_ids = list(nodes)
    edges: list[IREdge] = []
    for idx in range(300):
        src = sym_ids[idx]
        # Target in a different tier to avoid parent-child containment edges
        tgt_idx = (idx + 45) % 300
        tgt = sym_ids[tgt_idx]
        edges.append(
            IREdge(
                edge_id=f"e_{idx:04d}",
                source=src,
                target=tgt,
                relation=EdgeRelation.CALLS if idx % 3 != 0 else EdgeRelation.WRITES_DATA,
                sync_kind=SyncKind.SYNC,
                provenance=_BENCH_STAMP,
            )
        )
    return StructuralGraphIR(
        graph_id="perf_c4_300",
        nodes=nodes,
        edges=tuple(edges),
    )


def _build_400_event_concurrent_trace() -> InteractionTraceIR:
    participant_count = 10
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.actor_{idx}",
            label=f"Service Actor {idx}",
            kind=NodeKind.SERVICE,
            provenance=_BENCH_STAMP,
        )
        for idx in range(participant_count)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(400):
        eid = f"evt_{idx:04d}"
        group = idx // 4
        offset = idx % 4
        # Offset 1 and 2 share parent and overlap in time to generate parallel {} blocks
        if offset in {1, 2}:
            parent_id: str | None = f"evt_{(group * 4):04d}"
            start_ns = group * 5_000 + 100
            end_ns = start_ns + 400
            sig = f"concurrent_fetch_{offset}()"
        else:
            parent_id = None
            start_ns = group * 5_000 + offset * 1_000
            end_ns = start_ns + 500
            sig = f"step_{idx}()"

        caller_idx = group % participant_count
        callee_idx = (group + offset + 1) % participant_count
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{caller_idx}",
            callee_id=f"p_{callee_idx}",
            signature=sig,
            parent_event_id=parent_id,
            clock_domain_id="clock_0",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=_BENCH_STAMP,
        )

    return InteractionTraceIR(
        trace_id="perf_trace_400",
        participants=participants,
        events=events,
    )


_GRAPH_300 = _build_300_node_hierarchical_graph()
_TRACE_400 = _build_400_event_concurrent_trace()
_EMITTER = LikeC4Emitter()


def test_emit_likec4_300_node_hierarchical_structural_graph(benchmark: Any) -> None:
    output: str = benchmark(_EMITTER.emit, _GRAPH_300)
    assert output.startswith("specification {\n")
    assert "model {\n" in output
    assert "views {\n" in output
    assert "view index {\n" in output
    assert "include *\n" in output
    assert "<-" not in output


def test_emit_likec4_400_event_concurrent_interaction_trace(benchmark: Any) -> None:
    output: str = benchmark(_EMITTER.emit, _TRACE_400)
    assert output.startswith("specification {\n")
    assert "dynamic view trace_" in output
    assert "variant sequence\n" in output
    assert "parallel {\n" in output
    assert "<-" not in output
