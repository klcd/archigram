"""Performance benchmark suite for Canonical IR v1 validation and JSON serialization."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    ClassMember,
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
    Visibility,
)

_OBSERVED_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="benchmark_fixture",
    input_sha256="a" * 64,
    file_path="src/service.py",
    line_span=(1, 42),
)


def _build_structural_graph(node_count: int = 500) -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(node_count):
        sym_id = f"pkg.mod_{idx // 25}.node_{idx}"
        parent_id = f"pkg.mod_{idx // 25}.node_{(idx // 25) * 25}" if idx % 25 != 0 else None
        is_class = idx % 5 == 0
        members = (
            (
                ClassMember(
                    name="execute",
                    visibility=Visibility.PUBLIC,
                    signature="(payload: bytes) -> bool",
                    is_method=True,
                ),
                ClassMember(
                    name="_state",
                    visibility=Visibility.PRIVATE,
                    signature="dict[str, str]",
                    is_method=False,
                ),
            )
            if is_class
            else ()
        )
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Node {idx}",
            kind=NodeKind.CLASS if is_class else NodeKind.SERVICE,
            parent_id=parent_id,
            members=members,
            attributes={"tier": f"t{idx % 4}"},
            provenance=_OBSERVED_STAMP,
        )

    sym_ids = list(nodes)
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=sym_ids[idx],
            target=sym_ids[(idx + 1) % node_count],
            relation=EdgeRelation.CALLS if idx % 3 != 0 else EdgeRelation.ASYNC_SPAWN,
            sync_kind=SyncKind.SYNC if idx % 3 != 0 else SyncKind.ASYNC_FIRE_FORGET,
            weight=float((idx % 10) + 1),
            parallel_region_id=f"par_{idx % 8}" if idx % 3 == 0 else None,
            provenance=_OBSERVED_STAMP,
        )
        for idx in range(node_count)
    )
    return StructuralGraphIR(graph_id="bench_500", nodes=nodes, edges=edges)


def _build_interaction_trace(event_count: int = 1000) -> InteractionTraceIR:
    participant_count = 16
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.worker_{idx}",
            label=f"Worker {idx}",
            kind=NodeKind.SERVICE,
            provenance=_OBSERVED_STAMP,
        )
        for idx in range(participant_count)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(event_count):
        eid = f"evt_{idx}"
        parent_id = f"evt_{idx - 1}" if idx > 0 and idx % 20 != 0 else None
        links = (f"evt_{idx - 2}",) if idx >= 2 and idx % 7 == 0 else ()
        start_ns = idx * 1_000
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{idx % participant_count}",
            callee_id=f"p_{(idx + 1) % participant_count}",
            signature=f"handle_{idx % 12}()",
            parent_event_id=parent_id,
            causal_links=links,
            clock_domain_id=f"host_{idx % 4}",
            start_ns=start_ns,
            end_ns=start_ns + 500,
            sync_kind=SyncKind.FORK_JOIN_BRANCH if links else SyncKind.SYNC,
            status=EventStatus.OK if idx % 50 != 0 else EventStatus.ERROR,
            provenance=_OBSERVED_STAMP,
        )

    return InteractionTraceIR(trace_id="trace_1000", participants=participants, events=events)


_STRUCTURAL_500 = _build_structural_graph(500)
_STRUCTURAL_500_JSON = _STRUCTURAL_500.model_dump_json()
_TRACE_1000 = _build_interaction_trace(1000)
_TRACE_1000_JSON = _TRACE_1000.model_dump_json()


def test_structural_graph_500_nodes_json_serialization(benchmark: Any) -> None:
    payload = benchmark(_STRUCTURAL_500.model_dump_json)
    assert len(payload) > 0


def test_structural_graph_500_nodes_validation(benchmark: Any) -> None:
    restored = benchmark(StructuralGraphIR.model_validate_json, _STRUCTURAL_500_JSON)
    assert len(restored.nodes) == 500
    assert len(restored.edges) == 500


def test_interaction_trace_1000_events_json_serialization(benchmark: Any) -> None:
    payload = benchmark(_TRACE_1000.model_dump_json)
    assert len(payload) > 0


def test_interaction_trace_1000_events_validation(benchmark: Any) -> None:
    restored = benchmark(InteractionTraceIR.model_validate_json, _TRACE_1000_JSON)
    assert len(restored.events) == 1000
    assert len(restored.participants) == 16
