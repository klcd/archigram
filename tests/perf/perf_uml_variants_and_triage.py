"""Performance benchmarks for UML 2.5 diagram variant emitters and 3-stage triage compilation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from archigram.domain.spec import (
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    IRMode,
    PruneLeavesRule,
    TargetRenderer,
    ViewInvariants,
)
from archigram.entrypoints.cli.triage import compute_triage_stages
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.features.emit_plantuml import PlantUmlEmitter

if TYPE_CHECKING:
    from collections.abc import Callable

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_uml_variants_and_triage",
    input_sha256="b" * 64,
)


def _sample_trace(variant: str, event_count: int = 120) -> InteractionTraceIR:
    participants = {
        f"svc_{idx}": TraceParticipant(
            participant_id=f"svc_{idx}",
            symbol_id=f"svc_{idx}",
            label=f"Service {idx}",
            kind=NodeKind.SERVICE if idx == 0 else NodeKind.MODULE,
            provenance=_STAMP,
        )
        for idx in range(12)
    }
    events = {
        f"ev_{idx}": TraceEvent(
            event_id=f"ev_{idx}",
            caller_id=f"svc_{idx % 11}",
            callee_id=f"svc_{(idx + 1) % 12}",
            signature=f"step_{idx % 8}()",
            sync_kind=SyncKind.FORK_JOIN_BRANCH if idx % 7 == 0 else SyncKind.SYNC,
            clock_domain_id="host_0",
            start_ns=idx * 100,
            end_ns=idx * 100 + 75,
            status=EventStatus.OK,
            attributes={"repeat_count": "2"} if idx % 5 == 0 else {},
            provenance=_STAMP,
        )
        for idx in range(event_count)
    }
    return InteractionTraceIR(
        trace_id=f"perf_{variant}",
        participants=participants,
        events=events,
        metadata={"diagram_variant": variant},
    )


def _sample_structural_graph(node_count: int = 80) -> StructuralGraphIR:
    nodes = {
        f"pkg.mod_{idx}": IRNode(
            symbol_id=f"pkg.mod_{idx}",
            kind=NodeKind.SERVICE if idx < 3 else NodeKind.MODULE,
            label=f"Module {idx}",
            provenance=_STAMP,
        )
        for idx in range(node_count)
    }
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=f"pkg.mod_{idx % (node_count - 1)}",
            target=f"pkg.mod_{(idx + 1) % node_count}",
            relation=EdgeRelation.IMPORTS if idx % 2 == 0 else EdgeRelation.CALLS,
            provenance=_STAMP,
        )
        for idx in range(node_count * 2)
        if (idx % (node_count - 1)) != ((idx + 1) % node_count)
    )
    return StructuralGraphIR(nodes=nodes, edges=edges)


def test_perf_emit_behavioral_uml_variants_120_events(benchmark: Callable[..., Any]) -> None:
    """Benchmark Mermaid, D2, and PlantUML emission across state, activity & communication."""
    mmd = MermaidEmitter()
    d2 = D2Emitter()
    puml = PlantUmlEmitter()
    traces = [
        _sample_trace("state", 120),
        _sample_trace("activity", 120),
        _sample_trace("communication", 120),
    ]

    def _run() -> int:
        total = 0
        for tr in traces:
            total += len(mmd.emit(tr)) + len(d2.emit(tr)) + len(puml.emit(tr))
        return total

    chars = benchmark(_run)
    assert chars > 1000


def test_perf_compute_3stage_structural_triage_80_nodes(benchmark: Callable[..., Any]) -> None:
    """Benchmark 3-stage triage classification and D2/Mermaid emission on an 80-node graph."""
    graph = _sample_structural_graph(80)
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="perf_triage",
                title="Perf Triage",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                diagram_variant=DiagramVariant.PACKAGE,
                rules=(
                    PruneLeavesRule(id="p_leaves", node_id_globs=("pkg.mod_7*",)),
                    CollapsePartitionRule(
                        id="c_core",
                        target_node_id="core_group",
                        label="Core Group",
                        member_globs=("pkg.mod_1*", "pkg.mod_2*", "pkg.mod_3*", "pkg.mod_4*"),
                    ),
                    CollapsePartitionRule(
                        id="c_edge",
                        target_node_id="edge_group",
                        label="Edge Group",
                        member_globs=("pkg.mod_5*", "pkg.mod_6*"),
                    ),
                ),
                invariants=ViewInvariants(),
            ),
        )
    )
    outcome = benchmark(compute_triage_stages, graph, spec, "perf_triage")
    assert outcome.stats.raw_elements == 80
    assert len(outcome.stage2_d2) > len(outcome.stage3_d2)
