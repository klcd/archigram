"""Bounded 3-Turn Lint-Repair Loop, Deterministic Heuristic Proposer, and View Compilation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from archigram.domain.concurrency import SpanInterval, cluster_overlapping_siblings
from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.ir import (
    CanonicalIR,
    InteractionTraceIR,
    StructuralGraphIR,
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
    parse_curation_spec_yaml,
)
from archigram.features.digest import (
    GraphDigestGenerator,
    StructuralDigest,
)
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_likec4 import LikeC4Emitter
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.features.emit_plantuml import PlantUmlEmitter
from archigram.features.lint_budget import (
    CognitiveBudgetAndOrphanGate,
    apply_spanning_tree_edge_fallback,
)
from archigram.features.lint_grounding import (
    GroundingAndBoundaryGate,
    collect_observed_symbols,
    matches_glob,
)
from archigram.features.lint_syntax import Gate5SyntaxHarness
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.reduce_structural import (
    CycleInducedError,
    StructuralTopologicalReducer,
)
from archigram.features.reduce_trace import TraceCompactionEngine

if TYPE_CHECKING:
    from collections.abc import Sequence

_CANONICAL_IR_ADAPTER: TypeAdapter[CanonicalIR] = TypeAdapter(CanonicalIR)
_DIGEST_GENERATOR = GraphDigestGenerator()
_STRUCTURAL_REDUCER = StructuralTopologicalReducer()
_TRACE_COMPACTOR = TraceCompactionEngine()
_GATE_1_2 = GroundingAndBoundaryGate()
_GATE_3 = TopologicalInvariantGate()
_GATE_4 = CognitiveBudgetAndOrphanGate()
_GATE_5 = Gate5SyntaxHarness()
_EMITTERS = {
    TargetRenderer.MERMAID: MermaidEmitter(),
    TargetRenderer.D2: D2Emitter(),
    TargetRenderer.PLANTUML: PlantUmlEmitter(),
    TargetRenderer.LIKEC4: LikeC4Emitter(),
}


def parse_canonical_ir_json(ir_json: str) -> CanonicalIR:
    """Parse and validate a raw JSON string into a CanonicalIR instance."""
    return _CANONICAL_IR_ADAPTER.validate_json(ir_json)


@runtime_checkable
class RuleProposerProtocol(Protocol):
    """Protocol for temperature-0 LLM or deterministic rule-based CurationSpecV1 proposers."""

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str: ...


class CompiledViewPreview(BaseModel):
    """Compiled preview metrics, emitted diagram text, and lint summary for a single view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: str
    title: str
    ir_mode: str
    target_renderer: str
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    branch_count: int = Field(ge=0)
    diagram_text: str
    overflow_markdown_table: str = ""
    syntax_valid: bool = True


class CuratedViewsPreviewReport(BaseModel):
    """Structured JSON preview payload returned by the preview_curated_views MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    lint_report: LintReport
    views: tuple[CompiledViewPreview, ...] = Field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProposeResult:
    """Outcome of executing the bounded 3-turn lint-repair curation spec proposal loop."""

    succeeded: bool
    turns_used: int
    spec: CurationSpecV1 | None
    spec_yaml: str
    final_report: LintReport
    history: tuple[LintReport, ...]


def make_schema_error_diagnostic(exc: Exception) -> LintDiagnostic:
    """Wrap a YAML or CurationSpecV1 schema validation error into a Gate 1 LintDiagnostic."""
    err_text = str(exc).strip() or "Invalid CurationSpecV1 YAML structure."
    return LintDiagnostic(
        gate=GateId.GATE_1_GROUNDING,
        severity=DiagnosticSeverity.ERROR,
        code="INVALID_CURATION_SPEC_SCHEMA",
        message=f"Failed to parse or validate CurationSpecV1 YAML: {err_text}",
        offending_symbols=("schema_version",),
        repair_hint=(
            "Emit valid CurationSpecV1 YAML with schema_version='1.0', valid closed-world "
            "rule operators ('prune_leaves', 'bypass_vertices', 'collapse_partition'), "
            "and non-blank identifiers."
        ),
    )


def _emit_dot_diagram(ir: CanonicalIR) -> str:
    lines: list[str] = ["digraph G {"]
    if isinstance(ir, StructuralGraphIR):
        for sym_id in sorted(ir.nodes):
            node = ir.nodes[sym_id]
            safe_id = sym_id.replace("\\", "\\\\").replace('"', '\\"')
            safe_lbl = node.label.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{safe_id}" [label="{safe_lbl}"];')
        for edge in ir.edges:
            src = edge.source.replace("\\", "\\\\").replace('"', '\\"')
            dst = edge.target.replace("\\", "\\\\").replace('"', '\\"')
            rel = edge.relation.value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{src}" -> "{dst}" [label="{rel}"];')
    else:
        for pid in sorted(ir.participants):
            part = ir.participants[pid]
            safe_id = pid.replace("\\", "\\\\").replace('"', '\\"')
            safe_lbl = part.label.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{safe_id}" [label="{safe_lbl}"];')
        for eid in sorted(ir.events):
            ev = ir.events[eid]
            src = ev.caller_id.replace("\\", "\\\\").replace('"', '\\"')
            dst = ev.callee_id.replace("\\", "\\\\").replace('"', '\\"')
            sig = ev.signature.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{src}" -> "{dst}" [label="{sig}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def emit_view_diagram(ir: CanonicalIR, renderer: TargetRenderer) -> str:
    """Deterministically emit diagram text for a curated view IR and target renderer."""
    if renderer is TargetRenderer.DOT:
        return _emit_dot_diagram(ir)
    emitter = _EMITTERS[renderer]
    return emitter.emit(ir)


def _compute_view_branch_count(curated: CanonicalIR, view: DiagramViewSpec) -> int:
    if isinstance(curated, StructuralGraphIR):
        regions = {e.parallel_region_id for e in curated.edges if e.parallel_region_id is not None}
        return len(regions)
    if len(curated.events) < 2:
        return 0
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
        for ev in curated.events.values()
    ]
    clusters = cluster_overlapping_siblings(
        intervals,
        epsilon_ns=view.trace_compaction.parallel_epsilon_ns,
    )
    return max((len(c.event_ids) for c in clusters), default=0)


def _reduce_single_view(
    observed_ir: CanonicalIR,
    view: DiagramViewSpec,
    global_rules: tuple[GlobalAbstractionRule, ...],
) -> tuple[CanonicalIR | None, list[LintDiagnostic]]:
    if isinstance(observed_ir, StructuralGraphIR) and view.ir_mode is IRMode.STRUCTURAL_GRAPH:
        try:
            reduced = _STRUCTURAL_REDUCER.reduce(observed_ir, view, global_rules)
        except CycleInducedError as exc:
            return None, [
                LintDiagnostic(
                    gate=GateId.GATE_3_TOPOLOGY_POSET,
                    severity=DiagnosticSeverity.ERROR,
                    code="INDUCED_PARTITION_CYCLE",
                    rule_id=exc.rule_id,
                    view_id=view.id,
                    offending_symbols=exc.member_ids or (exc.target_node_id,),
                    witness_path=exc.witness_path,
                    message=str(exc),
                    repair_hint=(
                        f"Split member_globs or add exclude_ids on collapse_partition rule "
                        f"'{exc.rule_id}' to prevent inducing a cycle."
                    ),
                )
            ]
        except ValueError as exc:
            return None, [
                LintDiagnostic(
                    gate=GateId.GATE_1_GROUNDING,
                    severity=DiagnosticSeverity.ERROR,
                    code="REDUCTION_RULE_CONFLICT",
                    view_id=view.id,
                    offending_symbols=(view.id,),
                    message=str(exc),
                    repair_hint="Choose a unique target_node_id or adjust rule globs.",
                )
            ]
        return reduced, []

    if isinstance(observed_ir, InteractionTraceIR) and view.ir_mode is IRMode.INTERACTION_TRACE:
        compacted = _TRACE_COMPACTOR.compact(observed_ir, view.trace_compaction)
        return compacted, []

    return None, [
        LintDiagnostic(
            gate=GateId.GATE_1_GROUNDING,
            severity=DiagnosticSeverity.ERROR,
            code="IR_MODE_MISMATCH",
            view_id=view.id,
            offending_symbols=(view.id,),
            message=(
                f"View '{view.id}' specifies ir_mode='{view.ir_mode.value}', "
                f"which does not match observed ir_mode='{observed_ir.ir_mode}'."
            ),
            repair_hint=f"Set view '{view.id}' ir_mode to '{observed_ir.ir_mode}'.",
        )
    ]


def _preview_and_validate_syntax(
    view: DiagramViewSpec,
    curated: CanonicalIR,
) -> tuple[CompiledViewPreview, list[LintDiagnostic]]:
    render_ir = curated
    overflow_md = ""
    if (
        isinstance(curated, StructuralGraphIR)
        and view.invariants.overflow_strategy == "spanning_tree_table"
        and len(curated.edges) > view.invariants.edge_budget_max
    ):
        render_ir, overflow_md = apply_spanning_tree_edge_fallback(
            curated,
            view.invariants.edge_budget_max,
        )

    diagram_text = emit_view_diagram(render_ir, view.target_renderer)
    syntax_res = _GATE_5.validate(view.target_renderer.value, diagram_text)
    diagnostics: list[LintDiagnostic] = []
    if not syntax_res.valid:
        err_joined = "; ".join(syntax_res.errors) or "Invalid emitted syntax"
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_5_SYNTAX,
                severity=DiagnosticSeverity.ERROR,
                code="SYNTAX_VALIDATION_FAILED",
                view_id=view.id,
                offending_symbols=(view.id,),
                message=(
                    f"Gate 5 syntax validation failed for view '{view.id}' "
                    f"({view.target_renderer.value}): {err_joined}"
                ),
                repair_hint=(
                    f"Ensure view '{view.id}' produces non-empty, syntactically valid "
                    f"'{view.target_renderer.value}' diagram source."
                ),
            )
        )

    node_count = (
        len(curated.nodes) if isinstance(curated, StructuralGraphIR) else len(curated.participants)
    )
    edge_count = (
        len(curated.edges) if isinstance(curated, StructuralGraphIR) else len(curated.events)
    )
    preview = CompiledViewPreview(
        view_id=view.id,
        title=view.title,
        ir_mode=view.ir_mode.value,
        target_renderer=view.target_renderer.value,
        node_count=node_count,
        edge_count=edge_count,
        branch_count=_compute_view_branch_count(curated, view),
        diagram_text=diagram_text,
        overflow_markdown_table=overflow_md,
        syntax_valid=syntax_res.valid,
    )
    return preview, diagnostics


def evaluate_curation_spec(
    observed_ir: CanonicalIR,
    spec: CurationSpecV1,
) -> CuratedViewsPreviewReport:
    """Execute reductions, run Gates 1-5, and compile previews for all views in CurationSpecV1."""
    diagnostics: list[LintDiagnostic] = []
    if not spec.views:
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_1_GROUNDING,
                severity=DiagnosticSeverity.ERROR,
                code="EMPTY_VIEWS_SPEC",
                message="CurationSpecV1 must define at least one view in 'views'.",
                offending_symbols=("views",),
                repair_hint="Add at least one DiagramViewSpec entry under 'views'.",
            )
        )

    g12_initial = _GATE_1_2.check(observed_ir, spec, curated_views=None)
    g1_errors = [
        d
        for d in g12_initial.diagnostics
        if d.gate is GateId.GATE_1_GROUNDING and d.severity is DiagnosticSeverity.ERROR
    ]

    curated_views: dict[str, CanonicalIR] = {}
    reduction_diags: list[LintDiagnostic] = []
    if not g1_errors:
        for view in spec.views:
            curated, r_diags = _reduce_single_view(observed_ir, view, spec.global_abstractions)
            reduction_diags.extend(r_diags)
            if curated is not None:
                curated_views[view.id] = curated

    g12_report = _GATE_1_2.check(
        observed_ir,
        spec,
        curated_views=curated_views if curated_views else None,
    )
    diagnostics.extend(g12_report.diagnostics)
    diagnostics.extend(reduction_diags)

    g3_report = _GATE_3.check(
        observed_ir,
        spec,
        curated_views=curated_views if curated_views else None,
    )
    diagnostics.extend(g3_report.diagnostics)

    if curated_views:
        g4_report = _GATE_4.check(observed_ir, spec, curated_views=curated_views)
        diagnostics.extend(g4_report.diagnostics)

    previews: list[CompiledViewPreview] = []
    for view in spec.views:
        curated = curated_views.get(view.id)
        if curated is None:
            continue
        preview, g5_diags = _preview_and_validate_syntax(view, curated)
        previews.append(preview)
        diagnostics.extend(g5_diags)

    lint_report = LintReport.from_diagnostics(diagnostics)
    return CuratedViewsPreviewReport(
        passed=lint_report.passed,
        lint_report=lint_report,
        views=tuple(previews),
    )


def _compute_orphan_leaves(ir: StructuralGraphIR) -> tuple[str, ...]:
    degrees: dict[str, int] = dict.fromkeys(ir.nodes, 0)
    for edge in ir.edges:
        degrees[edge.source] = degrees.get(edge.source, 0) + 1
        degrees[edge.target] = degrees.get(edge.target, 0) + 1
    parents = {node.parent_id for node in ir.nodes.values() if node.parent_id is not None}
    return tuple(sym for sym in sorted(ir.nodes) if degrees.get(sym, 0) == 0 and sym not in parents)


def _build_structural_baseline_rules(
    ir: StructuralGraphIR,
    digest: StructuralDigest,
    target_budget: int = 20,
) -> tuple[GlobalAbstractionRule, ...]:
    rules: list[GlobalAbstractionRule] = []
    orphans = _compute_orphan_leaves(ir)
    if orphans:
        rules.append(
            PruneLeavesRule(
                id="prune_isolated_leaves",
                node_id_globs=orphans,
                direction="both",
            )
        )

    remaining_count = len(ir.nodes) - len(orphans)
    if remaining_count <= target_budget:
        return tuple(rules)

    parents = {node.parent_id for node in ir.nodes.values() if node.parent_id is not None}
    sink_candidates = [
        leaf.symbol_id
        for leaf in digest.candidate_leaf_sinks
        if leaf.symbol_id not in orphans and leaf.symbol_id not in parents
    ]
    if sink_candidates and remaining_count - len(sink_candidates) >= 1:
        need = remaining_count - target_budget
        pruned_sinks = tuple(sink_candidates[: max(1, min(len(sink_candidates), need))])
        rules.append(
            PruneLeavesRule(
                id="prune_sink_leaves",
                node_id_globs=pruned_sinks,
                direction="sink",
            )
        )
        remaining_count -= len(pruned_sinks)

    return tuple(rules)


def build_heuristic_baseline_spec(ir: CanonicalIR | None) -> CurationSpecV1:
    """Build a deterministic, valid baseline CurationSpecV1 tailored to the observed CanonicalIR."""
    if isinstance(ir, InteractionTraceIR):
        part_count = len(ir.participants)
        ev_count = len(ir.events)
        min_nodes = 1 if part_count >= 1 else 0
        max_nodes = max(25, part_count + 5)
        max_edges = max(40, ev_count + 5)
        return CurationSpecV1(
            views=(
                DiagramViewSpec(
                    id="interaction_overview",
                    title=f"Interaction Trace Overview ({ir.trace_id})",
                    ir_mode=IRMode.INTERACTION_TRACE,
                    target_renderer=TargetRenderer.MERMAID,
                    trace_compaction=TraceCompactionConfig(
                        fold_loops=True,
                        fold_loop_with_opt=True,
                        cut_recursion=True,
                        fold_parallel_siblings=True,
                        fold_parallel_map=True,
                        max_par_branches=6,
                    ),
                    invariants=ViewInvariants(
                        node_budget=BudgetRange(min_count=min_nodes, max_count=max_nodes),
                        edge_budget_max=max_edges,
                    ),
                ),
            ),
        )

    if isinstance(ir, StructuralGraphIR):
        digest = _DIGEST_GENERATOR.digest_structural(ir)
        rules = _build_structural_baseline_rules(ir, digest)
        node_count = len(ir.nodes)
        min_nodes = 1 if node_count >= 1 else 0
        max_nodes = max(25, node_count)
        scope = FullScope()
        if node_count > 25 and digest.top_hubs:
            focal = tuple(h.symbol_id for h in digest.top_hubs[:3])
            scope = KHopNeighborhoodScope(
                focal_nodes=focal,
                upstream_depth=1,
                downstream_depth=1,
            )
            max_nodes = max(25, node_count)
        return CurationSpecV1(
            views=(
                DiagramViewSpec(
                    id="structural_overview",
                    title=f"Structural Overview ({ir.graph_id})",
                    ir_mode=IRMode.STRUCTURAL_GRAPH,
                    target_renderer=TargetRenderer.MERMAID,
                    rules=rules,
                    scope=scope,
                    invariants=ViewInvariants(
                        node_budget=BudgetRange(min_count=min_nodes, max_count=max_nodes),
                        edge_budget_max=max(40, len(ir.edges)),
                        overflow_strategy="spanning_tree_table",
                    ),
                ),
            ),
        )

    return CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="default_overview",
                title="Default Architectural Overview",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                invariants=ViewInvariants(
                    node_budget=BudgetRange(min_count=0, max_count=50),
                    edge_budget_max=100,
                ),
            ),
        ),
    )


def _extract_suggested_symbol(repair_hint: str) -> str | None:
    marker = "Did you mean '"
    pos = repair_hint.find(marker)
    if pos == -1:
        return None
    start = pos + len(marker)
    end = repair_hint.find("'", start)
    if end == -1:
        return None
    return repair_hint[start:end]


def _repair_symbol_tuple(
    symbols: tuple[str, ...],
    bad_to_good: dict[str, str | None],
    fallback_pool: tuple[str, ...],
    *,
    min_len: int = 0,
) -> tuple[str, ...]:
    repaired: list[str] = []
    for sym in symbols:
        if sym in bad_to_good:
            replacement = bad_to_good[sym]
            if replacement is not None and replacement not in repaired:
                repaired.append(replacement)
        elif sym not in repaired:
            repaired.append(sym)
    if len(repaired) < min_len and fallback_pool:
        for cand in fallback_pool:
            if cand not in repaired:
                repaired.append(cand)
            if len(repaired) >= min_len:
                break
    return tuple(repaired)


def _repair_prune_rule(
    rule: PruneLeavesRule,
    rule_diags: Sequence[LintDiagnostic],
    codes: set[str],
    zero_globs: set[str],
) -> tuple[GlobalAbstractionRule, ...]:
    if "PRUNE_INTERNAL_BRIDGE" in codes:
        bridge_syms = {
            sym
            for d in rule_diags
            if d.code == "PRUNE_INTERNAL_BRIDGE"
            for sym in d.offending_symbols
        }
        kept_globs = tuple(
            g
            for g in rule.node_id_globs
            if g not in zero_globs and not any(matches_glob(bs, g) for bs in bridge_syms)
        )
        converted: list[GlobalAbstractionRule] = []
        if bridge_syms:
            converted.append(
                BypassVerticesRule(
                    id=f"{rule.id}_bypass",
                    node_id_globs=tuple(sorted(bridge_syms)),
                )
            )
        if kept_globs and not bridge_syms:
            converted.append(rule.model_copy(update={"node_id_globs": kept_globs}))
        return tuple(converted)

    if "PROTECTED_NODE_DROPPED" in codes:
        dropped = {
            sym
            for d in rule_diags
            if d.code == "PROTECTED_NODE_DROPPED"
            for sym in d.offending_symbols
        }
        kept = tuple(
            g
            for g in rule.node_id_globs
            if g not in zero_globs and not any(matches_glob(p, g) for p in dropped)
        )
        return (rule.model_copy(update={"node_id_globs": kept}),) if kept else ()

    cleaned = tuple(g for g in rule.node_id_globs if g not in zero_globs)
    return (rule.model_copy(update={"node_id_globs": cleaned}),) if cleaned else ()


def _repair_bypass_rule(
    rule: BypassVerticesRule,
    rule_diags: Sequence[LintDiagnostic],
    codes: set[str],
    zero_globs: set[str],
) -> tuple[GlobalAbstractionRule, ...]:
    if "BICLIQUE_FANOUT_EXCEEDED" in codes or "PROTECTED_NODE_DROPPED" in codes:
        bad_nodes = {sym for d in rule_diags for sym in d.offending_symbols}
        kept = tuple(
            g
            for g in rule.node_id_globs
            if g not in zero_globs and not any(matches_glob(b, g) for b in bad_nodes)
        )
        return (rule.model_copy(update={"node_id_globs": kept}),) if kept else ()
    invalid_rels = {
        sym for d in rule_diags if d.code == "INVALID_EDGE_RELATION" for sym in d.offending_symbols
    }
    cleaned_globs = tuple(g for g in rule.node_id_globs if g not in zero_globs)
    if not cleaned_globs:
        return ()
    cleaned_rels = tuple(r for r in rule.preserve_edge_relations if r not in invalid_rels)
    return (
        rule.model_copy(
            update={
                "node_id_globs": cleaned_globs,
                "preserve_edge_relations": cleaned_rels,
                "preserve_concurrency_kind": True,
            }
        ),
    )


def _repair_single_rule(
    rule: GlobalAbstractionRule,
    diagnostics: Sequence[LintDiagnostic],
    bad_symbols: dict[str, str | None],
    zero_globs: set[str],
    observed_pool: tuple[str, ...],
) -> tuple[GlobalAbstractionRule, ...]:
    rule_diags = [d for d in diagnostics if d.rule_id == rule.id]
    codes = {d.code for d in rule_diags}
    if "INDUCED_PARTITION_CYCLE" in codes or "DISCONNECTED_PARTITION_MERGE" in codes:
        return ()
    if isinstance(rule, PruneLeavesRule):
        return _repair_prune_rule(rule, rule_diags, codes, zero_globs)
    if isinstance(rule, BypassVerticesRule):
        return _repair_bypass_rule(rule, rule_diags, codes, zero_globs)
    cleaned_members = tuple(g for g in rule.member_globs if g not in zero_globs)
    if not cleaned_members:
        return ()
    cleaned_excludes = _repair_symbol_tuple(rule.exclude_ids, bad_symbols, observed_pool)
    return (
        rule.model_copy(
            update={
                "member_globs": cleaned_members,
                "exclude_ids": cleaned_excludes,
            }
        ),
    )


def _repair_reachability_assertions(
    assertions: tuple[ReachabilityAssertion, ...],
    view_diags: Sequence[LintDiagnostic],
    codes: set[str],
    bad_symbols: dict[str, str | None],
    observed_pool: tuple[str, ...],
) -> tuple[ReachabilityAssertion, ...]:
    if "REACHABILITY_SEVERED" in codes:
        return ()
    invalid_rels = {
        sym for d in view_diags if d.code == "INVALID_EDGE_RELATION" for sym in d.offending_symbols
    }
    repaired: list[ReachabilityAssertion] = []
    for reach in assertions:
        f_rep = _repair_symbol_tuple((reach.from_node,), bad_symbols, observed_pool)
        t_rep = _repair_symbol_tuple((reach.to_node,), bad_symbols, observed_pool)
        if f_rep and t_rep:
            rels = tuple(r for r in reach.via_relations if r not in invalid_rels)
            repaired.append(
                ReachabilityAssertion(
                    from_node=f_rep[0],
                    to_node=t_rep[0],
                    via_relations=rels,
                )
            )
    return tuple(repaired)


def _repair_budgets_and_orphans(
    view: DiagramViewSpec,
    view_diags: Sequence[LintDiagnostic],
    codes: set[str],
    rules: list[GlobalAbstractionRule],
    observed_ir: CanonicalIR | None,
) -> tuple[BudgetRange, int, Literal["fail", "spanning_tree_table"], bool]:
    inv = view.invariants
    node_budget = inv.node_budget
    edge_budget_max = inv.edge_budget_max
    overflow_strategy: Literal["fail", "spanning_tree_table"] = inv.overflow_strategy
    widen_scope = False

    if "NODE_BUDGET_EXCEEDED" in codes:
        if isinstance(observed_ir, StructuralGraphIR) and observed_ir.nodes:
            digest = _DIGEST_GENERATOR.digest_structural(observed_ir)
            if digest.communities:
                rules.append(
                    CollapsePartitionRule(
                        id=f"collapse_{view.id}_budget",
                        target_node_id=f"super.{view.id}_cluster",
                        label="Collapsed Cluster",
                        member_globs=digest.communities[0].member_symbols,
                    )
                )
        offending_count = max(
            (len(d.offending_symbols) for d in view_diags if d.code == "NODE_BUDGET_EXCEEDED"),
            default=node_budget.max_count,
        )
        node_budget = BudgetRange(
            min_count=min(node_budget.min_count, offending_count),
            max_count=max(node_budget.max_count, offending_count),
        )
    if "NODE_BUDGET_UNDERRUN" in codes:
        rules[:] = [r for r in rules if not isinstance(r, PruneLeavesRule)]
        widen_scope = True
        node_budget = BudgetRange(min_count=0, max_count=max(1, node_budget.max_count))
    if "EDGE_BUDGET_EXCEEDED" in codes:
        overflow_strategy = "spanning_tree_table"
        offending_edges = max(
            (len(d.offending_symbols) for d in view_diags if d.code == "EDGE_BUDGET_EXCEEDED"),
            default=edge_budget_max,
        )
        edge_budget_max = max(edge_budget_max, offending_edges)
    if "ACCIDENTAL_ORPHAN_NODE" in codes:
        protected_set = frozenset(inv.protected_nodes)
        orphan_syms = tuple(
            sorted(
                {
                    sym
                    for d in view_diags
                    if d.code == "ACCIDENTAL_ORPHAN_NODE"
                    for sym in d.offending_symbols
                    if sym not in protected_set
                }
            )
        )
        if orphan_syms:
            rules.append(
                PruneLeavesRule(
                    id=f"prune_orphans_{view.id}",
                    node_id_globs=orphan_syms,
                    direction="both",
                )
            )
    return node_budget, edge_budget_max, overflow_strategy, widen_scope


def _repair_view_invariants_and_scope(
    view: DiagramViewSpec,
    view_diags: Sequence[LintDiagnostic],
    bad_symbols: dict[str, str | None],
    observed_pool: tuple[str, ...],
    observed_ir: CanonicalIR | None,
) -> DiagramViewSpec:
    codes = {d.code for d in view_diags}
    inv = view.invariants
    scope = view.scope
    rules = list(view.rules)

    protected = _repair_symbol_tuple(inv.protected_nodes, bad_symbols, observed_pool)
    repaired_reach = _repair_reachability_assertions(
        inv.preserve_reachability,
        view_diags,
        codes,
        bad_symbols,
        observed_pool,
    )
    node_budget, edge_budget_max, overflow_strategy, widen_scope = _repair_budgets_and_orphans(
        view,
        view_diags,
        codes,
        rules,
        observed_ir,
    )

    if isinstance(scope, KHopNeighborhoodScope):
        focals = _repair_symbol_tuple(scope.focal_nodes, bad_symbols, observed_pool, min_len=1)
        need_full = (
            widen_scope
            or "PROTECTED_NODE_DROPPED" in codes
            or "REACHABILITY_SEVERED" in codes
            or not focals
        )
        scope = FullScope() if need_full else scope.model_copy(update={"focal_nodes": focals})

    trace_cfg = view.trace_compaction
    if "PAR_BRANCH_BUDGET_EXCEEDED" in codes:
        max_branches = max(
            (
                len(d.offending_symbols)
                for d in view_diags
                if d.code == "PAR_BRANCH_BUDGET_EXCEEDED"
            ),
            default=trace_cfg.max_par_branches,
        )
        trace_cfg = trace_cfg.model_copy(
            update={
                "fold_parallel_siblings": True,
                "fold_parallel_map": True,
                "max_par_branches": max(2, max_branches),
            }
        )

    ir_mode = (
        IRMode(observed_ir.ir_mode)
        if ("IR_MODE_MISMATCH" in codes and observed_ir is not None)
        else view.ir_mode
    )
    new_inv = inv.model_copy(
        update={
            "protected_nodes": protected,
            "preserve_reachability": repaired_reach,
            "node_budget": node_budget,
            "edge_budget_max": edge_budget_max,
            "overflow_strategy": overflow_strategy,
        }
    )
    return view.model_copy(
        update={
            "ir_mode": ir_mode,
            "rules": tuple(rules),
            "scope": scope,
            "trace_compaction": trace_cfg,
            "invariants": new_inv,
        }
    )


def _extract_bad_symbols_and_globs(
    diagnostics: Sequence[LintDiagnostic],
) -> tuple[dict[str, str | None], set[str]]:
    bad_symbols: dict[str, str | None] = {}
    zero_globs: set[str] = set()
    for diag in diagnostics:
        if diag.code == "UNKNOWN_SYMBOL_ID":
            suggested = _extract_suggested_symbol(diag.repair_hint)
            for sym in diag.offending_symbols:
                bad_symbols[sym] = suggested
        elif diag.code == "ZERO_MATCH_GLOB":
            zero_globs.update(diag.offending_symbols)
    return bad_symbols, zero_globs


def _finalize_repaired_spec(
    prior_spec: CurationSpecV1,
    candidate: CurationSpecV1,
    observed_ir: CanonicalIR | None,
) -> CurationSpecV1:
    if observed_ir is None or evaluate_curation_spec(observed_ir, candidate).passed:
        return candidate
    baseline = build_heuristic_baseline_spec(observed_ir)
    if prior_spec.views and baseline.views:
        first_view = baseline.views[0].model_copy(
            update={
                "id": prior_spec.views[0].id,
                "title": prior_spec.views[0].title,
                "target_renderer": prior_spec.views[0].target_renderer,
            }
        )
        return baseline.model_copy(update={"views": (first_view,)})
    return baseline


def repair_curation_spec_from_diagnostics(
    prior_spec_yaml: str | None,
    diagnostics: tuple[LintDiagnostic, ...],
    observed_ir: CanonicalIR | None = None,
) -> CurationSpecV1:
    """Apply deterministic targeted self-repair to a CurationSpecV1 using Gate 1-5 diagnostics."""
    if prior_spec_yaml is None:
        return build_heuristic_baseline_spec(observed_ir)
    try:
        spec = parse_curation_spec_yaml(prior_spec_yaml)
    except Exception:
        return build_heuristic_baseline_spec(observed_ir)

    if not spec.views or any(d.code == "EMPTY_VIEWS_SPEC" for d in diagnostics):
        return build_heuristic_baseline_spec(observed_ir)

    observed_pool = (
        tuple(sorted(collect_observed_symbols(observed_ir))) if observed_ir is not None else ()
    )
    bad_symbols, zero_globs = _extract_bad_symbols_and_globs(diagnostics)

    repaired_globals = [
        rep
        for g_rule in spec.global_abstractions
        for rep in _repair_single_rule(g_rule, diagnostics, bad_symbols, zero_globs, observed_pool)
    ]

    repaired_views: list[DiagramViewSpec] = []
    for view in spec.views:
        v_diags = [d for d in diagnostics if d.view_id in {None, view.id}]
        v_rules = [
            rep
            for v_rule in view.rules
            for rep in _repair_single_rule(v_rule, v_diags, bad_symbols, zero_globs, observed_pool)
        ]
        updated_view = view.model_copy(update={"rules": tuple(v_rules)})
        repaired_views.append(
            _repair_view_invariants_and_scope(
                updated_view,
                v_diags,
                bad_symbols,
                observed_pool,
                observed_ir,
            )
        )

    try:
        candidate = CurationSpecV1(
            global_abstractions=tuple(repaired_globals),
            views=tuple(repaired_views),
            metadata=dict(spec.metadata),
        )
    except Exception:
        return build_heuristic_baseline_spec(observed_ir)

    return _finalize_repaired_spec(spec, candidate, observed_ir)


class DeterministicHeuristicProposer:
    """Offline deterministic rule-based baseline and diagnostic self-repair proposer."""

    def __init__(self, observed_ir: CanonicalIR | None = None) -> None:
        self._observed_ir = observed_ir

    def bind_ir(self, observed_ir: CanonicalIR) -> None:
        """Bind the current observed CanonicalIR for symbol-grounded baseline and repair."""
        self._observed_ir = observed_ir

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        """Generate or repair a CurationSpecV1 YAML deterministically."""
        _ = digest_markdown
        if turn <= 1 or prior_spec_yaml is None or not diagnostics:
            spec = build_heuristic_baseline_spec(self._observed_ir)
            return dump_curation_spec_yaml(spec)

        repaired = repair_curation_spec_from_diagnostics(
            prior_spec_yaml,
            diagnostics,
            self._observed_ir,
        )
        return dump_curation_spec_yaml(repaired)


def run_propose_loop(
    ir: CanonicalIR,
    proposer: RuleProposerProtocol | None = None,
    *,
    max_turns: int = 3,
) -> ProposeResult:
    """Run the bounded (<= 3 turns) digest -> propose -> 5-Gate lint-repair loop."""
    bounded_turns = max(1, min(max_turns, 3))
    active_proposer: RuleProposerProtocol = (
        proposer if proposer is not None else DeterministicHeuristicProposer(ir)
    )
    if isinstance(active_proposer, DeterministicHeuristicProposer):
        active_proposer.bind_ir(ir)

    digest_md = _DIGEST_GENERATOR.to_compact_markdown(ir)
    prior_yaml: str | None = None
    prior_diags: tuple[LintDiagnostic, ...] = ()
    history: list[LintReport] = []
    last_spec: CurationSpecV1 | None = None
    last_yaml = ""

    for turn in range(1, bounded_turns + 1):
        candidate_yaml = active_proposer.propose(digest_md, prior_yaml, prior_diags, turn)
        last_yaml = candidate_yaml
        try:
            parsed_spec = parse_curation_spec_yaml(candidate_yaml)
        except Exception as exc:
            schema_diag = make_schema_error_diagnostic(exc)
            report = LintReport.from_diagnostics((schema_diag,))
            history.append(report)
            last_spec = None
            prior_yaml = candidate_yaml
            prior_diags = report.diagnostics
            continue

        last_spec = parsed_spec
        eval_report = evaluate_curation_spec(ir, parsed_spec)
        report = eval_report.lint_report
        history.append(report)
        if report.passed:
            clean_yaml = dump_curation_spec_yaml(parsed_spec)
            return ProposeResult(
                succeeded=True,
                turns_used=turn,
                spec=parsed_spec,
                spec_yaml=clean_yaml,
                final_report=report,
                history=tuple(history),
            )
        prior_yaml = candidate_yaml
        prior_diags = report.diagnostics

    final_report = history[-1]
    return ProposeResult(
        succeeded=False,
        turns_used=bounded_turns,
        spec=last_spec,
        spec_yaml=last_yaml,
        final_report=final_report,
        history=tuple(history),
    )


def run_propose_cli(
    input_path: str,
    out_spec_path: str,
    max_turns: int = 3,
    *,
    proposer: RuleProposerProtocol | None = None,
) -> int:
    """CLI helper reading CanonicalIR JSON, running the 3-turn repair loop, and writing YAML."""
    in_file = Path(input_path)
    if not in_file.is_file():
        return 1
    try:
        raw_text = in_file.read_text(encoding="utf-8")
        ir = parse_canonical_ir_json(raw_text)
    except Exception:
        try:
            payload = json.loads(in_file.read_text(encoding="utf-8"))
            ir = _CANONICAL_IR_ADAPTER.validate_python(payload)
        except Exception:
            return 1

    result = run_propose_loop(ir, proposer=proposer, max_turns=max_turns)
    if not result.succeeded or result.spec is None:
        return 1

    out_file = Path(out_spec_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(result.spec_yaml, encoding="utf-8")
    return 0
