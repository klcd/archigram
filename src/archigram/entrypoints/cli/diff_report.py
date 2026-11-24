"""Baseline-vs-PR topological & budget regression comparator and GitHub PR Markdown reporter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from archigram.domain.diagnostics import DiagnosticSeverity, LintDiagnostic, LintReport
from archigram.domain.ir import CanonicalIR, StructuralGraphIR
from archigram.entrypoints.cli.pipeline import (
    CompiledViewArtifact,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
)

if TYPE_CHECKING:
    from archigram.domain.spec import CurationSpecV1, DiagramViewSpec


class ViewRegressionDelta(BaseModel):
    """Per-view topological, cognitive budget, reachability, boundary, and gate diagnostic delta."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: str
    title: str
    renderer: str
    base_nodes: int
    head_nodes: int
    node_delta: int
    node_budget_max: int
    base_node_budget_pct: float
    head_node_budget_pct: float
    base_edges: int
    head_edges: int
    edge_delta: int
    edge_budget_max: int
    base_edge_budget_pct: float
    head_edge_budget_pct: float
    newly_severed_reachability: tuple[str, ...] = Field(default_factory=tuple)
    restored_reachability: tuple[str, ...] = Field(default_factory=tuple)
    newly_dropped_protected_nodes: tuple[str, ...] = Field(default_factory=tuple)
    restored_protected_nodes: tuple[str, ...] = Field(default_factory=tuple)
    base_error_count: int = 0
    head_error_count: int = 0
    new_error_count: int = 0
    resolved_error_count: int = 0
    has_regression: bool = False


class PrDiffSummary(BaseModel):
    """Structured baseline-vs-PR regression summary and GitHub PR Markdown report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    has_regressions: bool
    base_passed: bool
    head_passed: bool
    base_error_count: int
    head_error_count: int
    base_warning_count: int
    head_warning_count: int
    new_errors: tuple[LintDiagnostic, ...] = Field(default_factory=tuple)
    resolved_errors: tuple[LintDiagnostic, ...] = Field(default_factory=tuple)
    new_warnings: tuple[LintDiagnostic, ...] = Field(default_factory=tuple)
    view_deltas: tuple[ViewRegressionDelta, ...] = Field(default_factory=tuple)
    markdown_report: str


def _view_node_edge_counts(artifact: CompiledViewArtifact | None) -> tuple[int, int]:
    if artifact is None:
        return 0, 0
    curated = artifact.curated_ir
    if isinstance(curated, StructuralGraphIR):
        return len(curated.nodes), len(curated.edges)
    return len(curated.participants), len(curated.events)


def _budget_pct(count: int, max_budget: int) -> float:
    if max_budget <= 0:
        return 100.0 if count > 0 else 0.0
    return round((count / max_budget) * 100.0, 1)


def _diagnostic_fingerprint(diag: LintDiagnostic) -> tuple[str, str, str, str, tuple[str, ...]]:
    return (
        diag.gate.value,
        diag.code,
        diag.view_id or "",
        diag.rule_id or "",
        diag.offending_symbols,
    )


def _diff_diagnostics(
    base_diags: tuple[LintDiagnostic, ...],
    head_diags: tuple[LintDiagnostic, ...],
    severity: DiagnosticSeverity,
) -> tuple[tuple[LintDiagnostic, ...], tuple[LintDiagnostic, ...]]:
    base_filtered = [d for d in base_diags if d.severity is severity]
    head_filtered = [d for d in head_diags if d.severity is severity]
    base_keys = {_diagnostic_fingerprint(d) for d in base_filtered}
    head_keys = {_diagnostic_fingerprint(d) for d in head_filtered}

    added = tuple(d for d in head_filtered if _diagnostic_fingerprint(d) not in base_keys)
    resolved = tuple(d for d in base_filtered if _diagnostic_fingerprint(d) not in head_keys)
    return added, resolved


def _extract_severed_reachability(
    report: LintReport,
    view_id: str,
) -> set[str]:
    severed: set[str] = set()
    for diag in report.diagnostics:
        if diag.view_id == view_id and diag.code == "REACHABILITY_SEVERED":
            if len(diag.witness_path) >= 2:
                severed.add(f"{diag.witness_path[0]} -> {diag.witness_path[-1]}")
            elif diag.offending_symbols:
                severed.add(", ".join(diag.offending_symbols))
            else:
                severed.add(diag.message)
    return severed


def _extract_dropped_protected_nodes(
    report: LintReport,
    view_id: str,
) -> set[str]:
    dropped: set[str] = set()
    for diag in report.diagnostics:
        if diag.view_id == view_id and diag.code == "PROTECTED_NODE_DROPPED":
            dropped.update(diag.offending_symbols)
    return dropped


def _build_view_delta(
    view: DiagramViewSpec,
    base_comp: PipelineCompilationResult,
    head_comp: PipelineCompilationResult,
) -> ViewRegressionDelta:
    base_art = base_comp.views.get(view.id)
    head_art = head_comp.views.get(view.id)
    base_nodes, base_edges = _view_node_edge_counts(base_art)
    head_nodes, head_edges = _view_node_edge_counts(head_art)

    node_max = view.invariants.node_budget.max_count
    edge_max = view.invariants.edge_budget_max

    base_severed = _extract_severed_reachability(base_comp.lint_report, view.id)
    head_severed = _extract_severed_reachability(head_comp.lint_report, view.id)
    newly_severed = tuple(sorted(head_severed - base_severed))
    restored_reach = tuple(sorted(base_severed - head_severed))

    base_dropped = _extract_dropped_protected_nodes(base_comp.lint_report, view.id)
    head_dropped = _extract_dropped_protected_nodes(head_comp.lint_report, view.id)
    newly_dropped = tuple(sorted(head_dropped - base_dropped))
    restored_prot = tuple(sorted(base_dropped - head_dropped))

    base_view_diags = tuple(d for d in base_comp.lint_report.diagnostics if d.view_id == view.id)
    head_view_diags = tuple(d for d in head_comp.lint_report.diagnostics if d.view_id == view.id)
    new_view_errs, resolved_view_errs = _diff_diagnostics(
        base_view_diags,
        head_view_diags,
        DiagnosticSeverity.ERROR,
    )
    base_err_count = sum(1 for d in base_view_diags if d.severity is DiagnosticSeverity.ERROR)
    head_err_count = sum(1 for d in head_view_diags if d.severity is DiagnosticSeverity.ERROR)

    has_regression = bool(new_view_errs or newly_severed or newly_dropped or head_err_count > 0)

    return ViewRegressionDelta(
        view_id=view.id,
        title=view.title,
        renderer=view.target_renderer.value,
        base_nodes=base_nodes,
        head_nodes=head_nodes,
        node_delta=head_nodes - base_nodes,
        node_budget_max=node_max,
        base_node_budget_pct=_budget_pct(base_nodes, node_max),
        head_node_budget_pct=_budget_pct(head_nodes, node_max),
        base_edges=base_edges,
        head_edges=head_edges,
        edge_delta=head_edges - base_edges,
        edge_budget_max=edge_max,
        base_edge_budget_pct=_budget_pct(base_edges, edge_max),
        head_edge_budget_pct=_budget_pct(head_edges, edge_max),
        newly_severed_reachability=newly_severed,
        restored_reachability=restored_reach,
        newly_dropped_protected_nodes=newly_dropped,
        restored_protected_nodes=restored_prot,
        base_error_count=base_err_count,
        head_error_count=head_err_count,
        new_error_count=len(new_view_errs),
        resolved_error_count=len(resolved_view_errs),
        has_regression=has_regression,
    )


def _format_signed(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


def _render_view_table_row(delta: ViewRegressionDelta) -> str:
    status = "FAIL" if delta.has_regression else "PASS"
    nodes_cell = (
        f"{delta.base_nodes} -> {delta.head_nodes} ({_format_signed(delta.node_delta)}) "
        f"[{delta.head_node_budget_pct:.1f}% of {delta.node_budget_max}]"
    )
    edges_cell = (
        f"{delta.base_edges} -> {delta.head_edges} ({_format_signed(delta.edge_delta)}) "
        f"[{delta.head_edge_budget_pct:.1f}% of {delta.edge_budget_max}]"
    )
    reach_cell = (
        f"Severed: {', '.join(delta.newly_severed_reachability)}"
        if delta.newly_severed_reachability
        else ("Restored" if delta.restored_reachability else "OK")
    )
    boundary_cell = (
        f"Dropped: {', '.join(delta.newly_dropped_protected_nodes)}"
        if delta.newly_dropped_protected_nodes
        else ("Restored" if delta.restored_protected_nodes else "OK")
    )
    gate_cell = (
        f"{delta.base_error_count} -> {delta.head_error_count} "
        f"(+{delta.new_error_count} / -{delta.resolved_error_count})"
    )
    return (
        f"| `{delta.view_id}` ({delta.renderer}) | {status} | {nodes_cell} | "
        f"{edges_cell} | {reach_cell} | {boundary_cell} | {gate_cell} |"
    )


def _render_markdown_summary(
    *,
    passed: bool,
    base_report: LintReport,
    head_report: LintReport,
    new_errors: tuple[LintDiagnostic, ...],
    resolved_errors: tuple[LintDiagnostic, ...],
    view_deltas: tuple[ViewRegressionDelta, ...],
) -> str:
    badge = "PASSED (Zero Regressions)" if passed else "FAILED (Regressions Detected)"
    lines: list[str] = [
        f"## ArchiGram PR Architectural & Topological Diff: **{badge}**",
        "",
        (
            f"- **Gate 1-5 Errors**: `{base_report.error_count}` (base) -> "
            f"`{head_report.error_count}` (head) "
            f"[`+{len(new_errors)}` new, `-{len(resolved_errors)}` resolved]"
        ),
        (
            f"- **Gate 1-5 Warnings**: `{base_report.warning_count}` (base) -> "
            f"`{head_report.warning_count}` (head)"
        ),
        "",
        (
            "| View | Status | Nodes (Budget) | Edges (Budget) | "
            "Reachability | Protected Boundaries | Gate Errors |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for delta in view_deltas:
        lines.append(_render_view_table_row(delta))

    if new_errors:
        lines.extend(["", "### New Gate 1-5 Error Diagnostics", ""])
        for diag in new_errors:
            view_tag = f" (view `{diag.view_id}`)" if diag.view_id else ""
            lines.append(
                f"- **[{diag.gate.value}] `{diag.code}`**{view_tag}: {diag.message} "
                f"*(Hint: {diag.repair_hint})*"
            )

    return "\n".join(lines)


def compute_pr_regression_diff(
    base_ir: CanonicalIR,
    head_ir: CanonicalIR,
    spec: CurationSpecV1,
) -> PrDiffSummary:
    """Compare baseline vs head Canonical IR across all curated views and Gate 1-5 invariants."""
    base_comp = compile_and_lint_pipeline(base_ir, spec)
    head_comp = compile_and_lint_pipeline(head_ir, spec)

    new_errors, resolved_errors = _diff_diagnostics(
        base_comp.lint_report.diagnostics,
        head_comp.lint_report.diagnostics,
        DiagnosticSeverity.ERROR,
    )
    new_warnings, _ = _diff_diagnostics(
        base_comp.lint_report.diagnostics,
        head_comp.lint_report.diagnostics,
        DiagnosticSeverity.WARNING,
    )

    view_deltas = tuple(_build_view_delta(view, base_comp, head_comp) for view in spec.views)
    has_regressions = bool(new_errors) or not head_comp.lint_report.passed
    passed = not has_regressions

    markdown = _render_markdown_summary(
        passed=passed,
        base_report=base_comp.lint_report,
        head_report=head_comp.lint_report,
        new_errors=new_errors,
        resolved_errors=resolved_errors,
        view_deltas=view_deltas,
    )

    return PrDiffSummary(
        passed=passed,
        has_regressions=has_regressions,
        base_passed=base_comp.lint_report.passed,
        head_passed=head_comp.lint_report.passed,
        base_error_count=base_comp.lint_report.error_count,
        head_error_count=head_comp.lint_report.error_count,
        base_warning_count=base_comp.lint_report.warning_count,
        head_warning_count=head_comp.lint_report.warning_count,
        new_errors=new_errors,
        resolved_errors=resolved_errors,
        new_warnings=new_warnings,
        view_deltas=view_deltas,
        markdown_report=markdown,
    )
