"""Performance benchmark suite for Gate 3 topological & partial-order invariant checks."""

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
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.lint_topology.gate import (
    _build_curated_resolver,  # pyright: ignore[reportPrivateUsage]
)

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf-benchmark",
    input_sha256="b" * 64,
)


def _build_400_node_observed() -> tuple[StructuralGraphIR, list[str]]:
    """Build a 400-node layered DAG observed StructuralGraphIR."""
    node_count = 400
    obs_nodes: dict[str, IRNode] = {}
    for idx in range(node_count):
        domain = f"dom{idx // 40}"
        sid = f"{domain}/svc_{idx:03d}"
        obs_nodes[sid] = IRNode(
            symbol_id=sid,
            label=f"Service {idx}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )

    sorted_ids = list(obs_nodes)
    obs_edges: list[IREdge] = []
    for idx in range(node_count - 1):
        obs_edges.append(
            IREdge(
                edge_id=f"e_{len(obs_edges)}",
                source=sorted_ids[idx],
                target=sorted_ids[idx + 1],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.ASYNC_FIRE_FORGET if idx % 20 == 0 else SyncKind.SYNC,
                provenance=_OBS_PROV,
            )
        )
        if idx + 5 < node_count:
            obs_edges.append(
                IREdge(
                    edge_id=f"e_{len(obs_edges)}",
                    source=sorted_ids[idx],
                    target=sorted_ids[idx + 5],
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    provenance=_OBS_PROV,
                )
            )
        if idx + 20 < node_count and idx % 4 == 0:
            obs_edges.append(
                IREdge(
                    edge_id=f"e_{len(obs_edges)}",
                    source=sorted_ids[idx],
                    target=sorted_ids[idx + 20],
                    relation=EdgeRelation.IMPORTS,
                    sync_kind=SyncKind.SYNC,
                    provenance=_OBS_PROV,
                )
            )
    return StructuralGraphIR(nodes=obs_nodes, edges=tuple(obs_edges)), sorted_ids


def _build_contracted_curated(
    observed: StructuralGraphIR,
    sorted_ids: list[str],
    *,
    induce_violations: bool,
) -> tuple[StructuralGraphIR, tuple[CollapsePartitionRule, ...]]:
    """Contract each 40-node domain into a supernode and project cross-domain edges."""
    cur_nodes: dict[str, IRNode] = {}
    member_to_super: dict[str, str] = {}
    collapse_rules: list[CollapsePartitionRule] = []
    for dom_idx in range(10):
        super_id = f"super/dom{dom_idx}"
        members = tuple(sorted_ids[dom_idx * 40 : (dom_idx + 1) * 40])
        for member in members:
            member_to_super[member] = super_id
        cur_nodes[super_id] = IRNode(
            symbol_id=super_id,
            label=f"Domain {dom_idx}",
            kind=NodeKind.SUPERNODE,
            provenance=ProvenanceStamp(
                grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                rule_id=f"collapse-dom-{dom_idx}",
                backing_ids=members,
            ),
        )
        collapse_rules.append(
            CollapsePartitionRule(
                id=f"collapse-dom-{dom_idx}",
                target_node_id=super_id,
                label=f"Domain {dom_idx}",
                member_globs=(f"dom{dom_idx}/*",),
            )
        )

    seen_cur_edges: set[tuple[str, str, EdgeRelation]] = set()
    cur_edges: list[IREdge] = []
    for edge in observed.edges:
        src = member_to_super[edge.source]
        dst = member_to_super[edge.target]
        key = (src, dst, edge.relation)
        if src == dst or key in seen_cur_edges:
            continue
        seen_cur_edges.add(key)
        cur_edges.append(
            IREdge(
                edge_id=f"ce_{len(cur_edges)}",
                source=src,
                target=dst,
                relation=edge.relation,
                sync_kind=edge.sync_kind,
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                    rule_id="project-edges",
                    backing_ids=(edge.edge_id,),
                ),
            )
        )

    if induce_violations:
        cur_edges.append(
            IREdge(
                edge_id="ce_cycle",
                source="super/dom9",
                target="super/dom0",
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                    rule_id="collapse-dom-9",
                    backing_ids=(observed.edges[0].edge_id,),
                ),
            )
        )
    return StructuralGraphIR(nodes=cur_nodes, edges=tuple(cur_edges)), tuple(collapse_rules)


def _build_400_node_benchmark_fixture(
    *,
    induce_violations: bool,
) -> tuple[StructuralGraphIR, CurationSpecV1, dict[str, StructuralGraphIR]]:
    """Build a 400-node layered DAG observed IR and its contracted curated projection."""
    observed, sorted_ids = _build_400_node_observed()
    curated, collapse_rules = _build_contracted_curated(
        observed,
        sorted_ids,
        induce_violations=induce_violations,
    )
    bypass_rule = BypassVerticesRule(
        id="bypass-mid",
        node_id_globs=("dom5/svc_200",),
        preserve_edge_relations=(EdgeRelation.CALLS.value,),
        max_fanout_product=1 if induce_violations else 25,
    )
    assertions = tuple(
        ReachabilityAssertion(
            from_node=sorted_ids[0],
            to_node=sorted_ids[target_idx],
            via_relations=(EdgeRelation.CALLS.value,),
        )
        for target_idx in (50, 150, 250, 399)
    )
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="overview",
                title="400-Node Overview",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=(*collapse_rules, bypass_rule),
                invariants=ViewInvariants(
                    preserve_reachability=assertions,
                    forbid_new_cycles=True,
                    preserve_partial_order=True,
                ),
            ),
        )
    )
    return observed, spec, {"overview": curated}


def _build_200_event_trace_fixture() -> tuple[
    InteractionTraceIR,
    CurationSpecV1,
    dict[str, InteractionTraceIR],
]:
    participants = {
        f"p{i}": TraceParticipant(
            participant_id=f"p{i}",
            symbol_id=f"svc/p{i}",
            label=f"Service {i}",
            provenance=_OBS_PROV,
        )
        for i in range(4)
    }
    events: dict[str, TraceEvent] = {}
    for idx in range(200):
        eid = f"ev_{idx:03d}"
        parent = f"ev_{idx - 1:03d}" if idx > 0 else None
        links = (f"ev_{idx - 4:03d}",) if idx >= 4 and idx % 5 == 0 else ()
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p{idx % 4}",
            callee_id=f"p{(idx + 1) % 4}",
            signature=f"handle_{idx}()",
            parent_event_id=parent,
            causal_links=links,
            clock_domain_id="clk_main",
            start_ns=idx * 100,
            end_ns=idx * 100 + 50,
            sync_kind=SyncKind.ASYNC_FIRE_FORGET if idx % 25 == 0 else SyncKind.SYNC,
            provenance=_OBS_PROV,
        )
    trace = InteractionTraceIR(participants=participants, events=events)
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="trace-view",
                title="200-Event Trace",
                ir_mode=IRMode.INTERACTION_TRACE,
                target_renderer=TargetRenderer.MERMAID,
                invariants=ViewInvariants(preserve_partial_order=True),
            ),
        )
    )
    return trace, spec, {"trace-view": trace}


_GATE = TopologicalInvariantGate()
_OBS_CLEAN, _SPEC_CLEAN, _CUR_CLEAN = _build_400_node_benchmark_fixture(induce_violations=False)
_OBS_BAD, _SPEC_BAD, _CUR_BAD = _build_400_node_benchmark_fixture(induce_violations=True)
_TRACE_OBS, _TRACE_SPEC, _TRACE_CUR = _build_200_event_trace_fixture()


def test_gate3_400_node_structural_clean_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _OBS_CLEAN, _SPEC_CLEAN, _CUR_CLEAN)
    assert report.passed
    assert report.error_count == 0


def test_gate3_400_node_structural_violations_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _OBS_BAD, _SPEC_BAD, _CUR_BAD)
    assert not report.passed
    codes = {d.code for d in report.diagnostics}
    assert "INDUCED_PARTITION_CYCLE" in codes
    assert "BICLIQUE_FANOUT_EXCEEDED" in codes
    assert "ASYNC_UPGRADED_TO_SYNC" in codes


def test_gate3_200_event_trace_poset_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _TRACE_OBS, _TRACE_SPEC, _TRACE_CUR)
    assert report.passed
    assert report.error_count == 0


def _with_required_reachability(
    spec: CurationSpecV1,
    *,
    via_relations: tuple[str, ...],
) -> CurationSpecV1:
    """Copy a spec, promoting its preserve_reachability assertions to require_reachability."""
    views = tuple(
        view.model_copy(
            update={
                "invariants": view.invariants.model_copy(
                    update={
                        "require_reachability": tuple(
                            a.model_copy(update={"via_relations": via_relations})
                            for a in view.invariants.preserve_reachability
                        )
                    }
                )
            }
        )
        for view in spec.views
    )
    return spec.model_copy(update={"views": views})


_SPEC_REQUIRE_OK = _with_required_reachability(_SPEC_CLEAN, via_relations=("calls",))
_SPEC_REQUIRE_UNOBSERVED = _with_required_reachability(_SPEC_CLEAN, via_relations=("writes_data",))


def test_gate3_400_node_require_reachability_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _OBS_CLEAN, _SPEC_REQUIRE_OK, _CUR_CLEAN)
    assert report.passed
    assert not [d for d in report.diagnostics if d.code.startswith("REACHABILITY")]


def test_gate3_400_node_require_reachability_unobserved_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _OBS_CLEAN, _SPEC_REQUIRE_UNOBSERVED, _CUR_CLEAN)
    assert not report.passed
    missing = [d for d in report.diagnostics if d.code == "REACHABILITY_NOT_OBSERVED"]
    assert len(missing) == 4
    assert all(d.witness_path == () for d in missing)


def _build_resolver_fixture() -> tuple[
    StructuralGraphIR, StructuralGraphIR, tuple[BypassVerticesRule | CollapsePartitionRule, ...]
]:
    ids = [f"res/n_{idx:04d}" for idx in range(2000)]
    observed = StructuralGraphIR(
        nodes={
            sid: IRNode(symbol_id=sid, label=sid, kind=NodeKind.MODULE, provenance=_OBS_PROV)
            for sid in ids
        },
        edges=(),
    )
    sup = IRNode(
        symbol_id="res/sup",
        label="sup",
        kind=NodeKind.SUPERNODE,
        provenance=ProvenanceStamp(
            grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
            rule_id="collapse-all",
            backing_ids=("res/n_1999",),
        ),
    )
    rules = (
        BypassVerticesRule(id="bypass-half", node_id_globs=("res/n_0*",)),
        CollapsePartitionRule(
            id="collapse-all", label="all", target_node_id="res/sup", member_globs=("res/n_*",)
        ),
    )
    return observed, StructuralGraphIR(nodes={"res/sup": sup}, edges=()), rules


_RES_OBS, _RES_CUR, _RES_RULES = _build_resolver_fixture()


def test_gate3_resolver_bypass_attribution_benchmark(benchmark: Any) -> None:
    resolver = benchmark(_build_curated_resolver, _RES_OBS, _RES_CUR, _RES_RULES)
    assert "res/n_0001" not in resolver
    assert resolver["res/n_1500"] == "res/sup"
