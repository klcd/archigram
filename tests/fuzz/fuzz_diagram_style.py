"""Hypothesis property-based fuzz suite for canonical dual-mode diagram styling policy."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

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
)
from archigram.domain.spec import (
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    dump_curation_spec_yaml,
    parse_curation_spec_yaml,
)
from archigram.domain.style import (
    CANONICAL_EDGE_STYLES,
    D2_CANONICAL_HEADER,
    MERMAID_DEFAULT_CLASSDEF,
    MERMAID_SEQUENCE_FRONTMATTER,
    MERMAID_STRUCTURAL_FRONTMATTER,
    NEUTRAL_BORDER_HEX,
    PLANTUML_ORTHO_SKINPARAMS,
    PLANTUML_SEQUENCE_SKINPARAMS,
    TRANSPARENT_FILL,
    LinePattern,
    UmlArrowheadKind,
    relative_luminance,
    resolve_edge_animated,
    resolve_edge_visual_style,
)
from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    sync_compiled_artifacts,
)

_HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
_LIGHT_BG_LUM = relative_luminance("#ffffff")
_DARK_BG_LUM = relative_luminance("#0f172a")
_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_diagram_style",
    input_sha256="c" * 64,
)
_RUNTIME_ANIMATED_RELATIONS: frozenset[EdgeRelation] = frozenset(
    {
        EdgeRelation.CALLS,
        EdgeRelation.READS_DATA,
        EdgeRelation.WRITES_DATA,
        EdgeRelation.ASYNC_SPAWN,
        EdgeRelation.FORK_JOIN,
    }
)


def _contrast_ratio(lum_a: float, lum_b: float) -> float:
    lighter = max(lum_a, lum_b)
    darker = min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


@settings(max_examples=60)
@given(
    relation=st.sampled_from(list(EdgeRelation)),
    sync_kind=st.sampled_from(list(SyncKind)),
)
def test_edge_style_completeness_and_dual_mode_contrast_invariants(
    relation: EdgeRelation,
    sync_kind: SyncKind,
) -> None:
    """Every (EdgeRelation, SyncKind) resolves to a mid-luminance hex visible on light & dark."""
    assert set(CANONICAL_EDGE_STYLES) == set(EdgeRelation)
    base = CANONICAL_EDGE_STYLES[relation]
    assert base.animated is (relation in _RUNTIME_ANIMATED_RELATIONS)

    resolved = resolve_edge_visual_style(relation, sync_kind)
    assert resolved.relation is relation
    assert _HEX_RE.match(resolved.stroke_hex) is not None
    assert resolved.usage_note.strip() != ""
    assert resolve_edge_animated(resolved, "true") is True
    assert resolve_edge_animated(resolved, "false") is False
    assert resolve_edge_animated(resolved, None) is resolved.animated

    lum = relative_luminance(resolved.stroke_hex)
    assert 0.08 <= lum <= 0.65
    assert _contrast_ratio(lum, _LIGHT_BG_LUM) >= 1.5
    assert _contrast_ratio(lum, _DARK_BG_LUM) >= 2.0

    if sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        assert resolved.line_pattern is LinePattern.DASHED
        assert resolved.animated is True
    if sync_kind is SyncKind.FORK_JOIN_BRANCH and relation is EdgeRelation.CALLS:
        assert resolved.line_pattern is LinePattern.BOLD
        assert resolved.animated is True


@settings(max_examples=40)
@given(
    r=st.integers(min_value=0, max_value=255),
    g=st.integers(min_value=0, max_value=255),
    b=st.integers(min_value=0, max_value=255),
)
def test_relative_luminance_monotonicity_and_bounds(r: int, g: int, b: int) -> None:
    """WCAG relative luminance stays in [0, 1] and increases monotonically with channel values."""
    hex_low = f"#{r:02x}{g:02x}{b:02x}"
    lum_low = relative_luminance(hex_low)
    assert 0.0 <= lum_low <= 1.0

    r_hi = min(255, r + 1)
    g_hi = min(255, g + 1)
    b_hi = min(255, b + 1)
    lum_hi = relative_luminance(f"#{r_hi:02x}{g_hi:02x}{b_hi:02x}")
    assert lum_hi >= lum_low


@settings(max_examples=40)
@given(
    renderer=st.sampled_from(list(TargetRenderer)),
    variant=st.sampled_from(list(DiagramVariant)),
    render_svg=st.booleans(),
    animate_edges=st.sampled_from([None, True, False]),
)
def test_transparent_preambles_and_view_spec_variant_roundtrip(
    renderer: TargetRenderer,
    variant: DiagramVariant,
    render_svg: bool,
    animate_edges: bool | None,
) -> None:
    """Canonical target preambles enforce transparent fills and DiagramViewSpec round-trips."""
    assert TRANSPARENT_FILL == "transparent"
    assert _HEX_RE.match(NEUTRAL_BORDER_HEX) is not None
    assert "curve: linear" in MERMAID_STRUCTURAL_FRONTMATTER
    assert "primaryColor: transparent" in MERMAID_STRUCTURAL_FRONTMATTER
    assert "actorBkg: transparent" in MERMAID_SEQUENCE_FRONTMATTER
    assert "fill:transparent" in MERMAID_DEFAULT_CLASSDEF
    assert "dark-theme-id: 200" in D2_CANONICAL_HEADER
    assert "style.fill: transparent" in D2_CANONICAL_HEADER
    assert "skinparam linetype ortho" in PLANTUML_ORTHO_SKINPARAMS
    assert "skinparam backgroundColor transparent" in PLANTUML_ORTHO_SKINPARAMS
    assert "skinparam backgroundColor transparent" in PLANTUML_SEQUENCE_SKINPARAMS
    assert CANONICAL_EDGE_STYLES[EdgeRelation.INHERITS].uml_arrowhead is (
        UmlArrowheadKind.HOLLOW_TRIANGLE
    )
    assert CANONICAL_EDGE_STYLES[EdgeRelation.COMPOSES].uml_arrowhead is (
        UmlArrowheadKind.FILLED_DIAMOND
    )

    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="styled_view",
                title="Styled View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderer,
                diagram_variant=variant,
                render_svg=render_svg,
                animate_edges=animate_edges,
            ),
        )
    )
    reloaded = parse_curation_spec_yaml(dump_curation_spec_yaml(spec))
    assert reloaded == spec


@settings(max_examples=30, deadline=None)
@given(
    renderer=st.sampled_from(list(TargetRenderer)),
    variant=st.sampled_from([DiagramVariant.ARCHITECTURE, DiagramVariant.CLASS]),
    relation=st.sampled_from(list(EdgeRelation)),
)
def test_diagram_variant_emission_and_companion_svg_drift_detection(
    renderer: TargetRenderer,
    variant: DiagramVariant,
    relation: EdgeRelation,
) -> None:
    """Explicit ARCHITECTURE vs CLASS variants pass Gate 5 and detect companion SVG drift."""
    ir = StructuralGraphIR(
        graph_id="variant_ir",
        nodes={
            "PortProtocol": IRNode(
                symbol_id="PortProtocol",
                label="PortProtocol",
                kind=NodeKind.INTERFACE,
                members=(ClassMember(name="run", is_method=True, signature="() -> None"),),
                provenance=_OBS_PROV,
            ),
            "ConcreteImpl": IRNode(
                symbol_id="ConcreteImpl",
                label="ConcreteImpl",
                kind=NodeKind.CLASS,
                members=(ClassMember(name="state", signature="str"),),
                provenance=_OBS_PROV,
            ),
        },
        edges=(
            IREdge(
                edge_id="e1",
                source="ConcreteImpl",
                target="PortProtocol",
                relation=relation,
                provenance=_OBS_PROV,
            ),
        ),
    )
    view = DiagramViewSpec(
        id="v_styled",
        title="Styled Variant",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=renderer,
        diagram_variant=variant,
        render_svg=True,
    )
    res = compile_and_lint_pipeline(ir, CurationSpecV1(views=(view,)))
    assert res.lint_report.passed
    src = res.views["v_styled"].diagram_source
    if renderer is TargetRenderer.MERMAID:
        expected_kw = "classDiagram" if variant is DiagramVariant.CLASS else "flowchart TD"
        assert expected_kw in src
    elif renderer is TargetRenderer.D2 and variant is DiagramVariant.CLASS:
        assert "shape: class" in src
    elif renderer is TargetRenderer.PLANTUML:
        if variant is DiagramVariant.CLASS:
            assert "interface " in src
        else:
            assert "interface " not in src

    with tempfile.TemporaryDirectory() as tmp_str:
        out_dir = Path(tmp_str)
        art = res.views["v_styled"]
        (out_dir / art.output_filename).write_text(src, encoding="utf-8")
        missing_check = sync_compiled_artifacts(res, out_dir, check_only=True)
        assert (out_dir / f"{art.output_filename}.svg") in missing_check.missing_files

        sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
        svg_path = out_dir / f"{art.output_filename}.svg"
        svg_path.write_text(
            f"<!-- archigram-source-sha256: {sha} -->\n<svg></svg>\n",
            encoding="utf-8",
        )
        clean_check = sync_compiled_artifacts(res, out_dir, check_only=True)
        assert clean_check.is_clean

        svg_path.write_text(
            "<!-- archigram-source-sha256: 0000 -->\n<svg></svg>\n",
            encoding="utf-8",
        )
        drift_check = sync_compiled_artifacts(res, out_dir, check_only=True)
        assert svg_path in drift_check.drifted_files


@settings(max_examples=30, deadline=None)
@given(
    relation=st.sampled_from(list(EdgeRelation)),
    animate_edges=st.sampled_from([None, True, False]),
)
def test_animated_edges_and_d2_steps_trace_compilation(
    relation: EdgeRelation,
    animate_edges: bool | None,
) -> None:
    """Flowing edge animation overrides and D2 steps trace compilation pass all 5 Gates."""
    ir = StructuralGraphIR(
        graph_id="anim_ir",
        nodes={
            "svc_a": IRNode(
                symbol_id="svc_a",
                label="Service A",
                kind=NodeKind.SERVICE,
                provenance=_OBS_PROV,
            ),
            "svc_b": IRNode(
                symbol_id="svc_b",
                label="Service B",
                kind=NodeKind.SERVICE,
                provenance=_OBS_PROV,
            ),
        },
        edges=(
            IREdge(
                edge_id="e_flow",
                source="svc_a",
                target="svc_b",
                relation=relation,
                provenance=_OBS_PROV,
            ),
        ),
    )
    expected_anim = (
        animate_edges if animate_edges is not None else (relation in _RUNTIME_ANIMATED_RELATIONS)
    )
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="mmd_anim",
                title="Mermaid Flow",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                diagram_variant=DiagramVariant.ARCHITECTURE,
                animate_edges=animate_edges,
            ),
            DiagramViewSpec(
                id="d2_anim",
                title="D2 Flow",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                diagram_variant=DiagramVariant.ARCHITECTURE,
                animate_edges=animate_edges,
            ),
        )
    )
    res = compile_and_lint_pipeline(ir, spec)
    assert res.lint_report.passed
    assert ("@{ animate: true }" in res.views["mmd_anim"].diagram_source) is expected_anim
    assert ("style.animated: true" in res.views["d2_anim"].diagram_source) is expected_anim

    trace = InteractionTraceIR(
        trace_id="steps_trace",
        participants={
            "cli": TraceParticipant(
                participant_id="cli",
                symbol_id="cli",
                label="CLI",
                kind=NodeKind.SERVICE,
                provenance=_OBS_PROV,
            ),
            "engine": TraceParticipant(
                participant_id="engine",
                symbol_id="engine",
                label="Engine",
                kind=NodeKind.SERVICE,
                provenance=_OBS_PROV,
            ),
        },
        events={
            "ev_1": TraceEvent(
                event_id="ev_1",
                caller_id="cli",
                callee_id="engine",
                signature="compile()",
                clock_domain_id="h0",
                start_ns=10,
                end_ns=50,
                status=EventStatus.OK,
                provenance=_OBS_PROV,
            ),
        },
    )
    steps_spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="d2_steps",
                title="D2 Steps",
                ir_mode=IRMode.INTERACTION_TRACE,
                target_renderer=TargetRenderer.D2,
                diagram_variant=DiagramVariant.STEPS,
                animate_edges=animate_edges,
            ),
        )
    )
    steps_res = compile_and_lint_pipeline(trace, steps_spec)
    assert steps_res.lint_report.passed
    steps_src = steps_res.views["d2_steps"].diagram_source
    assert "\nsteps: {\n" in steps_src
    assert "  step_1: {" in steps_src
    assert ("style.animated: true" in steps_src) is (animate_edges is not False)
