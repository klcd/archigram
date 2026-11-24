"""Gate 4: Cognitive view budgets, spanning-tree overflow fallback, and orphan detection."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from archigram.domain.concurrency import SpanInterval, cluster_overlapping_siblings
from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.ir import InteractionTraceIR, StructuralGraphIR
from archigram.domain.spec import IRMode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from archigram.domain import ProvenanceStamp
    from archigram.domain.ir import CanonicalIR, IREdge
    from archigram.domain.spec import BudgetRange, CurationSpecV1, DiagramViewSpec

_PAR_GROUP_ATTR_KEYS: tuple[str, ...] = (
    "par_group",
    "parallel_group_id",
    "parallel_region_id",
)


def _edge_priority_key(edge: IREdge) -> tuple[float, str, str, str]:
    """Return deterministic Kruskal priority key (-weight, source, target, edge_id)."""
    return (-edge.weight, edge.source, edge.target, edge.edge_id)


def _find_root(parent: dict[str, str], node_id: str) -> str:
    root = node_id
    while parent[root] != root:
        root = parent[root]
    curr = node_id
    while curr != root:
        nxt = parent[curr]
        parent[curr] = root
        curr = nxt
    return root


def _union_components(
    parent: dict[str, str],
    rank: dict[str, int],
    src: str,
    dst: str,
) -> bool:
    root_src = _find_root(parent, src)
    root_dst = _find_root(parent, dst)
    if root_src == root_dst:
        return False
    if rank[root_src] < rank[root_dst]:
        parent[root_src] = root_dst
    elif rank[root_src] > rank[root_dst]:
        parent[root_dst] = root_src
    else:
        parent[root_dst] = root_src
        rank[root_src] += 1
    return True


def _select_spanning_tree_edges(
    graph: StructuralGraphIR,
    edge_budget_max: int,
) -> tuple[tuple[IREdge, ...], tuple[IREdge, ...]]:
    sorted_edges = sorted(graph.edges, key=_edge_priority_key)
    if edge_budget_max == 0:
        return (), tuple(sorted_edges)

    parent = {sym_id: sym_id for sym_id in graph.nodes}
    rank = dict.fromkeys(graph.nodes, 0)

    kept: list[IREdge] = []
    deferred: list[IREdge] = []
    for edge in sorted_edges:
        if len(kept) < edge_budget_max and _union_components(
            parent,
            rank,
            edge.source,
            edge.target,
        ):
            kept.append(edge)
        else:
            deferred.append(edge)

    remaining_budget = edge_budget_max - len(kept)
    if remaining_budget > 0:
        kept.extend(deferred[:remaining_budget])
        omitted = tuple(deferred[remaining_budget:])
    else:
        omitted = tuple(deferred)

    kept_sorted = tuple(sorted(kept, key=_edge_priority_key))
    return kept_sorted, omitted


def _escape_markdown_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _format_provenance_cell(edge: IREdge) -> str:
    prov: ProvenanceStamp = edge.provenance
    details: list[str] = [f"edge_id={edge.edge_id}"]
    if prov.extractor is not None:
        details.append(f"extractor={prov.extractor}")
    if prov.rule_id is not None:
        details.append(f"rule_id={prov.rule_id}")
    if prov.file_path is not None:
        loc = (
            f"{prov.file_path}:{prov.line_span[0]}-{prov.line_span[1]}"
            if prov.line_span is not None
            else prov.file_path
        )
        details.append(f"file={loc}")
    elif prov.line_span is not None:
        details.append(f"lines={prov.line_span[0]}-{prov.line_span[1]}")
    if prov.backing_ids:
        details.append(f"backing={','.join(prov.backing_ids[:3])}")
    return _escape_markdown_cell(f"{prov.grade.value} ({'; '.join(details)})")


def _format_weight_cell(weight: float) -> str:
    formatted = f"{weight:.6f}".rstrip("0").rstrip(".")
    return formatted if formatted else f"{weight:.6g}"


def _render_overflow_markdown_table(omitted_edges: tuple[IREdge, ...]) -> str:
    if not omitted_edges:
        return ""
    lines = [
        "| Source | Target | Relation | Weight | Provenance |",
        "| --- | --- | --- | --- | --- |",
    ]
    for edge in omitted_edges:
        src = _escape_markdown_cell(edge.source)
        dst = _escape_markdown_cell(edge.target)
        rel = _escape_markdown_cell(edge.relation.value)
        wt = _format_weight_cell(edge.weight)
        prov = _format_provenance_cell(edge)
        lines.append(f"| {src} | {dst} | {rel} | {wt} | {prov} |")
    return "\n".join(lines)


def apply_spanning_tree_edge_fallback(
    graph: StructuralGraphIR,
    edge_budget_max: int,
) -> tuple[StructuralGraphIR, str]:
    """Keep a maximum-weight spanning forest + top edges and spill omitted edges to Markdown."""
    if edge_budget_max < 0:
        msg = f"edge_budget_max must be non-negative, got {edge_budget_max}"
        raise ValueError(msg)
    if len(graph.edges) <= edge_budget_max:
        return graph, ""
    kept, omitted = _select_spanning_tree_edges(graph, edge_budget_max)
    reduced = graph.model_copy(update={"edges": kept})
    return reduced, _render_overflow_markdown_table(omitted)


def _compute_node_degrees(graph: StructuralGraphIR) -> dict[str, int]:
    degrees: dict[str, int] = dict.fromkeys(graph.nodes, 0)
    for edge in graph.edges:
        degrees[edge.source] = degrees.get(edge.source, 0) + 1
        degrees[edge.target] = degrees.get(edge.target, 0) + 1
    return degrees


def _check_accidental_orphans(
    view: DiagramViewSpec,
    observed_graph: StructuralGraphIR,
    curated_graph: StructuralGraphIR,
) -> list[LintDiagnostic]:
    obs_degrees = _compute_node_degrees(observed_graph)
    cur_degrees = _compute_node_degrees(curated_graph)
    container_ids = {
        node.parent_id for node in curated_graph.nodes.values() if node.parent_id is not None
    }
    diagnostics: list[LintDiagnostic] = []
    for symbol_id in sorted(curated_graph.nodes):
        if cur_degrees.get(symbol_id, 0) > 0 or symbol_id in container_ids:
            continue
        obs_deg = obs_degrees.get(symbol_id, 0)
        if obs_deg == 0:
            continue
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_4_BUDGET_ORPHAN,
                severity=DiagnosticSeverity.ERROR,
                code="ACCIDENTAL_ORPHAN_NODE",
                message=(
                    f"View '{view.id}': node '{symbol_id}' became an accidental @unlinked "
                    f"orphan (0 incident edges in curated view vs {obs_deg} in observed graph)."
                ),
                view_id=view.id,
                offending_symbols=(symbol_id,),
                repair_hint=(
                    f"Add a 'prune_leaves' rule matching '{symbol_id}' or restore its "
                    "incident relation in the curated view."
                ),
            )
        )
    return diagnostics


def _check_node_budget(
    view: DiagramViewSpec,
    node_count: int,
    symbol_ids: tuple[str, ...],
    noun: str,
) -> list[LintDiagnostic]:
    budget: BudgetRange = view.invariants.node_budget
    if node_count > budget.max_count:
        return [
            LintDiagnostic(
                gate=GateId.GATE_4_BUDGET_ORPHAN,
                severity=DiagnosticSeverity.ERROR,
                code="NODE_BUDGET_EXCEEDED",
                message=(
                    f"View '{view.id}': {noun} count ({node_count}) exceeds "
                    f"node_budget.max_count ({budget.max_count})."
                ),
                view_id=view.id,
                offending_symbols=symbol_ids,
                repair_hint=(
                    "Apply 'collapse_partition' or 'prune_leaves' rules (or narrow scope) "
                    "to reduce nodes within node_budget.max_count."
                ),
            )
        ]
    if node_count < budget.min_count:
        return [
            LintDiagnostic(
                gate=GateId.GATE_4_BUDGET_ORPHAN,
                severity=DiagnosticSeverity.ERROR,
                code="NODE_BUDGET_UNDERRUN",
                message=(
                    f"View '{view.id}': {noun} count ({node_count}) is below "
                    f"node_budget.min_count ({budget.min_count})."
                ),
                view_id=view.id,
                offending_symbols=symbol_ids,
                repair_hint="Relax 'prune_leaves' / scope filters or lower node_budget.min_count.",
            )
        ]
    return []


def _check_structural_edge_budget(
    view: DiagramViewSpec,
    curated_graph: StructuralGraphIR,
) -> list[LintDiagnostic]:
    edge_count = len(curated_graph.edges)
    edge_budget_max = view.invariants.edge_budget_max
    if edge_count <= edge_budget_max:
        return []

    edge_ids = tuple(sorted(edge.edge_id for edge in curated_graph.edges))
    if view.invariants.overflow_strategy == "spanning_tree_table":
        spilled_count = edge_count - edge_budget_max
        return [
            LintDiagnostic(
                gate=GateId.GATE_4_BUDGET_ORPHAN,
                severity=DiagnosticSeverity.WARNING,
                code="EDGE_BUDGET_SPANNING_TREE_FALLBACK",
                message=(
                    f"View '{view.id}': edge count ({edge_count}) exceeds edge_budget_max "
                    f"({edge_budget_max}); {spilled_count} edge(s) spill to the spanning-tree "
                    "Markdown companion table."
                ),
                view_id=view.id,
                offending_symbols=edge_ids,
                repair_hint=(
                    "Apply 'transitive_reduction' or 'collapse_partition' to reduce edges, "
                    "or consult the Markdown overflow table."
                ),
            )
        ]

    return [
        LintDiagnostic(
            gate=GateId.GATE_4_BUDGET_ORPHAN,
            severity=DiagnosticSeverity.ERROR,
            code="EDGE_BUDGET_EXCEEDED",
            message=(
                f"View '{view.id}': edge count ({edge_count}) exceeds "
                f"edge_budget_max ({edge_budget_max})."
            ),
            view_id=view.id,
            offending_symbols=edge_ids,
            repair_hint=(
                "Enable 'transitive_reduction', apply 'collapse_partition' / 'prune_leaves', "
                "or set overflow_strategy='spanning_tree_table'."
            ),
        )
    ]


def _check_trace_edge_budget(
    view: DiagramViewSpec,
    curated_trace: InteractionTraceIR,
) -> list[LintDiagnostic]:
    event_count = len(curated_trace.events)
    edge_budget_max = view.invariants.edge_budget_max
    if event_count <= edge_budget_max:
        return []
    event_ids = tuple(sorted(curated_trace.events))
    return [
        LintDiagnostic(
            gate=GateId.GATE_4_BUDGET_ORPHAN,
            severity=DiagnosticSeverity.ERROR,
            code="EDGE_BUDGET_EXCEEDED",
            message=(
                f"View '{view.id}': event count ({event_count}) exceeds "
                f"edge_budget_max ({edge_budget_max})."
            ),
            view_id=view.id,
            offending_symbols=event_ids,
            repair_hint=(
                "Enable trace compaction ('fold_loops', 'fold_parallel_map') or "
                "apply 'transitive_reduction' / scope slicing to stay within edge_budget_max."
            ),
        )
    ]


def _extract_par_group_id(attributes: Mapping[str, str]) -> str | None:
    for key in _PAR_GROUP_ATTR_KEYS:
        val = attributes.get(key)
        if val is not None and val.strip():
            return val.strip()
    return None


def _collect_explicit_par_groups(
    trace: InteractionTraceIR,
) -> list[tuple[str, tuple[str, ...]]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for event_id in sorted(trace.events):
        group_id = _extract_par_group_id(trace.events[event_id].attributes)
        if group_id is not None:
            grouped[group_id].append(event_id)
    return [(group_id, tuple(eids)) for group_id, eids in sorted(grouped.items())]


def _collect_overlap_par_groups(
    trace: InteractionTraceIR,
    epsilon_ns: int,
) -> list[tuple[str, tuple[str, ...]]]:
    if len(trace.events) < 2:
        return []
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
        for ev in trace.events.values()
    ]
    clusters = cluster_overlapping_siblings(intervals, epsilon_ns=epsilon_ns)
    groups: list[tuple[str, tuple[str, ...]]] = []
    for cluster in clusters:
        parent_label = cluster.parent_event_id or "<root>"
        groups.append(
            (f"{parent_label}@{cluster.clock_domain_id}", tuple(sorted(cluster.event_ids)))
        )
    return groups


def _check_trace_par_branches(
    view: DiagramViewSpec,
    curated_trace: InteractionTraceIR,
) -> list[LintDiagnostic]:
    max_par = view.trace_compaction.max_par_branches
    epsilon_ns = view.trace_compaction.parallel_epsilon_ns
    explicit_groups = _collect_explicit_par_groups(curated_trace)
    overlap_groups = _collect_overlap_par_groups(curated_trace, epsilon_ns)

    seen_sets: set[frozenset[str]] = set()
    diagnostics: list[LintDiagnostic] = []
    for group_label, event_ids in (*explicit_groups, *overlap_groups):
        if len(event_ids) <= max_par:
            continue
        event_set = frozenset(event_ids)
        if event_set in seen_sets:
            continue
        seen_sets.add(event_set)
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_4_BUDGET_ORPHAN,
                severity=DiagnosticSeverity.ERROR,
                code="PAR_BRANCH_BUDGET_EXCEEDED",
                message=(
                    f"View '{view.id}': parallel group '{group_label}' has "
                    f"{len(event_ids)} concurrent branches, exceeding "
                    f"max_par_branches ({max_par})."
                ),
                view_id=view.id,
                offending_symbols=event_ids,
                repair_hint=(
                    "Enable 'fold_parallel_map' in trace_compaction (or increase "
                    "max_par_branches) to fold concurrent fan-out branches."
                ),
            )
        )
    return diagnostics


def _resolve_curated_view(
    view: DiagramViewSpec,
    observed_ir: CanonicalIR,
    curated_views: Mapping[str, CanonicalIR] | None,
) -> CanonicalIR | None:
    if curated_views is not None:
        return curated_views.get(view.id)
    if observed_ir.ir_mode != view.ir_mode.value:
        return None
    return observed_ir


def _check_single_view(
    view: DiagramViewSpec,
    observed_ir: CanonicalIR,
    curated: CanonicalIR,
) -> list[LintDiagnostic]:
    if view.ir_mode is IRMode.STRUCTURAL_GRAPH and isinstance(curated, StructuralGraphIR):
        diagnostics = [
            *_check_node_budget(
                view,
                len(curated.nodes),
                tuple(sorted(curated.nodes)),
                noun="node",
            ),
            *_check_structural_edge_budget(view, curated),
        ]
        if isinstance(observed_ir, StructuralGraphIR):
            diagnostics.extend(_check_accidental_orphans(view, observed_ir, curated))
        return diagnostics

    if view.ir_mode is IRMode.INTERACTION_TRACE and isinstance(curated, InteractionTraceIR):
        return [
            *_check_node_budget(
                view,
                len(curated.participants),
                tuple(sorted(curated.participants)),
                noun="participant",
            ),
            *_check_trace_edge_budget(view, curated),
            *_check_trace_par_branches(view, curated),
        ]

    return []


class CognitiveBudgetAndOrphanGate:
    """Gate 4: Cognitive view budgets, spanning-tree overflow warning, and orphan rejection."""

    @property
    def gate_id(self) -> GateId:
        """Return Gate 4 canonical identifier."""
        return GateId.GATE_4_BUDGET_ORPHAN

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport:
        """Validate node/edge/par-branch budgets and reject accidental @unlinked orphans."""
        diagnostics: list[LintDiagnostic] = []
        for view in spec.views:
            curated = _resolve_curated_view(view, observed_ir, curated_views)
            if curated is None:
                continue
            diagnostics.extend(_check_single_view(view, observed_ir, curated))
        return LintReport.from_diagnostics(diagnostics)
