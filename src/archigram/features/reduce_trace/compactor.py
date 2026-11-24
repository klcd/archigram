"""Deterministic Trace & Parallelism Compaction Engine (Rules R1-R6 + SP-Poset)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.concurrency import (
    ParallelSiblingCluster,
    PosetCotreeNode,
    PosetModuleKind,
    SpanInterval,
    cluster_overlapping_siblings,
    decompose_causal_poset,
    find_induced_n_witness,
)
from archigram.domain.ir import (
    EventStatus,
    InteractionTraceIR,
    SyncKind,
    TraceEvent,
)
from archigram.domain.spec import TraceCompactionConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class TraceFragmentKind(StrEnum):
    """Canonical structural fragment kinds for compacted interaction traces."""

    CALL = "call"
    LOOP = "loop"
    OPT = "opt"
    PAR = "par"
    PARALLEL_MAP = "parallel_map"
    RECURSION_CUT = "recursion_cut"
    DEPTH_TRUNCATED = "depth_truncated"
    PRIME_ASYNC = "prime_async"


class TraceFragment(BaseModel):
    """Structured hierarchical sequence fragment produced by deterministic trace compaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TraceFragmentKind
    event_id: str | None = None
    label: str = ""
    repeat_count: int = Field(default=1, ge=1)
    condition: str | None = None
    parallel_group_id: str | None = None
    backing_ids: tuple[str, ...] = Field(default_factory=tuple)
    children: tuple[TraceFragment, ...] = Field(default_factory=tuple)


TraceFragment.model_rebuild()


def collect_trace_backing_ids(ir: InteractionTraceIR) -> frozenset[str]:
    """Return all surviving event_ids and DERIVED_DETERMINISTIC backing_ids in a trace."""
    accounted: set[str] = set()
    for eid, ev in ir.events.items():
        accounted.add(eid)
        accounted.update(ev.provenance.backing_ids)
    return frozenset(accounted)


def _event_sort_key(ev: TraceEvent) -> tuple[int, int, str]:
    return (ev.start_ns, ev.end_ns, ev.event_id)


def _build_children_map(events: dict[str, TraceEvent]) -> dict[str | None, list[TraceEvent]]:
    children: dict[str | None, list[TraceEvent]] = defaultdict(list)
    for ev in events.values():
        children[ev.parent_event_id].append(ev)
    for group in children.values():
        group.sort(key=_event_sort_key)
    return children


def _collect_subtree_events(
    root_id: str,
    events: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
) -> list[TraceEvent]:
    collected: list[TraceEvent] = []
    stack = [events[root_id]]
    while stack:
        curr = stack.pop()
        collected.append(curr)
        stack.extend(reversed(children_map.get(curr.event_id, ())))
    return collected


def _collect_provenance_backing(sub_events: Sequence[TraceEvent]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for ev in sorted(sub_events, key=_event_sort_key):
        for candidate in (ev.event_id, *ev.provenance.backing_ids):
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
    return tuple(ordered)


def _derive_provenance(rule_id: str, sub_events: Sequence[TraceEvent]) -> ProvenanceStamp:
    return ProvenanceStamp(
        grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
        rule_id=rule_id,
        backing_ids=_collect_provenance_backing(sub_events),
    )


def _merge_status(sub_events: Sequence[TraceEvent]) -> EventStatus:
    statuses = {ev.status for ev in sub_events}
    if EventStatus.ERROR in statuses:
        return EventStatus.ERROR
    if EventStatus.OK in statuses:
        return EventStatus.OK
    return EventStatus.UNSET


def _resolve_rep_id(target_id: str, folded_to_rep: dict[str, str]) -> str:
    curr = target_id
    visited: set[str] = set()
    while curr in folded_to_rep and curr not in visited:
        visited.add(curr)
        curr = folded_to_rep[curr]
    return curr


def _remap_event_references(
    events: dict[str, TraceEvent],
    folded_to_rep: dict[str, str],
) -> dict[str, TraceEvent]:
    updated: dict[str, TraceEvent] = {}
    for eid, ev in events.items():
        new_parent = (
            _resolve_rep_id(ev.parent_event_id, folded_to_rep)
            if ev.parent_event_id is not None
            else None
        )
        if new_parent == eid or (new_parent is not None and new_parent not in events):
            new_parent = events[new_parent].parent_event_id if new_parent in events else None

        new_links: list[str] = []
        for link in ev.causal_links:
            resolved = _resolve_rep_id(link, folded_to_rep)
            if resolved != eid and resolved in events and resolved not in new_links:
                new_links.append(resolved)

        if new_parent != ev.parent_event_id or tuple(new_links) != ev.causal_links:
            updated[eid] = ev.model_copy(
                update={
                    "parent_event_id": new_parent,
                    "causal_links": tuple(new_links),
                }
            )
        else:
            updated[eid] = ev
    return updated


def _is_recursive_call(
    ev: TraceEvent,
    ancestor_frames: Sequence[tuple[str, str, str]],
) -> bool:
    for caller_id, callee_id, sig in ancestor_frames:
        if ev.caller_id == caller_id and ev.callee_id == callee_id and ev.signature == sig:
            return True
    if len(ancestor_frames) >= 2:
        for caller_id, callee_id, _ in ancestor_frames[:-1]:
            if ev.callee_id in {caller_id, callee_id}:
                return True
    return False


def _collapse_subtree_into_root(
    root: TraceEvent,
    sub_events: Sequence[TraceEvent],
    rule_id: str,
    compacted_kind: str,
    extra_attrs: dict[str, str],
    new_signature: str | None = None,
) -> TraceEvent:
    merged_attrs = dict(root.attributes)
    merged_attrs["compacted_kind"] = compacted_kind
    merged_attrs.update(extra_attrs)
    all_links: list[str] = []
    sub_ids = {ev.event_id for ev in sub_events}
    for ev in sub_events:
        for link in ev.causal_links:
            if link not in sub_ids and link not in all_links:
                all_links.append(link)
    return root.model_copy(
        update={
            "signature": new_signature if new_signature is not None else root.signature,
            "end_ns": max(ev.end_ns for ev in sub_events),
            "status": _merge_status(sub_events),
            "causal_links": tuple(all_links),
            "attributes": merged_attrs,
            "provenance": _derive_provenance(rule_id, sub_events),
        }
    )


def _record_folded_subtree(
    root_id: str,
    sub_events: Sequence[TraceEvent],
    folded_to_rep: dict[str, str],
) -> None:
    for member in sub_events:
        if member.event_id != root_id:
            folded_to_rep[member.event_id] = root_id


def _collapse_recursion_or_depth(
    ev: TraceEvent,
    events: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
    folded_to_rep: dict[str, str],
    *,
    is_rec: bool,
) -> TraceEvent:
    sub = _collect_subtree_events(ev.event_id, events, children_map)
    _record_folded_subtree(ev.event_id, sub, folded_to_rep)
    if is_rec:
        sig = (
            ev.signature if "[recursion cut]" in ev.signature else f"{ev.signature} [recursion cut]"
        )
        return _collapse_subtree_into_root(
            ev,
            sub,
            rule_id="R4_recursion_cut",
            compacted_kind="recursion_cut",
            extra_attrs={"recursion_cut": "true", "folded_subtree_size": str(len(sub))},
            new_signature=sig,
        )
    return _collapse_subtree_into_root(
        ev,
        sub,
        rule_id="max_call_depth_truncation",
        compacted_kind="depth_truncated",
        extra_attrs={
            "depth_truncated": "true",
            "truncated_descendants": str(len(sub) - 1),
        },
    )


def _apply_recursion_and_depth_pass(
    events: dict[str, TraceEvent],
    config: TraceCompactionConfig,
) -> dict[str, TraceEvent]:
    """Pass 1: R4 recursion cut and max_call_depth subtree summarization."""
    if not events or (not config.cut_recursion and config.max_call_depth is None):
        return events

    children_map = _build_children_map(events)
    surviving: dict[str, TraceEvent] = {}
    folded_to_rep: dict[str, str] = {}

    def _visit(
        ev: TraceEvent,
        depth: int,
        frames: tuple[tuple[str, str, str], ...],
    ) -> None:
        is_rec = config.cut_recursion and _is_recursive_call(ev, frames)
        exceeds_depth = config.max_call_depth is not None and depth >= config.max_call_depth
        child_list = children_map.get(ev.event_id, [])

        if is_rec or (exceeds_depth and child_list):
            surviving[ev.event_id] = _collapse_recursion_or_depth(
                ev, events, children_map, folded_to_rep, is_rec=is_rec
            )
            return

        surviving[ev.event_id] = ev
        next_frames = (*frames, (ev.caller_id, ev.callee_id, ev.signature))
        for child in child_list:
            _visit(child, depth + 1, next_frames)

    for root in children_map.get(None, []):
        _visit(root, 1, ())

    return _remap_event_references(surviving, folded_to_rep)


def _to_span_intervals(events: Iterable[TraceEvent]) -> list[SpanInterval]:
    return [
        SpanInterval(
            event_id=ev.event_id,
            parent_event_id=ev.parent_event_id,
            clock_domain_id=ev.clock_domain_id,
            start_ns=ev.start_ns,
            end_ns=ev.end_ns,
            callee_id=ev.callee_id,
            signature=ev.signature,
        )
        for ev in events
    ]


def _tag_cluster_events(
    cluster: ParallelSiblingCluster,
    cluster_idx: int,
    events: dict[str, TraceEvent],
) -> None:
    parent_tag = cluster.parent_event_id or "root"
    group_id = f"par_{parent_tag}_{cluster.clock_domain_id}_{cluster_idx}"
    for branch_idx, eid in enumerate(cluster.event_ids):
        ev = events[eid]
        attrs = dict(ev.attributes)
        attrs["par_group"] = group_id
        attrs["parallel_group_id"] = group_id
        attrs["par_branch"] = str(branch_idx)
        attrs.setdefault("compacted_kind", "parallel_branch")
        sync = SyncKind.FORK_JOIN_BRANCH if ev.sync_kind is SyncKind.SYNC else ev.sync_kind
        events[eid] = ev.model_copy(update={"attributes": attrs, "sync_kind": sync})


def _fold_homogeneous_bucket(
    members: list[TraceEvent],
    events: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
    folded_to_rep: dict[str, str],
) -> None:
    rep = members[0]
    all_sub: list[TraceEvent] = []
    for member in members:
        all_sub.extend(_collect_subtree_events(member.event_id, events, children_map))

    count = len(members)
    base_sig = rep.signature
    new_sig = f"{base_sig} [x{count} concurrent]"
    extra = {
        "parallel_map_count": str(count),
        "compacted_kind": "parallel_map",
    }
    if "par_group" in rep.attributes:
        extra["par_group"] = rep.attributes["par_group"]
        extra["parallel_group_id"] = rep.attributes["par_group"]

    collapsed = _collapse_subtree_into_root(
        rep,
        all_sub,
        rule_id="R6_parallel_map",
        compacted_kind="parallel_map",
        extra_attrs=extra,
        new_signature=new_sig,
    )
    events[rep.event_id] = collapsed.model_copy(
        update={
            "start_ns": min(m.start_ns for m in members),
            "end_ns": max(ev.end_ns for ev in all_sub),
        }
    )
    for ev in all_sub:
        if ev.event_id != rep.event_id:
            folded_to_rep[ev.event_id] = rep.event_id
            events.pop(ev.event_id, None)


def _fold_cluster_parallel_maps(
    cluster: ParallelSiblingCluster,
    working: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
    max_par_branches: int,
    folded_to_rep: dict[str, str],
) -> None:
    buckets: dict[tuple[str, str, str], list[TraceEvent]] = defaultdict(list)
    for eid in cluster.event_ids:
        if eid in working:
            ev = working[eid]
            buckets[(ev.caller_id, ev.callee_id, ev.signature)].append(ev)
    for key in sorted(buckets):
        members = sorted(buckets[key], key=_event_sort_key)
        if len(members) >= max_par_branches:
            _fold_homogeneous_bucket(members, working, children_map, folded_to_rep)


def _apply_parallel_siblings_and_map_pass(
    events: dict[str, TraceEvent],
    config: TraceCompactionConfig,
) -> dict[str, TraceEvent]:
    """Pass 2 & 3: R5 clock-domain sibling clustering and R6 homogeneous parallel map folding."""
    if len(events) < 2 or (not config.fold_parallel_siblings and not config.fold_parallel_map):
        return events

    working = dict(events)
    clusters = cluster_overlapping_siblings(
        _to_span_intervals(working.values()),
        epsilon_ns=config.parallel_epsilon_ns,
    )
    if not clusters:
        return working

    if config.fold_parallel_siblings:
        for idx, cluster in enumerate(clusters):
            _tag_cluster_events(cluster, idx, working)

    if not config.fold_parallel_map:
        return working

    children_map = _build_children_map(working)
    folded_to_rep: dict[str, str] = {}
    for cluster in clusters:
        _fold_cluster_parallel_maps(
            cluster, working, children_map, config.max_par_branches, folded_to_rep
        )

    return _remap_event_references(working, folded_to_rep) if folded_to_rep else working


def _build_causal_dag_edges(events: dict[str, TraceEvent]) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for eid, ev in events.items():
        if ev.parent_event_id is not None and ev.parent_event_id in events:
            edges.add((ev.parent_event_id, eid))
        for link in ev.causal_links:
            if link in events:
                edges.add((link, eid))
    return sorted(edges)


def _collect_cotree_leaves(node: PosetCotreeNode) -> list[str]:
    if node.kind is PosetModuleKind.LEAF and node.node_id is not None:
        return [node.node_id]
    leaves: list[str] = []
    for child in node.children:
        leaves.extend(_collect_cotree_leaves(child))
    return leaves


def _annotate_parallel_cotree_node(
    node: PosetCotreeNode,
    working: dict[str, TraceEvent],
    counter: list[int],
) -> None:
    group_id = f"sp_par_{counter[0]}"
    counter[0] += 1
    for branch_idx, child in enumerate(node.children):
        for eid in _collect_cotree_leaves(child):
            ev = working[eid]
            attrs = dict(ev.attributes)
            attrs.setdefault("poset_module", "parallel")
            attrs.setdefault("poset_par_group", group_id)
            attrs.setdefault("poset_par_branch", str(branch_idx))
            working[eid] = ev.model_copy(update={"attributes": attrs})


def _annotate_prime_cotree_node(
    node: PosetCotreeNode,
    working: dict[str, TraceEvent],
) -> None:
    witness_str = ",".join(node.induced_n_witness) if node.induced_n_witness else ""
    child_leaf_sets = [frozenset(_collect_cotree_leaves(c)) for c in node.children]
    connected_children = {idx for pair in node.prime_child_edges for idx in pair}
    for c_idx, leaf_set in enumerate(child_leaf_sets):
        for eid in leaf_set:
            ev = working[eid]
            attrs = dict(ev.attributes)
            attrs["poset_module"] = "prime"
            if witness_str:
                attrs["induced_n_witness"] = witness_str
            has_cross_link = any(link not in leaf_set for link in ev.causal_links)
            if has_cross_link or c_idx in connected_children:
                attrs["prime_async_lowered"] = "true"
                working[eid] = ev.model_copy(
                    update={"sync_kind": SyncKind.ASYNC_FIRE_FORGET, "attributes": attrs}
                )
            else:
                working[eid] = ev.model_copy(update={"attributes": attrs})


def _annotate_cotree_modules(
    node: PosetCotreeNode,
    working: dict[str, TraceEvent],
    counter: list[int],
) -> None:
    if node.kind is PosetModuleKind.LEAF:
        return
    if node.kind is PosetModuleKind.PARALLEL:
        _annotate_parallel_cotree_node(node, working, counter)
    elif node.kind is PosetModuleKind.PRIME:
        _annotate_prime_cotree_node(node, working)

    for child in node.children:
        _annotate_cotree_modules(child, working, counter)


def _apply_series_parallel_poset_pass(
    events: dict[str, TraceEvent],
    config: TraceCompactionConfig,
) -> tuple[dict[str, TraceEvent], PosetCotreeNode | None]:
    """Pass 4: Series-Parallel Poset Modular Decomposition and PRIME async lowering."""
    if not events or not config.decompose_series_parallel:
        return events, None

    has_links = any(bool(ev.causal_links) for ev in events.values())
    if not has_links:
        return events, None

    edges = _build_causal_dag_edges(events)
    node_ids = sorted(events.keys())
    cotree = decompose_causal_poset(node_ids, edges)
    working = dict(events)
    _annotate_cotree_modules(cotree, working, [0])

    witness = find_induced_n_witness(node_ids, edges)
    if witness is not None:
        a, b, c, d = witness
        for wid in (a, b, c, d):
            ev = working[wid]
            attrs = dict(ev.attributes)
            attrs["poset_module"] = "prime"
            attrs["prime_async_lowered"] = "true"
            attrs["induced_n_witness"] = f"{a},{b},{c},{d}"
            working[wid] = ev.model_copy(
                update={"sync_kind": SyncKind.ASYNC_FIRE_FORGET, "attributes": attrs}
            )
    return working, cotree


def _compute_subtree_hashes(
    events: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
) -> dict[str, str]:
    hashes: dict[str, str] = {}

    def _hash_node(eid: str) -> str:
        if eid in hashes:
            return hashes[eid]
        ev = events[eid]
        child_tuples = tuple(_hash_node(c.event_id) for c in children_map.get(eid, []))
        payload = (
            f"{ev.caller_id}|{ev.callee_id}|{ev.signature}|{ev.sync_kind.value}|{child_tuples}"
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        hashes[eid] = digest
        return digest

    for eid in sorted(events):
        _hash_node(eid)
    return hashes


def _are_sequential_siblings(first: TraceEvent, second: TraceEvent, epsilon_ns: int) -> bool:
    if first.attributes.get("par_group") or second.attributes.get("par_group"):
        return False
    if first.clock_domain_id != second.clock_domain_id:
        return False
    if first.start_ns == second.start_ns:
        return False
    return max(first.start_ns, second.start_ns) >= min(first.end_ns, second.end_ns) - epsilon_ns


def _optional_leaf_diff(
    first: TraceEvent,
    second: TraceEvent,
    children_map: dict[str | None, list[TraceEvent]],
    hashes: dict[str, str],
) -> TraceEvent | None:
    """Return the optional leaf child if two sibling subtrees differ by exactly one leaf."""
    if (
        first.caller_id != second.caller_id
        or first.callee_id != second.callee_id
        or first.signature != second.signature
    ):
        return None
    c1 = children_map.get(first.event_id, [])
    c2 = children_map.get(second.event_id, [])
    if abs(len(c1) - len(c2)) != 1:
        return None
    longer, shorter = (c1, c2) if len(c1) > len(c2) else (c2, c1)
    short_hashes = [hashes[c.event_id] for c in shorter]
    for idx, candidate in enumerate(longer):
        if children_map.get(candidate.event_id):
            continue
        remaining = [hashes[c.event_id] for i, c in enumerate(longer) if i != idx]
        if remaining == short_hashes:
            return candidate
    return None


def _can_join_loop_run(
    run: list[TraceEvent],
    candidate: TraceEvent,
    children_map: dict[str | None, list[TraceEvent]],
    hashes: dict[str, str],
    config: TraceCompactionConfig,
) -> bool:
    prev = run[-1]
    if not _are_sequential_siblings(prev, candidate, config.parallel_epsilon_ns):
        return False
    cand_hash = hashes[candidate.event_id]
    seen_hashes: list[TraceEvent] = []
    for member in run:
        if hashes[member.event_id] == cand_hash:
            return True
        if all(hashes[m.event_id] != hashes[member.event_id] for m in seen_hashes):
            seen_hashes.append(member)
    if not config.fold_loop_with_opt or len(seen_hashes) >= 2:
        return False
    return _optional_leaf_diff(seen_hashes[0], candidate, children_map, hashes) is not None


def _fold_loop_run(
    run: list[TraceEvent],
    working: dict[str, TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
    hashes: dict[str, str],
    folded_to_rep: dict[str, str],
) -> None:
    rep = max(run, key=lambda ev: len(children_map.get(ev.event_id, [])))
    all_sub: list[TraceEvent] = []
    opt_leaf: TraceEvent | None = None
    for member in run:
        all_sub.extend(_collect_subtree_events(member.event_id, working, children_map))
        if opt_leaf is None and hashes[member.event_id] != hashes[rep.event_id]:
            opt_leaf = _optional_leaf_diff(rep, member, children_map, hashes)

    rep_sub_ids = {
        ev.event_id for ev in _collect_subtree_events(rep.event_id, working, children_map)
    }
    repeat_count = len(run)
    is_multi_node = len(rep_sub_ids) > 1
    rule_id = (
        "R3_loop_with_opt"
        if opt_leaf is not None
        else ("R2_subtree_loop" if is_multi_node else "R1_tandem_loop")
    )
    compacted_kind = "loop_with_opt" if opt_leaf is not None else "loop"
    attrs = dict(rep.attributes)
    attrs["loop_count"] = str(repeat_count)
    attrs["compacted_kind"] = compacted_kind

    if opt_leaf is not None and opt_leaf.event_id in working:
        opt_ev = working[opt_leaf.event_id]
        opt_attrs = dict(opt_ev.attributes)
        opt_cond = f"optional {opt_ev.signature}"
        opt_attrs["opt_condition"] = opt_cond
        opt_attrs["compacted_kind"] = "opt"
        working[opt_ev.event_id] = opt_ev.model_copy(update={"attributes": opt_attrs})
        attrs["opt_condition"] = opt_cond

    base_sig = rep.signature
    new_sig = f"{base_sig} [loop x{repeat_count}]"
    working[rep.event_id] = rep.model_copy(
        update={
            "signature": new_sig,
            "start_ns": min(m.start_ns for m in run),
            "end_ns": max(ev.end_ns for ev in all_sub),
            "status": _merge_status(all_sub),
            "attributes": attrs,
            "provenance": _derive_provenance(rule_id, all_sub),
        }
    )

    for ev in all_sub:
        if ev.event_id not in rep_sub_ids:
            folded_to_rep[ev.event_id] = rep.event_id
            working.pop(ev.event_id, None)


def _apply_loop_folding_pass(
    events: dict[str, TraceEvent],
    config: TraceCompactionConfig,
) -> dict[str, TraceEvent]:
    """Pass 5: R1/R2 contiguous repeating loop folding and R3 loop + opt leaf folding."""
    if len(events) < 2 or not config.fold_loops:
        return events

    working = dict(events)
    children_map = _build_children_map(working)
    hashes = _compute_subtree_hashes(working, children_map)
    folded_to_rep: dict[str, str] = {}

    def _fold_level(parent_id: str | None) -> None:
        siblings = [ev for ev in children_map.get(parent_id, []) if ev.event_id in working]
        for ev in siblings:
            _fold_level(ev.event_id)

        siblings = [ev for ev in children_map.get(parent_id, []) if ev.event_id in working]
        if len(siblings) < 2:
            return
        idx = 0
        while idx < len(siblings):
            run = [siblings[idx]]
            nxt = idx + 1
            while nxt < len(siblings) and _can_join_loop_run(
                run, siblings[nxt], children_map, hashes, config
            ):
                run.append(siblings[nxt])
                nxt += 1
            if len(run) >= 2:
                _fold_loop_run(run, working, children_map, hashes, folded_to_rep)
            idx = nxt

    _fold_level(None)
    return _remap_event_references(working, folded_to_rep) if folded_to_rep else working


_FRAGMENT_KIND_BY_COMPACTED: dict[str, TraceFragmentKind] = {
    "recursion_cut": TraceFragmentKind.RECURSION_CUT,
    "depth_truncated": TraceFragmentKind.DEPTH_TRUNCATED,
    "parallel_map": TraceFragmentKind.PARALLEL_MAP,
    "loop": TraceFragmentKind.LOOP,
    "loop_with_opt": TraceFragmentKind.LOOP,
    "opt": TraceFragmentKind.OPT,
}


def _resolve_event_fragment_kind(ev: TraceEvent) -> TraceFragmentKind:
    ckind = ev.attributes.get("compacted_kind", "")
    if ckind in _FRAGMENT_KIND_BY_COMPACTED:
        return _FRAGMENT_KIND_BY_COMPACTED[ckind]
    if "parallel_map_count" in ev.attributes:
        return TraceFragmentKind.PARALLEL_MAP
    if "loop_count" in ev.attributes:
        return TraceFragmentKind.LOOP
    if "opt_condition" in ev.attributes:
        return TraceFragmentKind.OPT
    return (
        TraceFragmentKind.PRIME_ASYNC
        if ev.attributes.get("prime_async_lowered") == "true"
        else TraceFragmentKind.CALL
    )


def _build_event_fragment(
    ev: TraceEvent,
    children_map: dict[str | None, list[TraceEvent]],
) -> TraceFragment:
    child_frags = build_fragment_tree_for_siblings(children_map.get(ev.event_id, []), children_map)
    backing = ev.provenance.backing_ids or (ev.event_id,)
    frag_kind = _resolve_event_fragment_kind(ev)
    repeat_raw = ev.attributes.get("parallel_map_count") or ev.attributes.get("loop_count") or "1"
    repeat_count = max(1, int(repeat_raw))
    return TraceFragment(
        kind=frag_kind,
        event_id=ev.event_id,
        label=ev.signature,
        repeat_count=repeat_count,
        condition=ev.attributes.get("opt_condition"),
        parallel_group_id=ev.attributes.get("par_group"),
        backing_ids=backing,
        children=child_frags,
    )


def build_fragment_tree_for_siblings(
    siblings: Sequence[TraceEvent],
    children_map: dict[str | None, list[TraceEvent]],
) -> tuple[TraceFragment, ...]:
    """Group ordered sibling events into structured PAR, LOOP, OPT, and CALL TraceFragments."""
    fragments: list[TraceFragment] = []
    idx = 0
    while idx < len(siblings):
        ev = siblings[idx]
        par_group = ev.attributes.get("par_group")
        if par_group:
            group_members = [ev]
            nxt = idx + 1
            while nxt < len(siblings) and siblings[nxt].attributes.get("par_group") == par_group:
                group_members.append(siblings[nxt])
                nxt += 1
            if len(group_members) >= 2:
                branch_frags = tuple(_build_event_fragment(m, children_map) for m in group_members)
                all_backing = _collect_provenance_backing(group_members)
                fragments.append(
                    TraceFragment(
                        kind=TraceFragmentKind.PAR,
                        label=par_group,
                        parallel_group_id=par_group,
                        backing_ids=all_backing,
                        children=branch_frags,
                    )
                )
                idx = nxt
                continue
        fragments.append(_build_event_fragment(ev, children_map))
        idx += 1
    return tuple(fragments)


def build_trace_fragment_tree(ir: InteractionTraceIR) -> tuple[TraceFragment, ...]:
    """Construct the structured sequence fragment tree for a compacted InteractionTraceIR."""
    children_map = _build_children_map(ir.events)
    return build_fragment_tree_for_siblings(children_map.get(None, []), children_map)


class TraceCompactionEngine:
    """Deterministic R1-R6 + Series-Parallel Poset interaction trace compaction engine."""

    def compact_with_fragments(
        self,
        ir: InteractionTraceIR,
        config: TraceCompactionConfig | None = None,
    ) -> tuple[InteractionTraceIR, tuple[TraceFragment, ...]]:
        """Run ordered R4/depth -> R5/R6 -> SP-Poset -> R1-R3 passes and return IR + fragments."""
        cfg = config if config is not None else TraceCompactionConfig()
        events = dict(ir.events)

        events = _apply_recursion_and_depth_pass(events, cfg)
        events = _apply_parallel_siblings_and_map_pass(events, cfg)
        events, cotree = _apply_series_parallel_poset_pass(events, cfg)
        events = _apply_loop_folding_pass(events, cfg)

        ordered_events = {ev.event_id: ev for ev in sorted(events.values(), key=_event_sort_key)}
        temp_ir = ir.model_copy(update={"events": ordered_events})
        fragments = build_trace_fragment_tree(temp_ir)

        metadata = dict(ir.metadata)
        metadata["compacted_from_event_count"] = str(len(ir.events))
        metadata["compacted_to_event_count"] = str(len(ordered_events))
        metadata["fragment_tree_json"] = json.dumps(
            [frag.model_dump(mode="json") for frag in fragments],
            separators=(",", ":"),
            sort_keys=True,
        )
        if cotree is not None:
            metadata["poset_root_kind"] = cotree.kind.value

        compacted_ir = ir.model_copy(
            update={
                "events": ordered_events,
                "metadata": metadata,
            }
        )
        return compacted_ir, fragments

    def compact(
        self,
        ir: InteractionTraceIR,
        config: TraceCompactionConfig,
    ) -> InteractionTraceIR:
        """Compact an InteractionTraceIR deterministically per TraceCompactorPort."""
        compacted_ir, _ = self.compact_with_fragments(ir, config)
        return compacted_ir
