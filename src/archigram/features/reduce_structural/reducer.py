"""Deterministic rustworkx-backed structural topological reduction operators."""

from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict, deque
from typing import TYPE_CHECKING

import rustworkx as rx

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    DiagramViewSpec,
    GlobalAbstractionRule,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

type _EdgeGroupKey = tuple[str, str, EdgeRelation, SyncKind]


class CycleInducedError(rx.DAGWouldCycle):
    """Raised when partition contraction introduces a directed cycle in the quotient graph."""

    def __init__(
        self,
        message: str,
        *,
        rule_id: str,
        target_node_id: str,
        member_ids: tuple[str, ...],
        witness_path: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.target_node_id = target_node_id
        self.member_ids = member_ids
        self.witness_path = witness_path


def _matches_any_glob(symbol_id: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(symbol_id, pattern) for pattern in patterns)


def _build_rx_graph(
    ir: StructuralGraphIR,
) -> tuple[rx.PyDiGraph[IRNode, IREdge], dict[str, int]]:
    graph: rx.PyDiGraph[IRNode, IREdge] = rx.PyDiGraph(multigraph=True)
    idx_of: dict[str, int] = {}
    for symbol_id in sorted(ir.nodes):
        idx_of[symbol_id] = graph.add_node(ir.nodes[symbol_id])
    for edge in ir.edges:
        graph.add_edge(idx_of[edge.source], idx_of[edge.target], edge)
    return graph, idx_of


def _sorted_unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(items)))


def _node_backing_ids(node: IRNode) -> tuple[str, ...]:
    return (node.symbol_id, *node.provenance.backing_ids)


def _edge_backing_ids(edge: IREdge) -> tuple[str, ...]:
    return (edge.edge_id, *edge.provenance.backing_ids)


def _stable_derived_edge_id(
    prefix: str,
    rule_id: str,
    key: _EdgeGroupKey,
    used_ids: set[str],
) -> str:
    source, target, relation, sync_kind = key
    payload = f"{prefix}|{rule_id}|{source}|{target}|{relation.value}|{sync_kind.value}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    candidate = f"{prefix}:{rule_id}:{digest}"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    suffix = 1
    while f"{candidate}:{suffix}" in used_ids:
        suffix += 1
    unique_id = f"{candidate}:{suffix}"
    used_ids.add(unique_id)
    return unique_id


def _merge_edge_attributes(edges: Sequence[IREdge]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for edge in sorted(edges, key=lambda item: item.edge_id):
        merged.update(edge.attributes)
    return merged


def _shared_parallel_region(edges: Sequence[IREdge]) -> str | None:
    regions = {edge.parallel_region_id for edge in edges}
    if len(regions) == 1:
        return next(iter(regions))
    return None


def _reparent_to_surviving_ancestors(
    surviving: Mapping[str, IRNode],
    original: Mapping[str, IRNode],
) -> dict[str, IRNode]:
    reparented: dict[str, IRNode] = {}
    for symbol_id in sorted(surviving):
        node = surviving[symbol_id]
        ancestor = node.parent_id
        while ancestor is not None and ancestor not in surviving:
            parent_node = original.get(ancestor)
            ancestor = parent_node.parent_id if parent_node is not None else None
        if ancestor == node.parent_id:
            reparented[symbol_id] = node
        else:
            reparented[symbol_id] = node.model_copy(update={"parent_id": ancestor})
    return reparented


def _nearest_external_rep(
    symbol_id: str,
    own_rep: str,
    original: Mapping[str, IRNode],
    node_to_rep: Mapping[str, str],
) -> str | None:
    cursor = original[symbol_id].parent_id
    while cursor is not None:
        rep = node_to_rep.get(cursor)
        if rep is not None and rep != own_rep:
            return rep
        parent_node = original.get(cursor)
        cursor = parent_node.parent_id if parent_node is not None else None
    return None


def _would_close_parent_cycle(
    node_id: str,
    candidate_parent: str | None,
    accepted_parents: Mapping[str, str | None],
) -> bool:
    cursor = candidate_parent
    while cursor is not None:
        if cursor == node_id:
            return True
        cursor = accepted_parents.get(cursor)
    return False


def _resolve_contraction_parents(
    original: Mapping[str, IRNode],
    node_to_rep: Mapping[str, str],
    contracted_groups: Mapping[str, list[str]],
    base_nodes: Mapping[str, IRNode],
) -> dict[str, IRNode]:
    candidates: dict[str, str | None] = {}
    for rep_id in sorted(base_nodes):
        if rep_id in contracted_groups:
            ext_parents = {
                _nearest_external_rep(member, rep_id, original, node_to_rep)
                for member in contracted_groups[rep_id]
            }
            candidates[rep_id] = next(iter(ext_parents)) if len(ext_parents) == 1 else None
        else:
            candidates[rep_id] = _nearest_external_rep(rep_id, rep_id, original, node_to_rep)

    accepted: dict[str, str | None] = {}
    resolved: dict[str, IRNode] = {}
    for rep_id in sorted(base_nodes):
        cand = candidates[rep_id]
        if cand is not None and (
            cand not in base_nodes or _would_close_parent_cycle(rep_id, cand, accepted)
        ):
            cand = None
        accepted[rep_id] = cand
        node = base_nodes[rep_id]
        resolved[rep_id] = (
            node if node.parent_id == cand else node.model_copy(update={"parent_id": cand})
        )
    return resolved


def _bfs_hops(
    graph: rx.PyDiGraph[IRNode, IREdge],
    seeds: Sequence[int],
    max_depth: int,
    *,
    upstream: bool,
) -> set[int]:
    visited: set[int] = set(seeds)
    if max_depth <= 0 or not seeds:
        return visited
    queue: deque[tuple[int, int]] = deque((idx, 0) for idx in seeds)
    while queue:
        curr, depth = queue.popleft()
        if depth >= max_depth:
            continue
        neighbors = graph.predecessor_indices(curr) if upstream else graph.successor_indices(curr)
        for nxt in neighbors:
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, depth + 1))
    return visited


def _collect_containment_ancestors(
    kept_ids: set[str],
    nodes: Mapping[str, IRNode],
) -> set[str]:
    expanded = set(kept_ids)
    for symbol_id in list(kept_ids):
        cursor = nodes[symbol_id].parent_id
        while cursor is not None and cursor not in expanded:
            expanded.add(cursor)
            cursor = nodes[cursor].parent_id
    return expanded


def k_hop_neighborhood(
    ir: StructuralGraphIR,
    scope: KHopNeighborhoodScope,
    *,
    retain_ancestors: bool = True,
) -> StructuralGraphIR:
    """Slice StructuralGraphIR to k-hop upstream/downstream neighborhood around focal nodes."""
    graph, idx_of = _build_rx_graph(ir)
    focal_indices = [idx_of[nid] for nid in scope.focal_nodes if nid in idx_of]
    if not focal_indices:
        return StructuralGraphIR(graph_id=ir.graph_id, metadata=dict(ir.metadata))

    upstream = _bfs_hops(graph, focal_indices, scope.upstream_depth, upstream=True)
    downstream = _bfs_hops(graph, focal_indices, scope.downstream_depth, upstream=False)
    kept_ids = {graph[idx].symbol_id for idx in (upstream | downstream)}

    if retain_ancestors:
        kept_ids = _collect_containment_ancestors(kept_ids, ir.nodes)
        kept_nodes = {nid: ir.nodes[nid] for nid in sorted(kept_ids)}
    else:
        raw_nodes = {nid: ir.nodes[nid] for nid in sorted(kept_ids)}
        kept_nodes = _reparent_to_surviving_ancestors(raw_nodes, ir.nodes)

    kept_edges = tuple(
        edge for edge in ir.edges if edge.source in kept_nodes and edge.target in kept_nodes
    )
    return StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=kept_nodes,
        edges=kept_edges,
        metadata=dict(ir.metadata),
    )


def _is_prunable_leaf(
    graph: rx.PyDiGraph[IRNode, IREdge],
    idx: int,
    direction: str,
) -> bool:
    if direction == "sink":
        return graph.out_degree(idx) == 0
    if direction == "root":
        return graph.in_degree(idx) == 0
    return graph.out_degree(idx) == 0 or graph.in_degree(idx) == 0


def _prune_pass(
    graph: rx.PyDiGraph[IRNode, IREdge],
    idx_of: dict[str, int],
    surviving: dict[str, IRNode],
    rule: PruneLeavesRule,
) -> list[str]:
    active_parents = {node.parent_id for node in surviving.values() if node.parent_id is not None}
    to_remove = [
        nid
        for nid in sorted(surviving)
        if nid not in active_parents
        and _matches_any_glob(nid, rule.node_id_globs)
        and _is_prunable_leaf(graph, idx_of[nid], rule.direction)
    ]
    for nid in to_remove:
        graph.remove_node(idx_of.pop(nid))
        del surviving[nid]
    return to_remove


def prune_leaves(
    ir: StructuralGraphIR,
    rule: PruneLeavesRule,
    *,
    iterative: bool = True,
) -> StructuralGraphIR:
    """Remove matching degree-0 sink/root leaf nodes that have no surviving containment children."""
    graph, idx_of = _build_rx_graph(ir)
    surviving = dict(ir.nodes)

    while True:
        removed = _prune_pass(graph, idx_of, surviving, rule)
        if not removed or not iterative:
            break

    kept_nodes = {nid: surviving[nid] for nid in sorted(surviving)}
    kept_edges = tuple(
        edge for edge in ir.edges if edge.source in kept_nodes and edge.target in kept_nodes
    )
    return StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=kept_nodes,
        edges=kept_edges,
        metadata=dict(ir.metadata),
    )


def _resolve_spliced_sync_kind(
    in_kind: SyncKind,
    out_kind: SyncKind,
    *,
    preserve_concurrency_kind: bool,
) -> SyncKind:
    if in_kind is SyncKind.ASYNC_FIRE_FORGET or out_kind is SyncKind.ASYNC_FIRE_FORGET:
        return SyncKind.ASYNC_FIRE_FORGET
    if preserve_concurrency_kind and (
        in_kind is SyncKind.FORK_JOIN_BRANCH or out_kind is SyncKind.FORK_JOIN_BRANCH
    ):
        return SyncKind.FORK_JOIN_BRANCH
    return SyncKind.SYNC


def _edge_relation_allowed(edge: IREdge, preserve_set: frozenset[str]) -> bool:
    return not preserve_set or edge.relation.value in preserve_set


def _build_spliced_groups(
    bypassed_node: IRNode,
    in_edges: Sequence[tuple[int, int, IREdge]],
    out_edges: Sequence[tuple[int, int, IREdge]],
    rule: BypassVerticesRule,
    preserve_set: frozenset[str],
) -> dict[tuple[int, int, EdgeRelation, SyncKind], list[tuple[float, tuple[str, ...], str | None]]]:
    groups: dict[
        tuple[int, int, EdgeRelation, SyncKind],
        list[tuple[float, tuple[str, ...], str | None]],
    ] = defaultdict(list)
    node_backing = _node_backing_ids(bypassed_node)

    for src_idx, _, in_edge in in_edges:
        if not _edge_relation_allowed(in_edge, preserve_set):
            continue
        for _, dst_idx, out_edge in out_edges:
            if src_idx == dst_idx or not _edge_relation_allowed(out_edge, preserve_set):
                continue
            relation = (
                in_edge.relation if in_edge.relation is out_edge.relation else out_edge.relation
            )
            sync_kind = _resolve_spliced_sync_kind(
                in_edge.sync_kind,
                out_edge.sync_kind,
                preserve_concurrency_kind=rule.preserve_concurrency_kind,
            )
            backing = (*_edge_backing_ids(in_edge), *node_backing, *_edge_backing_ids(out_edge))
            region = (
                in_edge.parallel_region_id
                if in_edge.parallel_region_id == out_edge.parallel_region_id
                else None
            )
            weight = min(in_edge.weight, out_edge.weight)
            groups[(src_idx, dst_idx, relation, sync_kind)].append((weight, backing, region))
    return groups


def _find_matching_edge_index(
    graph: rx.PyDiGraph[IRNode, IREdge],
    src_idx: int,
    dst_idx: int,
    relation: EdgeRelation,
    sync_kind: SyncKind,
) -> int | None:
    for edge_idx in graph.edge_indices_from_endpoints(src_idx, dst_idx):
        existing = graph.get_edge_data_by_index(edge_idx)
        if existing.relation is relation and existing.sync_kind is sync_kind:
            return edge_idx
    return None


def _upsert_spliced_edges(
    graph: rx.PyDiGraph[IRNode, IREdge],
    groups: Mapping[
        tuple[int, int, EdgeRelation, SyncKind],
        list[tuple[float, tuple[str, ...], str | None]],
    ],
    rule_id: str,
    used_edge_ids: set[str],
) -> None:
    for (src_idx, dst_idx, relation, sync_kind), entries in sorted(
        groups.items(),
        key=lambda item: (
            graph[item[0][0]].symbol_id,
            graph[item[0][1]].symbol_id,
            item[0][2].value,
            item[0][3].value,
        ),
    ):
        source_id = graph[src_idx].symbol_id
        target_id = graph[dst_idx].symbol_id
        total_weight = sum(weight for weight, _, _ in entries)
        backing: list[str] = [bid for _, bids, _ in entries for bid in bids]
        regions = {region for _, _, region in entries}
        existing_idx = _find_matching_edge_index(graph, src_idx, dst_idx, relation, sync_kind)
        if existing_idx is not None:
            existing = graph.get_edge_data_by_index(existing_idx)
            total_weight += existing.weight
            backing.extend(_edge_backing_ids(existing))
            regions.add(existing.parallel_region_id)
            edge_id = existing.edge_id
            attributes = dict(existing.attributes)
        else:
            key: _EdgeGroupKey = (source_id, target_id, relation, sync_kind)
            edge_id = _stable_derived_edge_id("bypass", rule_id, key, used_edge_ids)
            attributes = {}
        parallel_region = next(iter(regions)) if len(regions) == 1 else None

        spliced = IREdge(
            edge_id=edge_id,
            source=source_id,
            target=target_id,
            relation=relation,
            sync_kind=sync_kind,
            weight=total_weight,
            parallel_region_id=parallel_region,
            attributes=attributes,
            provenance=ProvenanceStamp(
                grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                rule_id=rule_id,
                backing_ids=_sorted_unique(backing),
            ),
        )
        if existing_idx is not None:
            graph.update_edge_by_index(existing_idx, spliced)
        else:
            graph.add_edge(src_idx, dst_idx, spliced)


def bypass_vertices(
    ir: StructuralGraphIR,
    rule: BypassVerticesRule,
) -> StructuralGraphIR:
    """Splice matching intermediate vertices within fanout budget while preserving SyncKind."""
    graph, idx_of = _build_rx_graph(ir)
    preserve_set = frozenset(rule.preserve_edge_relations)
    used_edge_ids = {edge.edge_id for edge in ir.edges}
    candidates = [nid for nid in sorted(ir.nodes) if _matches_any_glob(nid, rule.node_id_globs)]

    for symbol_id in candidates:
        v_idx = idx_of.get(symbol_id)
        if v_idx is None:
            continue
        in_edges = [entry for entry in graph.in_edges(v_idx) if entry[0] != v_idx]
        out_edges = [entry for entry in graph.out_edges(v_idx) if entry[1] != v_idx]
        if not in_edges or not out_edges:
            continue
        if len(in_edges) * len(out_edges) > rule.max_fanout_product:
            continue

        bypassed_node = graph[v_idx]
        groups = _build_spliced_groups(bypassed_node, in_edges, out_edges, rule, preserve_set)
        graph.remove_node(v_idx)
        del idx_of[symbol_id]
        _upsert_spliced_edges(graph, groups, rule.id, used_edge_ids)

    surviving_raw = {graph[idx].symbol_id: graph[idx] for idx in graph.node_indices()}
    nodes = _reparent_to_surviving_ancestors(surviving_raw, ir.nodes)
    edges = tuple(
        sorted(
            (edge for _, _, edge in graph.weighted_edge_list()),
            key=lambda item: (
                item.source,
                item.target,
                item.relation.value,
                item.sync_kind.value,
                item.edge_id,
            ),
        )
    )
    return StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=nodes,
        edges=edges,
        metadata=dict(ir.metadata),
    )


def _find_cycle_witness(
    graph: rx.PyDiGraph[IRNode, IREdge],
    member_indices: set[int],
) -> tuple[str, ...]:
    """Find a shortest external path m1 -> v1 -> ... -> m2 witnessing an induced quotient cycle."""
    for start in sorted(member_indices, key=lambda idx: graph[idx].symbol_id):
        parent: dict[int, int] = {}
        queue: deque[int] = deque()
        for nxt in sorted(graph.successor_indices(start), key=lambda idx: graph[idx].symbol_id):
            if nxt not in member_indices and nxt not in parent:
                parent[nxt] = start
                queue.append(nxt)
        while queue:
            curr = queue.popleft()
            for nxt in sorted(graph.successor_indices(curr), key=lambda idx: graph[idx].symbol_id):
                if nxt in member_indices:
                    path = [graph[nxt].symbol_id, graph[curr].symbol_id]
                    cursor = curr
                    while cursor in parent:
                        cursor = parent[cursor]
                        path.append(graph[cursor].symbol_id)
                    path.reverse()
                    return tuple(path)
                if nxt not in parent:
                    parent[nxt] = curr
                    queue.append(nxt)
    return ()


def _rollup_contracted_edges(
    graph: rx.PyDiGraph[IRNode, IREdge],
    super_indices: set[int],
    rule_id: str,
    prefix: str,
) -> tuple[IREdge, ...]:
    preserved: list[IREdge] = []
    boundary_groups: dict[_EdgeGroupKey, list[IREdge]] = defaultdict(list)

    for src_idx, dst_idx, edge in graph.weighted_edge_list():
        if src_idx == dst_idx:
            continue
        if src_idx not in super_indices and dst_idx not in super_indices:
            preserved.append(edge)
            continue
        src_id = graph[src_idx].symbol_id
        dst_id = graph[dst_idx].symbol_id
        boundary_groups[(src_id, dst_id, edge.relation, edge.sync_kind)].append(edge)

    used_ids = {edge.edge_id for edge in preserved}
    rolled: list[IREdge] = list(preserved)
    for key in sorted(
        boundary_groups,
        key=lambda item: (item[0], item[1], item[2].value, item[3].value),
    ):
        group = boundary_groups[key]
        src_id, dst_id, relation, sync_kind = key
        backing = _sorted_unique(bid for edge in group for bid in _edge_backing_ids(edge))
        rolled.append(
            IREdge(
                edge_id=_stable_derived_edge_id(prefix, rule_id, key, used_ids),
                source=src_id,
                target=dst_id,
                relation=relation,
                sync_kind=sync_kind,
                weight=sum(edge.weight for edge in group),
                parallel_region_id=_shared_parallel_region(group),
                attributes=_merge_edge_attributes(group),
                provenance=ProvenanceStamp(
                    grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                    rule_id=rule_id,
                    backing_ids=backing,
                ),
            )
        )

    rolled.sort(
        key=lambda item: (
            item.source,
            item.target,
            item.relation.value,
            item.sync_kind.value,
            item.edge_id,
        )
    )
    return tuple(rolled)


def collapse_partition(
    ir: StructuralGraphIR,
    rule: CollapsePartitionRule,
    *,
    forbid_new_cycles: bool = True,
) -> StructuralGraphIR:
    """Contract matched partition members into a single SUPERNODE with optional cycle checking."""
    excluded = frozenset(rule.exclude_ids)
    member_ids = [
        nid
        for nid in sorted(ir.nodes)
        if nid not in excluded and _matches_any_glob(nid, rule.member_globs)
    ]
    if not member_ids:
        return ir

    member_set = set(member_ids)
    if rule.target_node_id in ir.nodes and rule.target_node_id not in member_set:
        msg = (
            f"CollapsePartitionRule '{rule.id}' target_node_id '{rule.target_node_id}' "
            "collides with an existing non-member node"
        )
        raise ValueError(msg)

    graph, idx_of = _build_rx_graph(ir)
    member_indices = [idx_of[nid] for nid in member_ids]
    backing = _sorted_unique(bid for nid in member_ids for bid in _node_backing_ids(ir.nodes[nid]))
    attributes = {"summary": rule.summary} if rule.summary else {}
    supernode = IRNode(
        symbol_id=rule.target_node_id,
        label=rule.label,
        kind=NodeKind.SUPERNODE,
        attributes=attributes,
        provenance=ProvenanceStamp(
            grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
            rule_id=rule.id,
            backing_ids=backing,
        ),
    )

    try:
        super_idx = graph.contract_nodes(member_indices, supernode, check_cycle=forbid_new_cycles)
    except rx.DAGWouldCycle as exc:
        witness = _find_cycle_witness(graph, set(member_indices))
        msg = (
            f"CollapsePartitionRule '{rule.id}' contracting {len(member_ids)} members into "
            f"'{rule.target_node_id}' would induce a directed cycle"
        )
        if witness:
            msg = f"{msg}: {' -> '.join(witness)}"
        raise CycleInducedError(
            msg,
            rule_id=rule.id,
            target_node_id=rule.target_node_id,
            member_ids=tuple(member_ids),
            witness_path=witness,
        ) from exc

    node_to_rep = {
        nid: (rule.target_node_id if nid in member_set else nid) for nid in sorted(ir.nodes)
    }
    base_nodes = {graph[idx].symbol_id: graph[idx] for idx in graph.node_indices()}
    nodes = _resolve_contraction_parents(
        ir.nodes,
        node_to_rep,
        {rule.target_node_id: member_ids},
        base_nodes,
    )
    edges = _rollup_contracted_edges(graph, {super_idx}, rule.id, "collapse")
    return StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=nodes,
        edges=edges,
        metadata=dict(ir.metadata),
    )


def _scc_supernode_id(members: Sequence[str], used_ids: set[str]) -> str:
    digest = hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()[:12]
    candidate = f"scc:{digest}"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    suffix = 1
    while f"{candidate}:{suffix}" in used_ids:
        suffix += 1
    unique_id = f"{candidate}:{suffix}"
    used_ids.add(unique_id)
    return unique_id


def _scc_label(members: Sequence[str]) -> str:
    if len(members) <= 2:
        return f"SCC({', '.join(members)})"
    return f"SCC({members[0]}, ..., +{len(members) - 1})"


def condense_sccs(
    ir: StructuralGraphIR,
    *,
    rule_id: str = "simplify.condense_sccs",
) -> StructuralGraphIR:
    """Collapse non-trivial SCCs (|SCC| > 1) into deterministic SUPERNODEs forming a strict DAG."""
    graph, idx_of = _build_rx_graph(ir)
    nontrivial = sorted(
        (
            sorted(graph[idx].symbol_id for idx in comp)
            for comp in rx.strongly_connected_components(graph)
            if len(comp) > 1
        ),
        key=tuple,
    )
    has_self_loop = any(edge.source == edge.target for edge in ir.edges)
    if not nontrivial and not has_self_loop:
        return ir

    used_node_ids = set(ir.nodes)
    node_to_rep = {nid: nid for nid in ir.nodes}
    contracted_groups: dict[str, list[str]] = {}
    super_indices: set[int] = set()

    for members in nontrivial:
        super_id = _scc_supernode_id(members, used_node_ids)
        backing = _sorted_unique(bid for nid in members for bid in _node_backing_ids(ir.nodes[nid]))
        supernode = IRNode(
            symbol_id=super_id,
            label=_scc_label(members),
            kind=NodeKind.SUPERNODE,
            attributes={"scc_size": str(len(members))},
            provenance=ProvenanceStamp(
                grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                rule_id=rule_id,
                backing_ids=backing,
            ),
        )
        member_indices = [idx_of[nid] for nid in members]
        super_idx = graph.contract_nodes(member_indices, supernode, check_cycle=False)
        super_indices.add(super_idx)
        contracted_groups[super_id] = members
        for nid in members:
            node_to_rep[nid] = super_id

    base_nodes = {graph[idx].symbol_id: graph[idx] for idx in graph.node_indices()}
    nodes = _resolve_contraction_parents(ir.nodes, node_to_rep, contracted_groups, base_nodes)
    edges = _rollup_contracted_edges(graph, super_indices, rule_id, "scc")
    return StructuralGraphIR(
        graph_id=ir.graph_id,
        nodes=nodes,
        edges=edges,
        metadata=dict(ir.metadata),
    )


def _build_simple_digraph(ir: StructuralGraphIR) -> rx.PyDiGraph[str, None]:
    simple: rx.PyDiGraph[str, None] = rx.PyDiGraph(multigraph=False)
    idx_of: dict[str, int] = {}
    for symbol_id in sorted(ir.nodes):
        idx_of[symbol_id] = simple.add_node(symbol_id)
    seen_pairs: set[tuple[int, int]] = set()
    for edge in ir.edges:
        pair = (idx_of[edge.source], idx_of[edge.target])
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            simple.add_edge(pair[0], pair[1], None)
    return simple


def transitive_reduction(
    ir: StructuralGraphIR,
    *,
    condense_cycles: bool = True,
) -> StructuralGraphIR:
    """Compute DAG transitive reduction while retaining original IREdge attributes & provenance."""
    if not ir.edges:
        return ir

    working_ir = ir
    simple = _build_simple_digraph(working_ir)
    if not rx.is_directed_acyclic_graph(simple):
        if not condense_cycles:
            msg = (
                "transitive_reduction requires a strict DAG; condense SCCs first or pass "
                "condense_cycles=True"
            )
            raise ValueError(msg)
        working_ir = condense_sccs(working_ir)
        if not working_ir.edges:
            return working_ir
        simple = _build_simple_digraph(working_ir)

    reduced, orig_to_reduced = rx.transitive_reduction(simple)
    reduced_to_symbol = {red_idx: simple[orig_idx] for orig_idx, red_idx in orig_to_reduced.items()}
    kept_pairs = {
        (reduced_to_symbol[src], reduced_to_symbol[dst]) for src, dst in reduced.edge_list()
    }
    kept_edges = tuple(
        edge for edge in working_ir.edges if (edge.source, edge.target) in kept_pairs
    )
    return StructuralGraphIR(
        graph_id=working_ir.graph_id,
        nodes=working_ir.nodes,
        edges=kept_edges,
        metadata=dict(working_ir.metadata),
    )


def apply_structural_rule(
    ir: StructuralGraphIR,
    rule: GlobalAbstractionRule,
    *,
    forbid_new_cycles: bool = True,
) -> StructuralGraphIR:
    """Dispatch a single closed-world structural reduction rule onto StructuralGraphIR."""
    if isinstance(rule, PruneLeavesRule):
        return prune_leaves(ir, rule)
    if isinstance(rule, BypassVerticesRule):
        return bypass_vertices(ir, rule)
    return collapse_partition(ir, rule, forbid_new_cycles=forbid_new_cycles)


class StructuralTopologicalReducer:
    """Deterministic StructuralReducerPort implementation backed by rustworkx.PyDiGraph."""

    def reduce(
        self,
        ir: StructuralGraphIR,
        view: DiagramViewSpec,
        global_abstractions: tuple[GlobalAbstractionRule, ...] = (),
    ) -> StructuralGraphIR:
        """Apply global abstractions, view scoping, view rules, and DAG simplification."""
        if view.ir_mode is not IRMode.STRUCTURAL_GRAPH:
            msg = (
                f"StructuralTopologicalReducer requires ir_mode='structural_graph', "
                f"got '{view.ir_mode.value}'"
            )
            raise ValueError(msg)

        forbid_new_cycles = view.invariants.forbid_new_cycles
        current = ir

        for rule in global_abstractions:
            current = apply_structural_rule(current, rule, forbid_new_cycles=forbid_new_cycles)

        if isinstance(view.scope, KHopNeighborhoodScope):
            current = k_hop_neighborhood(current, view.scope)

        for rule in view.rules:
            current = apply_structural_rule(current, rule, forbid_new_cycles=forbid_new_cycles)

        if view.simplify.condense_sccs:
            current = condense_sccs(current)

        if view.simplify.transitive_reduction:
            current = transitive_reduction(current, condense_cycles=True)

        return current
