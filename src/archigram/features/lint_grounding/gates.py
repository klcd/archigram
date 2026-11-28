"""Gates 1 & 2: Closed-world referential grounding, glob integrity, and boundary/leaf safety."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from difflib import get_close_matches
from typing import TYPE_CHECKING

from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.glob import matches_any_glob, matches_glob
from archigram.domain.ir import (
    EdgeRelation,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    GlobalAbstractionRule,
    KHopNeighborhoodScope,
    PruneLeavesRule,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Literal

    from archigram.domain.ir import CanonicalIR
    from archigram.domain.spec import ReachabilityAssertion

_VALID_RELATIONS: frozenset[str] = frozenset(rel.value for rel in EdgeRelation)
_VALID_RELATIONS_STR: str = ", ".join(sorted(_VALID_RELATIONS))


def collect_observed_symbols(observed_ir: CanonicalIR) -> frozenset[str]:
    """Collect all valid observed symbol/participant identifiers from a CanonicalIR."""
    if isinstance(observed_ir, StructuralGraphIR):
        return frozenset(observed_ir.nodes.keys())
    symbols: set[str] = set()
    for pid, participant in observed_ir.participants.items():
        symbols.add(pid)
        symbols.add(participant.symbol_id)
    return frozenset(symbols)


def match_symbols_for_globs(
    observed_symbols: Iterable[str],
    globs: tuple[str, ...],
    exclude_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return sorted observed symbols matched by any glob in globs and not in exclude_ids."""
    excluded = frozenset(exclude_ids)
    matched = [
        sym for sym in observed_symbols if sym not in excluded and matches_any_glob(sym, globs)
    ]
    matched.sort()
    return tuple(matched)


def _find_best_symbol_match(target: str, pool: list[str]) -> str | None:
    lower_target = target.lower()
    for item in pool:
        if item.lower() == lower_target:
            return item
    for item in pool:
        if item.lower().startswith(lower_target) or lower_target.startswith(item.lower()):
            return item
    close = get_close_matches(target, pool, n=1, cutoff=0.45)
    if close:
        return close[0]
    lower_map = {item.lower(): item for item in pool}
    close_lower = get_close_matches(lower_target, list(lower_map.keys()), n=1, cutoff=0.45)
    return lower_map[close_lower[0]] if close_lower else None


def _suggest_closest_symbol(target: str, candidates: Iterable[str]) -> str:
    pool = sorted(set(candidates))
    if not pool:
        return "Verify the symbol exists in the observed Canonical IR."
    best = _find_best_symbol_match(target, pool)
    if best is not None:
        if best.lower() == target.lower():
            return f"Did you mean '{best}' (exact case mismatch)?"
        return f"Did you mean '{best}'?"
    sample = ", ".join(f"'{s}'" for s in pool[:3])
    return f"Available observed symbols include: {sample}."


def _suggest_for_glob(pattern: str, candidates: Iterable[str]) -> str:
    stem = pattern.replace("*", "").replace("?", "").strip(".:/_-")
    if stem:
        hint = _suggest_closest_symbol(stem, candidates)
        return (
            f"Glob '{pattern}' matched 0 observed symbols. {hint} "
            "Broaden the glob or remove the rule."
        )
    return f"Glob '{pattern}' matched 0 observed symbols. Broaden the glob or remove the rule."


@dataclass(frozen=True, slots=True)
class _RuleContext:
    observed_symbols: frozenset[str]
    target_node_ids: frozenset[str]
    view_id: str | None


def _check_relation_names(
    relations: tuple[str, ...],
    rule_id: str | None,
    view_id: str | None,
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    for rel in relations:
        if rel not in _VALID_RELATIONS:
            close = get_close_matches(rel.lower(), sorted(_VALID_RELATIONS), n=1, cutoff=0.4)
            suggestion = f"Did you mean '{close[0]}'?" if close else ""
            hint = f"{suggestion} Valid EdgeRelation values: {_VALID_RELATIONS_STR}.".strip()
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_1_GROUNDING,
                    severity=DiagnosticSeverity.ERROR,
                    code="INVALID_EDGE_RELATION",
                    message=f"Unknown EdgeRelation '{rel}' referenced in spec.",
                    rule_id=rule_id,
                    view_id=view_id,
                    offending_symbols=(rel,),
                    repair_hint=hint,
                )
            )
    return diagnostics


def _check_glob_list(
    globs: tuple[str, ...],
    rule_id: str,
    ctx: _RuleContext,
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    for pat in globs:
        has_match = any(matches_glob(sym, pat) for sym in ctx.observed_symbols)
        if not has_match:
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_1_GROUNDING,
                    severity=DiagnosticSeverity.ERROR,
                    code="ZERO_MATCH_GLOB",
                    message=(
                        f"Glob pattern '{pat}' in rule '{rule_id}' matched 0 symbols "
                        "in the observed Canonical IR."
                    ),
                    rule_id=rule_id,
                    view_id=ctx.view_id,
                    offending_symbols=(pat,),
                    repair_hint=_suggest_for_glob(pat, ctx.observed_symbols),
                )
            )
    return diagnostics


def _check_rule_grounding(
    rule: GlobalAbstractionRule,
    ctx: _RuleContext,
) -> list[LintDiagnostic]:
    if isinstance(rule, PruneLeavesRule):
        return _check_glob_list(rule.node_id_globs, rule.id, ctx)
    if isinstance(rule, BypassVerticesRule):
        diags = _check_glob_list(rule.node_id_globs, rule.id, ctx)
        diags.extend(_check_relation_names(rule.preserve_edge_relations, rule.id, ctx.view_id))
        return diags
    diags = _check_glob_list(rule.member_globs, rule.id, ctx)
    for exc_id in rule.exclude_ids:
        if exc_id not in ctx.observed_symbols:
            hint = _suggest_closest_symbol(exc_id, ctx.observed_symbols)
            diags.append(
                LintDiagnostic(
                    gate=GateId.GATE_1_GROUNDING,
                    severity=DiagnosticSeverity.ERROR,
                    code="UNKNOWN_SYMBOL_ID",
                    message=(
                        f"exclude_ids entry '{exc_id}' in rule '{rule.id}' does not exist "
                        "in the observed Canonical IR."
                    ),
                    rule_id=rule.id,
                    view_id=ctx.view_id,
                    offending_symbols=(exc_id,),
                    repair_hint=f"{hint} Remove '{exc_id}' from exclude_ids or fix the typo.",
                )
            )
    return diags


def _check_symbol_ref(
    symbol_id: str,
    field_label: str,
    allow_partition_targets: bool,
    ctx: _RuleContext,
) -> LintDiagnostic | None:
    if symbol_id in ctx.observed_symbols:
        return None
    if allow_partition_targets and symbol_id in ctx.target_node_ids:
        return None
    pool = (
        ctx.observed_symbols | ctx.target_node_ids
        if allow_partition_targets
        else ctx.observed_symbols
    )
    hint = _suggest_closest_symbol(symbol_id, pool)
    return LintDiagnostic(
        gate=GateId.GATE_1_GROUNDING,
        severity=DiagnosticSeverity.ERROR,
        code="UNKNOWN_SYMBOL_ID",
        message=(
            f"Referenced symbol '{symbol_id}' in {field_label} does not exist "
            "in the observed Canonical IR."
        ),
        view_id=ctx.view_id,
        offending_symbols=(symbol_id,),
        repair_hint=f"{hint} Replace '{symbol_id}' with a valid observed symbol_id.",
    )


def _collect_partition_targets(
    global_rules: tuple[GlobalAbstractionRule, ...],
    view_rules: tuple[GlobalAbstractionRule, ...] = (),
) -> frozenset[str]:
    return frozenset(
        rule.target_node_id
        for rule in (*global_rules, *view_rules)
        if isinstance(rule, CollapsePartitionRule)
    )


def _check_view_grounding(
    view: DiagramViewSpec,
    global_rules: tuple[GlobalAbstractionRule, ...],
    observed_symbols: frozenset[str],
) -> list[LintDiagnostic]:
    targets = _collect_partition_targets(global_rules, view.rules)
    ctx = _RuleContext(
        observed_symbols=observed_symbols,
        target_node_ids=targets,
        view_id=view.id,
    )
    diagnostics: list[LintDiagnostic] = []
    for rule in view.rules:
        diagnostics.extend(_check_rule_grounding(rule, ctx))

    if isinstance(view.scope, KHopNeighborhoodScope):
        for focal in view.scope.focal_nodes:
            diag = _check_symbol_ref(focal, "scope.focal_nodes", True, ctx)
            if diag is not None:
                diagnostics.append(diag)

    for prot in view.invariants.protected_nodes:
        diag = _check_symbol_ref(prot, "invariants.protected_nodes", True, ctx)
        if diag is not None:
            diagnostics.append(diag)

    diagnostics.extend(
        _check_reachability_grounding(view.invariants.preserve_reachability, "preserve", ctx)
    )
    diagnostics.extend(
        _check_reachability_grounding(view.invariants.require_reachability, "require", ctx)
    )

    return diagnostics


def _check_reachability_grounding(
    assertions: tuple[ReachabilityAssertion, ...],
    kind: Literal["preserve", "require"],
    ctx: _RuleContext,
) -> list[LintDiagnostic]:
    """Ground endpoints and relation filters of preserve/require reachability assertions."""
    diagnostics: list[LintDiagnostic] = []
    field = f"{kind}_reachability"
    for reach in assertions:
        d_from = _check_symbol_ref(reach.from_node, f"{field}.from_node", True, ctx)
        if d_from is not None:
            diagnostics.append(d_from)
        d_to = _check_symbol_ref(reach.to_node, f"{field}.to_node", True, ctx)
        if d_to is not None:
            diagnostics.append(d_to)
        diagnostics.extend(_check_relation_names(reach.via_relations, None, ctx.view_id))
    return diagnostics


def run_gate_1_grounding(
    observed_ir: CanonicalIR,
    spec: CurationSpecV1,
) -> list[LintDiagnostic]:
    """Execute Gate 1 (Closed-World Referential & Glob Integrity) checks."""
    observed_symbols = collect_observed_symbols(observed_ir)
    global_targets = _collect_partition_targets(spec.global_abstractions)
    global_ctx = _RuleContext(
        observed_symbols=observed_symbols,
        target_node_ids=global_targets,
        view_id=None,
    )
    diagnostics: list[LintDiagnostic] = []
    for rule in spec.global_abstractions:
        diagnostics.extend(_check_rule_grounding(rule, global_ctx))
    for view in spec.views:
        diagnostics.extend(_check_view_grounding(view, spec.global_abstractions, observed_symbols))
    return diagnostics


def _is_prune_bridge_violation(
    direction: str,
    in_deg: int,
    out_deg: int,
) -> bool:
    if direction == "sink":
        return out_deg > 0
    if direction == "root":
        return in_deg > 0
    return in_deg > 0 and out_deg > 0


def _compute_non_pruned_degrees(
    ir: StructuralGraphIR,
    pruned_set: frozenset[str],
    active_nodes: frozenset[str] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    allowed = active_nodes if active_nodes is not None else frozenset(ir.nodes.keys())
    in_deg: dict[str, int] = dict.fromkeys(allowed, 0)
    out_deg: dict[str, int] = dict.fromkeys(allowed, 0)
    for edge in ir.edges:
        if edge.source == edge.target:
            continue
        if edge.source not in allowed or edge.target not in allowed:
            continue
        if edge.target not in pruned_set:
            out_deg[edge.source] += 1
        if edge.source not in pruned_set:
            in_deg[edge.target] += 1
    return in_deg, out_deg


def _check_prune_leaves_rule(
    rule: PruneLeavesRule,
    ir: StructuralGraphIR,
    view_id: str | None,
    active_nodes: frozenset[str] | None = None,
) -> tuple[list[LintDiagnostic], frozenset[str]]:
    candidate_pool = active_nodes if active_nodes is not None else frozenset(ir.nodes.keys())
    matched = match_symbols_for_globs(candidate_pool, rule.node_id_globs)
    matched_set = frozenset(matched)
    in_deg, out_deg = _compute_non_pruned_degrees(ir, matched_set, candidate_pool)
    diagnostics: list[LintDiagnostic] = []
    valid_pruned: set[str] = set()

    for sym in matched:
        u_in = in_deg.get(sym, 0)
        u_out = out_deg.get(sym, 0)
        if _is_prune_bridge_violation(rule.direction, u_in, u_out):
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_2_BOUNDARY_LEAF,
                    severity=DiagnosticSeverity.ERROR,
                    code="PRUNE_INTERNAL_BRIDGE",
                    message=(
                        f"prune_leaves rule '{rule.id}' (direction='{rule.direction}') targets "
                        f"non-leaf node '{sym}' (in_degree={u_in}, out_degree={u_out})."
                    ),
                    rule_id=rule.id,
                    view_id=view_id,
                    offending_symbols=(sym,),
                    repair_hint=(
                        "Use 'bypass_vertices' instead of 'prune_leaves' to splice through "
                        f"intermediate bridge node '{sym}', or exclude '{sym}' from '{rule.id}'."
                    ),
                )
            )
        else:
            valid_pruned.add(sym)
    return diagnostics, frozenset(valid_pruned)


def _compute_khop_visible(
    nodes: frozenset[str],
    edges: list[tuple[str, str]],
    scope: KHopNeighborhoodScope,
) -> frozenset[str]:
    seeds = [f for f in scope.focal_nodes if f in nodes]
    if not seeds:
        return frozenset()
    fwd: dict[str, list[str]] = {n: [] for n in nodes}
    rev: dict[str, list[str]] = {n: [] for n in nodes}
    for u, v in edges:
        if u in nodes and v in nodes:
            fwd[u].append(v)
            rev[v].append(u)

    visible: set[str] = set(seeds)
    visible.update(_bfs_within_depth(seeds, fwd, scope.downstream_depth))
    visible.update(_bfs_within_depth(seeds, rev, scope.upstream_depth))
    return frozenset(visible)


def _bfs_within_depth(
    seeds: list[str],
    adj: dict[str, list[str]],
    max_depth: int,
) -> set[str]:
    visited: set[str] = set(seeds)
    if max_depth <= 0:
        return visited
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    while queue:
        curr, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for nxt in adj.get(curr, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, depth + 1))
    return visited


@dataclass(frozen=True, slots=True)
class _ProtectedNodeSimulation:
    visible_nodes: frozenset[str]
    dropped_by_rule: dict[str, tuple[str, str]]
    collapsed_into: dict[str, tuple[str, str]]


def _apply_rule_to_active_set(
    rule: GlobalAbstractionRule,
    active_nodes: set[str],
    dropped_by_rule: dict[str, tuple[str, str]],
    collapsed_into: dict[str, tuple[str, str]],
) -> None:
    if isinstance(rule, (PruneLeavesRule, BypassVerticesRule)):
        for sym in match_symbols_for_globs(active_nodes, rule.node_id_globs):
            dropped_by_rule.setdefault(sym, (rule.id, rule.op))
            active_nodes.discard(sym)
        return
    members = match_symbols_for_globs(active_nodes, rule.member_globs, rule.exclude_ids)
    if members:
        for sym in members:
            collapsed_into[sym] = (rule.target_node_id, rule.id)
            if sym != rule.target_node_id:
                active_nodes.discard(sym)
        active_nodes.add(rule.target_node_id)


def _expand_containment_ancestors(
    visible: set[str],
    ir: StructuralGraphIR,
    collapsed_into: dict[str, tuple[str, str]],
    active_nodes: set[str],
) -> set[str]:
    expanded = set(visible)
    for sym in list(visible):
        node = ir.nodes.get(sym)
        cursor = node.parent_id if node is not None else None
        visited: set[str] = set()
        while cursor is not None and cursor not in visited:
            visited.add(cursor)
            resolved = _resolve_collapse_target(cursor, collapsed_into)
            rep = resolved[0] if resolved is not None else cursor
            if rep in active_nodes:
                expanded.add(rep)
            parent_node = ir.nodes.get(cursor)
            cursor = parent_node.parent_id if parent_node is not None else None
    return expanded


def _simulate_view_survival(
    observed_ir: CanonicalIR,
    view: DiagramViewSpec,
    global_rules: tuple[GlobalAbstractionRule, ...],
) -> _ProtectedNodeSimulation:
    active_nodes = set(collect_observed_symbols(observed_ir))
    dropped_by_rule: dict[str, tuple[str, str]] = {}
    collapsed_into: dict[str, tuple[str, str]] = {}

    for rule in global_rules:
        _apply_rule_to_active_set(rule, active_nodes, dropped_by_rule, collapsed_into)

    if isinstance(view.scope, KHopNeighborhoodScope) and isinstance(observed_ir, StructuralGraphIR):
        view_targets = _collect_partition_targets((), view.rules)
        uses_view_target_focal = any(focal in view_targets for focal in view.scope.focal_nodes)
        if not uses_view_target_focal and any(
            focal in active_nodes for focal in view.scope.focal_nodes
        ):
            proj_edges = _project_edges(observed_ir, collapsed_into, active_nodes)
            scoped = _compute_khop_visible(frozenset(active_nodes), proj_edges, view.scope)
            active_nodes = _expand_containment_ancestors(
                set(scoped), observed_ir, collapsed_into, active_nodes
            )
            for rule in view.rules:
                _apply_rule_to_active_set(rule, active_nodes, dropped_by_rule, collapsed_into)
            visible = frozenset(active_nodes)
        else:
            for rule in view.rules:
                _apply_rule_to_active_set(rule, active_nodes, dropped_by_rule, collapsed_into)
            resolved_focals = tuple(
                (_resolve_collapse_target(f, collapsed_into) or (f, ""))[0]
                for f in view.scope.focal_nodes
            )
            resolved_scope = view.scope.model_copy(update={"focal_nodes": resolved_focals})
            proj_edges = _project_edges(observed_ir, collapsed_into, active_nodes)
            visible = _compute_khop_visible(frozenset(active_nodes), proj_edges, resolved_scope)
    else:
        for rule in view.rules:
            _apply_rule_to_active_set(rule, active_nodes, dropped_by_rule, collapsed_into)
        visible = frozenset(active_nodes)

    return _ProtectedNodeSimulation(
        visible_nodes=visible,
        dropped_by_rule=dropped_by_rule,
        collapsed_into=collapsed_into,
    )


def _project_edges(
    ir: StructuralGraphIR,
    collapsed_into: dict[str, tuple[str, str]],
    active_nodes: set[str],
) -> list[tuple[str, str]]:
    def _resolve(sym: str) -> str:
        curr = sym
        visited: set[str] = set()
        while curr in collapsed_into and curr not in visited:
            visited.add(curr)
            curr = collapsed_into[curr][0]
        return curr

    edges: list[tuple[str, str]] = []
    for edge in ir.edges:
        u = _resolve(edge.source)
        v = _resolve(edge.target)
        if u in active_nodes and v in active_nodes and u != v:
            edges.append((u, v))
    return edges


def _resolve_collapse_target(
    sym: str,
    collapsed_into: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    if sym not in collapsed_into:
        return None
    curr, last_rule = collapsed_into[sym]
    visited: set[str] = {sym}
    while curr in collapsed_into and curr not in visited:
        visited.add(curr)
        curr, last_rule = collapsed_into[curr]
    return curr, last_rule


def _check_single_protected_node(
    prot: str,
    view: DiagramViewSpec,
    sim: _ProtectedNodeSimulation,
    curated_symbols: frozenset[str] | None,
) -> LintDiagnostic | None:
    if prot in sim.dropped_by_rule:
        rule_id, op_name = sim.dropped_by_rule[prot]
        return LintDiagnostic(
            gate=GateId.GATE_2_BOUNDARY_LEAF,
            severity=DiagnosticSeverity.ERROR,
            code="PROTECTED_NODE_DROPPED",
            message=(
                f"Protected node '{prot}' in view '{view.id}' was eliminated by "
                f"{op_name} rule '{rule_id}'."
            ),
            rule_id=rule_id,
            view_id=view.id,
            offending_symbols=(prot,),
            repair_hint=(
                f"Exclude '{prot}' from {op_name} rule '{rule_id}' or fold it into a "
                "visible 'collapse_partition' supernode."
            ),
        )

    collapse_info = _resolve_collapse_target(prot, sim.collapsed_into)
    surviving_id = collapse_info[0] if collapse_info is not None else prot
    collapse_rule_id = collapse_info[1] if collapse_info is not None else None

    if surviving_id in sim.dropped_by_rule:
        drop_rule_id, op_name = sim.dropped_by_rule[surviving_id]
        return LintDiagnostic(
            gate=GateId.GATE_2_BOUNDARY_LEAF,
            severity=DiagnosticSeverity.ERROR,
            code="PROTECTED_NODE_DROPPED",
            message=(
                f"Protected node '{prot}' (mapped to '{surviving_id}') in view '{view.id}' "
                f"was eliminated by {op_name} rule '{drop_rule_id}'."
            ),
            rule_id=drop_rule_id,
            view_id=view.id,
            offending_symbols=(prot,),
            repair_hint=(
                f"Prevent '{surviving_id}' from being dropped by {op_name} rule '{drop_rule_id}'."
            ),
        )

    in_sim = surviving_id in sim.visible_nodes
    in_curated = (
        True
        if curated_symbols is None
        else (prot in curated_symbols or surviving_id in curated_symbols)
    )
    if in_sim and in_curated:
        return None

    return LintDiagnostic(
        gate=GateId.GATE_2_BOUNDARY_LEAF,
        severity=DiagnosticSeverity.ERROR,
        code="PROTECTED_NODE_DROPPED",
        message=(
            f"Protected node '{prot}' does not survive in curated view '{view.id}' "
            "(dropped outside k_hop_neighborhood scope or missing from curated view)."
        ),
        rule_id=collapse_rule_id or f"scope:{view.id}",
        view_id=view.id,
        offending_symbols=(prot,),
        repair_hint=(
            f"Increase k_hop_neighborhood depth, add '{prot}' to focal_nodes, "
            "or fold it into a visible collapse_partition supernode."
        ),
    )


def _check_structural_prune_rules(
    observed_ir: StructuralGraphIR,
    global_rules: tuple[GlobalAbstractionRule, ...],
    views: tuple[DiagramViewSpec, ...],
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    global_active = set(observed_ir.nodes.keys())
    for rule in global_rules:
        if isinstance(rule, PruneLeavesRule):
            rule_diags, valid_pruned = _check_prune_leaves_rule(
                rule, observed_ir, None, frozenset(global_active)
            )
            diagnostics.extend(rule_diags)
            global_active -= valid_pruned

    for view in views:
        view_active = set(global_active)
        for v_rule in view.rules:
            if isinstance(v_rule, PruneLeavesRule):
                v_diags, v_pruned = _check_prune_leaves_rule(
                    v_rule, observed_ir, view.id, frozenset(view_active)
                )
                diagnostics.extend(v_diags)
                view_active -= v_pruned
    return diagnostics


def run_gate_2_boundary_leaf(
    observed_ir: CanonicalIR,
    spec: CurationSpecV1,
    curated_views: Mapping[str, CanonicalIR] | None = None,
) -> list[LintDiagnostic]:
    """Execute Gate 2 (Leaf-Only Pruning & Protected Boundary Survival) checks."""
    diagnostics: list[LintDiagnostic] = []
    observed_symbols = collect_observed_symbols(observed_ir)

    if isinstance(observed_ir, StructuralGraphIR):
        diagnostics.extend(
            _check_structural_prune_rules(observed_ir, spec.global_abstractions, spec.views)
        )

    for view in spec.views:
        sim = _simulate_view_survival(observed_ir, view, spec.global_abstractions)
        curated_ir = curated_views.get(view.id) if curated_views is not None else None
        curated_symbols = collect_observed_symbols(curated_ir) if curated_ir is not None else None
        known_targets = _collect_partition_targets(spec.global_abstractions, view.rules)
        for prot in view.invariants.protected_nodes:
            if prot not in observed_symbols and prot not in known_targets:
                continue
            diag = _check_single_protected_node(prot, view, sim, curated_symbols)
            if diag is not None:
                diagnostics.append(diag)

    return diagnostics


class Gate1GroundingGate:
    """LintGatePort implementation for Gate 1: Closed-World Referential & Glob Integrity."""

    @property
    def gate_id(self) -> GateId:
        return GateId.GATE_1_GROUNDING

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport:
        _ = curated_views
        return LintReport.from_diagnostics(run_gate_1_grounding(observed_ir, spec))


class Gate2BoundaryLeafGate:
    """LintGatePort implementation for Gate 2: Leaf-Only Pruning & Protected Boundary Survival."""

    @property
    def gate_id(self) -> GateId:
        return GateId.GATE_2_BOUNDARY_LEAF

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport:
        return LintReport.from_diagnostics(
            run_gate_2_boundary_leaf(observed_ir, spec, curated_views)
        )


class GroundingAndBoundaryGate:
    """Combined Gate 1 & Gate 2 LintGatePort validating grounding, globs, and boundary safety."""

    def __init__(self, primary_gate_id: GateId = GateId.GATE_1_GROUNDING) -> None:
        self._gate_id = primary_gate_id

    @property
    def gate_id(self) -> GateId:
        return self._gate_id

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport:
        diagnostics = [
            *run_gate_1_grounding(observed_ir, spec),
            *run_gate_2_boundary_leaf(observed_ir, spec, curated_views),
        ]
        return LintReport.from_diagnostics(diagnostics)
