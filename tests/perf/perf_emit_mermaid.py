"""Performance benchmarks for MermaidEmitter on 300-node StructuralGraphIR & 500-event TraceIR."""

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
from archigram.features.emit_mermaid import MermaidEmitter

_BENCH_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_emit_mermaid",
    input_sha256="b" * 64,
    file_path="src/topology.py",
    line_span=(1, 100),
)


def _build_300_node_hierarchical_graph() -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(300):
        sym_id = f"domain.tier_{idx // 30}.svc_{idx}"
        parent_id: str | None = None
        if idx % 30 != 0:
            parent_id = f"domain.tier_{idx // 30}.svc_{(idx // 30) * 30}"
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Service Node {idx}; [tier={idx // 30}]",
            kind=NodeKind.CONTAINER if idx % 30 == 0 else NodeKind.SERVICE,
            parent_id=parent_id,
            provenance=_BENCH_STAMP,
        )

    sym_ids = list(nodes)
    edges = tuple(
        IREdge(
            edge_id=f"edge_{idx:04d}",
            source=sym_ids[idx],
            target=sym_ids[(idx + 7) % 300],
            relation=EdgeRelation.CALLS if idx % 4 != 0 else EdgeRelation.ASYNC_SPAWN,
            sync_kind=SyncKind.SYNC if idx % 4 != 0 else SyncKind.ASYNC_FIRE_FORGET,
            provenance=_BENCH_STAMP,
        )
        for idx in range(300)
    )
    return StructuralGraphIR(graph_id="perf_structural_300", nodes=nodes, edges=edges)


def _build_500_event_concurrent_trace() -> InteractionTraceIR:
    participant_count = 12
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.actor_{idx}",
            label=f"Actor {idx} (End)" if idx == 0 else f"Actor {idx}",
            kind=NodeKind.SERVICE,
            provenance=_BENCH_STAMP,
        )
        for idx in range(participant_count)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(500):
        eid = f"evt_{idx:04d}"
        group = idx // 5
        offset = idx % 5
        if group % 2 == 0 and offset < 3:
            start_ns = group * 10_000 + offset * 50
            end_ns = start_ns + 500
            sig = f"parallel_op_{offset}()"
        else:
            start_ns = group * 10_000 + 1_000 + offset * 600
            end_ns = start_ns + 200
            sig = f"repeat_call_{group}()"

        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{group % participant_count}",
            callee_id=f"p_{(group + 1) % participant_count}",
            signature=sig,
            parent_event_id=None,
            clock_domain_id="cluster_clock_0",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET if idx % 7 == 0 else SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=_BENCH_STAMP,
        )

    return InteractionTraceIR(
        trace_id="perf_trace_500",
        participants=participants,
        events=events,
    )


_GRAPH_300 = _build_300_node_hierarchical_graph()
_TRACE_500 = _build_500_event_concurrent_trace()
_EMITTER = MermaidEmitter()


def test_emit_mermaid_300_node_hierarchical_structural_graph(benchmark: Any) -> None:
    output = benchmark(_EMITTER.emit, _GRAPH_300)
    assert output.startswith("---\nconfig:\n  layout: elk\n---\nflowchart TD\n")
    assert "subgraph " in output


def test_emit_mermaid_500_event_concurrent_interaction_trace(benchmark: Any) -> None:
    output = benchmark(_EMITTER.emit, _TRACE_500)
    assert output.startswith("sequenceDiagram\n")
    assert "\n  par " in output
    assert "\n  loop [" in output
