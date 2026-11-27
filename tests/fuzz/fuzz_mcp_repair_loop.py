"""Hypothesis property-based fuzz suite for MCP Server tools and Bounded 3-Turn Lint-Repair Loop."""

from __future__ import annotations

import asyncio
import json
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.types import CallToolResult, TextContent

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import GateId, LintDiagnostic, LintReport
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
    BudgetRange,
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    emit_view_source,
    load_canonical_ir,
)
from archigram.entrypoints.mcp import (
    CuratedViewsPreviewReport,
    DeterministicHeuristicProposer,
    RuleProposerProtocol,
    create_mcp_server,
    evaluate_curation_spec,
    get_ir_digest,
    preview_curated_views,
    repair_curation_spec_from_diagnostics,
    run_propose_cli,
    run_propose_loop,
    validate_curation_spec,
)

if TYPE_CHECKING:
    from archigram.domain.ir import CanonicalIR

_OBS_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_mcp_repair_loop",
    input_sha256="c" * 64,
)
_TOKEN_STRAT = st.text(
    alphabet=string.ascii_lowercase + string.digits,
    min_size=3,
    max_size=10,
).filter(lambda s: s[0].isalpha())


@dataclass(frozen=True, slots=True)
class LayeredStructuralFixture:
    """Randomized 3-tier StructuralGraphIR with guaranteed roots, bridges, and leaf sinks."""

    ir: StructuralGraphIR
    roots: tuple[str, ...]
    bridges: tuple[str, ...]
    sinks: tuple[str, ...]


@st.composite
def layered_structural_strategy(draw: st.DrawFn) -> LayeredStructuralFixture:
    """Generate arbitrary 3-layer StructuralGraphIR topologies."""
    root_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=4, unique=True))
    bridge_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=4, unique=True))
    sink_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=4, unique=True))

    roots = tuple(f"pkg.root.{tok}" for tok in root_tokens)
    bridges = tuple(f"pkg.bridge.{tok}" for tok in bridge_tokens)
    sinks = tuple(f"pkg.sink.{tok}" for tok in sink_tokens)

    nodes: dict[str, IRNode] = {}
    for sym_id in (*roots, *bridges, *sinks):
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Node {sym_id}",
            kind=draw(st.sampled_from([NodeKind.SERVICE, NodeKind.MODULE, NodeKind.COMPONENT])),
            provenance=_OBS_STAMP,
        )

    edges: list[IREdge] = []
    edge_idx = 0
    for idx, b_id in enumerate(bridges):
        r_id = roots[idx % len(roots)]
        s_id = sinks[idx % len(sinks)]
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=r_id,
                target=b_id,
                relation=EdgeRelation.CALLS,
                weight=draw(st.floats(min_value=1.0, max_value=10.0, allow_nan=False)),
                provenance=_OBS_STAMP,
            )
        )
        edge_idx += 1
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=b_id,
                target=s_id,
                relation=EdgeRelation.CALLS,
                weight=draw(st.floats(min_value=1.0, max_value=10.0, allow_nan=False)),
                provenance=_OBS_STAMP,
            )
        )
        edge_idx += 1

    for idx, r_id in enumerate(roots):
        if idx >= len(bridges):
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=r_id,
                    target=bridges[idx % len(bridges)],
                    relation=EdgeRelation.IMPORTS,
                    provenance=_OBS_STAMP,
                )
            )
            edge_idx += 1

    for idx, s_id in enumerate(sinks):
        if idx >= len(bridges):
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=bridges[idx % len(bridges)],
                    target=s_id,
                    relation=EdgeRelation.WRITES_DATA,
                    provenance=_OBS_STAMP,
                )
            )
            edge_idx += 1

    graph_id = draw(_TOKEN_STRAT)
    return LayeredStructuralFixture(
        ir=StructuralGraphIR(graph_id=graph_id, nodes=nodes, edges=tuple(edges)),
        roots=roots,
        bridges=bridges,
        sinks=sinks,
    )


@st.composite
def interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate arbitrary concurrent InteractionTraceIR payloads."""
    p_count = draw(st.integers(min_value=1, max_value=6))
    p_ids = [f"svc_{idx}" for idx in range(p_count)]
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"pkg.{pid}",
            label=f"Service {pid}",
            kind=NodeKind.SERVICE,
            provenance=_OBS_STAMP,
        )
        for pid in p_ids
    }
    ev_count = draw(st.integers(min_value=1, max_value=16))
    e_ids = [f"ev_{idx:02d}" for idx in range(ev_count)]
    events: dict[str, TraceEvent] = {}
    for idx, eid in enumerate(e_ids):
        parent_id = (
            e_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
            if idx > 0 and draw(st.booleans())
            else None
        )
        start_ns = draw(st.integers(min_value=0, max_value=400))
        dur_ns = draw(st.integers(min_value=10, max_value=200))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=draw(st.sampled_from(p_ids)),
            callee_id=draw(st.sampled_from(p_ids)),
            signature=draw(st.sampled_from(["rpc_call()", "fetch_state()", "emit_event()"])),
            parent_event_id=parent_id,
            clock_domain_id=draw(st.sampled_from(["clk_a", "clk_b"])),
            start_ns=start_ns,
            end_ns=start_ns + dur_ns,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=draw(st.sampled_from(list(EventStatus))),
            provenance=_OBS_STAMP,
        )
    return InteractionTraceIR(
        trace_id=draw(_TOKEN_STRAT),
        participants=participants,
        events=events,
    )


class FaultInjectedMultiTurnProposer:
    """Proposer injecting Turn 1 hallucinated symbols and Turn 2 Gate 2/4 errors before repair."""

    def __init__(
        self,
        fixture: LayeredStructuralFixture,
        turn2_fault: str,
        renderer: TargetRenderer,
    ) -> None:
        self._fixture = fixture
        self._turn2_fault = turn2_fault
        self._renderer = renderer

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        assert len(digest_markdown) > 0
        root0 = self._fixture.roots[0]
        bridge0 = self._fixture.bridges[0]
        sink0 = self._fixture.sinks[0]

        if turn == 1:
            # Turn 1: emit hallucinated typo symbol IDs and zero-match globs
            spec_t1 = CurationSpecV1(
                global_abstractions=(
                    PruneLeavesRule(
                        id="prune_ghost_glob",
                        node_id_globs=("hallucinated.ghost.**",),
                        direction="sink",
                    ),
                ),
                views=(
                    DiagramViewSpec(
                        id="arch_view",
                        title="Architectural View",
                        ir_mode=IRMode.STRUCTURAL_GRAPH,
                        target_renderer=self._renderer,
                        scope=KHopNeighborhoodScope(
                            focal_nodes=(f"{root0}_typoz",),
                            upstream_depth=2,
                            downstream_depth=2,
                        ),
                        invariants=ViewInvariants(
                            protected_nodes=(root0.upper(),),
                        ),
                    ),
                ),
            )
            return dump_curation_spec_yaml(spec_t1)

        if turn == 2:
            # Verify Turn 1 produced Gate 1 grounding diagnostics with offending_symbols
            assert len(diagnostics) >= 1
            assert any(d.gate is GateId.GATE_1_GROUNDING for d in diagnostics)
            assert any(bool(d.offending_symbols) and bool(d.repair_hint) for d in diagnostics)

            # Repair Turn 1 grounding issues, but inject Turn 2 Gate 2 or Gate 4 fault
            if self._turn2_fault == "prune_bridge":
                rules = (
                    PruneLeavesRule(
                        id="bad_prune_bridge",
                        node_id_globs=(bridge0,),
                        direction="sink",
                    ),
                )
                invariants = ViewInvariants(
                    protected_nodes=(root0,),
                    node_budget=BudgetRange(min_count=1, max_count=50),
                    edge_budget_max=100,
                )
            elif self._turn2_fault == "budget_overflow":
                rules = ()
                invariants = ViewInvariants(
                    protected_nodes=(root0,),
                    node_budget=BudgetRange(min_count=1, max_count=1),
                    edge_budget_max=0,
                    overflow_strategy="fail",
                )
            else:
                rules = (
                    PruneLeavesRule(
                        id="drop_protected_sink",
                        node_id_globs=(sink0,),
                        direction="sink",
                    ),
                )
                invariants = ViewInvariants(
                    protected_nodes=(sink0,),
                    node_budget=BudgetRange(min_count=1, max_count=50),
                )

            spec_t2 = CurationSpecV1(
                views=(
                    DiagramViewSpec(
                        id="arch_view",
                        title="Architectural View",
                        ir_mode=IRMode.STRUCTURAL_GRAPH,
                        target_renderer=self._renderer,
                        rules=rules,
                        scope=FullScope(),
                        invariants=invariants,
                    ),
                ),
            )
            return dump_curation_spec_yaml(spec_t2)

        # Turn 3: repair using the exact LintDiagnostic offending_symbols and repair_hints
        assert prior_spec_yaml is not None
        assert len(diagnostics) >= 1
        repaired_spec = repair_curation_spec_from_diagnostics(
            prior_spec_yaml,
            diagnostics,
            self._fixture.ir,
        )
        return dump_curation_spec_yaml(repaired_spec)


class PermanentlyBrokenProposer:
    """Proposer that stubbornly emits ungrounded symbols or broken YAML on every turn."""

    def __init__(self, broken_mode: str) -> None:
        self._broken_mode = broken_mode

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        _ = (digest_markdown, prior_spec_yaml, diagnostics, turn)
        if self._broken_mode == "invalid_yaml_schema":
            return "schema_version: '99.0'\nunknown_field: [1, 2, 3]\n"
        spec = CurationSpecV1(
            views=(
                DiagramViewSpec(
                    id="broken_view",
                    title="Broken View",
                    ir_mode=IRMode.STRUCTURAL_GRAPH,
                    target_renderer=TargetRenderer.MERMAID,
                    invariants=ViewInvariants(
                        protected_nodes=("permanently.nonexistent.symbol_xyz",),
                    ),
                ),
            ),
        )
        return dump_curation_spec_yaml(spec)


@settings(max_examples=40, deadline=None)
@given(
    ir=st.one_of(
        layered_structural_strategy().map(lambda f: f.ir),
        interaction_trace_strategy(),
    ),
    renderer=st.sampled_from(list(TargetRenderer)),
    max_tokens=st.integers(min_value=120, max_value=3800),
)
def test_mcp_tools_deterministic_across_structural_and_trace_ir(
    ir: CanonicalIR,
    renderer: TargetRenderer,
    max_tokens: int,
) -> None:
    """All 3 MCP tools execute deterministically over randomized CanonicalIR payloads."""
    ir_json = ir.model_dump_json()

    digest_a = get_ir_digest(ir_json, max_tokens=max_tokens)
    digest_b = get_ir_digest(ir_json, max_tokens=max_tokens)
    assert digest_a == digest_b
    assert 0 < len(digest_a) <= min(12_000, max_tokens * 3)

    proposer = DeterministicHeuristicProposer(ir)
    loop_res = run_propose_loop(ir, proposer, max_turns=3)
    assert loop_res.succeeded
    assert loop_res.spec is not None

    view_with_renderer = loop_res.spec.views[0].model_copy(update={"target_renderer": renderer})
    spec = loop_res.spec.model_copy(update={"views": (view_with_renderer,)})
    spec_yaml = dump_curation_spec_yaml(spec)

    lint_json_a = validate_curation_spec(ir_json, spec_yaml)
    lint_json_b = validate_curation_spec(ir_json, spec_yaml)
    assert lint_json_a == lint_json_b
    lint_report = LintReport.model_validate_json(lint_json_a)
    assert lint_report.passed

    preview_json_a = preview_curated_views(ir_json, spec_yaml)
    preview_json_b = preview_curated_views(ir_json, spec_yaml)
    assert preview_json_a == preview_json_b
    preview_report = CuratedViewsPreviewReport.model_validate_json(preview_json_a)
    assert preview_report.passed
    assert len(preview_report.views) == 1
    compiled = preview_report.views[0]
    assert compiled.target_renderer == renderer.value
    assert compiled.syntax_valid
    assert len(compiled.diagram_text.strip()) > 0


@settings(max_examples=20, deadline=None)
@given(
    fixture=layered_structural_strategy(),
    bad_spec_yaml=st.sampled_from(
        [
            "not_valid_yaml: [unclosed",
            "schema_version: '2.0'\nviews: []\n",
            "schema_version: '1.0'\nviews: []\n",
        ]
    ),
)
def test_mcp_server_dispatch_and_graceful_error_handling(
    fixture: LayeredStructuralFixture,
    bad_spec_yaml: str,
) -> None:
    """FastMCP server registers all 3 tools and returns structured diagnostics on bad inputs."""
    server = create_mcp_server()
    tools = asyncio.run(server.list_tools())
    tool_names = {t.name for t in tools}
    assert tool_names == {"get_ir_digest", "validate_curation_spec", "preview_curated_views"}

    ir_json = fixture.ir.model_dump_json()
    call_res = asyncio.run(
        server.call_tool("get_ir_digest", {"ir_json": ir_json, "max_tokens": 1500})
    )
    assert isinstance(call_res, CallToolResult)
    assert not call_res.is_error
    first_block = call_res.content[0]
    assert isinstance(first_block, TextContent)
    assert fixture.ir.graph_id in first_block.text

    invalid_lint_json = validate_curation_spec(ir_json, bad_spec_yaml)
    invalid_lint = LintReport.model_validate_json(invalid_lint_json)
    assert not invalid_lint.passed
    assert invalid_lint.error_count >= 1
    assert invalid_lint.diagnostics[0].gate is GateId.GATE_1_GROUNDING

    bad_ir_preview_json = preview_curated_views("{malformed_json", bad_spec_yaml)
    bad_ir_preview = CuratedViewsPreviewReport.model_validate_json(bad_ir_preview_json)
    assert not bad_ir_preview.passed
    assert bad_ir_preview.lint_report.diagnostics[0].code == "INVALID_CANONICAL_IR_JSON"


@settings(max_examples=45, deadline=None)
@given(
    fixture=layered_structural_strategy(),
    turn2_fault=st.sampled_from(["prune_bridge", "budget_overflow", "drop_protected"]),
    renderer=st.sampled_from(
        [
            TargetRenderer.MERMAID,
            TargetRenderer.D2,
            TargetRenderer.PLANTUML,
            TargetRenderer.LIKEC4,
        ]
    ),
)
def test_fault_injected_proposer_converges_within_3_turns(
    fixture: LayeredStructuralFixture,
    turn2_fault: str,
    renderer: TargetRenderer,
) -> None:
    """Fault-injected proposers (Turn 1 Gate 1, Turn 2 Gate 2/4) self-repair within <= 3 turns."""
    proposer = FaultInjectedMultiTurnProposer(fixture, turn2_fault, renderer)
    assert isinstance(proposer, RuleProposerProtocol)

    result = run_propose_loop(fixture.ir, proposer, max_turns=3)
    assert result.succeeded, f"Failed to converge: {result.final_report.diagnostics}"
    assert result.turns_used == 3
    assert len(result.history) == 3
    assert not result.history[0].passed
    assert not result.history[1].passed
    assert result.history[2].passed
    assert result.final_report.passed
    assert result.spec is not None


@settings(max_examples=30, deadline=None)
@given(
    fixture=layered_structural_strategy(),
    broken_mode=st.sampled_from(["invalid_yaml_schema", "hallucinated_symbol"]),
    max_turns=st.integers(min_value=1, max_value=3),
)
def test_permanently_broken_proposer_terminates_cleanly_in_max_turns(
    fixture: LayeredStructuralFixture,
    broken_mode: str,
    max_turns: int,
) -> None:
    """Permanently broken proposers terminate in exactly max_turns with succeeded=False."""
    proposer = PermanentlyBrokenProposer(broken_mode)
    result = run_propose_loop(fixture.ir, proposer, max_turns=max_turns)
    assert not result.succeeded
    assert result.turns_used == max_turns
    assert len(result.history) == max_turns
    assert not result.final_report.passed
    assert result.final_report.error_count >= 1


@settings(max_examples=20, deadline=None)
@given(fixture=layered_structural_strategy())
def test_run_propose_cli_round_trip(fixture: LayeredStructuralFixture) -> None:
    """run_propose_cli reads CanonicalIR JSON, runs the repair loop, and writes valid YAML."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        in_file = tmp_path / "ir.json"
        out_file = tmp_path / "diagram-views.yaml"
        in_file.write_text(fixture.ir.model_dump_json(), encoding="utf-8")

        exit_code = run_propose_cli(
            str(in_file), str(out_file), max_turns=3, ir_loader=load_canonical_ir
        )
        assert exit_code == 0
        assert out_file.is_file()

        spec_yaml = out_file.read_text(encoding="utf-8")
        lint_data = json.loads(validate_curation_spec(fixture.ir.model_dump_json(), spec_yaml))
        assert lint_data["passed"] is True


_VIEW_CONFIG_STRAT = st.tuples(
    st.sampled_from(list(TargetRenderer)),
    st.sampled_from(list(DiagramVariant)),
    st.one_of(st.none(), st.booleans()),
    st.booleans(),
)


@settings(max_examples=40, deadline=None)
@given(
    ir=st.one_of(
        layered_structural_strategy().map(lambda f: f.ir),
        interaction_trace_strategy(),
    ),
    renderer=st.sampled_from(list(TargetRenderer)),
    diagram_variant=st.sampled_from(list(DiagramVariant)),
    animate_edges=st.one_of(st.none(), st.booleans()),
)
def test_mcp_preview_matches_cli_emit_view_source(
    ir: CanonicalIR,
    renderer: TargetRenderer,
    diagram_variant: DiagramVariant,
    animate_edges: bool | None,
) -> None:
    """MCP preview_curated_views emits text identical to CLI emit_view_source across options."""
    loop_res = run_propose_loop(ir, DeterministicHeuristicProposer(ir), max_turns=3)
    assert loop_res.succeeded
    assert loop_res.spec is not None

    view = loop_res.spec.views[0].model_copy(
        update={
            "target_renderer": renderer,
            "diagram_variant": diagram_variant,
            "animate_edges": animate_edges,
        }
    )
    spec = loop_res.spec.model_copy(update={"views": (view,)})
    spec_yaml = dump_curation_spec_yaml(spec)

    preview_report = CuratedViewsPreviewReport.model_validate_json(
        preview_curated_views(ir.model_dump_json(), spec_yaml)
    )
    cli_result = compile_and_lint_pipeline(ir, spec)
    compiled = preview_report.views[0]
    cli_artifact = cli_result.views[view.id]

    assert compiled.diagram_text == cli_artifact.diagram_source
    assert compiled.diagram_text == emit_view_source(
        cli_artifact.emitted_ir, view, overflow_markdown=""
    )
    if renderer is TargetRenderer.DOT:
        assert 'rankdir="LR"' in compiled.diagram_text
        assert 'bgcolor="transparent"' in compiled.diagram_text


@settings(max_examples=30, deadline=None)
@given(
    fixture=layered_structural_strategy(),
    view_configs=st.lists(_VIEW_CONFIG_STRAT, min_size=2, max_size=4),
    meta_val=_TOKEN_STRAT,
)
def test_multiview_fallback_repair_preserves_all_views_and_metadata(
    fixture: LayeredStructuralFixture,
    view_configs: list[tuple[TargetRenderer, DiagramVariant, bool | None, bool]],
    meta_val: str,
) -> None:
    """Fallback repair preserves all 2..N views' fields and spec metadata."""
    unmet_reach = (ReachabilityAssertion(from_node=fixture.sinks[0], to_node=fixture.roots[0]),)
    views = tuple(
        DiagramViewSpec(
            id=f"view_{idx}",
            title=f"Curated View {idx}",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=renderer,
            diagram_variant=variant,
            animate_edges=animate,
            render_svg=render_svg,
            invariants=ViewInvariants(require_reachability=unmet_reach),
        )
        for idx, (renderer, variant, animate, render_svg) in enumerate(view_configs)
    )
    prior_spec = CurationSpecV1(views=views, metadata={"custom_key": meta_val})
    diagnostics = evaluate_curation_spec(fixture.ir, prior_spec).lint_report.diagnostics

    repaired = repair_curation_spec_from_diagnostics(
        dump_curation_spec_yaml(prior_spec),
        diagnostics,
        fixture.ir,
    )
    assert len(repaired.views) == len(prior_spec.views)
    assert repaired.metadata == {"custom_key": meta_val}
    for pv, rv in zip(prior_spec.views, repaired.views, strict=True):
        assert rv.id == pv.id
        assert rv.title == pv.title
        assert rv.target_renderer == pv.target_renderer
        assert rv.diagram_variant == pv.diagram_variant
        assert rv.animate_edges == pv.animate_edges
        assert rv.render_svg == pv.render_svg


@settings(max_examples=30, deadline=None)
@given(
    fixture=layered_structural_strategy(),
    invalid_turns=st.one_of(
        st.integers(max_value=0),
        st.integers(min_value=4, max_value=100),
        st.booleans(),
    ),
)
def test_run_propose_loop_rejects_invalid_max_turns(
    fixture: LayeredStructuralFixture,
    invalid_turns: int,
) -> None:
    """run_propose_loop raises ValueError for non-1..3 or bool max_turns."""
    with pytest.raises(ValueError, match="max_turns must be between 1 and 3"):
        run_propose_loop(fixture.ir, max_turns=invalid_turns)
