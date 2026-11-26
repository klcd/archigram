"""Gate 3: Topological & Partial-Order Invariant Linter.

Verifies mathematical invariants between observed Canonical IR and curated views:
- Reachability preservation with shortest-path witnesses and bottleneck attribution.
- Biclique fanout explosion guard on bypass_vertices rules.
- Induced quotient/partition cycle detection and SemRef component cohesion checks.
- Partial-order preservation and async fire-and-forget non-upgrade invariants.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from itertools import pairwise
from typing import TYPE_CHECKING

import rustworkx as rx

from archigram.domain import ProvenanceGrade
from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.ir import (
    CanonicalIR,
    InteractionTraceIR,
    IREdge,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    GlobalAbstractionRule,
    KHopNeighborhoodScope,
    ReachabilityAssertion,
    ViewScope,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class _IndexedDiGraph:
    """Deterministic rustworkx.PyDiGraph paired with bidirectional ID maps."""

    graph: rx.PyDiGraph[str, float]
    id_to_idx: dict[str, int]
    idx_to_id: tuple[str, ...]


@dataclass(slots=True)
class _StructuralContext:
    """Precomputed observed StructuralGraphIR indices shared across view checks."""

    observed_ir: StructuralGraphIR
    full_digraph: _IndexedDiGraph
    obs_scc_by_id: dict[str, int]
    obs_wcc_by_id: dict[str, int]
    obs_edges_by_id: dict[str, IREdge]
    filtered_cache: dict[frozenset[str], _IndexedDiGraph] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _TracePoset:
    """Causal DAG adjacency, bitmask descendants, and indexed graph for an InteractionTraceIR."""

    event_ids: tuple[str, ...]
    id_to_idx: dict[str, int]
    desc_mask: tuple[int, ...]
    indexed_graph: _IndexedDiGraph


def _build_structural_digraph(
    ir: StructuralGraphIR,
    allowed_relations: frozenset[str] | None = None,
) -> _IndexedDiGraph:
    """Build a deterministic unit-weighted PyDiGraph optionally filtered by edge relation."""
    graph: rx.PyDiGraph[str, float] = rx.PyDiGraph()
    sorted_ids = tuple(sorted(ir.nodes))
    id_to_idx: dict[str, int] = {}
    for symbol_id in sorted_ids:
        id_to_idx[symbol_id] = graph.add_node(symbol_id)

    for edge in sorted(ir.edges, key=lambda e: e.edge_id):
        if allowed_relations and edge.relation.value not in allowed_relations:
            continue
        graph.add_edge(id_to_idx[edge.source], id_to_idx[edge.target], 1.0)

    return _IndexedDiGraph(graph=graph, id_to_idx=id_to_idx, idx_to_id=sorted_ids)


def _build_structural_context(observed_ir: StructuralGraphIR) -> _StructuralContext:
    """Precompute SCCs, undirected WCCs, and edge lookup for an observed StructuralGraphIR."""
    full_digraph = _build_structural_digraph(observed_ir)
    obs_scc_by_id: dict[str, int] = {}
    for scc_id, component in enumerate(rx.strongly_connected_components(full_digraph.graph)):
        for idx in component:
            obs_scc_by_id[full_digraph.idx_to_id[idx]] = scc_id

    obs_wcc_by_id: dict[str, int] = {}
    for wcc_id, component in enumerate(rx.weakly_connected_components(full_digraph.graph)):
        for idx in component:
            obs_wcc_by_id[full_digraph.idx_to_id[idx]] = wcc_id

    obs_edges_by_id = {edge.edge_id: edge for edge in observed_ir.edges}
    cache: dict[frozenset[str], _IndexedDiGraph] = {frozenset(): full_digraph}
    return _StructuralContext(
        observed_ir=observed_ir,
        full_digraph=full_digraph,
        obs_scc_by_id=obs_scc_by_id,
        obs_wcc_by_id=obs_wcc_by_id,
        obs_edges_by_id=obs_edges_by_id,
        filtered_cache=cache,
    )


def _get_filtered_digraph(
    ir: StructuralGraphIR,
    allowed_relations: frozenset[str],
    cache: dict[frozenset[str], _IndexedDiGraph],
) -> _IndexedDiGraph:
    cached = cache.get(allowed_relations)
    if cached is not None:
        return cached
    built = _build_structural_digraph(ir, allowed_relations)
    cache[allowed_relations] = built
    return built


def _matches_globs(symbol_id: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatchcase(symbol_id, pattern) for pattern in globs)


def _collapse_members(
    observed_ir: StructuralGraphIR,
    rule: CollapsePartitionRule,
) -> tuple[str, ...]:
    excluded = frozenset(rule.exclude_ids)
    return tuple(
        symbol_id
        for symbol_id in sorted(observed_ir.nodes)
        if symbol_id not in excluded and _matches_globs(symbol_id, rule.member_globs)
    )


def _apply_rule_bindings(
    resolver: dict[str, str],
    observed_ir: StructuralGraphIR,
    curated_ir: StructuralGraphIR,
    rules: tuple[GlobalAbstractionRule, ...],
) -> None:
    """Bind collapse members in rule order, never rebinding symbols an earlier bypass dropped."""
    bypassed: set[str] = set()
    for rule in rules:
        if isinstance(rule, BypassVerticesRule):
            bypassed.update(
                symbol_id
                for symbol_id in observed_ir.nodes
                if symbol_id not in resolver and _matches_globs(symbol_id, rule.node_id_globs)
            )
            continue
        if not isinstance(rule, CollapsePartitionRule):
            continue
        if rule.target_node_id not in curated_ir.nodes:
            continue
        for member in _collapse_members(observed_ir, rule):
            if member not in bypassed:
                resolver.setdefault(member, rule.target_node_id)


def _build_curated_resolver(
    observed_ir: StructuralGraphIR,
    curated_ir: StructuralGraphIR,
    rules: tuple[GlobalAbstractionRule, ...],
) -> dict[str, str]:
    """Map each observed symbol_id to its surviving curated symbol_id or supernode."""
    resolver: dict[str, str] = {
        symbol_id: symbol_id for symbol_id in observed_ir.nodes if symbol_id in curated_ir.nodes
    }

    derived_nodes = sorted(
        curated_ir.nodes.values(),
        key=lambda n: (0 if n.kind is NodeKind.SUPERNODE else 1, n.symbol_id),
    )
    for node in derived_nodes:
        if node.provenance.grade is not ProvenanceGrade.DERIVED_DETERMINISTIC:
            continue
        for backing_id in node.provenance.backing_ids:
            if backing_id in observed_ir.nodes and backing_id not in resolver:
                resolver[backing_id] = node.symbol_id

    _apply_rule_bindings(resolver, observed_ir, curated_ir, rules)

    return resolver


def _build_backing_map(
    observed_ir: StructuralGraphIR,
    curated_ir: StructuralGraphIR,
    rules: tuple[GlobalAbstractionRule, ...],
) -> dict[str, frozenset[str]]:
    """Map each curated node symbol_id to the set of observed symbol_ids backing it."""
    collapse_by_target: dict[str, set[str]] = {}
    for rule in rules:
        if isinstance(rule, CollapsePartitionRule):
            collapse_by_target.setdefault(rule.target_node_id, set()).update(
                _collapse_members(observed_ir, rule)
            )

    backing: dict[str, frozenset[str]] = {}
    for symbol_id, node in curated_ir.nodes.items():
        members: set[str] = set()
        if node.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
            members.update(bid for bid in node.provenance.backing_ids if bid in observed_ir.nodes)
        if symbol_id in collapse_by_target:
            members.update(collapse_by_target[symbol_id])
        if not members and symbol_id in observed_ir.nodes:
            members.add(symbol_id)
        backing[symbol_id] = frozenset(members)
    return backing


def _dijkstra_path(
    graph: rx.PyDiGraph[str, float],
    src_idx: int,
    dst_idx: int,
) -> tuple[int, ...] | None:
    paths = rx.dijkstra_shortest_paths(graph, src_idx, target=dst_idx, default_weight=1.0)
    if dst_idx not in paths:
        return None
    return tuple(paths[dst_idx])


def _shortest_path_ids(
    indexed: _IndexedDiGraph,
    src_id: str,
    dst_id: str,
) -> tuple[str, ...] | None:
    """Compute a deterministic shortest hop path between two IDs using Dijkstra."""
    src_idx = indexed.id_to_idx.get(src_id)
    dst_idx = indexed.id_to_idx.get(dst_id)
    if src_idx is None or dst_idx is None:
        return None
    if src_idx == dst_idx:
        return (src_id,)
    node_indices = _dijkstra_path(indexed.graph, src_idx, dst_idx)
    if node_indices is None:
        return None
    return tuple(indexed.idx_to_id[idx] for idx in node_indices)


def _is_reachable_in_curated(
    cur_indexed: _IndexedDiGraph,
    cur_src: str | None,
    cur_dst: str | None,
) -> bool:
    if cur_src is None or cur_dst is None:
        return False
    if cur_src == cur_dst:
        return True
    src_idx = cur_indexed.id_to_idx.get(cur_src)
    dst_idx = cur_indexed.id_to_idx.get(cur_dst)
    if src_idx is None or dst_idx is None:
        return False
    return bool(rx.has_path(cur_indexed.graph, src_idx, dst_idx))


def _find_bottlenecks(
    witness_path: tuple[str, ...],
    resolver: dict[str, str],
    cur_indexed: _IndexedDiGraph,
) -> tuple[str, ...]:
    """Identify deleted/excluded bottleneck symbols or severed hop endpoints along witness_path."""
    missing = tuple(sym for sym in witness_path if sym not in resolver)
    if missing:
        return missing

    severed_endpoints: list[str] = []
    for u_obs, v_obs in pairwise(witness_path):
        u_cur = resolver[u_obs]
        v_cur = resolver[v_obs]
        if u_cur == v_cur:
            continue
        u_idx = cur_indexed.id_to_idx[u_cur]
        v_idx = cur_indexed.id_to_idx[v_cur]
        if not cur_indexed.graph.has_edge(u_idx, v_idx):
            severed_endpoints.extend((u_obs, v_obs))
    if severed_endpoints:
        return tuple(dict.fromkeys(severed_endpoints))
    return (witness_path[0], witness_path[-1])


@dataclass(frozen=True, slots=True)
class _SeverContext:
    """View identity, scope, and effective rules used to attribute a severed path."""

    view_id: str
    scope: ViewScope
    rules: tuple[GlobalAbstractionRule, ...] = ()


@dataclass(frozen=True, slots=True)
class _Attribution:
    """Evidence-based cause of a severed curated path (rules and/or scope exclusions)."""

    rule_ids: tuple[str, ...] = ()
    rule_notes: tuple[str, ...] = ()
    scope_excluded: tuple[str, ...] = ()
    scope_note: str = ""

    @property
    def rule_id(self) -> str | None:
        """Return the rule id only when exactly one rule is attributable."""
        return self.rule_ids[0] if len(self.rule_ids) == 1 else None


def _rule_removal_note(rule: GlobalAbstractionRule) -> str:
    note = f"{rule.op} '{rule.id}'"
    if isinstance(rule, BypassVerticesRule) and rule.preserve_edge_relations:
        note += f" (splices only relations {list(rule.preserve_edge_relations)})"
    return note


def _rules_removing_symbol(
    symbol_id: str,
    rules: tuple[GlobalAbstractionRule, ...],
) -> dict[str, str]:
    """Map rule id -> note for the rules that dropped a symbol absent from the curated view.

    A matching bypass_vertices/prune rule is the causal drop; collapse globs matching the
    same symbol are not co-attributed.
    """
    hits: dict[str, str] = {}
    dropping_hits: dict[str, str] = {}
    for rule in rules:
        if isinstance(rule, CollapsePartitionRule):
            matched = symbol_id not in rule.exclude_ids and _matches_globs(
                symbol_id, rule.member_globs
            )
        else:
            matched = _matches_globs(symbol_id, rule.node_id_globs)
        if matched:
            hits[rule.id] = _rule_removal_note(rule)
            if not isinstance(rule, CollapsePartitionRule):
                dropping_hits[rule.id] = hits[rule.id]
    return dropping_hits or hits


def _rules_collapsing_hop(
    bottlenecks: tuple[str, ...],
    rules: tuple[GlobalAbstractionRule, ...],
) -> dict[str, str]:
    """Map rule id -> note for collapse_partition rules contracting a severed hop endpoint."""
    hits: dict[str, str] = {}
    for rule in rules:
        if not isinstance(rule, CollapsePartitionRule):
            continue
        for symbol_id in bottlenecks:
            if symbol_id not in rule.exclude_ids and _matches_globs(symbol_id, rule.member_globs):
                hits[rule.id] = _rule_removal_note(rule)
    return hits


def _scope_note(scope: ViewScope) -> str:
    if not isinstance(scope, KHopNeighborhoodScope):
        return ""
    return (
        f"k_hop_neighborhood(focal_nodes={list(scope.focal_nodes)}, "
        f"upstream_depth={scope.upstream_depth}, downstream_depth={scope.downstream_depth})"
    )


def _attribute_severance(
    bottlenecks: tuple[str, ...],
    resolver: dict[str, str],
    sever: _SeverContext,
) -> _Attribution:
    """Attribute a severed path to rules (glob match) or k-hop scope exclusion when evidenced."""
    missing = tuple(sym for sym in bottlenecks if sym not in resolver)
    hits: dict[str, str] = {}
    scope_excluded: list[str] = []
    for symbol_id in missing:
        symbol_hits = _rules_removing_symbol(symbol_id, sever.rules)
        hits.update(symbol_hits)
        if not symbol_hits and _scope_note(sever.scope):
            scope_excluded.append(symbol_id)
    if not missing:
        hits.update(_rules_collapsing_hop(bottlenecks, sever.rules))
    return _Attribution(
        rule_ids=tuple(hits),
        rule_notes=tuple(hits.values()),
        scope_excluded=tuple(scope_excluded),
        scope_note=_scope_note(sever.scope) if scope_excluded else "",
    )


def _attribution_message(attribution: _Attribution) -> str:
    parts: list[str] = []
    if attribution.rule_notes:
        label = "rule" if len(attribution.rule_notes) == 1 else "rules (multiple attributable)"
        parts.append(f"attributable {label}: {', '.join(attribution.rule_notes)}")
    if attribution.scope_excluded:
        parts.append(
            f"excluded node(s) {', '.join(attribution.scope_excluded)} fall outside view scope "
            f"{attribution.scope_note}"
        )
    return f" Cause: {'; '.join(parts)}." if parts else ""


def _severed_repair_hint(
    bottlenecks: tuple[str, ...],
    attribution: _Attribution,
    sever: _SeverContext,
) -> str:
    joined = ", ".join(bottlenecks)
    bypass_ids = {r.id for r in sever.rules if isinstance(r, BypassVerticesRule)}
    if bypass_ids.intersection(attribution.rule_ids):
        hint = f"Bottleneck(s) ({joined}) were dropped by an existing bypass_vertices rule."
    else:
        hint = (
            "Preserve path connectivity by adding a bypass_vertices rule across intermediate "
            f"bottleneck(s) ({joined}) or widening the view k_hop_neighborhood."
        )
    if attribution.rule_ids:
        hint += (
            f" Narrow or remove rule(s) {', '.join(attribution.rule_ids)} so they no longer "
            "remove symbols on the observed witness path."
        )
    if attribution.scope_excluded:
        hint += (
            f" Add the excluded node(s) {', '.join(attribution.scope_excluded)} to the "
            "k_hop_neighborhood focal_nodes or increase upstream_depth/downstream_depth."
        )
    return hint


def _severed_diagnostic(
    assertion: ReachabilityAssertion,
    witness_path: tuple[str, ...],
    cur_indexed: _IndexedDiGraph,
    resolver: dict[str, str],
    sever: _SeverContext,
) -> LintDiagnostic | None:
    """Return REACHABILITY_SEVERED when curation loses an observed path, else None."""
    cur_src = resolver.get(assertion.from_node)
    cur_dst = resolver.get(assertion.to_node)
    if _is_reachable_in_curated(cur_indexed, cur_src, cur_dst):
        return None

    bottlenecks = _find_bottlenecks(witness_path, resolver, cur_indexed)
    attribution = _attribute_severance(bottlenecks, resolver, sever)
    rel_note = (
        f" via relations {list(assertion.via_relations)}"
        if assertion.via_relations
        else " over any relation"
    )
    return LintDiagnostic(
        gate=GateId.GATE_3_TOPOLOGY_POSET,
        severity=DiagnosticSeverity.ERROR,
        code="REACHABILITY_SEVERED",
        view_id=sever.view_id,
        rule_id=attribution.rule_id,
        offending_symbols=bottlenecks,
        witness_path=witness_path,
        message=(
            f"Reachability from '{assertion.from_node}' to '{assertion.to_node}'{rel_note} "
            f"was severed in view '{sever.view_id}'; observed witness: "
            f"{' -> '.join(witness_path)}; bottleneck symbol(s): {', '.join(bottlenecks)}."
            f"{_attribution_message(attribution)}"
        ),
        repair_hint=_severed_repair_hint(bottlenecks, attribution, sever),
    )


def _check_single_reachability(
    assertion: ReachabilityAssertion,
    obs_indexed: _IndexedDiGraph,
    cur_indexed: _IndexedDiGraph,
    resolver: dict[str, str],
    sever: _SeverContext,
) -> LintDiagnostic | None:
    """preserve_reachability: preserve the observed path if one exists (vacuous otherwise)."""
    witness_path = _shortest_path_ids(obs_indexed, assertion.from_node, assertion.to_node)
    if witness_path is None:
        return None
    return _severed_diagnostic(assertion, witness_path, cur_indexed, resolver, sever)


def _check_required_reachability(
    assertion: ReachabilityAssertion,
    obs_indexed: _IndexedDiGraph,
    cur_indexed: _IndexedDiGraph,
    resolver: dict[str, str],
    sever: _SeverContext,
) -> LintDiagnostic | None:
    """require_reachability: observed path MUST exist AND curation must preserve it."""
    if assertion.from_node not in obs_indexed.id_to_idx:
        return None  # Unknown endpoint: reported by Gate 1 grounding, never a fake path error.
    if assertion.to_node not in obs_indexed.id_to_idx:
        return None

    witness_path = _shortest_path_ids(obs_indexed, assertion.from_node, assertion.to_node)
    if witness_path is not None:
        return _severed_diagnostic(assertion, witness_path, cur_indexed, resolver, sever)

    rel_note = (
        f" restricted to relations {list(assertion.via_relations)}"
        if assertion.via_relations
        else " over any relation"
    )
    return LintDiagnostic(
        gate=GateId.GATE_3_TOPOLOGY_POSET,
        severity=DiagnosticSeverity.ERROR,
        code="REACHABILITY_NOT_OBSERVED",
        view_id=sever.view_id,
        offending_symbols=(assertion.from_node, assertion.to_node),
        message=(
            f"Required reachability from '{assertion.from_node}' to '{assertion.to_node}'"
            f"{rel_note} has no directed path in the observed input for view "
            f"'{sever.view_id}'; no witness path exists."
        ),
        repair_hint=(
            "Fix the endpoints or relation filter in require_reachability, or move the "
            "assertion to preserve_reachability if the path is optional."
        ),
    )


def _check_view_reachability(
    ctx: _StructuralContext,
    curated_ir: StructuralGraphIR,
    view: DiagramViewSpec,
    resolver: dict[str, str],
    rules: tuple[GlobalAbstractionRule, ...] = (),
) -> list[LintDiagnostic]:
    inv = view.invariants
    if not inv.preserve_reachability and not inv.require_reachability:
        return []

    cur_cache: dict[frozenset[str], _IndexedDiGraph] = {}
    diagnostics: list[LintDiagnostic] = []
    sever = _SeverContext(view_id=view.id, scope=view.scope, rules=rules)
    checks = (
        *((a, _check_single_reachability) for a in inv.preserve_reachability),
        *((a, _check_required_reachability) for a in inv.require_reachability),
    )
    for assertion, check in checks:
        allowed = frozenset(assertion.via_relations)
        obs_indexed = _get_filtered_digraph(ctx.observed_ir, allowed, ctx.filtered_cache)
        cur_indexed = _get_filtered_digraph(curated_ir, allowed, cur_cache)
        diag = check(assertion, obs_indexed, cur_indexed, resolver, sever)
        if diag is not None:
            diagnostics.append(diag)
    return diagnostics


def _edge_degree_counts(
    observed_ir: StructuralGraphIR,
    allowed_relations: frozenset[str],
) -> tuple[dict[str, int], dict[str, int]]:
    in_counts: dict[str, int] = {}
    out_counts: dict[str, int] = {}
    for edge in observed_ir.edges:
        if edge.source == edge.target:
            continue
        if allowed_relations and edge.relation.value not in allowed_relations:
            continue
        out_counts[edge.source] = out_counts.get(edge.source, 0) + 1
        in_counts[edge.target] = in_counts.get(edge.target, 0) + 1
    return in_counts, out_counts


def _check_bypass_rule(
    observed_ir: StructuralGraphIR,
    rule: BypassVerticesRule,
    view_id: str | None,
    *,
    already_flagged: frozenset[str] = frozenset(),
) -> list[LintDiagnostic]:
    allowed = frozenset(rule.preserve_edge_relations)
    in_counts, out_counts = _edge_degree_counts(observed_ir, allowed)
    diagnostics: list[LintDiagnostic] = []

    for symbol_id in sorted(observed_ir.nodes):
        if symbol_id in already_flagged or not _matches_globs(symbol_id, rule.node_id_globs):
            continue
        in_deg = in_counts.get(symbol_id, 0)
        out_deg = out_counts.get(symbol_id, 0)
        product = in_deg * out_deg
        if product > rule.max_fanout_product:
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_3_TOPOLOGY_POSET,
                    severity=DiagnosticSeverity.ERROR,
                    code="BICLIQUE_FANOUT_EXCEEDED",
                    rule_id=rule.id,
                    view_id=view_id,
                    offending_symbols=(symbol_id,),
                    message=(
                        f"Bypassing vertex '{symbol_id}' under rule '{rule.id}' creates "
                        f"in_degree * out_degree = {in_deg} * {out_deg} = {product}, "
                        f"exceeding max_fanout_product={rule.max_fanout_product}."
                    ),
                    repair_hint=(
                        f"Replace bypass_vertices on hub '{symbol_id}' with collapse_partition "
                        f"or keep '{symbol_id}' visible as an explicit hub."
                    ),
                )
            )
    return diagnostics


def _count_partition_components(ctx: _StructuralContext, members: tuple[str, ...]) -> int:
    """Count undirected components across edge WCCs and in-partition parent_id containment."""
    parent_map: dict[int, int] = {ctx.obs_wcc_by_id[m]: ctx.obs_wcc_by_id[m] for m in members}
    if len(parent_map) < 2:
        return len(parent_map)

    def _find(comp: int) -> int:
        while parent_map[comp] != comp:
            parent_map[comp] = parent_map[parent_map[comp]]
            comp = parent_map[comp]
        return comp

    member_set = frozenset(members)
    for member in members:
        lexical_parent = ctx.observed_ir.nodes[member].parent_id
        if lexical_parent is not None and lexical_parent in member_set:
            ra = _find(ctx.obs_wcc_by_id[member])
            rb = _find(ctx.obs_wcc_by_id[lexical_parent])
            if ra != rb:
                parent_map[ra] = rb

    return len({_find(comp) for comp in parent_map})


def _check_collapse_rule(
    ctx: _StructuralContext,
    rule: CollapsePartitionRule,
    view_id: str | None,
    *,
    strict_cohesion: bool,
) -> LintDiagnostic | None:
    members = _collapse_members(ctx.observed_ir, rule)
    if len(members) < 2:
        return None

    component_count = _count_partition_components(ctx, members)
    if component_count < 2:
        return None

    severity = DiagnosticSeverity.ERROR if strict_cohesion else DiagnosticSeverity.WARNING
    return LintDiagnostic(
        gate=GateId.GATE_3_TOPOLOGY_POSET,
        severity=severity,
        code="DISCONNECTED_PARTITION_MERGE",
        rule_id=rule.id,
        view_id=view_id,
        offending_symbols=members,
        message=(
            f"Collapse rule '{rule.id}' merges {len(members)} observed symbols spanning "
            f"{component_count} disconnected undirected components (zero cohesion)."
        ),
        repair_hint=(
            f"Split rule '{rule.id}' member_globs into separate cohesive collapse_partition "
            "rules per connected component or exclude unrelated symbols via exclude_ids."
        ),
    )


def _check_structural_rules(
    ctx: _StructuralContext,
    rules: Sequence[GlobalAbstractionRule],
    view_id: str | None,
    *,
    strict_cohesion: bool,
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    for rule in rules:
        if isinstance(rule, BypassVerticesRule):
            diagnostics.extend(_check_bypass_rule(ctx.observed_ir, rule, view_id))
        elif isinstance(rule, CollapsePartitionRule):
            diag = _check_collapse_rule(ctx, rule, view_id, strict_cohesion=strict_cohesion)
            if diag is not None:
                diagnostics.append(diag)
    return diagnostics


def _check_post_collapse_bypass(
    ctx: _StructuralContext,
    curated_ir: StructuralGraphIR,
    rules: Sequence[GlobalAbstractionRule],
    view_id: str,
) -> list[LintDiagnostic]:
    """Enforce max_fanout_product on bypass nodes whose degrees grew after collapse."""
    diagnostics: list[LintDiagnostic] = []
    for rule in rules:
        if not isinstance(rule, BypassVerticesRule):
            continue
        observed_hits = _check_bypass_rule(ctx.observed_ir, rule, view_id)
        flagged = frozenset(s for d in observed_hits for s in d.offending_symbols)
        diagnostics.extend(_check_bypass_rule(curated_ir, rule, view_id, already_flagged=flagged))
    return diagnostics


def _rotate_cycle(cycle_nodes: list[str]) -> tuple[str, ...]:
    """Rotate a simple cycle to start at its lexicographically smallest symbol and close it."""
    min_pos = min(range(len(cycle_nodes)), key=cycle_nodes.__getitem__)
    rotated = cycle_nodes[min_pos:] + cycle_nodes[:min_pos]
    return (*rotated, rotated[0])


def _extract_edge_induced_cycle(
    scc_sorted: list[int],
    scc_set: set[int],
    cur_indexed: _IndexedDiGraph,
    node_obs_sccs: dict[str, frozenset[int]],
) -> tuple[str, ...] | None:
    """Find a shortest simple cycle traversing an edge whose endpoints share no observed SCC."""
    graph = cur_indexed.graph
    idx_to_id = cur_indexed.idx_to_id
    for u_idx in scc_sorted:
        u_sccs = node_obs_sccs[idx_to_id[u_idx]]
        neighbors = sorted(
            (n_idx for n_idx in graph.successor_indices(u_idx) if n_idx in scc_set),
            key=idx_to_id.__getitem__,
        )
        for v_idx in neighbors:
            if u_sccs.isdisjoint(node_obs_sccs[idx_to_id[v_idx]]):
                back_path = _dijkstra_path(graph, v_idx, u_idx)
                if back_path is not None:
                    cycle_nodes = [idx_to_id[idx] for idx in back_path]
                    return _rotate_cycle(cycle_nodes)
    return None


def _extract_walk_induced_cycle(
    scc_sorted: list[int],
    scc_set: set[int],
    cur_indexed: _IndexedDiGraph,
    node_obs_sccs: dict[str, frozenset[int]],
) -> tuple[str, ...] | None:
    """Fallback BFS witness when pairwise edges share SCCs but full SCC intersection is empty."""
    graph = cur_indexed.graph
    idx_to_id = cur_indexed.idx_to_id
    start_idx = scc_sorted[0]
    start_sccs = node_obs_sccs[idx_to_id[start_idx]]
    queue: deque[tuple[int, frozenset[int], tuple[int, ...]]] = deque(
        [(start_idx, start_sccs, (start_idx,))]
    )
    visited: set[tuple[int, frozenset[int]]] = {(start_idx, start_sccs)}
    max_states = max(256, 4 * len(scc_sorted))

    while queue and len(visited) <= max_states:
        curr, rem_sccs, path = queue.popleft()
        if not rem_sccs and len(path) >= 2:
            back = _dijkstra_path(graph, curr, start_idx)
            if back is not None:
                full = [idx_to_id[i] for i in path] + [idx_to_id[i] for i in back[1:]]
                return tuple(full)
        for nxt in sorted(graph.successor_indices(curr), key=idx_to_id.__getitem__):
            if nxt not in scc_set:
                continue
            next_rem = rem_sccs & node_obs_sccs[idx_to_id[nxt]]
            state = (nxt, next_rem)
            if state not in visited:
                visited.add(state)
                queue.append((nxt, next_rem, (*path, nxt)))
    return None


def _find_cycle_rule_id(
    witness_nodes: tuple[str, ...],
    curated_ir: StructuralGraphIR,
) -> str | None:
    for symbol_id in witness_nodes:
        node = curated_ir.nodes.get(symbol_id)
        if node is not None and node.provenance.rule_id is not None:
            return node.provenance.rule_id
    return None


def _scc_has_common_observed_scc(
    scc_indices: list[int],
    idx_to_id: tuple[str, ...],
    node_obs_sccs: dict[str, frozenset[int]],
) -> bool:
    common: frozenset[int] | None = None
    for idx in scc_indices:
        sccs = node_obs_sccs[idx_to_id[idx]]
        common = sccs if common is None else (common & sccs)
        if not common:
            return False
    return bool(common)


def _check_induced_cycles(
    ctx: _StructuralContext,
    curated_ir: StructuralGraphIR,
    view: DiagramViewSpec,
    rules: tuple[GlobalAbstractionRule, ...],
) -> list[LintDiagnostic]:
    if not view.invariants.forbid_new_cycles:
        return []

    cur_indexed = _build_structural_digraph(curated_ir)
    backing_map = _build_backing_map(ctx.observed_ir, curated_ir, rules)
    node_obs_sccs: dict[str, frozenset[int]] = {
        sym: frozenset(ctx.obs_scc_by_id[b] for b in backing if b in ctx.obs_scc_by_id)
        for sym, backing in backing_map.items()
    }

    sccs = [
        sorted(comp, key=cur_indexed.idx_to_id.__getitem__)
        for comp in rx.strongly_connected_components(cur_indexed.graph)
        if len(comp) >= 2
    ]
    sccs.sort(key=lambda comp: cur_indexed.idx_to_id[comp[0]])

    diagnostics: list[LintDiagnostic] = []
    for scc_sorted in sccs:
        if _scc_has_common_observed_scc(scc_sorted, cur_indexed.idx_to_id, node_obs_sccs):
            continue
        scc_set = set(scc_sorted)
        witness = _extract_edge_induced_cycle(
            scc_sorted, scc_set, cur_indexed, node_obs_sccs
        ) or _extract_walk_induced_cycle(scc_sorted, scc_set, cur_indexed, node_obs_sccs)
        if witness is None:
            continue
        unique_nodes = tuple(dict.fromkeys(witness[:-1]))
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_3_TOPOLOGY_POSET,
                severity=DiagnosticSeverity.ERROR,
                code="INDUCED_PARTITION_CYCLE",
                rule_id=_find_cycle_rule_id(unique_nodes, curated_ir),
                view_id=view.id,
                offending_symbols=unique_nodes,
                witness_path=witness,
                message=(
                    f"Curated view '{view.id}' introduces an induced cycle "
                    f"({' -> '.join(witness)}) not present between its underlying "
                    "observed partitions."
                ),
                repair_hint=(
                    "Split member_globs or add exclude_ids on the collapse_partition rule(s) "
                    "participating in this supernode to break the false architectural cycle."
                ),
            )
        )
    return diagnostics


def _async_backing_edges(
    edge: IREdge,
    obs_edges_by_id: dict[str, IREdge],
) -> tuple[str, ...]:
    candidates: list[str] = []
    if edge.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        candidates.extend(edge.provenance.backing_ids)
    if edge.edge_id in obs_edges_by_id:
        candidates.append(edge.edge_id)
    async_ids = [
        bid
        for bid in dict.fromkeys(candidates)
        if (obs := obs_edges_by_id.get(bid)) is not None
        and obs.sync_kind is SyncKind.ASYNC_FIRE_FORGET
    ]
    return tuple(async_ids)


def _check_structural_async_upgrade(
    ctx: _StructuralContext,
    curated_ir: StructuralGraphIR,
    view: DiagramViewSpec,
) -> list[LintDiagnostic]:
    if not view.invariants.preserve_partial_order:
        return []

    diagnostics: list[LintDiagnostic] = []
    for edge in sorted(curated_ir.edges, key=lambda e: e.edge_id):
        if edge.sync_kind is not SyncKind.SYNC:
            continue
        async_ids = _async_backing_edges(edge, ctx.obs_edges_by_id)
        if not async_ids:
            continue
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_3_TOPOLOGY_POSET,
                severity=DiagnosticSeverity.ERROR,
                code="ASYNC_UPGRADED_TO_SYNC",
                rule_id=edge.provenance.rule_id,
                view_id=view.id,
                offending_symbols=(edge.edge_id, *async_ids),
                witness_path=(edge.source, edge.target),
                message=(
                    f"Curated edge '{edge.edge_id}' ({edge.source} -> {edge.target}) upgraded "
                    f"ASYNC_FIRE_FORGET backing edge(s) {', '.join(async_ids)} to SYNC."
                ),
                repair_hint=(
                    "Preserve SyncKind.ASYNC_FIRE_FORGET when splicing edges across bypassed "
                    "vertices (ensure preserve_concurrency_kind=true)."
                ),
            )
        )
    return diagnostics


def _event_predecessors(event: TraceEvent) -> tuple[str, ...]:
    if event.parent_event_id is None:
        return event.causal_links
    return (event.parent_event_id, *event.causal_links)


def _compute_dag_descendants(adj: list[list[int]], indegree: list[int]) -> tuple[int, ...]:
    queue: deque[int] = deque(idx for idx, deg in enumerate(indegree) if deg == 0)
    topo: list[int] = []
    while queue:
        curr = queue.popleft()
        topo.append(curr)
        for nxt in adj[curr]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    desc = [0] * len(adj)
    for u in reversed(topo):
        mask = 0
        for v in adj[u]:
            mask |= (1 << v) | desc[v]
        desc[u] = mask
    return tuple(desc)


def _build_trace_poset(trace: InteractionTraceIR) -> _TracePoset:
    """Build deterministic causal DAG graph and transitive-closure descendant bitmasks."""
    event_ids = tuple(sorted(trace.events))
    count = len(event_ids)
    id_to_idx = {eid: idx for idx, eid in enumerate(event_ids)}
    graph: rx.PyDiGraph[str, float] = rx.PyDiGraph()
    for eid in event_ids:
        graph.add_node(eid)

    adj: list[list[int]] = [[] for _ in range(count)]
    indegree = [0] * count
    seen_edges: set[tuple[int, int]] = set()

    for eid in event_ids:
        dst = id_to_idx[eid]
        for pred_id in _event_predecessors(trace.events[eid]):
            src = id_to_idx[pred_id]
            if (src, dst) in seen_edges:
                continue
            seen_edges.add((src, dst))
            adj[src].append(dst)
            indegree[dst] += 1
            graph.add_edge(src, dst, 1.0)

    return _TracePoset(
        event_ids=event_ids,
        id_to_idx=id_to_idx,
        desc_mask=_compute_dag_descendants(adj, indegree),
        indexed_graph=_IndexedDiGraph(graph=graph, id_to_idx=id_to_idx, idx_to_id=event_ids),
    )


def _trace_event_backing_ids(
    event: TraceEvent,
    obs_events: Mapping[str, TraceEvent],
) -> tuple[str, ...]:
    candidates: list[str] = []
    if event.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        candidates.extend(event.provenance.backing_ids)
    if event.event_id in obs_events:
        candidates.append(event.event_id)
    return tuple(bid for bid in dict.fromkeys(candidates) if bid in obs_events)


def _check_trace_async_upgrade(
    observed: InteractionTraceIR,
    curated: InteractionTraceIR,
    view_id: str,
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    for eid in sorted(curated.events):
        event = curated.events[eid]
        if event.sync_kind is not SyncKind.SYNC:
            continue
        backing = _trace_event_backing_ids(event, observed.events)
        async_ids = tuple(
            bid for bid in backing if observed.events[bid].sync_kind is SyncKind.ASYNC_FIRE_FORGET
        )
        if not async_ids:
            continue
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_3_TOPOLOGY_POSET,
                severity=DiagnosticSeverity.ERROR,
                code="ASYNC_UPGRADED_TO_SYNC",
                rule_id=event.provenance.rule_id,
                view_id=view_id,
                offending_symbols=(eid, *async_ids),
                witness_path=(eid,),
                message=(
                    f"Curated trace event '{eid}' upgraded ASYNC_FIRE_FORGET backing event(s) "
                    f"{', '.join(async_ids)} to SYNC."
                ),
                repair_hint=(
                    "Preserve SyncKind.ASYNC_FIRE_FORGET on compacted or representative trace "
                    "events backed by asynchronous fire-and-forget spans."
                ),
            )
        )
    return diagnostics


@dataclass(frozen=True, slots=True)
class _TraceOrderContext:
    """Observed vs curated poset plus per-curated-event backing observed event sets."""

    obs_poset: _TracePoset
    cur_poset: _TracePoset
    cur_events: Mapping[str, TraceEvent]
    backing_ids: tuple[tuple[str, ...], ...]
    backing_mask: tuple[int, ...]
    obs_desc_union: tuple[int, ...]


def _build_trace_order_context(
    observed: InteractionTraceIR,
    curated: InteractionTraceIR,
) -> _TraceOrderContext:
    obs_poset = _build_trace_poset(observed)
    cur_poset = _build_trace_poset(curated)
    backing_ids: list[tuple[str, ...]] = []
    backing_mask: list[int] = []
    desc_union: list[int] = []
    for cur_eid in cur_poset.event_ids:
        backing = _trace_event_backing_ids(curated.events[cur_eid], observed.events)
        mask = 0
        union = 0
        for obs_eid in backing:
            o_idx = obs_poset.id_to_idx[obs_eid]
            mask |= 1 << o_idx
            union |= obs_poset.desc_mask[o_idx]
        backing_ids.append(backing)
        backing_mask.append(mask)
        desc_union.append(union)
    return _TraceOrderContext(
        obs_poset=obs_poset,
        cur_poset=cur_poset,
        cur_events=curated.events,
        backing_ids=tuple(backing_ids),
        backing_mask=tuple(backing_mask),
        obs_desc_union=tuple(desc_union),
    )


def _iter_bits(mask: int) -> Iterator[int]:
    while mask:
        bit = mask & -mask
        mask ^= bit
        yield bit.bit_length() - 1


def _find_ordered_backing_pair(
    src_backing_ids: tuple[str, ...],
    dst_backing_mask: int,
    obs_poset: _TracePoset,
) -> tuple[str, str]:
    """Return the first observed pair ``(b_src, b_dst)`` with ``b_src`` before ``b_dst``."""
    for obs_src in src_backing_ids:
        overlap = obs_poset.desc_mask[obs_poset.id_to_idx[obs_src]] & dst_backing_mask
        if overlap:
            return (obs_src, obs_poset.event_ids[(overlap & -overlap).bit_length() - 1])
    return ("<unknown>", "<unknown>")


def _check_reversed_order(
    ctx: _TraceOrderContext,
    view_id: str,
) -> tuple[list[LintDiagnostic], frozenset[tuple[int, int]]]:
    """Check 1: curated ``u`` precedes ``v`` while observed backing of ``v`` precedes ``u``."""
    diagnostics: list[LintDiagnostic] = []
    reversed_pairs: set[tuple[int, int]] = set()
    event_ids = ctx.cur_poset.event_ids
    for earlier_idx, earlier_id in enumerate(event_ids):
        earlier_mask = ctx.backing_mask[earlier_idx]
        if not earlier_mask:
            continue
        for later_idx in _iter_bits(ctx.cur_poset.desc_mask[earlier_idx]):
            if not ctx.obs_desc_union[later_idx] & earlier_mask:
                continue
            reversed_pairs.add((earlier_idx, later_idx))
            later_id = event_ids[later_idx]
            obs_a, obs_b = _find_ordered_backing_pair(
                ctx.backing_ids[later_idx], earlier_mask, ctx.obs_poset
            )
            witness = _shortest_path_ids(ctx.cur_poset.indexed_graph, earlier_id, later_id) or (
                earlier_id,
                later_id,
            )
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_3_TOPOLOGY_POSET,
                    severity=DiagnosticSeverity.ERROR,
                    code="PARTIAL_ORDER_VIOLATED",
                    view_id=view_id,
                    offending_symbols=tuple(dict.fromkeys((earlier_id, later_id, obs_a, obs_b))),
                    witness_path=witness,
                    message=(
                        f"Curated trace reverses observed happens-before order: '{earlier_id}' "
                        f"precedes '{later_id}' in view '{view_id}', whereas observed '{obs_a}' "
                        f"precedes '{obs_b}'."
                    ),
                    repair_hint=(
                        "Preserve observed parent_event_id and causal_links partial-order "
                        "direction during trace compaction; do not invert happens-before edges."
                    ),
                )
            )
    return diagnostics, frozenset(reversed_pairs)


def _direct_curated_edges(ctx: _TraceOrderContext) -> Iterator[tuple[int, int]]:
    """Yield deduplicated direct curated causal edges ``(u_idx, v_idx)`` with ``u != v``."""
    id_to_idx = ctx.cur_poset.id_to_idx
    for v_idx, v_id in enumerate(ctx.cur_poset.event_ids):
        preds = dict.fromkeys(_event_predecessors(ctx.cur_events[v_id]))
        for pred_id in preds:
            u_idx = id_to_idx.get(pred_id)
            if u_idx is not None and u_idx != v_idx:
                yield (u_idx, v_idx)


def _check_invented_order(
    ctx: _TraceOrderContext,
    view_id: str,
    reversed_pairs: frozenset[tuple[int, int]],
) -> list[LintDiagnostic]:
    """Check 2: curated direct edge ``u -> v`` with no observed ``b_u < b_v`` backing pair."""
    diagnostics: list[LintDiagnostic] = []
    event_ids = ctx.cur_poset.event_ids
    for u_idx, v_idx in _direct_curated_edges(ctx):
        u_mask, v_mask = ctx.backing_mask[u_idx], ctx.backing_mask[v_idx]
        if not u_mask or not v_mask or u_mask & v_mask or (u_idx, v_idx) in reversed_pairs:
            continue
        if ctx.obs_desc_union[u_idx] & v_mask:
            continue
        u_id, v_id = event_ids[u_idx], event_ids[v_idx]
        symbols = tuple(
            dict.fromkeys((u_id, v_id, *ctx.backing_ids[u_idx], *ctx.backing_ids[v_idx]))
        )
        diagnostics.append(
            LintDiagnostic(
                gate=GateId.GATE_3_TOPOLOGY_POSET,
                severity=DiagnosticSeverity.ERROR,
                code="PARTIAL_ORDER_FALSE_BARRIER",
                view_id=view_id,
                offending_symbols=symbols,
                witness_path=(u_id, v_id),
                message=(
                    f"Curated trace invents happens-before order '{u_id}' -> '{v_id}' in view "
                    f"'{view_id}': observed backing event(s) "
                    f"{', '.join(ctx.backing_ids[u_idx])} are not ordered before "
                    f"{', '.join(ctx.backing_ids[v_idx])} (concurrent or causally independent)."
                ),
                repair_hint=(
                    "Remove the parent_event_id/causal_links edge that has no observed "
                    "happens-before backing; async fire-and-forget continuations must not "
                    "depend on the spawned child."
                ),
            )
        )
    return diagnostics


def _observed_projection(ctx: _TraceOrderContext) -> tuple[list[int], list[int]]:
    """Project observed order onto distinct curated events.

    Returns ``(succ, pred)`` bitmasks over curated indices: ``succ[u]`` has bit ``v`` iff some
    backing event of ``u`` precedes some backing event of ``v`` in the observed poset, ``u != v``,
    and the two representatives share no backing events (shared backing means the order is
    internal to a contracted group and may be abstracted away).
    """
    count = len(ctx.cur_poset.event_ids)
    succ = [0] * count
    pred = [0] * count
    for u_idx in range(count):
        u_mask = ctx.backing_mask[u_idx]
        if not u_mask:
            continue
        reach = ctx.obs_desc_union[u_idx]
        for v_idx in range(count):
            v_mask = ctx.backing_mask[v_idx]
            if v_idx != u_idx and v_mask & reach and not v_mask & u_mask:
                succ[u_idx] |= 1 << v_idx
                pred[v_idx] |= 1 << u_idx
    return succ, pred


def _check_lost_order(
    ctx: _TraceOrderContext,
    view_id: str,
    reversed_pairs: frozenset[tuple[int, int]],
) -> list[LintDiagnostic]:
    """Check 3: observed order between distinct survivors missing from the curated poset.

    Only minimal missing pairs are reported (no intermediate survivor ``w`` with observed
    ``u < w < v``), so a single severed chain yields one diagnostic instead of O(N^2) noise.
    """
    succ, pred = _observed_projection(ctx)
    diagnostics: list[LintDiagnostic] = []
    event_ids = ctx.cur_poset.event_ids
    for u_idx, u_id in enumerate(event_ids):
        missing = succ[u_idx] & ~ctx.cur_poset.desc_mask[u_idx]
        for v_idx in _iter_bits(missing):
            if (v_idx, u_idx) in reversed_pairs or succ[u_idx] & pred[v_idx]:
                continue
            v_id = event_ids[v_idx]
            b_u, b_v = _find_ordered_backing_pair(
                ctx.backing_ids[u_idx], ctx.backing_mask[v_idx], ctx.obs_poset
            )
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_3_TOPOLOGY_POSET,
                    severity=DiagnosticSeverity.ERROR,
                    code="PARTIAL_ORDER_LOST_EDGE",
                    view_id=view_id,
                    offending_symbols=tuple(dict.fromkeys((u_id, v_id, b_u, b_v))),
                    witness_path=(u_id, v_id),
                    message=(
                        f"Curated trace drops observed happens-before order between surviving "
                        f"events '{u_id}' and '{v_id}' in view '{view_id}': observed '{b_u}' "
                        f"precedes '{b_v}' but '{u_id}' does not precede '{v_id}'."
                    ),
                    repair_hint=(
                        "Retain a parent_event_id or causal_links path from the earlier "
                        "representative to the later one; only order internal to a contracted "
                        "group (loop, recursion cut, depth cut, parallel map) may be abstracted."
                    ),
                )
            )
    return diagnostics


def _check_trace_partial_order(
    observed: InteractionTraceIR,
    curated: InteractionTraceIR,
    view_id: str,
) -> list[LintDiagnostic]:
    """Verify the compaction ordering contract (reversed, invented, and dropped order)."""
    ctx = _build_trace_order_context(observed, curated)
    diagnostics, reversed_pairs = _check_reversed_order(ctx, view_id)
    diagnostics.extend(_check_invented_order(ctx, view_id, reversed_pairs))
    diagnostics.extend(_check_lost_order(ctx, view_id, reversed_pairs))
    return diagnostics


class TopologicalInvariantGate:
    """Gate 3 LintGatePort verifying reachability, fanout, cycles, cohesion, and partial order."""

    def __init__(self, *, strict_cohesion: bool = False) -> None:
        self._strict_cohesion = strict_cohesion

    @property
    def gate_id(self) -> GateId:
        """Return canonical Gate 3 identifier."""
        return GateId.GATE_3_TOPOLOGY_POSET

    def _check_structural(
        self,
        observed_ir: StructuralGraphIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None,
    ) -> list[LintDiagnostic]:
        ctx = _build_structural_context(observed_ir)
        diagnostics = _check_structural_rules(
            ctx,
            spec.global_abstractions,
            view_id=None,
            strict_cohesion=self._strict_cohesion,
        )

        for view in spec.views:
            diagnostics.extend(
                _check_structural_rules(
                    ctx,
                    view.rules,
                    view_id=view.id,
                    strict_cohesion=self._strict_cohesion,
                )
            )
            curated = curated_views.get(view.id) if curated_views is not None else None
            if not isinstance(curated, StructuralGraphIR):
                continue
            combined_rules = (*spec.global_abstractions, *view.rules)
            resolver = _build_curated_resolver(observed_ir, curated, combined_rules)
            diagnostics.extend(_check_post_collapse_bypass(ctx, curated, combined_rules, view.id))
            diagnostics.extend(
                _check_view_reachability(ctx, curated, view, resolver, combined_rules)
            )
            diagnostics.extend(_check_induced_cycles(ctx, curated, view, combined_rules))
            diagnostics.extend(_check_structural_async_upgrade(ctx, curated, view))

        return diagnostics

    def _check_trace(
        self,
        observed_ir: InteractionTraceIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None,
    ) -> list[LintDiagnostic]:
        if curated_views is None:
            return []

        diagnostics: list[LintDiagnostic] = []
        for view in spec.views:
            if not view.invariants.preserve_partial_order:
                continue
            curated = curated_views.get(view.id)
            if not isinstance(curated, InteractionTraceIR):
                continue
            diagnostics.extend(_check_trace_async_upgrade(observed_ir, curated, view.id))
            diagnostics.extend(_check_trace_partial_order(observed_ir, curated, view.id))
        return diagnostics

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport:
        """Run all Gate 3 topological and partial-order invariant checks."""
        if isinstance(observed_ir, StructuralGraphIR):
            diagnostics = self._check_structural(observed_ir, spec, curated_views)
        else:
            diagnostics = self._check_trace(observed_ir, spec, curated_views)
        return LintReport.from_diagnostics(diagnostics)
