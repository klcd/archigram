"""Hypothesis property-based fuzz suite for self-hosted ArchiGram dogfooding & fixes."""

from __future__ import annotations

import importlib
import string
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import GateId
from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    KHopNeighborhoodScope,
    TargetRenderer,
    ViewInvariants,
    parse_curation_spec_yaml,
)
from archigram.entrypoints.cli.markdown_sync import sync_markdown_views
from archigram.entrypoints.cli.pipeline import compile_and_lint_pipeline
from archigram.entrypoints.cli.self_trace import capture_self_hosted_pipeline_trace
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.features.emit_plantuml import PlantUmlEmitter
from archigram.features.ingest_python import (
    PythonAstStructuralIngestor,
    capture_python_trace,
)
from archigram.features.lint_grounding import GroundingAndBoundaryGate
from archigram.features.reduce_structural import StructuralTopologicalReducer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ARCHIGRAM = _REPO_ROOT / "src" / "archigram"
_DOCS_ARCH = _REPO_ROOT / "docs" / "architecture"
_DIAGRAMS_DIR = _DOCS_ARCH / "diagrams"
_DIAGRAM_SPEC_PATH = _DOCS_ARCH / "diagram-views.yaml"
_TRACE_SPEC_PATH = _DOCS_ARCH / "trace-views.yaml"
_TRACE_SNAPSHOT_PATH = _DOCS_ARCH / "traces" / "pipeline-and-repair.ir.json"

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_self_dogfood",
    input_sha256="b" * 64,
)

_IDENT = st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=8)


@lru_cache(maxsize=1)
def _cached_self_ir() -> StructuralGraphIR:
    return PythonAstStructuralIngestor().ingest(_SRC_ARCHIGRAM)


@lru_cache(maxsize=1)
def _cached_diagram_spec() -> CurationSpecV1:
    return parse_curation_spec_yaml(_DIAGRAM_SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _cached_trace_spec() -> CurationSpecV1:
    return parse_curation_spec_yaml(_TRACE_SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _cached_trace_snapshot() -> InteractionTraceIR:
    return InteractionTraceIR.model_validate_json(_TRACE_SNAPSHOT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _cached_live_self_trace() -> InteractionTraceIR:
    return capture_self_hosted_pipeline_trace()


_ALL_STRUCTURAL_VIEW_IDS: tuple[str, ...] = tuple(v.id for v in _cached_diagram_spec().views)


@settings(max_examples=12, deadline=None)
@given(
    sampled_ids=st.lists(
        st.sampled_from(_ALL_STRUCTURAL_VIEW_IDS),
        min_size=1,
        max_size=len(_ALL_STRUCTURAL_VIEW_IDS),
        unique=True,
    )
)
def test_self_hosted_structural_views_pass_all_gates_and_sync_check(
    sampled_ids: list[str],
) -> None:
    """Any sampled/permuted subset of self-hosted structural views passes all 5 Gates & sync."""
    full_spec = _cached_diagram_spec()
    views_by_id = {v.id: v for v in full_spec.views}
    subset_spec = CurationSpecV1(
        schema_version=full_spec.schema_version,
        metadata=dict(full_spec.metadata),
        global_abstractions=full_spec.global_abstractions,
        views=tuple(views_by_id[vid] for vid in sampled_ids),
    )

    result = compile_and_lint_pipeline(_cached_self_ir(), subset_spec)
    assert result.lint_report.passed, f"Self-hosted lint errors: {result.lint_report.diagnostics}"

    for view in subset_spec.views:
        artifact = result.views[view.id]
        committed = (_DIAGRAMS_DIR / artifact.output_filename).read_text(encoding="utf-8")
        assert artifact.diagram_source == committed

    sync_res = sync_markdown_views(result.views, _DOCS_ARCH, check_only=True)
    assert sync_res.is_clean


@settings(max_examples=4, deadline=None)
@given(reverse_events=st.booleans(), reverse_participants=st.booleans())
def test_self_hosted_trace_views_and_snapshot_determinism(
    reverse_events: bool,
    reverse_participants: bool,
) -> None:
    """Committed L3 trace matches live capture and passes all 5 Gates invariant to order."""
    snapshot = _cached_trace_snapshot()
    live = _cached_live_self_trace()
    assert live.model_dump(mode="json") == snapshot.model_dump(mode="json")

    participants = (
        dict(reversed(list(snapshot.participants.items())))
        if reverse_participants
        else dict(snapshot.participants)
    )
    events = (
        dict(reversed(list(snapshot.events.items()))) if reverse_events else dict(snapshot.events)
    )
    permuted = InteractionTraceIR(
        trace_id=snapshot.trace_id,
        participants=participants,
        events=events,
        metadata=dict(snapshot.metadata),
    )

    trace_spec = _cached_trace_spec()
    result = compile_and_lint_pipeline(permuted, trace_spec)
    assert result.lint_report.passed, f"Trace lint errors: {result.lint_report.diagnostics}"
    assert result.lint_report.warning_count == 0

    for view in trace_spec.views:
        artifact = result.views[view.id]
        committed = (_DIAGRAMS_DIR / artifact.output_filename).read_text(encoding="utf-8")
        assert artifact.diagram_source == committed

    sync_res = sync_markdown_views(result.views, _DOCS_ARCH, check_only=True)
    assert sync_res.is_clean


@settings(max_examples=30, deadline=None)
@given(
    node_count=st.integers(min_value=2, max_value=6),
    node_kind=st.sampled_from([NodeKind.SUPERNODE, NodeKind.PACKAGE, NodeKind.MODULE]),
    relation=st.sampled_from(
        [EdgeRelation.COMPOSES, EdgeRelation.IMPLEMENTS, EdgeRelation.INHERITS]
    ),
)
def test_supernode_with_oop_edges_stays_in_flowchart_and_component_mode(
    node_count: int,
    node_kind: NodeKind,
    relation: EdgeRelation,
) -> None:
    """Rolled-up OOP edges on supernodes/packages must stay in flowchart/component mode."""
    nodes = {
        f"ring_{idx}": IRNode(
            symbol_id=f"ring_{idx}",
            label=f"Ring {idx}",
            kind=node_kind,
            provenance=_OBS_PROV,
        )
        for idx in range(node_count)
    }
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=f"ring_{idx}",
            target=f"ring_{idx + 1}",
            relation=relation,
            provenance=_OBS_PROV,
        )
        for idx in range(node_count - 1)
    )
    ir = StructuralGraphIR(graph_id="arch_supernodes", nodes=nodes, edges=edges)

    mmd = MermaidEmitter().emit(ir)
    puml = PlantUmlEmitter().emit(ir)

    assert "flowchart TD" in mmd
    assert "classDiagram" not in mmd
    assert "@startuml" in puml
    assert "\nclass " not in puml


@settings(max_examples=30, deadline=None)
@given(
    prefix=_IDENT,
    member_count=st.integers(min_value=2, max_value=5),
)
def test_gate2_khop_neighborhood_with_view_collapse_preserves_protected_supernode(
    prefix: str,
    member_count: int,
) -> None:
    """Gate 2 survival matches reducer order (k_hop_neighborhood before view.rules)."""
    pkg_id = f"{prefix}.pkg"
    members = tuple(f"{pkg_id}.mod_{i}" for i in range(member_count))
    far_id = f"{prefix}.far_isolated"

    nodes: dict[str, IRNode] = {
        pkg_id: IRNode(symbol_id=pkg_id, label="pkg", kind=NodeKind.PACKAGE, provenance=_OBS_PROV),
        far_id: IRNode(symbol_id=far_id, label="far", kind=NodeKind.MODULE, provenance=_OBS_PROV),
    }
    for mid in members:
        nodes[mid] = IRNode(
            symbol_id=mid,
            label=mid.rsplit(".", 1)[-1],
            kind=NodeKind.MODULE,
            parent_id=pkg_id,
            provenance=_OBS_PROV,
        )
    edges = tuple(
        IREdge(
            edge_id=f"e_{i}",
            source=members[i],
            target=members[i + 1],
            relation=EdgeRelation.IMPORTS,
            provenance=_OBS_PROV,
        )
        for i in range(member_count - 1)
    )
    ir = StructuralGraphIR(graph_id="khop_collapse_ir", nodes=nodes, edges=edges)

    target_super = f"{prefix}_super"
    view = DiagramViewSpec(
        id="khop_view",
        title="K-Hop Collapsed View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        scope=KHopNeighborhoodScope(
            focal_nodes=(members[0],),
            upstream_depth=0,
            downstream_depth=member_count,
        ),
        rules=(
            CollapsePartitionRule(
                id="collapse_members",
                target_node_id=target_super,
                label="Collapsed Members",
                member_globs=(f"{pkg_id}.*",),
            ),
        ),
        invariants=ViewInvariants(protected_nodes=(target_super,)),
    )
    spec = CurationSpecV1(views=(view,))
    curated = StructuralTopologicalReducer().reduce(ir, view)
    report = GroundingAndBoundaryGate().check(ir, spec, curated_views={view.id: curated})
    assert report.passed, f"Unexpected Gate 1/2 diagnostics: {report.diagnostics}"

    bad_view = view.model_copy(
        update={"invariants": ViewInvariants(protected_nodes=(target_super, far_id))}
    )
    bad_spec = CurationSpecV1(views=(bad_view,))
    bad_report = GroundingAndBoundaryGate().check(
        ir, bad_spec, curated_views={bad_view.id: curated}
    )
    g2_codes = [d.code for d in bad_report.by_gate(GateId.GATE_2_BOUNDARY_LEAF)]
    assert "PROTECTED_NODE_DROPPED" in g2_codes


@settings(max_examples=8, deadline=None)
@given(
    leaf=st.sampled_from(["emitter", "gate", "worker", "handler"]),
    suffix=_IDENT,
)
def test_trace_ingestor_disambiguates_colliding_leaf_module_labels(
    leaf: str,
    suffix: str,
) -> None:
    """Two packages sharing the same leaf module name get distinct parent.leaf trace labels."""
    root_pkg = f"zzdogfood_{suffix}"
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        pkg_root = tmp / root_pkg
        (pkg_root / "alpha").mkdir(parents=True)
        (pkg_root / "beta").mkdir(parents=True)
        (pkg_root / "__init__.py").write_text("", encoding="utf-8")
        (pkg_root / "alpha" / "__init__.py").write_text("", encoding="utf-8")
        (pkg_root / "beta" / "__init__.py").write_text("", encoding="utf-8")
        (pkg_root / "beta" / f"{leaf}.py").write_text(
            "def finish(x: int) -> int:\n    return x + 1\n",
            encoding="utf-8",
        )
        (pkg_root / "alpha" / f"{leaf}.py").write_text(
            f"from {root_pkg}.beta.{leaf} import finish\n"
            "def start(x: int) -> int:\n"
            "    return finish(x)\n",
            encoding="utf-8",
        )

        sys.path.insert(0, tmp_str)
        try:
            alpha_mod = importlib.import_module(f"{root_pkg}.alpha.{leaf}")
            trace_ir = capture_python_trace(
                lambda: alpha_mod.start(1),
                module_prefixes=(root_pkg,),
                trace_id="collision_test",
            )
        finally:
            sys.path.remove(tmp_str)
            for mod_name in [
                k for k in sys.modules if k == root_pkg or k.startswith(f"{root_pkg}.")
            ]:
                sys.modules.pop(mod_name, None)

        labels = {p.participant_id: p.label for p in trace_ir.participants.values()}
        alpha_pid = f"{root_pkg}.alpha.{leaf}"
        beta_pid = f"{root_pkg}.beta.{leaf}"
        assert labels[alpha_pid] == f"alpha.{leaf}"
        assert labels[beta_pid] == f"beta.{leaf}"
        assert labels[alpha_pid] != labels[beta_pid]
