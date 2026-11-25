"""Hypothesis property-based fuzz suite for canonical dual-mode diagram styling policy."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

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
    DEFAULT_MERMAID_THEME,
    MERMAID_DEFAULT_CLASSDEF,
    MERMAID_GITEA_MAX_SOURCE_CHARS,
    MERMAID_SEQUENCE_FRONTMATTER,
    MERMAID_STRUCTURAL_FRONTMATTER,
    NEUTRAL_BORDER_HEX,
    PLANTUML_ORTHO_SKINPARAMS,
    PLANTUML_SEQUENCE_SKINPARAMS,
    TRANSPARENT_FILL,
    LinePattern,
    MermaidThemeConfig,
    MermaidWebModePalette,
    UmlArrowheadKind,
    contrast_ratio,
    dump_mermaid_theme_yaml,
    parse_mermaid_theme_yaml,
    relative_luminance,
    render_mermaid_web_theme_js,
    resolve_edge_animated,
    resolve_edge_visual_style,
)
from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    inject_svg_dual_mode_style,
    load_mermaid_theme,
    sync_compiled_artifacts,
)
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_mermaid import MermaidEmitter

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
    assert MERMAID_GITEA_MAX_SOURCE_CHARS == 5000
    assert _HEX_RE.match(NEUTRAL_BORDER_HEX) is not None
    assert "defaultRenderer: elk" in MERMAID_STRUCTURAL_FRONTMATTER
    assert "curve: linear" in MERMAID_STRUCTURAL_FRONTMATTER
    assert "primaryColor: transparent" not in MERMAID_STRUCTURAL_FRONTMATTER
    assert "actorBkg: transparent" not in MERMAID_SEQUENCE_FRONTMATTER
    assert "mirrorActors: false" in MERMAID_SEQUENCE_FRONTMATTER
    assert "fill:transparent" in MERMAID_DEFAULT_CLASSDEF
    assert "color:" not in MERMAID_DEFAULT_CLASSDEF
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
    expected_d2_anim = (
        animate_edges if animate_edges is not None else (relation in _RUNTIME_ANIMATED_RELATIONS)
    )
    expected_mmd_anim = animate_edges is True
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
    assert ("@{ animate: true }" in res.views["mmd_anim"].diagram_source) is expected_mmd_anim
    assert ("style.animated: true" in res.views["d2_anim"].diagram_source) is expected_d2_anim

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


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MID_BORDERS = ("#64748b", "#3b82f6", "#14b8a6", "#8b5cf6", "#10b981", "#f59e0b")


@settings(max_examples=30, deadline=None)
@given(
    layout_engine=st.sampled_from(["elk", "dagre"]),
    curve=st.sampled_from(["linear", "basis", "stepBefore", "stepAfter"]),
    border_hex=st.sampled_from(_MID_BORDERS),
    mirror_actors=st.booleans(),
)
def test_central_mermaid_theme_governance_and_repo_sync(
    layout_engine: str,
    curve: str,
    border_hex: str,
    mirror_actors: bool,
) -> None:
    """Central mermaid-theme.yaml stays synced with mermaid-theme.js and forbids AI overrides."""
    repo_yaml = (_REPO_ROOT / "docs" / "architecture" / "mermaid-theme.yaml").read_text(
        encoding="utf-8"
    )
    repo_js = (_REPO_ROOT / "docs" / "mermaid-theme.js").read_text(encoding="utf-8")
    assert parse_mermaid_theme_yaml(repo_yaml) == DEFAULT_MERMAID_THEME
    assert repo_js == render_mermaid_web_theme_js(DEFAULT_MERMAID_THEME)

    custom = MermaidThemeConfig.model_validate(
        {
            "layout_engine": layout_engine,
            "flowchart_curve": curve,
            "node_border_hex": border_hex,
            "subgraph_border_hex": border_hex,
            "sequence_mirror_actors": mirror_actors,
        }
    )
    raw_yaml = dump_mermaid_theme_yaml(custom)
    assert parse_mermaid_theme_yaml(raw_yaml) == custom
    for mode in ("light", "dark"):
        variables = custom.web_theme_variables(mode)
        assert variables["primaryColor"] != "transparent"
        assert variables["actorBkg"] != "transparent"
        assert contrast_ratio(str(variables["textColor"]), str(variables["background"])) >= 7.0

    with pytest.raises(ValidationError):
        MermaidWebModePalette(
            background_hex="#0b0f19",
            surface_hex="#111827",
            text_hex="#334155",
            muted_text_hex="#94a3b8",
        )
    with pytest.raises(ValidationError):
        DiagramViewSpec.model_validate(
            {
                "id": "ai_override",
                "title": "Forbidden AI Theme",
                "ir_mode": "structural_graph",
                "target_renderer": "mermaid",
                "theme_variables": {"primaryColor": "#ff00ff"},
            }
        )

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        spec_file = tmp_dir / "curation.yaml"
        spec_file.write_text("schema_version: '1.0'\nviews: []\n", encoding="utf-8")
        (tmp_dir / "mermaid-theme.yaml").write_text(raw_yaml, encoding="utf-8")
        assert load_mermaid_theme(spec_file) == custom

    ir = StructuralGraphIR(
        graph_id="theme_ir",
        nodes={
            "n1": IRNode(
                symbol_id="n1",
                label="Node 1",
                kind=NodeKind.MODULE,
                provenance=_OBS_PROV,
            )
        },
    )
    emitted = MermaidEmitter(theme=custom).emit(ir)
    assert f"layout: {layout_engine}" in emitted
    assert f"curve: {curve}" in emitted
    assert f"stroke:{border_hex}" in emitted


@settings(max_examples=30, deadline=None)
@given(
    variant=st.sampled_from([DiagramVariant.CLASS, DiagramVariant.DEPLOYMENT]),
    css_token=st.sampled_from(["text{fill:#e2e8f0;}", ".appendix-icon{opacity:0.9;}"]),
)
def test_d2_dark_mode_class_deployment_and_svg_style_injection(
    variant: DiagramVariant,
    css_token: str,
) -> None:
    """D2 class shapes avoid fill/stroke inversion, deployment avoids 3d, and SVG CSS injects."""
    ir = StructuralGraphIR(
        graph_id="d2_dual_ir",
        nodes={
            "archigram.ports.ValidatorPort": IRNode(
                symbol_id="archigram.ports.ValidatorPort",
                label="ValidatorPort",
                kind=NodeKind.INTERFACE,
                members=(ClassMember(name="validate", is_method=True, signature="(str) -> bool"),),
                provenance=_OBS_PROV,
            ),
            "archigram.features.MermaidValidator": IRNode(
                symbol_id="archigram.features.MermaidValidator",
                label="MermaidValidator",
                kind=NodeKind.CLASS,
                members=(ClassMember(name="strict", signature="bool"),),
                provenance=_OBS_PROV,
            ),
        },
        edges=(
            IREdge(
                edge_id="e_impl",
                source="archigram.features.MermaidValidator",
                target="archigram.ports.ValidatorPort",
                relation=EdgeRelation.IMPLEMENTS,
                provenance=_OBS_PROV,
            ),
        ),
        metadata={"diagram_variant": variant.value},
    )
    d2_out = D2Emitter().emit(ir)
    assert "dark-theme-id: 200" in d2_out
    assert "style.3d: true" not in d2_out
    if variant is DiagramVariant.CLASS:
        assert "shape: class" in d2_out
        assert d2_out.count("style.fill: transparent") == 1
    else:
        assert "style.double-border: true" in d2_out

    raw_svg = '<svg xmlns="http://www.w3.org/2000/svg"><g><text>Label</text></g></svg>'
    styled_svg = inject_svg_dual_mode_style(raw_svg, css_token)
    assert styled_svg.endswith("</svg>")
    assert css_token in styled_svg
    assert inject_svg_dual_mode_style("<not-svg/>", css_token) == "<not-svg/>"
