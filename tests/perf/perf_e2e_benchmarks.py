"""Step 4 End-to-End Throughput & Latency Benchmarks (500-Node Graph & 1,000-Span Trace).

Measures full zero-LLM pipeline performance:
1. Full structural curation + 5-Gate lint + multi-target emission (`.mmd`, `.d2`, `.puml`, `.c4`)
   on a 500-node enterprise dependency graph.
2. Full R1-R6 + Series-Parallel Poset trace compaction + 5-Gate lint + multi-target sequence
   emission on a 1,000-span concurrent multi-service trace.
"""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
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
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    FullScope,
    GlobalAbstractionRule,
    IRMode,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
)
from archigram.entrypoints.cli.pipeline import (
    PipelineCompilationResult,
    compile_and_lint_pipeline,
)
from archigram.features.reduce_trace import collect_trace_backing_ids

_PERF_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_e2e_benchmarks",
    input_sha256="f" * 64,
    file_path="src/enterprise/platform.dot",
    line_span=(1, 1000),
)
_FOUR_RENDERERS: tuple[TargetRenderer, ...] = (
    TargetRenderer.MERMAID,
    TargetRenderer.D2,
    TargetRenderer.PLANTUML,
    TargetRenderer.LIKEC4,
)


def _add_500_node_domain_subsystem(
    d_idx: int,
    nodes: dict[str, IRNode],
    edges: list[IREdge],
) -> None:
    dom = f"dom_{d_idx:02d}"
    bridge_id = f"mw.{dom}_proxy"
    nodes[bridge_id] = IRNode(
        symbol_id=bridge_id,
        label=f"Middleware Proxy {d_idx:02d}",
        kind=NodeKind.COMPONENT,
        provenance=_PERF_STAMP,
    )
    # 16 domain services + 1 middleware bridge + 16 telemetry leaf sinks = 33 nodes/domain
    for s_idx in range(16):
        svc_id = f"{dom}.svc_{s_idx:02d}"
        nodes[svc_id] = IRNode(
            symbol_id=svc_id,
            label=f"Domain {d_idx:02d} Service {s_idx:02d}",
            kind=NodeKind.SERVICE,
            provenance=_PERF_STAMP,
        )
        if s_idx == 0:
            edges.append(
                IREdge(
                    edge_id=f"e_{dom}_mw_to_s0",
                    source=bridge_id,
                    target=svc_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=4.0,
                    provenance=_PERF_STAMP,
                )
            )
        else:
            prev_svc = f"{dom}.svc_{s_idx - 1:02d}"
            edges.append(
                IREdge(
                    edge_id=f"e_{dom}_s{s_idx - 1}_to_s{s_idx}",
                    source=prev_svc,
                    target=svc_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=2.0,
                    provenance=_PERF_STAMP,
                )
            )

    for l_idx in range(16):
        leaf_id = f"telemetry.{dom}_leaf_{l_idx:02d}"
        nodes[leaf_id] = IRNode(
            symbol_id=leaf_id,
            label=f"Telemetry {d_idx:02d} Leaf {l_idx:02d}",
            kind=NodeKind.FUNCTION,
            provenance=_PERF_STAMP,
        )
        edges.append(
            IREdge(
                edge_id=f"e_{dom}_leaf_{l_idx:02d}",
                source=f"{dom}.svc_{l_idx:02d}",
                target=leaf_id,
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                weight=0.5,
                provenance=_PERF_STAMP,
            )
        )


def _build_500_node_enterprise_graph_and_spec() -> tuple[StructuralGraphIR, CurationSpecV1]:
    """Build an exact 500-node enterprise structural graph and 4-renderer CurationSpecV1."""
    nodes: dict[str, IRNode] = {}
    edges: list[IREdge] = []

    entrypoint_id = "edge.api_gateway"
    auth_gateway_id = "edge.auth_ingress"
    core_db_id = "infra.primary_ledger_db"
    replica_db_id = "infra.read_replica_db"
    audit_bus_id = "infra.async_audit_bus"
    for sym_id, lbl, kind in (
        (entrypoint_id, "Edge API Gateway", NodeKind.SERVICE),
        (auth_gateway_id, "Edge Auth Ingress", NodeKind.SERVICE),
        (core_db_id, "Primary Ledger DB", NodeKind.DATABASE),
        (replica_db_id, "Read Replica DB", NodeKind.DATABASE),
        (audit_bus_id, "Async Audit Bus", NodeKind.QUEUE),
    ):
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=lbl,
            kind=kind,
            provenance=_PERF_STAMP,
        )

    edges.append(
        IREdge(
            edge_id="e_entry_to_auth",
            source=entrypoint_id,
            target=auth_gateway_id,
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            weight=6.0,
            provenance=_PERF_STAMP,
        )
    )
    edges.append(
        IREdge(
            edge_id="e_core_to_replica",
            source=core_db_id,
            target=replica_db_id,
            relation=EdgeRelation.WRITES_DATA,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET,
            weight=3.0,
            provenance=_PERF_STAMP,
        )
    )
    edges.append(
        IREdge(
            edge_id="e_entry_to_audit",
            source=entrypoint_id,
            target=audit_bus_id,
            relation=EdgeRelation.ASYNC_SPAWN,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET,
            weight=1.0,
            provenance=_PERF_STAMP,
        )
    )

    # 15 domains * 33 nodes = 495 nodes + 5 edge/infra nodes = 500 nodes exact
    domain_count = 15
    for d_idx in range(domain_count):
        _add_500_node_domain_subsystem(d_idx, nodes, edges)
        dom = f"dom_{d_idx:02d}"
        bridge_id = f"mw.{dom}_proxy"
        tail_svc = f"{dom}.svc_15"
        if d_idx == 0:
            edges.append(
                IREdge(
                    edge_id="e_auth_to_dom00",
                    source=auth_gateway_id,
                    target=bridge_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=5.0,
                    provenance=_PERF_STAMP,
                )
            )
        else:
            prev_tail = f"dom_{d_idx - 1:02d}.svc_15"
            edges.append(
                IREdge(
                    edge_id=f"e_dom_{d_idx - 1:02d}_to_{d_idx:02d}",
                    source=prev_tail,
                    target=bridge_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=4.5,
                    provenance=_PERF_STAMP,
                )
            )
        edges.append(
            IREdge(
                edge_id=f"e_{dom}_to_core_db",
                source=tail_svc,
                target=core_db_id,
                relation=EdgeRelation.WRITES_DATA,
                sync_kind=SyncKind.SYNC,
                weight=3.0,
                provenance=_PERF_STAMP,
            )
        )

    graph = StructuralGraphIR(
        graph_id="enterprise_500_node_benchmark",
        nodes=nodes,
        edges=tuple(edges),
    )

    collapse_rules: list[GlobalAbstractionRule] = [
        CollapsePartitionRule(
            id=f"collapse_dom_{d_idx:02d}",
            target_node_id=f"subsys.dom_{d_idx:02d}",
            label=f"Domain {d_idx:02d} Subsystem",
            member_globs=(f"dom_{d_idx:02d}.*",),
        )
        for d_idx in range(domain_count)
    ]
    invariants = ViewInvariants(
        protected_nodes=(entrypoint_id, "subsys.dom_00", core_db_id),
        preserve_reachability=(ReachabilityAssertion(from_node=entrypoint_id, to_node=core_db_id),),
        forbid_new_cycles=True,
        preserve_partial_order=True,
        node_budget=BudgetRange(min_count=5, max_count=25),
        edge_budget_max=40,
    )
    views = tuple(
        DiagramViewSpec(
            id=f"enterprise_500_{renderer.value}",
            title=f"500-Node Enterprise View ({renderer.value.upper()})",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=renderer,
            scope=FullScope(),
            invariants=invariants,
        )
        for renderer in _FOUR_RENDERERS
    )
    spec = CurationSpecV1(
        global_abstractions=(
            PruneLeavesRule(
                id="prune_telemetry_leaves",
                node_id_globs=("telemetry.*",),
                direction="sink",
            ),
            BypassVerticesRule(
                id="bypass_mw_proxies",
                node_id_globs=("mw.*",),
                preserve_concurrency_kind=True,
            ),
            *collapse_rules,
        ),
        views=views,
    )
    return graph, spec


def _build_1000_span_concurrent_trace_and_spec() -> tuple[InteractionTraceIR, CurationSpecV1]:
    """Build an exact 1,000-span concurrent multi-service trace and sequence CurationSpecV1."""
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=f"Service {pid.title()}",
            kind=NodeKind.SERVICE,
            provenance=_PERF_STAMP,
        )
        for pid in ("gateway", "orchestrator", "worker", "cache", "db")
    }
    events: dict[str, TraceEvent] = {
        "root_0": TraceEvent(
            event_id="root_0",
            caller_id="gateway",
            callee_id="orchestrator",
            signature="POST /v1/execute",
            parent_event_id=None,
            clock_domain_id="hostA:1",
            start_ns=0,
            end_ns=600_000_000,
            sync_kind=SyncKind.SYNC,
            provenance=_PERF_STAMP,
        )
    }

    # 540 contiguous loop spans -> folded to 1 representative event via R1
    cursor = 1_000
    for idx in range(540):
        eid = f"loop_span_{idx:04d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="orchestrator",
            callee_id="cache",
            signature="GET /cache/key",
            parent_event_id="root_0",
            clock_domain_id="hostA:1",
            start_ns=cursor,
            end_ns=cursor + 400,
            sync_kind=SyncKind.SYNC,
            provenance=_PERF_STAMP,
        )
        cursor += 1_000

    # 9 waves * (1 wave_parent + 50 parallel branches) = 459 spans -> 1 + 540 + 459 = 1,000 spans
    for wave in range(9):
        wave_parent = f"wave_parent_{wave:02d}"
        wave_start = cursor + wave * 1_000_000
        events[wave_parent] = TraceEvent(
            event_id=wave_parent,
            caller_id="orchestrator",
            callee_id="orchestrator",
            signature=f"dispatch_wave_{wave}",
            parent_event_id="root_0",
            clock_domain_id="hostA:1",
            start_ns=wave_start,
            end_ns=wave_start + 500_000,
            sync_kind=SyncKind.SYNC,
            provenance=_PERF_STAMP,
        )
        for branch in range(50):
            bid = f"wave_{wave:02d}_branch_{branch:02d}"
            events[bid] = TraceEvent(
                event_id=bid,
                caller_id="orchestrator",
                callee_id="worker",
                signature="process_partition",
                parent_event_id=wave_parent,
                clock_domain_id=f"host_{wave % 3}:1",
                start_ns=wave_start + 1_000 + branch * 10,
                end_ns=wave_start + 250_000,
                sync_kind=SyncKind.ASYNC_FIRE_FORGET,
                provenance=_PERF_STAMP,
            )

    trace = InteractionTraceIR(
        trace_id="enterprise_1000_span_trace",
        participants=participants,
        events=events,
    )
    compaction = TraceCompactionConfig(
        fold_loops=True,
        fold_loop_with_opt=True,
        cut_recursion=True,
        fold_parallel_siblings=True,
        parallel_epsilon_ns=1_000,
        fold_parallel_map=True,
        max_par_branches=6,
        decompose_series_parallel=True,
        max_call_depth=8,
    )
    invariants = ViewInvariants(
        protected_nodes=("gateway", "orchestrator", "worker"),
        preserve_partial_order=True,
        node_budget=BudgetRange(min_count=3, max_count=25),
        edge_budget_max=40,
    )
    views = tuple(
        DiagramViewSpec(
            id=f"trace_1000_{renderer.value}",
            title=f"1,000-Span Concurrent Trace ({renderer.value.upper()})",
            ir_mode=IRMode.INTERACTION_TRACE,
            target_renderer=renderer,
            trace_compaction=compaction,
            invariants=invariants,
        )
        for renderer in _FOUR_RENDERERS
    )
    return trace, CurationSpecV1(views=views)


_GRAPH_500, _SPEC_500 = _build_500_node_enterprise_graph_and_spec()
_TRACE_1000, _TRACE_SPEC_1000 = _build_1000_span_concurrent_trace_and_spec()


def test_perf_e2e_500_node_structural_curation_5gate_lint_and_4_target_emission(
    benchmark: Any,
) -> None:
    """Benchmark full 500-node structural reduction + 5-Gate lint + 4-target emission."""
    assert len(_GRAPH_500.nodes) == 500

    def _run() -> PipelineCompilationResult:
        return compile_and_lint_pipeline(_GRAPH_500, _SPEC_500)

    result = benchmark(_run)
    assert result.lint_report.passed
    assert len(result.views) == 4
    for artifact in result.views.values():
        assert isinstance(artifact.curated_ir, StructuralGraphIR)
        assert len(artifact.curated_ir.nodes) == 20
        assert len(artifact.curated_ir.nodes) <= 25
        assert len(artifact.curated_ir.edges) <= 40
        assert len(artifact.diagram_source) > 0


def test_perf_e2e_1000_span_trace_compaction_5gate_lint_and_sequence_emission(
    benchmark: Any,
) -> None:
    """Benchmark full 1,000-span R1-R6 + SP-Poset compaction + 5-Gate lint + sequence emission."""
    assert len(_TRACE_1000.events) == 1000

    def _run() -> PipelineCompilationResult:
        return compile_and_lint_pipeline(_TRACE_1000, _TRACE_SPEC_1000)

    result = benchmark(_run)
    assert result.lint_report.passed
    assert len(result.views) == 4
    expected_ids = frozenset(_TRACE_1000.events)
    for artifact in result.views.values():
        assert isinstance(artifact.curated_ir, InteractionTraceIR)
        assert len(artifact.curated_ir.participants) <= 25
        assert len(artifact.curated_ir.events) <= 40
        assert collect_trace_backing_ids(artifact.curated_ir) == expected_ids
        assert len(artifact.diagram_source) > 0
