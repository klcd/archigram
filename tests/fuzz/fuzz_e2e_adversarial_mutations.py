"""Step 4 End-to-End Falsifiable Acceptance & Adversarial Mutation Hypothesis Fuzz Suite.

Proves all 4 falsifiable acceptance guarantees across `apply`, `lint`, `diff`, and `propose`:
1. Byte-Identical Reproducibility on 150-300+ node structural graphs & concurrent traces
   across `.mmd`, `.d2`, `.puml`, and `.c4` under arbitrary input serialization permutation.
2. 100% Grounding & Provenance Fidelity (zero unbacked edges, 100% protected/reachability survival).
3. Cognitive Compression Within Human Budgets (150+ nodes & 500+ concurrent spans ->
   node_budget <= 25, edge_budget_max <= 40, max_par_branches <= 6, Gate 5 syntax valid).
4. 100% Adversarial PR Mutation Detection across Mutations A, B, C, and D via Gates 1-4 & CLI.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import rustworkx as rx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.concurrency import SpanInterval, cluster_overlapping_siblings
from archigram.domain.diagnostics import GateId, LintReport
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
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import (
    EXIT_CLEAN,
    EXIT_LINT_OR_DRIFT,
    app,
)
from archigram.entrypoints.cli.diff_report import compute_pr_regression_diff
from archigram.entrypoints.cli.pipeline import (
    PipelineCompilationResult,
    compile_and_lint_pipeline,
    renderer_file_extension,
    sync_compiled_artifacts,
)
from archigram.entrypoints.mcp import (
    repair_curation_spec_from_diagnostics,
    run_propose_loop,
)
from archigram.features.reduce_trace import collect_trace_backing_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from archigram.domain.diagnostics import LintDiagnostic

_RUNNER = CliRunner()
_FOUR_RENDERERS: tuple[TargetRenderer, ...] = (
    TargetRenderer.MERMAID,
    TargetRenderer.D2,
    TargetRenderer.PLANTUML,
    TargetRenderer.LIKEC4,
)
_OBS_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="step4_e2e_benchmark",
    input_sha256="e" * 64,
    file_path="src/enterprise/topology.dot",
    line_span=(1, 500),
)


@dataclass(frozen=True, slots=True)
class EnterpriseArchitectureFixture:
    """Realistic 150-300+ node enterprise architecture with baseline CurationSpecV1."""

    graph: StructuralGraphIR
    spec: CurationSpecV1
    entrypoint_id: str
    auth_bridge_id: str
    billing_bridge_id: str
    auth_utils_id: str
    billing_utils_id: str
    core_storage_id: str
    async_edge_id: str


def _add_enterprise_domain_tier(
    domain_idx: int,
    services_per_domain: int,
    leaves_per_domain: int,
    nodes: dict[str, IRNode],
    edges: list[IREdge],
) -> None:
    dom = f"dom_{domain_idx:02d}"
    bridge_id = f"mw.{dom}_retry_proxy"
    utils_id = f"{dom}.utils"
    nodes[bridge_id] = IRNode(
        symbol_id=bridge_id,
        label=f"Middleware {dom} Retry Proxy",
        kind=NodeKind.COMPONENT,
        provenance=_OBS_STAMP,
    )
    nodes[utils_id] = IRNode(
        symbol_id=utils_id,
        label=f"Domain {dom} Utils",
        kind=NodeKind.MODULE,
        provenance=_OBS_STAMP,
    )
    for s_idx in range(services_per_domain):
        svc_id = f"{dom}.svc_{s_idx:02d}"
        nodes[svc_id] = IRNode(
            symbol_id=svc_id,
            label=f"Domain {dom} Service {s_idx}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_STAMP,
        )
        if s_idx == 0:
            edges.append(
                IREdge(
                    edge_id=f"e_br_to_{dom}_s0",
                    source=bridge_id,
                    target=svc_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=3.0,
                    provenance=_OBS_STAMP,
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
                    provenance=_OBS_STAMP,
                )
            )
        edges.append(
            IREdge(
                edge_id=f"e_{dom}_s{s_idx}_to_utils",
                source=svc_id,
                target=utils_id,
                relation=EdgeRelation.IMPORTS,
                sync_kind=SyncKind.SYNC,
                weight=1.0,
                provenance=_OBS_STAMP,
            )
        )

    for l_idx in range(leaves_per_domain):
        leaf_id = f"telemetry.{dom}_metric_{l_idx:02d}"
        nodes[leaf_id] = IRNode(
            symbol_id=leaf_id,
            label=f"Telemetry {dom} Metric {l_idx}",
            kind=NodeKind.FUNCTION,
            provenance=_OBS_STAMP,
        )
        caller_svc = f"{dom}.svc_{l_idx % services_per_domain:02d}"
        edges.append(
            IREdge(
                edge_id=f"e_{dom}_leaf_{l_idx:02d}",
                source=caller_svc,
                target=leaf_id,
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                weight=0.5,
                provenance=_OBS_STAMP,
            )
        )


def _build_enterprise_architecture(
    domain_count: int,
    services_per_domain: int,
    leaves_per_domain: int,
) -> EnterpriseArchitectureFixture:
    """Build a 150-300+ node multi-layer enterprise structural graph and 4-renderer spec."""
    nodes: dict[str, IRNode] = {}
    edges: list[IREdge] = []

    entrypoint_id = "edge.api_gateway"
    core_storage_id = "infra.primary_ledger_db"
    audit_bus_id = "infra.async_audit_bus"
    for sym_id, lbl, kind in (
        (entrypoint_id, "Edge API Gateway", NodeKind.SERVICE),
        (core_storage_id, "Primary Ledger DB", NodeKind.DATABASE),
        (audit_bus_id, "Async Audit Event Bus", NodeKind.QUEUE),
    ):
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=lbl,
            kind=kind,
            provenance=_OBS_STAMP,
        )

    for d_idx in range(domain_count):
        _add_enterprise_domain_tier(d_idx, services_per_domain, leaves_per_domain, nodes, edges)
        dom = f"dom_{d_idx:02d}"
        bridge_id = f"mw.{dom}_retry_proxy"
        last_svc = f"{dom}.svc_{services_per_domain - 1:02d}"
        if d_idx == 0:
            edges.append(
                IREdge(
                    edge_id="e_entry_to_dom00",
                    source=entrypoint_id,
                    target=bridge_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=5.0,
                    provenance=_OBS_STAMP,
                )
            )
        else:
            prev_dom = f"dom_{d_idx - 1:02d}"
            prev_utils = f"{prev_dom}.utils"
            edges.append(
                IREdge(
                    edge_id=f"e_{prev_dom}_to_{dom}",
                    source=prev_utils,
                    target=bridge_id,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    weight=4.0,
                    provenance=_OBS_STAMP,
                )
            )
        edges.append(
            IREdge(
                edge_id=f"e_{dom}_to_storage",
                source=last_svc,
                target=core_storage_id,
                relation=EdgeRelation.WRITES_DATA,
                sync_kind=SyncKind.SYNC,
                weight=3.5,
                provenance=_OBS_STAMP,
            )
        )

    async_edge_id = "e_async_gateway_audit"
    edges.append(
        IREdge(
            edge_id=async_edge_id,
            source=entrypoint_id,
            target=audit_bus_id,
            relation=EdgeRelation.ASYNC_SPAWN,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET,
            weight=1.0,
            provenance=_OBS_STAMP,
        )
    )

    graph = StructuralGraphIR(
        graph_id="enterprise_platform_ir",
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
    global_rules: tuple[GlobalAbstractionRule, ...] = (
        PruneLeavesRule(
            id="prune_telemetry_sinks",
            node_id_globs=("telemetry.*",),
            direction="sink",
        ),
        BypassVerticesRule(
            id="bypass_middleware_proxies",
            node_id_globs=("mw.*",),
            preserve_concurrency_kind=True,
        ),
        *collapse_rules,
    )
    invariants = ViewInvariants(
        protected_nodes=(entrypoint_id, "subsys.dom_00", core_storage_id),
        preserve_reachability=(
            ReachabilityAssertion(from_node=entrypoint_id, to_node=core_storage_id),
        ),
        forbid_new_cycles=True,
        preserve_partial_order=True,
        node_budget=BudgetRange(min_count=4, max_count=25),
        edge_budget_max=40,
        overflow_strategy="fail",
    )
    views = tuple(
        DiagramViewSpec(
            id=f"enterprise_{renderer.value}",
            title=f"Enterprise Architecture ({renderer.value.upper()})",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=renderer,
            scope=FullScope(),
            invariants=invariants,
        )
        for renderer in _FOUR_RENDERERS
    )
    spec = CurationSpecV1(
        global_abstractions=global_rules,
        views=views,
    )
    return EnterpriseArchitectureFixture(
        graph=graph,
        spec=spec,
        entrypoint_id=entrypoint_id,
        auth_bridge_id="mw.dom_00_retry_proxy",
        billing_bridge_id="mw.dom_01_retry_proxy",
        auth_utils_id="dom_00.utils",
        billing_utils_id="dom_01.utils",
        core_storage_id=core_storage_id,
        async_edge_id=async_edge_id,
    )


def _build_noisy_500_span_trace(
    loop_count: int,
    fanout_branches: int,
    wave_count: int,
) -> tuple[InteractionTraceIR, CurationSpecV1]:
    """Build a 500+ span concurrent trace with R1 loops and R5/R6 parallel fan-out waves."""
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=f"Service {pid.title()}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_STAMP,
        )
        for pid in ("gateway", "auth", "orchestrator", "worker", "cache", "ledger_db")
    }
    events: dict[str, TraceEvent] = {
        "ev_root": TraceEvent(
            event_id="ev_root",
            caller_id="gateway",
            callee_id="orchestrator",
            signature="POST /v1/checkout",
            parent_event_id=None,
            clock_domain_id="clk_main",
            start_ns=0,
            end_ns=900_000_000,
            sync_kind=SyncKind.SYNC,
            provenance=_OBS_STAMP,
        ),
        "ev_auth": TraceEvent(
            event_id="ev_auth",
            caller_id="orchestrator",
            callee_id="auth",
            signature="verify_token()",
            parent_event_id="ev_root",
            clock_domain_id="clk_main",
            start_ns=100,
            end_ns=500,
            sync_kind=SyncKind.SYNC,
            provenance=_OBS_STAMP,
        ),
    }

    cursor = 1_000
    for idx in range(loop_count):
        eid = f"ev_loop_{idx:04d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="orchestrator",
            callee_id="cache",
            signature="GET /cache/item",
            parent_event_id="ev_root",
            clock_domain_id="clk_main",
            start_ns=cursor,
            end_ns=cursor + 300,
            sync_kind=SyncKind.SYNC,
            provenance=_OBS_STAMP,
        )
        cursor += 1_000

    for wave in range(wave_count):
        w_start = cursor + wave * 500_000
        for branch in range(fanout_branches):
            bid = f"ev_wave_{wave:02d}_b_{branch:03d}"
            events[bid] = TraceEvent(
                event_id=bid,
                caller_id="orchestrator",
                callee_id="worker",
                signature=f"process_shard_wave_{wave}",
                parent_event_id="ev_root",
                clock_domain_id="clk_main",
                start_ns=w_start + branch * 5,
                end_ns=w_start + 200_000,
                sync_kind=SyncKind.ASYNC_FIRE_FORGET,
                provenance=_OBS_STAMP,
            )

    events["ev_commit"] = TraceEvent(
        event_id="ev_commit",
        caller_id="orchestrator",
        callee_id="ledger_db",
        signature="COMMIT tx",
        parent_event_id="ev_root",
        clock_domain_id="clk_main",
        start_ns=cursor + wave_count * 500_000 + 10_000,
        end_ns=cursor + wave_count * 500_000 + 50_000,
        sync_kind=SyncKind.SYNC,
        provenance=_OBS_STAMP,
    )

    trace = InteractionTraceIR(
        trace_id="noisy_500_plus_trace",
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
        protected_nodes=("gateway", "orchestrator", "ledger_db"),
        preserve_partial_order=True,
        node_budget=BudgetRange(min_count=3, max_count=25),
        edge_budget_max=40,
    )
    views = tuple(
        DiagramViewSpec(
            id=f"trace_{renderer.value}",
            title=f"Concurrent Checkout Trace ({renderer.value.upper()})",
            ir_mode=IRMode.INTERACTION_TRACE,
            target_renderer=renderer,
            trace_compaction=compaction,
            invariants=invariants,
        )
        for renderer in _FOUR_RENDERERS
    )
    return trace, CurationSpecV1(views=views)


@st.composite
def _enterprise_dims_strategy(draw: st.DrawFn) -> tuple[int, int, int]:
    domain_count = draw(st.integers(min_value=6, max_value=10))
    services_per_domain = draw(st.integers(min_value=12, max_value=16))
    leaves_per_domain = draw(st.integers(min_value=12, max_value=15))
    return domain_count, services_per_domain, leaves_per_domain


@st.composite
def _noisy_trace_dims_strategy(draw: st.DrawFn) -> tuple[int, int, int]:
    loop_count = draw(st.integers(min_value=330, max_value=380))
    fanout_branches = draw(st.integers(min_value=35, max_value=50))
    wave_count = draw(st.integers(min_value=5, max_value=6))
    return loop_count, fanout_branches, wave_count


def _deterministic_permute[T](items: list[T], seed: int) -> list[T]:
    count = len(items)
    if count <= 1:
        return items
    offset = seed % count
    rotated = items[offset:] + items[:offset]
    if seed % 2 == 1:
        rotated.reverse()
    return [rotated[i] for i in range(0, count, 2)] + [rotated[i] for i in range(1, count, 2)]


def _permute_structural_ir(graph: StructuralGraphIR, seed: int) -> StructuralGraphIR:
    node_items = _deterministic_permute(list(graph.nodes.items()), seed)
    edge_list = _deterministic_permute(list(graph.edges), seed + 7)
    return StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(node_items),
        edges=tuple(edge_list),
    )


def _permute_trace_ir(trace: InteractionTraceIR, seed: int) -> InteractionTraceIR:
    part_items = _deterministic_permute(list(trace.participants.items()), seed)
    ev_items = _deterministic_permute(list(trace.events.items()), seed + 13)
    return InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(part_items),
        events=dict(ev_items),
    )


@given(
    dims=_enterprise_dims_strategy(),
    trace_dims=_noisy_trace_dims_strategy(),
    perm_seed=st.integers(min_value=1, max_value=999_999),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_guarantee_1_byte_identical_reproducibility_across_4_renderers(
    dims: tuple[int, int, int],
    trace_dims: tuple[int, int, int],
    perm_seed: int,
) -> None:
    """Guarantee 1: 150-300+ node graphs & 500+ span traces compile byte-identically."""
    fixture = _build_enterprise_architecture(*dims)
    assert 150 <= len(fixture.graph.nodes) <= 360
    trace_ir, trace_spec = _build_noisy_500_span_trace(*trace_dims)
    assert len(trace_ir.events) >= 500

    permuted_graph = _permute_structural_ir(fixture.graph, perm_seed)
    comp_a = compile_and_lint_pipeline(fixture.graph, fixture.spec)
    comp_b = compile_and_lint_pipeline(permuted_graph, fixture.spec)
    assert comp_a.lint_report.passed
    assert comp_b.lint_report.passed

    for renderer in _FOUR_RENDERERS:
        view_id = f"enterprise_{renderer.value}"
        ext = renderer_file_extension(renderer)
        art_a = comp_a.views[view_id]
        art_b = comp_b.views[view_id]
        assert art_a.file_extension == ext
        assert art_a.diagram_source.encode("utf-8") == art_b.diagram_source.encode("utf-8")
        assert len(art_a.diagram_source.strip()) > 0

    permuted_trace = _permute_trace_ir(trace_ir, perm_seed)
    t_comp_a = compile_and_lint_pipeline(trace_ir, trace_spec)
    t_comp_b = compile_and_lint_pipeline(permuted_trace, trace_spec)
    assert t_comp_a.lint_report.passed
    assert t_comp_b.lint_report.passed

    for renderer in _FOUR_RENDERERS:
        t_view_id = f"trace_{renderer.value}"
        t_art_a = t_comp_a.views[t_view_id]
        t_art_b = t_comp_b.views[t_view_id]
        assert t_art_a.diagram_source.encode("utf-8") == t_art_b.diagram_source.encode("utf-8")

    with tempfile.TemporaryDirectory() as tmp_str:
        out_dir = Path(tmp_str)
        sync_out = sync_compiled_artifacts(comp_a, out_dir, check_only=False)
        check_out = sync_compiled_artifacts(comp_b, out_dir, check_only=True)
        assert sync_out.is_clean
        assert check_out.is_clean


def _verify_structural_grounding_and_reachability(
    observed: StructuralGraphIR,
    curated: StructuralGraphIR,
    view: DiagramViewSpec,
) -> None:
    obs_node_ids = frozenset(observed.nodes)
    obs_edge_ids = frozenset(e.edge_id for e in observed.edges)
    obs_all_ids = obs_node_ids | obs_edge_ids

    for sym_id, node in curated.nodes.items():
        if node.provenance.grade is ProvenanceGrade.OBSERVED:
            assert sym_id in obs_node_ids
        else:
            assert node.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
            assert len(node.provenance.backing_ids) >= 1
            assert any(bid in obs_node_ids for bid in node.provenance.backing_ids)

    for edge in curated.edges:
        if edge.provenance.grade is ProvenanceGrade.OBSERVED:
            assert edge.edge_id in obs_edge_ids
        else:
            assert edge.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
            assert len(edge.provenance.backing_ids) >= 1
            assert any(bid in obs_all_ids for bid in edge.provenance.backing_ids)

    for prot in view.invariants.protected_nodes:
        assert prot in curated.nodes

    graph: rx.PyDiGraph[str, float] = rx.PyDiGraph()
    id_to_idx = {sym: graph.add_node(sym) for sym in sorted(curated.nodes)}
    for edge in curated.edges:
        graph.add_edge(id_to_idx[edge.source], id_to_idx[edge.target], 1.0)

    for reach in view.invariants.preserve_reachability:
        assert rx.has_path(graph, id_to_idx[reach.from_node], id_to_idx[reach.to_node])


@given(
    dims=_enterprise_dims_strategy(),
    trace_dims=_noisy_trace_dims_strategy(),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_guarantee_2_and_3_grounding_fidelity_and_cognitive_compression(
    dims: tuple[int, int, int],
    trace_dims: tuple[int, int, int],
) -> None:
    """Guarantees 2 & 3: 100% provenance grounding + survival, and compression within budgets."""
    fixture = _build_enterprise_architecture(*dims)
    comp = compile_and_lint_pipeline(fixture.graph, fixture.spec)
    assert comp.lint_report.passed

    for artifact in comp.views.values():
        assert isinstance(artifact.curated_ir, StructuralGraphIR)
        _verify_structural_grounding_and_reachability(
            fixture.graph,
            artifact.curated_ir,
            artifact.view,
        )
        assert len(artifact.curated_ir.nodes) <= 25
        assert len(artifact.curated_ir.edges) <= 40

    trace_ir, trace_spec = _build_noisy_500_span_trace(*trace_dims)
    t_comp = compile_and_lint_pipeline(trace_ir, trace_spec)
    assert t_comp.lint_report.passed
    obs_event_ids = frozenset(trace_ir.events)

    for t_art in t_comp.views.values():
        assert isinstance(t_art.curated_ir, InteractionTraceIR)
        assert len(t_art.curated_ir.participants) <= 25
        assert len(t_art.curated_ir.events) <= 40
        assert collect_trace_backing_ids(t_art.curated_ir).issubset(obs_event_ids)
        for ev in t_art.curated_ir.events.values():
            if ev.provenance.grade is ProvenanceGrade.OBSERVED:
                assert ev.event_id in obs_event_ids
            else:
                assert len(ev.provenance.backing_ids) >= 1
                assert set(ev.provenance.backing_ids).issubset(obs_event_ids)

        intervals = [
            SpanInterval(
                event_id=ev.event_id,
                parent_event_id=ev.parent_event_id,
                clock_domain_id=ev.clock_domain_id,
                start_ns=ev.start_ns,
                end_ns=ev.end_ns,
                callee_id=ev.callee_id,
                signature=ev.signature,
            )
            for ev in t_art.curated_ir.events.values()
        ]
        clusters = cluster_overlapping_siblings(
            intervals,
            epsilon_ns=t_art.view.trace_compaction.parallel_epsilon_ns,
        )
        max_branches = max((len(c.event_ids) for c in clusters), default=0)
        assert max_branches <= 6


def _mutate_symbol_drift_protected(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    base_graph = fixture.graph
    renamed = f"{fixture.entrypoint_id}_drifted_v2"
    new_nodes = {
        (renamed if k == fixture.entrypoint_id else k): (
            v.model_copy(update={"symbol_id": renamed}) if k == fixture.entrypoint_id else v
        )
        for k, v in base_graph.nodes.items()
    }
    new_edges = tuple(
        e.model_copy(
            update={
                "source": renamed if e.source == fixture.entrypoint_id else e.source,
                "target": renamed if e.target == fixture.entrypoint_id else e.target,
            }
        )
        for e in base_graph.edges
    )
    mut_graph = StructuralGraphIR(
        graph_id=base_graph.graph_id,
        nodes=new_nodes,
        edges=new_edges,
    )
    return mut_graph, fixture.spec, GateId.GATE_1_GROUNDING, "UNKNOWN_SYMBOL_ID"


def _mutate_symbol_drift_focal(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    base_spec = fixture.spec
    view0 = base_spec.views[0].model_copy(
        update={
            "scope": KHopNeighborhoodScope(
                focal_nodes=("ghost.renamed.focal_symbol_xyz",),
                upstream_depth=2,
                downstream_depth=2,
            )
        }
    )
    mut_spec = base_spec.model_copy(update={"views": (view0, *base_spec.views[1:])})
    return fixture.graph, mut_spec, GateId.GATE_1_GROUNDING, "UNKNOWN_SYMBOL_ID"


def _mutate_naive_bridge_prune(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    replaced_globals = tuple(
        PruneLeavesRule(
            id="naive_prune_middleware_bridges",
            node_id_globs=("mw.*",),
            direction="sink",
        )
        if isinstance(r, BypassVerticesRule)
        else r
        for r in fixture.spec.global_abstractions
    )
    mut_spec = fixture.spec.model_copy(update={"global_abstractions": replaced_globals})
    return fixture.graph, mut_spec, GateId.GATE_2_BOUNDARY_LEAF, "PRUNE_INTERNAL_BRIDGE"


def _mutate_false_cycle_merge(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    # Add an edge from subsys.shared_utils target back into dom_00.svc_00 so merging
    # dom_00.utils and dom_01.utils across dom_00 -> dom_01 induces a 2-supernode cycle:
    # subsys.dom_00 -> subsys.shared_utils -> subsys.dom_01 -> subsys.shared_utils
    base_graph = fixture.graph
    cycle_edges = (
        *base_graph.edges,
        IREdge(
            edge_id="e_dom00_utils_to_dom01_s0",
            source=fixture.auth_utils_id,
            target="dom_01.svc_00",
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            weight=2.0,
            provenance=_OBS_STAMP,
        ),
    )
    acyclic_graph = base_graph.model_copy(update={"edges": cycle_edges})

    updated_globals: list[GlobalAbstractionRule] = []
    for rule in fixture.spec.global_abstractions:
        if isinstance(rule, CollapsePartitionRule) and rule.id in {
            "collapse_dom_00",
            "collapse_dom_01",
        }:
            excluded = (
                fixture.auth_utils_id if rule.id == "collapse_dom_00" else fixture.billing_utils_id
            )
            updated_globals.append(rule.model_copy(update={"exclude_ids": (excluded,)}))
        else:
            updated_globals.append(rule)
    updated_globals.append(
        CollapsePartitionRule(
            id="collapse_shared_utils_cycle",
            target_node_id="subsys.shared_utils",
            label="Shared Utils Supernode",
            member_globs=(fixture.auth_utils_id, fixture.billing_utils_id),
        )
    )
    no_condense_views = tuple(
        v.model_copy(
            update={
                "simplify": v.simplify.model_copy(
                    update={"condense_sccs": False, "transitive_reduction": False}
                )
            }
        )
        for v in fixture.spec.views
    )
    mut_spec = fixture.spec.model_copy(
        update={
            "global_abstractions": tuple(updated_globals),
            "views": no_condense_views,
        }
    )
    return acyclic_graph, mut_spec, GateId.GATE_3_TOPOLOGY_POSET, "INDUCED_PARTITION_CYCLE"


def _mutate_async_to_sync_corruption(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    # In the full CLI pipeline, test biclique fanout + async->sync corruption on Gate 3,
    # while also directly validating ASYNC_UPGRADED_TO_SYNC when an ASYNC_FIRE_FORGET
    # backing edge is upgraded to SYNC in the curated view.
    base_graph = fixture.graph
    extra_edges = (
        *base_graph.edges,
        IREdge(
            edge_id="e_fanout_extra_in",
            source="dom_01.utils",
            target=fixture.auth_bridge_id,
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET,
            weight=1.0,
            provenance=_OBS_STAMP,
        ),
        IREdge(
            edge_id="e_fanout_extra_out",
            source=fixture.auth_bridge_id,
            target="dom_00.svc_01",
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            weight=1.0,
            provenance=_OBS_STAMP,
        ),
    )
    fanout_graph = base_graph.model_copy(update={"edges": extra_edges})
    corrupted_globals = tuple(
        r.model_copy(
            update={
                "preserve_concurrency_kind": False,
                "max_fanout_product": 1,
            }
        )
        if isinstance(r, BypassVerticesRule)
        else r
        for r in fixture.spec.global_abstractions
    )
    mut_spec = fixture.spec.model_copy(update={"global_abstractions": corrupted_globals})
    return fanout_graph, mut_spec, GateId.GATE_3_TOPOLOGY_POSET, "BICLIQUE_FANOUT_EXCEEDED"


def _mutate_budget_blowout(
    fixture: EnterpriseArchitectureFixture,
) -> tuple[StructuralGraphIR, CurationSpecV1, GateId, str]:
    uncollapsed_globals = tuple(
        r for r in fixture.spec.global_abstractions if not isinstance(r, CollapsePartitionRule)
    )
    uncollapsed_views = tuple(
        v.model_copy(
            update={
                "invariants": v.invariants.model_copy(
                    update={"protected_nodes": (fixture.entrypoint_id, fixture.core_storage_id)}
                )
            }
        )
        for v in fixture.spec.views
    )
    mut_spec = fixture.spec.model_copy(
        update={
            "global_abstractions": uncollapsed_globals,
            "views": uncollapsed_views,
        }
    )
    return fixture.graph, mut_spec, GateId.GATE_4_BUDGET_ORPHAN, "NODE_BUDGET_EXCEEDED"


_MUTATION_BUILDERS: dict[
    str,
    Callable[
        [EnterpriseArchitectureFixture],
        tuple[StructuralGraphIR, CurationSpecV1, GateId, str],
    ],
] = {
    "mutation_a_symbol_drift_protected": _mutate_symbol_drift_protected,
    "mutation_a_symbol_drift_focal": _mutate_symbol_drift_focal,
    "mutation_b_naive_bridge_prune": _mutate_naive_bridge_prune,
    "mutation_c_false_cycle_merge": _mutate_false_cycle_merge,
    "mutation_d_async_to_sync_corruption": _mutate_async_to_sync_corruption,
    "mutation_d_budget_blowout": _mutate_budget_blowout,
}


def _assert_mutation_specific_invariants(
    fixture: EnterpriseArchitectureFixture,
    mutation_kind: str,
    baseline_comp: PipelineCompilationResult,
    mut_comp: PipelineCompilationResult,
) -> None:
    from archigram.features.lint_topology import TopologicalInvariantGate

    if mutation_kind == "mutation_b_naive_bridge_prune":
        severed_nodes = {k: v for k, v in fixture.graph.nodes.items() if not k.startswith("mw.")}
        severed_edges = tuple(
            e
            for e in fixture.graph.edges
            if not e.source.startswith("mw.") and not e.target.startswith("mw.")
        )
        severed_curated = StructuralGraphIR(
            graph_id=fixture.graph.graph_id,
            nodes=severed_nodes,
            edges=severed_edges,
        )
        g3_rep = TopologicalInvariantGate().check(
            fixture.graph,
            fixture.spec,
            curated_views={v.id: severed_curated for v in fixture.spec.views},
        )
        assert not g3_rep.passed
        assert any(d.code == "REACHABILITY_SEVERED" for d in g3_rep.diagnostics)
    elif mutation_kind == "mutation_c_false_cycle_merge":
        cycle_diags = [
            d for d in mut_comp.lint_report.diagnostics if d.code == "INDUCED_PARTITION_CYCLE"
        ]
        assert len(cycle_diags) >= 1
        assert len(cycle_diags[0].witness_path) >= 3
    elif mutation_kind == "mutation_d_async_to_sync_corruption":
        corrupted_curated_views: dict[str, StructuralGraphIR] = {}
        for v_id, art in baseline_comp.views.items():
            assert isinstance(art.curated_ir, StructuralGraphIR)
            upgraded_edges = tuple(
                e.model_copy(update={"sync_kind": SyncKind.SYNC})
                if e.edge_id == fixture.async_edge_id
                else e
                for e in art.curated_ir.edges
            )
            corrupted_curated_views[v_id] = art.curated_ir.model_copy(
                update={"edges": upgraded_edges}
            )
        async_rep = TopologicalInvariantGate().check(
            fixture.graph,
            fixture.spec,
            curated_views=corrupted_curated_views,
        )
        assert not async_rep.passed
        assert any(
            d.gate is GateId.GATE_3_TOPOLOGY_POSET and d.code == "ASYNC_UPGRADED_TO_SYNC"
            for d in async_rep.diagnostics
        )


@given(
    dims=_enterprise_dims_strategy(),
    mutation_kind=st.sampled_from(tuple(_MUTATION_BUILDERS.keys())),
)
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_guarantee_4_100_percent_adversarial_mutation_detection_and_cli_gates(
    dims: tuple[int, int, int],
    mutation_kind: str,
) -> None:
    """Guarantee 4: All 4 adversarial PR mutations trigger Gate 1-4 failures (CLI exit 1)."""
    fixture = _build_enterprise_architecture(*dims)
    baseline_comp = compile_and_lint_pipeline(fixture.graph, fixture.spec)
    assert baseline_comp.lint_report.passed

    mut_graph, mut_spec, expected_gate, expected_code = _MUTATION_BUILDERS[mutation_kind](fixture)
    mut_comp = compile_and_lint_pipeline(mut_graph, mut_spec)
    assert not mut_comp.lint_report.passed
    diag_gates = {d.gate for d in mut_comp.lint_report.diagnostics}
    diag_codes = {d.code for d in mut_comp.lint_report.diagnostics}
    assert expected_gate in diag_gates
    assert expected_code in diag_codes

    _assert_mutation_specific_invariants(fixture, mutation_kind, baseline_comp, mut_comp)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        base_ir_file = tmp_dir / "base.ir.json"
        mut_ir_file = tmp_dir / "mut.ir.json"
        mut_spec_file = tmp_dir / "mut-views.yaml"
        base_ir_file.write_text(fixture.graph.model_dump_json(), encoding="utf-8")
        mut_ir_file.write_text(mut_graph.model_dump_json(), encoding="utf-8")
        mut_spec_file.write_text(dump_curation_spec_yaml(mut_spec), encoding="utf-8")

        lint_res = _RUNNER.invoke(
            app,
            ["lint", str(mut_ir_file), "--spec", str(mut_spec_file), "--json"],
        )
        assert lint_res.exit_code == EXIT_LINT_OR_DRIFT
        parsed_report = LintReport.model_validate_json(lint_res.stdout)
        assert not parsed_report.passed

        apply_res = _RUNNER.invoke(
            app,
            [
                "apply",
                str(mut_ir_file),
                "--spec",
                str(mut_spec_file),
                "--out-dir",
                str(tmp_dir / "out"),
            ],
        )
        assert apply_res.exit_code == EXIT_LINT_OR_DRIFT

        diff_summary = compute_pr_regression_diff(fixture.graph, mut_graph, mut_spec)
        # Regression means newly introduced on head; diagnostics that the (shared) mutated spec
        # already produces on base are pre-existing and must not fail the diff.
        assert not diff_summary.head_passed
        assert diff_summary.passed is (not diff_summary.new_errors)


class _EnterpriseSelfHealingProposer:
    """Proposer exercising a Turn 1 Gate 1 typo -> Turn 2 self-repaired enterprise spec."""

    def __init__(self, fixture: EnterpriseArchitectureFixture) -> None:
        self._fixture = fixture

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        assert len(digest_markdown) > 0
        if turn == 1:
            bad_view = self._fixture.spec.views[0].model_copy(
                update={
                    "invariants": self._fixture.spec.views[0].invariants.model_copy(
                        update={"protected_nodes": (f"{self._fixture.entrypoint_id}_typo",)}
                    )
                }
            )
            bad_spec = self._fixture.spec.model_copy(update={"views": (bad_view,)})
            return dump_curation_spec_yaml(bad_spec)

        assert prior_spec_yaml is not None
        repaired = repair_curation_spec_from_diagnostics(
            prior_spec_yaml,
            diagnostics,
            self._fixture.graph,
        )
        return dump_curation_spec_yaml(repaired)


@given(
    dims=_enterprise_dims_strategy(),
    trace_dims=_noisy_trace_dims_strategy(),
)
@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_end_to_end_propose_apply_lint_diff_lifecycle(
    dims: tuple[int, int, int],
    trace_dims: tuple[int, int, int],
) -> None:
    """End-to-end `propose` -> `apply` -> `apply --check` -> `lint` -> `diff` pipeline exits 0."""
    fixture = _build_enterprise_architecture(*dims)
    proposer = _EnterpriseSelfHealingProposer(fixture)
    propose_res = run_propose_loop(fixture.graph, proposer=proposer, max_turns=3)
    assert propose_res.succeeded
    assert propose_res.turns_used == 2
    assert propose_res.spec is not None

    trace_ir, _ = _build_noisy_500_span_trace(*trace_dims)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        trace_file = tmp_dir / "trace.ir.json"
        trace_spec_file = tmp_dir / "trace-proposed.yaml"
        trace_file.write_text(trace_ir.model_dump_json(), encoding="utf-8")

        cli_propose = _RUNNER.invoke(
            app,
            ["propose", str(trace_file), "--out", str(trace_spec_file), "--max-turns", "3"],
        )
        assert cli_propose.exit_code == EXIT_CLEAN, cli_propose.output
        assert trace_spec_file.is_file()

        ir_file = tmp_dir / "enterprise.ir.json"
        spec_file = tmp_dir / "enterprise-views.yaml"
        out_dir = tmp_dir / "diagrams"
        ir_file.write_text(fixture.graph.model_dump_json(), encoding="utf-8")
        spec_file.write_text(propose_res.spec_yaml, encoding="utf-8")

        cli_apply = _RUNNER.invoke(
            app,
            ["apply", str(ir_file), "--spec", str(spec_file), "--out-dir", str(out_dir)],
        )
        assert cli_apply.exit_code == EXIT_CLEAN, cli_apply.output

        cli_check = _RUNNER.invoke(
            app,
            [
                "apply",
                str(ir_file),
                "--spec",
                str(spec_file),
                "--out-dir",
                str(out_dir),
                "--check",
            ],
        )
        assert cli_check.exit_code == EXIT_CLEAN, cli_check.output

        cli_diff = _RUNNER.invoke(
            app,
            [
                "diff",
                "--base",
                str(ir_file),
                "--head",
                str(ir_file),
                "--spec",
                str(spec_file),
            ],
        )
        assert cli_diff.exit_code == EXIT_CLEAN, cli_diff.output
