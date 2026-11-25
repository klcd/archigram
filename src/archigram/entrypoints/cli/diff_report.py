"""Baseline-vs-PR topological & budget regression comparator and GitHub PR Markdown reporter.

Surfaces *declared* architectural relationships (``require_reachability``,
``preserve_reachability``, ``protected_nodes``) ahead of bulk graph churn, classifies each
failing relationship as newly broken / pre-existing / restored, and degrades gracefully to a
head-only Gate 1-5 validation when the baseline is unavailable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from archigram.domain.diagnostics import DiagnosticSeverity, LintDiagnostic, LintReport
from archigram.domain.ir import CanonicalIR, IREdge, StructuralGraphIR
from archigram.entrypoints.cli.pipeline import (
    CompiledViewArtifact,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
)

if TYPE_CHECKING:
    from archigram.domain.spec import CurationSpecV1, DiagramViewSpec

KIND_REQUIRE = "require_reachability"
KIND_PRESERVE = "preserve_reachability"
KIND_PROTECTED = "protected_nodes"
KIND_DECLARED_SYMBOL = "declared_symbol"

_CODE_NOT_OBSERVED = "REACHABILITY_NOT_OBSERVED"
_CODE_SEVERED = "REACHABILITY_SEVERED"
_CODE_PROTECTED = "PROTECTED_NODE_DROPPED"
_CODE_UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL_ID"
_REACH_KINDS = frozenset({KIND_REQUIRE, KIND_PRESERVE})
_KIND_RANK = {KIND_REQUIRE: 0, KIND_PRESERVE: 1, KIND_PROTECTED: 2, KIND_DECLARED_SYMBOL: 3}

_MAX_RENDERED_CHANGES = 25
_MAX_RENDERED_EDGES = 3
_MAX_REASON_CHARS = 200


class RelationshipStatus(StrEnum):
    """Classification of a declared-relationship failure relative to the baseline."""

    NEWLY_BROKEN = "newly_broken"
    FAILING = "failing"  # head fails, baseline unavailable (cannot classify)
    PRE_EXISTING = "pre_existing"
    RESTORED = "restored"


_STATUS_RANK = {
    RelationshipStatus.NEWLY_BROKEN: 0,
    RelationshipStatus.FAILING: 1,
    RelationshipStatus.PRE_EXISTING: 2,
    RelationshipStatus.RESTORED: 3,
}
_STATUS_LABEL = {
    RelationshipStatus.NEWLY_BROKEN: "NEWLY BROKEN",
    RelationshipStatus.FAILING: "FAILING",
    RelationshipStatus.PRE_EXISTING: "PRE-EXISTING",
    RelationshipStatus.RESTORED: "RESTORED",
}


class DeclaredRelationshipChange(BaseModel):
    """One declared relationship (reachability / protected node) and its baseline-vs-head status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: str
    kind: str
    status: RelationshipStatus
    code: str
    label: str
    from_node: str | None = None
    to_node: str | None = None
    detail: str = ""
    removed_edges: tuple[str, ...] = Field(default_factory=tuple)
    added_edges: tuple[str, ...] = Field(default_factory=tuple)


class EdgeChurnSummary(BaseModel):
    """Raw observed-graph edge churn between base and head, split declared vs bulk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edges_added: int = 0
    edges_removed: int = 0
    declared_edges_changed: int = 0
    bulk_edges_changed: int = 0


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
    newly_broken_reachability: tuple[str, ...] = Field(default_factory=tuple)
    pre_existing_reachability_failures: tuple[str, ...] = Field(default_factory=tuple)
    head_reachability_failures: tuple[str, ...] = Field(default_factory=tuple)
    restored_reachability: tuple[str, ...] = Field(default_factory=tuple)
    newly_dropped_protected_nodes: tuple[str, ...] = Field(default_factory=tuple)
    restored_protected_nodes: tuple[str, ...] = Field(default_factory=tuple)
    base_error_count: int = 0
    head_error_count: int = 0
    new_error_count: int = 0
    pre_existing_error_count: int = 0
    resolved_error_count: int = 0
    has_regression: bool = False


class PrDiffSummary(BaseModel):
    """Structured baseline-vs-PR regression summary and GitHub PR Markdown report.

    When ``base_available`` is False the ``base_*`` fields are placeholders (``base_passed`` is
    True, counts are 0): only head validation ran and every head error is reported as new.
    """

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
    base_available: bool = True
    base_unavailable_reason: str | None = None
    declared_changes: tuple[DeclaredRelationshipChange, ...] = Field(default_factory=tuple)
    affected_view_ids: tuple[str, ...] = Field(default_factory=tuple)
    edge_churn: EdgeChurnSummary = Field(default_factory=EdgeChurnSummary)
    pre_existing_error_count: int = 0


# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------
# Declared relationship failures (extracted from Gate 1/2/3 diagnostics)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeclaredFailure:
    view_id: str
    kind: str
    label: str
    code: str
    from_node: str | None
    to_node: str | None
    via: tuple[str, ...]
    offending: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.view_id, self.kind, self.label)


def _declared_symbols(view: DiagramViewSpec) -> frozenset[str]:
    inv = view.invariants
    symbols: set[str] = set(inv.protected_nodes)
    for assertion in (*inv.preserve_reachability, *inv.require_reachability):
        symbols.add(assertion.from_node)
        symbols.add(assertion.to_node)
    return frozenset(symbols)


def _assertion_kind(
    view: DiagramViewSpec | None,
    src: str,
    dst: str,
    code: str,
) -> tuple[str, tuple[str, ...]]:
    if view is not None:
        for req in view.invariants.require_reachability:
            if (req.from_node, req.to_node) == (src, dst):
                return KIND_REQUIRE, tuple(req.via_relations)
        for pres in view.invariants.preserve_reachability:
            if (pres.from_node, pres.to_node) == (src, dst):
                return KIND_PRESERVE, tuple(pres.via_relations)
    return (KIND_REQUIRE if code == _CODE_NOT_OBSERVED else KIND_PRESERVE), ()


def _reach_endpoints(diag: LintDiagnostic) -> tuple[str, str] | None:
    if diag.code == _CODE_SEVERED and len(diag.witness_path) >= 2:
        return diag.witness_path[0], diag.witness_path[-1]
    if diag.code == _CODE_NOT_OBSERVED and len(diag.offending_symbols) == 2:
        return diag.offending_symbols[0], diag.offending_symbols[1]
    return None


def _reach_failures(diag: LintDiagnostic, view: DiagramViewSpec | None) -> list[_DeclaredFailure]:
    endpoints = _reach_endpoints(diag)
    if endpoints is None:
        return []
    src, dst = endpoints
    kind, via = _assertion_kind(view, src, dst, diag.code)
    return [
        _DeclaredFailure(
            view_id=diag.view_id or "",
            kind=kind,
            label=f"{src} -> {dst}",
            code=diag.code,
            from_node=src,
            to_node=dst,
            via=via,
            offending=diag.offending_symbols,
        )
    ]


def _symbol_failures(diag: LintDiagnostic, kind: str) -> list[_DeclaredFailure]:
    return [
        _DeclaredFailure(
            view_id=diag.view_id or "",
            kind=kind,
            label=symbol,
            code=diag.code,
            from_node=None,
            to_node=None,
            via=(),
            offending=(symbol,),
        )
        for symbol in diag.offending_symbols
    ]


def _failures_for_diag(
    diag: LintDiagnostic,
    view: DiagramViewSpec | None,
) -> list[_DeclaredFailure]:
    if diag.code in {_CODE_NOT_OBSERVED, _CODE_SEVERED}:
        return _reach_failures(diag, view)
    if diag.code == _CODE_PROTECTED:
        return _symbol_failures(diag, KIND_PROTECTED)
    if diag.code == _CODE_UNKNOWN_SYMBOL and view is not None:
        declared = _declared_symbols(view)
        if any(sym in declared for sym in diag.offending_symbols):
            return _symbol_failures(diag, KIND_DECLARED_SYMBOL)
    return []


def _declared_failures(
    report: LintReport,
    spec: CurationSpecV1,
) -> dict[tuple[str, str, str], _DeclaredFailure]:
    views = {view.id: view for view in spec.views}
    found: dict[tuple[str, str, str], _DeclaredFailure] = {}
    for diag in report.diagnostics:
        if diag.severity is not DiagnosticSeverity.ERROR:
            continue
        for failure in _failures_for_diag(diag, views.get(diag.view_id or "")):
            found.setdefault(failure.key, failure)
    return found


def _is_declared_diagnostic(
    diag: LintDiagnostic,
    declared_by_view: dict[str, frozenset[str]],
) -> bool:
    if diag.code in {_CODE_NOT_OBSERVED, _CODE_SEVERED, _CODE_PROTECTED}:
        return True
    if diag.code == _CODE_UNKNOWN_SYMBOL:
        declared = declared_by_view.get(diag.view_id or "", frozenset())
        return any(sym in declared for sym in diag.offending_symbols)
    return False


def _prioritize_diagnostics(
    diags: tuple[LintDiagnostic, ...],
    spec: CurationSpecV1,
) -> tuple[LintDiagnostic, ...]:
    """Stable-sort declared-relationship diagnostics ahead of bulk budget/style diagnostics."""
    declared_by_view = {view.id: _declared_symbols(view) for view in spec.views}
    return tuple(
        sorted(diags, key=lambda d: 0 if _is_declared_diagnostic(d, declared_by_view) else 1)
    )


# --------------------------------------------------------------------------------------
# Raw observed-graph edge deltas
# --------------------------------------------------------------------------------------


def _edge_key(edge: IREdge) -> str:
    return f"{edge.source} -[{edge.relation.value}]-> {edge.target}"


def _edge_keys(ir: CanonicalIR | None) -> frozenset[str]:
    if not isinstance(ir, StructuralGraphIR):
        return frozenset()
    return frozenset(_edge_key(edge) for edge in ir.edges)


def _touching_edge_keys(ir: CanonicalIR | None, symbol: str) -> frozenset[str]:
    if not isinstance(ir, StructuralGraphIR):
        return frozenset()
    return frozenset(_edge_key(edge) for edge in ir.edges if symbol in {edge.source, edge.target})


def _bfs_parents(
    ir: StructuralGraphIR,
    src: str,
    dst: str,
    allowed: frozenset[str],
) -> dict[str, IREdge] | None:
    adjacency: dict[str, list[IREdge]] = {}
    for edge in ir.edges:
        if allowed and edge.relation.value not in allowed:
            continue
        adjacency.setdefault(edge.source, []).append(edge)
    parents: dict[str, IREdge] = {}
    seen = {src}
    queue: deque[str] = deque([src])
    while queue:
        current = queue.popleft()
        for edge in adjacency.get(current, ()):
            if edge.target in seen:
                continue
            seen.add(edge.target)
            parents[edge.target] = edge
            if edge.target == dst:
                return parents
            queue.append(edge.target)
    return None


def _path_edge_keys(
    ir: CanonicalIR | None,
    src: str,
    dst: str,
    via: tuple[str, ...],
) -> tuple[str, ...]:
    """Edge keys along the deterministic shortest observed path (empty if none / not graph)."""
    if not isinstance(ir, StructuralGraphIR) or src == dst:
        return ()
    if src not in ir.nodes or dst not in ir.nodes:
        return ()
    parents = _bfs_parents(ir, src, dst, frozenset(via))
    if parents is None:
        return ()
    keys: list[str] = []
    node = dst
    while node != src:
        edge = parents[node]
        keys.append(_edge_key(edge))
        node = edge.source
    keys.reverse()
    return tuple(keys)


def _failure_edge_delta(
    failure: _DeclaredFailure,
    base_ir: CanonicalIR | None,
    head_ir: CanonicalIR,
    base_edges: frozenset[str],
    head_edges: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Raw edges removed from / added to the declared dependency between base and head."""
    if base_ir is None:
        return (), ()
    if failure.from_node is not None and failure.to_node is not None:
        base_path = _path_edge_keys(base_ir, failure.from_node, failure.to_node, failure.via)
        head_path = _path_edge_keys(head_ir, failure.from_node, failure.to_node, failure.via)
        removed = tuple(k for k in base_path if k not in head_edges)
        added = tuple(k for k in head_path if k not in base_edges)
        return removed, added
    symbol = failure.offending[0]
    removed_set = _touching_edge_keys(base_ir, symbol) - head_edges
    added_set = _touching_edge_keys(head_ir, symbol) - base_edges
    return tuple(sorted(removed_set)), tuple(sorted(added_set))


def _failure_detail(failure: _DeclaredFailure) -> str:
    if failure.code == _CODE_NOT_OBSERVED:
        return "no directed path in observed input"
    if failure.code == _CODE_SEVERED:
        return f"curated view severs observed path; bottleneck: {', '.join(failure.offending)}"
    if failure.code == _CODE_PROTECTED:
        return "protected node missing from curated view"
    return "declared symbol not found in observed input"


def _classify_changes(
    base_failures: dict[tuple[str, str, str], _DeclaredFailure] | None,
    head_failures: dict[tuple[str, str, str], _DeclaredFailure],
) -> list[tuple[_DeclaredFailure, RelationshipStatus]]:
    classified: list[tuple[_DeclaredFailure, RelationshipStatus]] = []
    for key, failure in head_failures.items():
        if base_failures is None:
            status = RelationshipStatus.FAILING
        elif key in base_failures:
            status = RelationshipStatus.PRE_EXISTING
        else:
            status = RelationshipStatus.NEWLY_BROKEN
        classified.append((failure, status))
    if base_failures is not None:
        classified.extend(
            (failure, RelationshipStatus.RESTORED)
            for key, failure in base_failures.items()
            if key not in head_failures
        )
    return classified


def _build_declared_changes(
    classified: list[tuple[_DeclaredFailure, RelationshipStatus]],
    base_ir: CanonicalIR | None,
    head_ir: CanonicalIR,
) -> tuple[DeclaredRelationshipChange, ...]:
    base_edges = _edge_keys(base_ir)
    head_edges = _edge_keys(head_ir)
    changes: list[DeclaredRelationshipChange] = []
    for failure, status in classified:
        removed, added = _failure_edge_delta(failure, base_ir, head_ir, base_edges, head_edges)
        changes.append(
            DeclaredRelationshipChange(
                view_id=failure.view_id,
                kind=failure.kind,
                status=status,
                code=failure.code,
                label=failure.label,
                from_node=failure.from_node,
                to_node=failure.to_node,
                detail="restored in head"
                if status is RelationshipStatus.RESTORED
                else _failure_detail(failure),
                removed_edges=removed,
                added_edges=added,
            )
        )
    changes.sort(
        key=lambda c: (
            _STATUS_RANK[c.status],
            _KIND_RANK.get(c.kind, 9),
            c.view_id,
            c.label,
        )
    )
    return tuple(changes)


def _edge_churn(
    base_ir: CanonicalIR | None,
    head_ir: CanonicalIR,
    changes: tuple[DeclaredRelationshipChange, ...],
) -> EdgeChurnSummary:
    if base_ir is None:
        return EdgeChurnSummary()
    base_edges = _edge_keys(base_ir)
    head_edges = _edge_keys(head_ir)
    added = len(head_edges - base_edges)
    removed = len(base_edges - head_edges)
    declared: set[str] = set()
    for change in changes:
        declared.update(change.removed_edges)
        declared.update(change.added_edges)
    return EdgeChurnSummary(
        edges_added=added,
        edges_removed=removed,
        declared_edges_changed=len(declared),
        bulk_edges_changed=max(added + removed - len(declared), 0),
    )


# --------------------------------------------------------------------------------------
# Per-view deltas
# --------------------------------------------------------------------------------------


def _labels(
    changes: tuple[DeclaredRelationshipChange, ...],
    view_id: str,
    kinds: frozenset[str],
    status: RelationshipStatus,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            c.label
            for c in changes
            if c.view_id == view_id and c.kind in kinds and c.status is status
        )
    )


def _build_view_delta(
    view: DiagramViewSpec,
    base_comp: PipelineCompilationResult | None,
    head_comp: PipelineCompilationResult,
    changes: tuple[DeclaredRelationshipChange, ...],
) -> ViewRegressionDelta:
    base_art = base_comp.views.get(view.id) if base_comp is not None else None
    head_art = head_comp.views.get(view.id)
    base_nodes, base_edges = _view_node_edge_counts(base_art)
    head_nodes, head_edges = _view_node_edge_counts(head_art)

    node_max = view.invariants.node_budget.max_count
    edge_max = view.invariants.edge_budget_max

    newly_broken = _labels(changes, view.id, _REACH_KINDS, RelationshipStatus.NEWLY_BROKEN)
    pre_existing = _labels(changes, view.id, _REACH_KINDS, RelationshipStatus.PRE_EXISTING)
    still_failing = _labels(changes, view.id, _REACH_KINDS, RelationshipStatus.FAILING)
    head_failures = tuple(sorted({*newly_broken, *pre_existing, *still_failing}))
    protected = frozenset({KIND_PROTECTED})
    newly_dropped = _labels(changes, view.id, protected, RelationshipStatus.NEWLY_BROKEN)

    base_view_diags = (
        tuple(d for d in base_comp.lint_report.diagnostics if d.view_id == view.id)
        if base_comp is not None
        else ()
    )
    head_view_diags = tuple(d for d in head_comp.lint_report.diagnostics if d.view_id == view.id)
    new_view_errs, resolved_view_errs = _diff_diagnostics(
        base_view_diags,
        head_view_diags,
        DiagnosticSeverity.ERROR,
    )
    base_err_count = sum(1 for d in base_view_diags if d.severity is DiagnosticSeverity.ERROR)
    head_err_count = sum(1 for d in head_view_diags if d.severity is DiagnosticSeverity.ERROR)
    has_regression = bool(new_view_errs or newly_broken or newly_dropped or head_err_count > 0)

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
        newly_severed_reachability=newly_broken,
        newly_broken_reachability=newly_broken,
        pre_existing_reachability_failures=pre_existing,
        head_reachability_failures=head_failures,
        restored_reachability=_labels(changes, view.id, _REACH_KINDS, RelationshipStatus.RESTORED),
        newly_dropped_protected_nodes=newly_dropped,
        restored_protected_nodes=_labels(changes, view.id, protected, RelationshipStatus.RESTORED),
        base_error_count=base_err_count,
        head_error_count=head_err_count,
        new_error_count=len(new_view_errs),
        pre_existing_error_count=head_err_count - len(new_view_errs),
        resolved_error_count=len(resolved_view_errs),
        has_regression=has_regression,
    )


# --------------------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------------------


def _format_signed(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


def _reach_cell(delta: ViewRegressionDelta) -> str:
    parts: list[str] = []
    if delta.newly_broken_reachability:
        parts.append(f"Newly broken: {', '.join(delta.newly_broken_reachability)}")
    if delta.pre_existing_reachability_failures:
        parts.append(f"Pre-existing: {', '.join(delta.pre_existing_reachability_failures)}")
    if not parts and delta.head_reachability_failures:
        parts.append(f"Failing: {', '.join(delta.head_reachability_failures)}")
    if not parts:
        parts.append("Restored" if delta.restored_reachability else "OK")
    return "; ".join(parts)


def _render_view_table_row(delta: ViewRegressionDelta, *, base_available: bool = True) -> str:
    if not base_available:
        return _render_head_only_row(delta)
    status = "FAIL" if delta.has_regression else "PASS"
    nodes_cell = (
        f"{delta.base_nodes} -> {delta.head_nodes} ({_format_signed(delta.node_delta)}) "
        f"[{delta.head_node_budget_pct:.1f}% of {delta.node_budget_max}]"
    )
    edges_cell = (
        f"{delta.base_edges} -> {delta.head_edges} ({_format_signed(delta.edge_delta)}) "
        f"[{delta.head_edge_budget_pct:.1f}% of {delta.edge_budget_max}]"
    )
    boundary_cell = (
        f"Dropped: {', '.join(delta.newly_dropped_protected_nodes)}"
        if delta.newly_dropped_protected_nodes
        else ("Restored" if delta.restored_protected_nodes else "OK")
    )
    gate_cell = (
        f"{delta.base_error_count} -> {delta.head_error_count} "
        f"(+{delta.new_error_count} new / {delta.pre_existing_error_count} pre-existing / "
        f"-{delta.resolved_error_count})"
    )
    return (
        f"| `{delta.view_id}` ({delta.renderer}) | {status} | {nodes_cell} | "
        f"{edges_cell} | {_reach_cell(delta)} | {boundary_cell} | {gate_cell} |"
    )


def _render_head_only_row(delta: ViewRegressionDelta) -> str:
    status = "FAIL" if delta.has_regression else "PASS"
    return (
        f"| `{delta.view_id}` ({delta.renderer}) | {status} | "
        f"{delta.head_nodes} [{delta.head_node_budget_pct:.1f}% of {delta.node_budget_max}] | "
        f"{delta.head_edges} [{delta.head_edge_budget_pct:.1f}% of {delta.edge_budget_max}] | "
        f"{_reach_cell(delta)} | n/a (no base) | {delta.head_error_count} (head) |"
    )


def _render_edge_list(label: str, edges: tuple[str, ...]) -> str:
    shown = ", ".join(f"`{e}`" for e in edges[:_MAX_RENDERED_EDGES])
    extra = len(edges) - _MAX_RENDERED_EDGES
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return f"{label} edge(s) {shown}{suffix}"


def _render_change(change: DeclaredRelationshipChange) -> str:
    tag = _STATUS_LABEL[change.status]
    line = (
        f"- **[{tag}]** `{change.kind}` `{change.label}` in view `{change.view_id or '(global)'}` "
        f"(`{change.code}`): {change.detail}"
    )
    extras: list[str] = []
    if change.removed_edges:
        extras.append(_render_edge_list("removed", change.removed_edges))
    if change.added_edges:
        extras.append(_render_edge_list("added", change.added_edges))
    if extras:
        line += ". Changed dependency: " + "; ".join(extras)
    return line


@dataclass(frozen=True, slots=True)
class _ReportData:
    passed: bool
    base_report: LintReport | None
    head_report: LintReport
    base_unavailable_reason: str | None
    new_errors: tuple[LintDiagnostic, ...]
    resolved_errors: tuple[LintDiagnostic, ...]
    view_deltas: tuple[ViewRegressionDelta, ...]
    declared_changes: tuple[DeclaredRelationshipChange, ...]
    affected_view_ids: tuple[str, ...]
    edge_churn: EdgeChurnSummary


def _render_priority_section(data: _ReportData) -> list[str]:
    lines = ["", "### Declared Relationship Changes & Affected Views", ""]
    if data.affected_view_ids:
        views = ", ".join(f"`{v}`" for v in data.affected_view_ids)
        lines.append(f"**Affected views ({len(data.affected_view_ids)}):** {views}")
    else:
        lines.append("**Affected views (0):** none")
    lines.append("")
    if not data.declared_changes:
        lines.append("- No declared relationship (reachability / protected node) changes.")
    for change in data.declared_changes[:_MAX_RENDERED_CHANGES]:
        lines.append(_render_change(change))
    hidden = len(data.declared_changes) - _MAX_RENDERED_CHANGES
    if hidden > 0:
        lines.append(f"- ... and {hidden} more declared relationship change(s) (see --json).")
    if data.base_report is not None:
        churn = data.edge_churn
        lines.append(
            f"- Raw edge churn: `+{churn.edges_added}` added / `-{churn.edges_removed}` removed "
            f"({churn.declared_edges_changed} on declared relationships, "
            f"{churn.bulk_edges_changed} bulk non-declared)."
        )
    return lines


def _render_count_lines(data: _ReportData) -> list[str]:
    head = data.head_report
    if data.base_report is None:
        return [
            f"- **Gate 1-5 Errors**: `{head.error_count}` (head)",
            f"- **Gate 1-5 Warnings**: `{head.warning_count}` (head)",
        ]
    base = data.base_report
    return [
        (
            f"- **Gate 1-5 Errors**: `{base.error_count}` (base) -> "
            f"`{head.error_count}` (head) "
            f"[`+{len(data.new_errors)}` new, `-{len(data.resolved_errors)}` resolved]"
        ),
        (
            f"- **Gate 1-5 Warnings**: `{base.warning_count}` (base) -> "
            f"`{head.warning_count}` (head)"
        ),
    ]


def _render_badge(data: _ReportData) -> str:
    if data.base_report is None:
        return (
            "PASSED (Head Valid; Base Unavailable)"
            if data.passed
            else ("FAILED (Head Invalid; Base Unavailable)")
        )
    return "PASSED (Zero Regressions)" if data.passed else "FAILED (Regressions Detected)"


def _render_markdown_summary(data: _ReportData) -> str:
    lines: list[str] = [
        f"## ArchiGram PR Architectural & Topological Diff: **{_render_badge(data)}**"
    ]
    if data.base_report is None:
        lines.extend(
            [
                "",
                f"> **Base comparison unavailable:** {data.base_unavailable_reason}. "
                "Gates 1-5 were still run on head; failures are reported as `[FAILING]` "
                "and are not classified as newly broken vs pre-existing.",
            ]
        )
    lines.extend(_render_priority_section(data))
    lines.extend(["", *_render_count_lines(data), ""])
    lines.extend(
        [
            (
                "| View | Status | Nodes (Budget) | Edges (Budget) | "
                "Reachability | Protected Boundaries | Gate Errors |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        _render_view_table_row(delta, base_available=data.base_report is not None)
        for delta in data.view_deltas
    )

    if data.new_errors:
        heading = (
            "New Gate 1-5 Error Diagnostics" if data.base_report else "Gate 1-5 Error Diagnostics"
        )
        lines.extend(["", f"### {heading}", ""])
        for diag in data.new_errors:
            view_tag = f" (view `{diag.view_id}`)" if diag.view_id else ""
            lines.append(
                f"- **[{diag.gate.value}] `{diag.code}`**{view_tag}: {diag.message} "
                f"*(Hint: {diag.repair_hint})*"
            )

    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------------------


def _clean_reason(reason: str | None) -> str:
    text = " ".join((reason or "base input was not provided").split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[: _MAX_REASON_CHARS - 3] + "..."
    return text


def _affected_views(
    changes: tuple[DeclaredRelationshipChange, ...],
    view_deltas: tuple[ViewRegressionDelta, ...],
) -> tuple[str, ...]:
    ids = {c.view_id for c in changes if c.view_id}
    ids.update(d.view_id for d in view_deltas if d.has_regression)
    return tuple(sorted(ids))


def compute_pr_regression_diff(
    base_ir: CanonicalIR | None,
    head_ir: CanonicalIR,
    spec: CurationSpecV1,
    *,
    base_unavailable_reason: str | None = None,
) -> PrDiffSummary:
    """Compare baseline vs head Canonical IR across all curated views and Gate 1-5 invariants.

    When ``base_ir`` is None only head validation runs: ``passed`` mirrors the head lint result,
    every head error is reported as new, and the summary records why the base was unavailable.
    """
    base_comp = compile_and_lint_pipeline(base_ir, spec) if base_ir is not None else None
    head_comp = compile_and_lint_pipeline(head_ir, spec)
    base_diags = base_comp.lint_report.diagnostics if base_comp is not None else ()
    head_diags = head_comp.lint_report.diagnostics

    new_errors, resolved_errors = _diff_diagnostics(
        base_diags, head_diags, DiagnosticSeverity.ERROR
    )
    new_errors = _prioritize_diagnostics(new_errors, spec)
    new_warnings, _ = _diff_diagnostics(base_diags, head_diags, DiagnosticSeverity.WARNING)

    base_failures = (
        _declared_failures(base_comp.lint_report, spec) if base_comp is not None else None
    )
    head_failures = _declared_failures(head_comp.lint_report, spec)
    declared_changes = _build_declared_changes(
        _classify_changes(base_failures, head_failures), base_ir, head_ir
    )
    view_deltas = tuple(
        _build_view_delta(view, base_comp, head_comp, declared_changes) for view in spec.views
    )
    has_regressions = bool(new_errors) or not head_comp.lint_report.passed
    passed = not has_regressions
    reason = None if base_comp is not None else _clean_reason(base_unavailable_reason)
    edge_churn = _edge_churn(base_ir, head_ir, declared_changes)
    affected = _affected_views(declared_changes, view_deltas)

    markdown = _render_markdown_summary(
        _ReportData(
            passed=passed,
            base_report=base_comp.lint_report if base_comp is not None else None,
            head_report=head_comp.lint_report,
            base_unavailable_reason=reason,
            new_errors=new_errors,
            resolved_errors=resolved_errors,
            view_deltas=view_deltas,
            declared_changes=declared_changes,
            affected_view_ids=affected,
            edge_churn=edge_churn,
        )
    )

    return PrDiffSummary(
        passed=passed,
        has_regressions=has_regressions,
        base_passed=base_comp.lint_report.passed if base_comp is not None else True,
        head_passed=head_comp.lint_report.passed,
        base_error_count=base_comp.lint_report.error_count if base_comp is not None else 0,
        head_error_count=head_comp.lint_report.error_count,
        base_warning_count=base_comp.lint_report.warning_count if base_comp is not None else 0,
        head_warning_count=head_comp.lint_report.warning_count,
        new_errors=new_errors,
        resolved_errors=resolved_errors,
        new_warnings=new_warnings,
        view_deltas=view_deltas,
        markdown_report=markdown,
        base_available=base_comp is not None,
        base_unavailable_reason=reason,
        declared_changes=declared_changes,
        affected_view_ids=affected,
        edge_churn=edge_churn,
        pre_existing_error_count=sum(d.pre_existing_error_count for d in view_deltas),
    )
