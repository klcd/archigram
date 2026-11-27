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
    _build_trace_order_context,  # pyright: ignore[reportPrivateUsage]
    _observed_projection,  # pyright: ignore[reportPrivateUsage]
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


def _hub_fixture() -> tuple[StructuralGraphIR, StructuralGraphIR]:
    hub = "shared/hub"
    sides = 20
    nodes = {hub: IRNode(symbol_id=hub, label=hub, kind=NodeKind.SERVICE, provenance=_OBS_PROV)}
    edges: list[IREdge] = []
    for idx in range(sides):
        for name, src, dst in (
            (f"in_{idx}", f"up/u{idx}", hub),
            (f"out_{idx}", hub, f"down/d{idx}"),
        ):
            other = src if dst == hub else dst
            nodes[other] = IRNode(
                symbol_id=other, label=other, kind=NodeKind.SERVICE, provenance=_OBS_PROV
            )
            edges.append(
                IREdge(
                    edge_id=name,
                    source=src,
                    target=dst,
                    relation=EdgeRelation.CALLS,
                    sync_kind=SyncKind.SYNC,
                    provenance=_OBS_PROV,
                )
            )
    graph = StructuralGraphIR(nodes=nodes, edges=tuple(edges))
    return graph, graph


def test_gate3_post_collapse_bypass_fanout_benchmark(benchmark: Any) -> None:
    observed, curated = _hub_fixture()
    rule = BypassVerticesRule(id="bypass-hub", node_id_globs=("shared/*",), max_fanout_product=1)
    view = DiagramViewSpec(
        id="v",
        title="V",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        rules=(rule,),
        invariants=ViewInvariants(),
    )
    spec = CurationSpecV1(global_abstractions=(), views=(view,))
    report = benchmark(_GATE.check, observed, spec, {"v": curated})
    assert any(d.code == "BICLIQUE_FANOUT_EXCEEDED" for d in report.diagnostics)


def _build_severed_attribution_fixture() -> tuple[
    StructuralGraphIR, CurationSpecV1, StructuralGraphIR
]:
    ids = [f"svc/n{idx}" for idx in range(300)]
    nodes = {
        nid: IRNode(symbol_id=nid, label=nid, kind=NodeKind.MODULE, provenance=_OBS_PROV)
        for nid in ids
    }
    edges = tuple(
        IREdge(
            edge_id=f"e{idx}",
            source=ids[idx],
            target=ids[idx + 1],
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            provenance=_OBS_PROV,
        )
        for idx in range(len(ids) - 1)
    )
    cut = ids[150]
    curated = StructuralGraphIR(
        nodes={k: v for k, v in nodes.items() if k != cut},
        edges=tuple(e for e in edges if cut not in {e.source, e.target}),
    )
    view = DiagramViewSpec(
        id="arch-view",
        title="Architecture View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        rules=(
            CollapsePartitionRule(
                id="fold-rule", target_node_id="fold/all", label="Fold", member_globs=("fold/*",)
            ),
            BypassVerticesRule(id="bypass-cut", node_id_globs=(cut,)),
        ),
        invariants=ViewInvariants(
            preserve_reachability=(ReachabilityAssertion(from_node=ids[0], to_node=ids[-1]),)
        ),
    )
    return StructuralGraphIR(nodes=nodes, edges=edges), CurationSpecV1(views=(view,)), curated


_OBS_SEV, _SPEC_SEV, _CUR_SEV = _build_severed_attribution_fixture()


def test_gate3_severed_reachability_attribution_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _OBS_SEV, _SPEC_SEV, {"arch-view": _CUR_SEV})
    (diag,) = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert "bypass-cut" in diag.message
    assert "fold-rule" not in diag.message


def _build_sparse_trace_fixture(
    span_count: int = 800,
) -> tuple[InteractionTraceIR, CurationSpecV1, dict[str, InteractionTraceIR]]:
    """Build a 3,200-observed / 2,400-curated sparse multi-span InteractionTraceIR fixture."""
    participants = {
        f"p{i}": TraceParticipant(
            participant_id=f"p{i}",
            symbol_id=f"svc/p{i}",
            label=f"Service {i}",
            provenance=_OBS_PROV,
        )
        for i in range(4)
    }
    obs_events: dict[str, TraceEvent] = {}
    cur_events: dict[str, TraceEvent] = {}
    for span_idx in range(span_count):
        base = span_idx * 4
        for offset in range(4):
            idx = base + offset
            eid = f"sp_{idx:04d}"
            obs_events[eid] = TraceEvent(
                event_id=eid,
                caller_id=f"p{offset}",
                callee_id=f"p{(offset + 1) % 4}",
                signature=f"span_op_{offset}()",
                parent_event_id=f"sp_{idx - 1:04d}" if offset > 0 else None,
                clock_domain_id=f"clk_{span_idx % 8}",
                start_ns=idx * 50,
                end_ns=idx * 50 + 25,
                sync_kind=SyncKind.SYNC,
                provenance=_OBS_PROV,
            )
            if offset == 2:
                continue
            cur_parent = (
                None if offset == 0 else (f"sp_{base:04d}" if offset == 1 else f"sp_{base + 1:04d}")
            )
            cur_events[eid] = TraceEvent(
                event_id=eid,
                caller_id=f"p{offset}",
                callee_id=f"p{(offset + 1) % 4}",
                signature=f"span_op_{offset}()",
                parent_event_id=cur_parent,
                clock_domain_id=f"clk_{span_idx % 8}",
                start_ns=idx * 50,
                end_ns=idx * 50 + 25,
                sync_kind=SyncKind.SYNC,
                provenance=_OBS_PROV,
            )

    observed = InteractionTraceIR(participants=participants, events=obs_events)
    curated = InteractionTraceIR(participants=participants, events=cur_events)
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="sparse-trace",
                title="3200/2400 Sparse Trace",
                ir_mode=IRMode.INTERACTION_TRACE,
                target_renderer=TargetRenderer.MERMAID,
                invariants=ViewInvariants(preserve_partial_order=True),
            ),
        )
    )
    return observed, spec, {"sparse-trace": curated}


_SPARSE_OBS, _SPARSE_SPEC, _SPARSE_CUR = _build_sparse_trace_fixture()
_SPARSE_CTX = _build_trace_order_context(_SPARSE_OBS, _SPARSE_CUR["sparse-trace"])


def test_gate3_2400_event_sparse_trace_poset_benchmark(benchmark: Any) -> None:
    report = benchmark(_GATE.check, _SPARSE_OBS, _SPARSE_SPEC, _SPARSE_CUR)
    assert report.passed
    assert report.error_count == 0


def test_gate3_2400_event_sparse_observed_projection_benchmark(benchmark: Any) -> None:
    succ, pred = benchmark(_observed_projection, _SPARSE_CTX)
    assert len(succ) == 2400
    assert len(pred) == 2400
    assert sum(mask.bit_count() for mask in succ) == 2400
