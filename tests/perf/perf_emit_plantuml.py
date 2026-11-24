"""Performance benchmarks for PlantUmlEmitter on 300-node graphs & 500-event traces."""

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
from archigram.features.emit_plantuml import PlantUmlEmitter

_BENCH_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_emit_plantuml",
    input_sha256="c" * 64,
    file_path="src/architecture.py",
    line_span=(1, 120),
)


def _build_300_node_component_graph() -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(300):
        sym_id = f"domain.tier_{idx // 30}.svc_{idx}"
        parent_id: str | None = None
        if idx % 30 != 0:
            parent_id = f"domain.tier_{idx // 30}.svc_{(idx // 30) * 30}"
        kind = NodeKind.PACKAGE if idx % 30 == 0 else NodeKind.COMPONENT
        if idx % 17 == 0 and parent_id is not None:
            kind = NodeKind.DATABASE
        elif idx % 19 == 0 and parent_id is not None:
            kind = NodeKind.QUEUE
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Service Node {idx} [tier={idx // 30}]",
            kind=kind,
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
    return StructuralGraphIR(graph_id="perf_plantuml_component_300", nodes=nodes, edges=edges)


def _build_300_node_class_graph() -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    kinds = (NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM)
    members = (
        ClassMember(
            name="execute",
            visibility=Visibility.PUBLIC,
            signature="(ctx: Context) -> Result",
            is_method=True,
            generic_params=("T",),
        ),
        ClassMember(
            name="_state",
            visibility=Visibility.PRIVATE,
            signature="dict[str, int]",
            is_method=False,
        ),
    )
    for idx in range(300):
        sym_id = f"pkg.mod_{idx // 25}.Type_{idx}"
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Type_{idx}",
            kind=kinds[idx % len(kinds)],
            members=members,
            attributes={"generic_params": "T, K"} if idx % 5 == 0 else {},
            provenance=_BENCH_STAMP,
        )

    sym_ids = list(nodes)
    relations = (
        EdgeRelation.INHERITS,
        EdgeRelation.IMPLEMENTS,
        EdgeRelation.COMPOSES,
        EdgeRelation.AGGREGATES,
        EdgeRelation.ASSOCIATES,
    )
    edges = tuple(
        IREdge(
            edge_id=f"cls_edge_{idx:04d}",
            source=sym_ids[idx],
            target=sym_ids[(idx + 3) % 300],
            relation=relations[idx % len(relations)],
            sync_kind=SyncKind.SYNC,
            provenance=_BENCH_STAMP,
        )
        for idx in range(300)
    )
    return StructuralGraphIR(
        graph_id="perf_plantuml_class_300",
        nodes=nodes,
        edges=edges,
        metadata={"diagram_variant": "class"},
    )


def _build_500_event_concurrent_trace() -> InteractionTraceIR:
    participant_count = 12
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.actor_{idx}",
            label=f"Actor {idx}",
            kind=NodeKind.QUEUE if idx % 5 == 0 else NodeKind.SERVICE,
            provenance=_BENCH_STAMP,
        )
        for idx in range(participant_count)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(500):
        eid = f"evt_{idx:04d}"
        group = idx // 5
        offset = idx % 5
        if group % 3 == 0 and offset < 3:
            start_ns = group * 10_000 + offset * 50
            end_ns = start_ns + 500
            sig = f"parallel_op_{offset}()"
            clock_domain = "cluster_clock_0"
            callee_id = f"p_{(group + 1 + offset) % participant_count}"
        elif group % 3 == 1 and offset < 2:
            start_ns = group * 10_000
            end_ns = start_ns + 300 + offset * 40
            sig = f"anchor_op_{offset}()"
            clock_domain = f"cross_clock_{offset}"
            callee_id = f"p_{(group + 1 + offset) % participant_count}"
        else:
            start_ns = group * 10_000 + 1_000 + offset * 600
            end_ns = start_ns + 200
            sig = f"repeat_call_{group}()"
            clock_domain = "cluster_clock_0"
            callee_id = f"p_{(group + 1) % participant_count}"

        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{group % participant_count}",
            callee_id=callee_id,
            signature=sig,
            parent_event_id=None,
            clock_domain_id=clock_domain,
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET if idx % 7 == 0 else SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=_BENCH_STAMP,
        )

    return InteractionTraceIR(
        trace_id="perf_plantuml_trace_500",
        participants=participants,
        events=events,
    )


_COMPONENT_GRAPH_300 = _build_300_node_component_graph()
_CLASS_GRAPH_300 = _build_300_node_class_graph()
_TRACE_500 = _build_500_event_concurrent_trace()
_EMITTER = PlantUmlEmitter()


def test_emit_plantuml_300_node_component_elk_graph(benchmark: Any) -> None:
    output = benchmark(_EMITTER.emit, _COMPONENT_GRAPH_300)
    assert output.startswith("@startuml\n!pragma layout elk\n")
    assert output.endswith("\n@enduml\n")
    assert "package " in output


def test_emit_plantuml_300_node_oop_class_graph(benchmark: Any) -> None:
    output = benchmark(_EMITTER.emit, _CLASS_GRAPH_300)
    assert output.startswith("@startuml\n")
    assert output.endswith("\n@enduml\n")
    assert "<|--" in output
    assert "<|.." in output


def test_emit_plantuml_500_event_teoz_concurrent_trace(benchmark: Any) -> None:
    output = benchmark(_EMITTER.emit, _TRACE_500)
    assert output.startswith("@startuml\n!pragma teoz true\n")
    assert output.endswith("\n@enduml\n")
    assert "\npar " in output
    assert "\nloop " in output
    assert "\n& " in output
