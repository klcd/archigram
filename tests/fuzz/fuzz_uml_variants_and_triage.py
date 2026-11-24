"""Hypothesis fuzz suite for UML 2.5 diagram variants, CLI curation, and 3-stage triage."""

from __future__ import annotations

import tempfile
from pathlib import Path

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
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
    parse_curation_spec_yaml,
)
from archigram.entrypoints.cli.curate_cli import (
    delete_view_rule,
    set_view_budget,
    set_view_scope,
    set_view_trace_compaction,
    upsert_view,
    upsert_view_rule,
)
from archigram.entrypoints.cli.triage import compute_triage_stages
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.features.emit_plantuml import PlantUmlEmitter
from archigram.features.lint_syntax.validators import build_default_validators

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_uml_variants_and_triage",
    input_sha256="a" * 64,
)
_FAST_VALIDATORS = build_default_validators(use_cli=False)
_NEW_UML_VARIANTS: tuple[DiagramVariant, ...] = (
    DiagramVariant.PACKAGE,
    DiagramVariant.DEPLOYMENT,
    DiagramVariant.USE_CASE,
    DiagramVariant.COMMUNICATION,
    DiagramVariant.ACTIVITY,
    DiagramVariant.STATE,
)


def _make_structural_ir(
    node_count: int,
    edge_pairs: list[tuple[int, int, EdgeRelation]],
    variant: DiagramVariant,
) -> StructuralGraphIR:
    kinds = (NodeKind.SERVICE, NodeKind.PACKAGE, NodeKind.MODULE, NodeKind.CLASS)
    nodes = {
        f"pkg.node_{idx}": IRNode(
            symbol_id=f"pkg.node_{idx}",
            kind=kinds[idx % len(kinds)],
            label=f"Node {idx}",
            parent_id=None if idx < 2 else "pkg.node_1",
            provenance=_STAMP,
        )
        for idx in range(node_count)
    }
    seen: set[tuple[str, str, EdgeRelation]] = set()
    edges: list[IREdge] = []
    for src_i, dst_i, rel in edge_pairs:
        src = f"pkg.node_{src_i % node_count}"
        dst = f"pkg.node_{dst_i % node_count}"
        if src == dst or (src, dst, rel) in seen:
            continue
        seen.add((src, dst, rel))
        edges.append(
            IREdge(
                edge_id=f"e_{len(edges)}",
                source=src,
                target=dst,
                relation=rel,
                provenance=_STAMP,
            )
        )
    return StructuralGraphIR(
        nodes=nodes,
        edges=tuple(edges),
        metadata={"diagram_variant": variant.value},
    )


def _make_trace_ir(
    participant_count: int,
    event_specs: list[tuple[int, int, SyncKind, int]],
    variant: DiagramVariant,
) -> InteractionTraceIR:
    participants = {
        f"svc_{idx}": TraceParticipant(
            participant_id=f"svc_{idx}",
            symbol_id=f"svc_{idx}",
            label=f"Participant {idx}",
            kind=NodeKind.SERVICE if idx == 0 else NodeKind.MODULE,
            provenance=_STAMP,
        )
        for idx in range(participant_count)
    }
    events: dict[str, TraceEvent] = {}
    for idx, (caller_i, callee_i, sync_k, repeat_n) in enumerate(event_specs):
        caller = f"svc_{caller_i % participant_count}"
        callee = f"svc_{callee_i % participant_count}"
        parent_id = "ev_0" if idx > 1 and sync_k is SyncKind.FORK_JOIN_BRANCH else None
        events[f"ev_{idx}"] = TraceEvent(
            event_id=f"ev_{idx}",
            parent_event_id=parent_id,
            caller_id=caller,
            callee_id=callee,
            signature=f"op_{idx}()",
            sync_kind=sync_k,
            clock_domain_id="host_0",
            start_ns=(idx + 1) * 100,
            end_ns=(idx + 1) * 100 + 60,
            status=EventStatus.OK,
            attributes={"repeat_count": str(repeat_n)} if repeat_n > 1 else {},
            provenance=_STAMP,
        )
    return InteractionTraceIR(
        trace_id="fuzz_trace",
        participants=participants,
        events=events,
        metadata={"diagram_variant": variant.value},
    )


@settings(max_examples=50, deadline=None)
@given(
    variant=st.sampled_from(list(_NEW_UML_VARIANTS)),
    node_count=st.integers(min_value=2, max_value=8),
    edge_pairs=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=7),
            st.integers(min_value=0, max_value=7),
            st.sampled_from(list(EdgeRelation)),
        ),
        min_size=1,
        max_size=12,
    ),
)
def test_structural_uml_variants_determinism_and_gate5_validity(
    variant: DiagramVariant,
    node_count: int,
    edge_pairs: list[tuple[int, int, EdgeRelation]],
) -> None:
    """All 6 UML 2.5 variants emit deterministic, Gate-5-valid Mermaid, D2, and PlantUML."""
    graph = _make_structural_ir(node_count, edge_pairs, variant)
    mmd_emitter = MermaidEmitter()
    d2_emitter = D2Emitter()
    puml_emitter = PlantUmlEmitter()

    mmd_src = mmd_emitter.emit(graph)
    d2_src = d2_emitter.emit(graph)
    puml_src = puml_emitter.emit(graph)

    assert mmd_src == mmd_emitter.emit(graph)
    assert d2_src == d2_emitter.emit(graph)
    assert puml_src == puml_emitter.emit(graph)

    mmd_res = _FAST_VALIDATORS["mermaid"].validate(mmd_src)
    d2_res = _FAST_VALIDATORS["d2"].validate(d2_src)
    puml_res = _FAST_VALIDATORS["plantuml"].validate(puml_src)
    assert mmd_res.valid, mmd_res.errors
    assert d2_res.valid, d2_res.errors
    assert puml_res.valid, puml_res.errors


@settings(max_examples=50, deadline=None)
@given(
    variant=st.sampled_from(list(_NEW_UML_VARIANTS)),
    participant_count=st.integers(min_value=2, max_value=6),
    event_specs=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=5),
            st.sampled_from(list(SyncKind)),
            st.integers(min_value=1, max_value=4),
        ),
        min_size=1,
        max_size=10,
    ),
)
def test_trace_uml_variants_determinism_and_gate5_validity(
    variant: DiagramVariant,
    participant_count: int,
    event_specs: list[tuple[int, int, SyncKind, int]],
) -> None:
    """All 6 UML 2.5 variants over InteractionTraceIR pass Gate 5 across all 3 UML emitters."""
    trace = _make_trace_ir(participant_count, event_specs, variant)
    mmd_src = MermaidEmitter().emit(trace)
    d2_src = D2Emitter().emit(trace)
    puml_src = PlantUmlEmitter().emit(trace)

    assert mmd_src == MermaidEmitter().emit(trace)
    assert d2_src == D2Emitter().emit(trace)
    assert puml_src == PlantUmlEmitter().emit(trace)

    assert _FAST_VALIDATORS["mermaid"].validate(mmd_src).valid
    assert _FAST_VALIDATORS["d2"].validate(d2_src).valid
    assert _FAST_VALIDATORS["plantuml"].validate(puml_src).valid


@settings(max_examples=35, deadline=None)
@given(
    variant=st.sampled_from(list(_NEW_UML_VARIANTS)),
    min_nodes=st.integers(min_value=1, max_value=5),
    extra_nodes=st.integers(min_value=3, max_value=15),
    max_depth=st.integers(min_value=1, max_value=5),
    up_hops=st.integers(min_value=1, max_value=3),
)
def test_curate_cli_mutations_roundtrip_deterministically(
    variant: DiagramVariant,
    min_nodes: int,
    extra_nodes: int,
    max_depth: int,
    up_hops: int,
) -> None:
    """CLI curation helpers persist valid, deterministic CurationSpecV1 YAML across mutations."""
    with tempfile.TemporaryDirectory() as tmp:
        spec_file = Path(tmp) / "spec.yaml"
        view = DiagramViewSpec(
            id="v_struct",
            title="Fuzz View",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=TargetRenderer.D2,
            diagram_variant=variant,
            scope=FullScope(),
            invariants=ViewInvariants(),
        )
        upsert_view(spec_file, view)
        set_view_budget(spec_file, "v_struct", min_nodes, min_nodes + extra_nodes, 50)
        set_view_scope(
            spec_file,
            "v_struct",
            KHopNeighborhoodScope(focal_nodes=("pkg.node_0",), upstream_depth=up_hops),
        )
        upsert_view_rule(
            spec_file,
            "v_struct",
            PruneLeavesRule(id="r_prune", node_id_globs=("pkg.stub_*",)),
        )
        upsert_view_rule(
            spec_file,
            "v_struct",
            BypassVerticesRule(id="r_bypass", node_id_globs=("pkg.mid_*",)),
        )
        upsert_view_rule(
            spec_file,
            "v_struct",
            CollapsePartitionRule(
                id="r_collapse",
                target_node_id="super_pkg",
                label="Super Package",
                member_globs=("pkg.sub_*",),
            ),
        )
        delete_view_rule(spec_file, "v_struct", "r_prune")

        trace_view = DiagramViewSpec(
            id="v_trace",
            title="Trace Fuzz View",
            ir_mode=IRMode.INTERACTION_TRACE,
            target_renderer=TargetRenderer.MERMAID,
            diagram_variant=variant,
        )
        upsert_view(spec_file, trace_view)
        set_view_trace_compaction(
            spec_file,
            "v_trace",
            TraceCompactionConfig(max_call_depth=max_depth),
        )

        parsed = parse_curation_spec_yaml(spec_file.read_text(encoding="utf-8"))
        assert [v.id for v in parsed.views] == ["v_struct", "v_trace"]
        assert [r.id for r in parsed.views[0].rules] == ["r_bypass", "r_collapse"]
        assert parsed.views[1].trace_compaction.max_call_depth == max_depth


@settings(max_examples=35, deadline=None)
@given(
    node_count=st.integers(min_value=4, max_value=9),
    edge_pairs=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=8),
            st.integers(min_value=0, max_value=8),
            st.sampled_from([EdgeRelation.IMPORTS, EdgeRelation.CALLS]),
        ),
        min_size=2,
        max_size=14,
    ),
)
def test_triage_stages_partition_conservation_and_syntax_validity(
    node_count: int,
    edge_pairs: list[tuple[int, int, EdgeRelation]],
) -> None:
    """3-stage triage partitions every observed node and emits Gate-5-valid D2 & Mermaid."""
    observed = _make_structural_ir(node_count, edge_pairs, DiagramVariant.ARCHITECTURE)
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="triage_fuzz",
                title="Triage Fuzz",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                rules=(
                    PruneLeavesRule(id="p0", node_id_globs=("pkg.node_0",)),
                    CollapsePartitionRule(
                        id="c1",
                        target_node_id="collapsed_pair",
                        label="Collapsed Pair",
                        member_globs=("pkg.node_2", "pkg.node_3"),
                    ),
                ),
                invariants=ViewInvariants(),
            ),
        )
    )
    outcome = compute_triage_stages(observed, spec, "triage_fuzz")
    stats = outcome.stats
    total_classified = (
        stats.kept_count + stats.removed_count + stats.bypassed_count + stats.collapsed_count
    )
    assert total_classified == len(observed.nodes)
    assert _FAST_VALIDATORS["d2"].validate(outcome.stage1_d2).valid
    assert _FAST_VALIDATORS["d2"].validate(outcome.stage2_d2).valid
    assert _FAST_VALIDATORS["d2"].validate(outcome.stage3_d2).valid
    assert _FAST_VALIDATORS["mermaid"].validate(outcome.stage3_mermaid).valid
