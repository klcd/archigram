"""Pure domain models and deterministic algorithms for concurrency & causal poset analysis."""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from archigram.domain import NonBlankStr, validate_non_blank

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_validate_non_blank = validate_non_blank


class ConcurrencyMode(StrEnum):
    """Normalized execution concurrency semantics across runtime traces and static call graphs."""

    SEQUENTIAL = "sequential"
    FORK_JOIN = "fork_join"
    ASYNC_FIRE_FORGET = "async_fire_forget"
    WEAK_SEQUENCING = "weak_sequencing"


class PosetModuleKind(StrEnum):
    """Modular decomposition node kind for Series-Parallel / N-free causal posets."""

    LEAF = "leaf"
    SERIES = "series"
    PARALLEL = "parallel"
    PRIME = "prime"


class SpanInterval(BaseModel):
    """Immutable span timing record anchored to an explicit caller clock domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: NonBlankStr
    parent_event_id: NonBlankStr | None = None
    clock_domain_id: NonBlankStr
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    callee_id: NonBlankStr | None = None
    signature: NonBlankStr | None = None
    concurrency_mode: ConcurrencyMode = ConcurrencyMode.SEQUENTIAL

    @model_validator(mode="after")
    def _validate_interval(self) -> SpanInterval:
        if self.end_ns < self.start_ns:
            msg = (
                f"SpanInterval '{self.event_id}' has end_ns ({self.end_ns}) "
                f"earlier than start_ns ({self.start_ns})"
            )
            raise ValueError(msg)
        return self


class ParallelSiblingCluster(BaseModel):
    """Connected overlap component (>= 2 spans) within one (parent, clock_domain) group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_event_id: NonBlankStr | None = None
    clock_domain_id: NonBlankStr
    event_ids: tuple[NonBlankStr, ...] = Field(min_length=2)
    window_start_ns: int = Field(ge=0)
    window_end_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_window(self) -> ParallelSiblingCluster:
        if self.window_end_ns < self.window_start_ns:
            msg = "ParallelSiblingCluster window_end_ns cannot precede window_start_ns"
            raise ValueError(msg)
        return self


class ParallelMapSummary(BaseModel):
    """Compacted R6 homogeneous parallel fan-out summary retaining full backing_ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_event_id: NonBlankStr | None = None
    clock_domain_id: NonBlankStr
    callee_id: NonBlankStr
    signature: NonBlankStr
    concurrency_count: int = Field(ge=2)
    backing_ids: tuple[NonBlankStr, ...] = Field(min_length=2)
    window_start_ns: int = Field(ge=0)
    window_end_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_summary(self) -> ParallelMapSummary:
        if self.concurrency_count != len(self.backing_ids):
            msg = "concurrency_count must equal len(backing_ids)"
            raise ValueError(msg)
        if self.window_end_ns < self.window_start_ns:
            msg = "ParallelMapSummary window_end_ns cannot precede window_start_ns"
            raise ValueError(msg)
        return self


def _validate_prime_edges(
    child_count: int,
    edges: tuple[tuple[int, int], ...],
) -> None:
    for src, dst in edges:
        if src == dst or not (0 <= src < child_count and 0 <= dst < child_count):
            msg = f"Invalid prime_child_edges entry ({src}, {dst}) for {child_count} children"
            raise ValueError(msg)


class PosetCotreeNode(BaseModel):
    """Recursive modular decomposition tree of a causal DAG partial order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PosetModuleKind
    concurrency_mode: ConcurrencyMode
    node_id: NonBlankStr | None = None
    children: tuple[PosetCotreeNode, ...] = Field(default_factory=tuple)
    prime_child_edges: tuple[tuple[int, int], ...] = Field(default_factory=tuple)
    induced_n_witness: tuple[NonBlankStr, NonBlankStr, NonBlankStr, NonBlankStr] | None = None

    @model_validator(mode="after")
    def _validate_cotree_node(self) -> PosetCotreeNode:
        if self.kind is PosetModuleKind.LEAF:
            if self.node_id is None or self.children or self.prime_child_edges:
                msg = "LEAF PosetCotreeNode requires node_id and no children/prime_child_edges"
                raise ValueError(msg)
            return self
        if self.node_id is not None:
            msg = f"{self.kind.value} PosetCotreeNode must not define node_id"
            raise ValueError(msg)
        if self.kind is PosetModuleKind.PRIME:
            if len(self.children) < 4 or self.induced_n_witness is None:
                msg = "PRIME PosetCotreeNode requires >= 4 children and an induced_n_witness"
                raise ValueError(msg)
            _validate_prime_edges(len(self.children), self.prime_child_edges)
        elif len(self.children) < 2 or self.prime_child_edges or self.induced_n_witness is not None:
            msg = f"{self.kind.value} PosetCotreeNode requires >= 2 children and no prime metadata"
            raise ValueError(msg)
        return self


PosetCotreeNode.model_rebuild()


def _group_sort_key(key: tuple[str | None, str]) -> tuple[int, str, str]:
    parent_id, clock_domain_id = key
    return (0 if parent_id is None else 1, parent_id or "", clock_domain_id)


def _build_cluster(
    parent_event_id: str | None,
    clock_domain_id: str,
    members: list[SpanInterval],
) -> ParallelSiblingCluster:
    return ParallelSiblingCluster(
        parent_event_id=parent_event_id,
        clock_domain_id=clock_domain_id,
        event_ids=tuple(span.event_id for span in members),
        window_start_ns=min(span.start_ns for span in members),
        window_end_ns=max(span.end_ns for span in members),
    )


def _sweep_domain_group(
    parent_event_id: str | None,
    clock_domain_id: str,
    group: list[SpanInterval],
    epsilon_ns: int,
) -> list[ParallelSiblingCluster]:
    """Extract connected overlap components (>= 2 spans) via O(n log n) sweep-line."""
    # Intervals with duration <= epsilon_ns can never satisfy
    # max(s_i, s_j) < min(e_i, e_j) - epsilon_ns against any interval.
    eligible = [span for span in group if span.end_ns - span.start_ns > epsilon_ns]
    if len(eligible) < 2:
        return []
    eligible.sort(key=lambda span: (span.start_ns, span.end_ns, span.event_id))

    clusters: list[ParallelSiblingCluster] = []
    current: list[SpanInterval] = [eligible[0]]
    max_end = eligible[0].end_ns

    for span in eligible[1:]:
        if span.start_ns < max_end - epsilon_ns:
            current.append(span)
            max_end = max(max_end, span.end_ns)
        else:
            if len(current) >= 2:
                clusters.append(_build_cluster(parent_event_id, clock_domain_id, current))
            current = [span]
            max_end = span.end_ns

    if len(current) >= 2:
        clusters.append(_build_cluster(parent_event_id, clock_domain_id, current))
    return clusters


def cluster_overlapping_siblings(
    intervals: Sequence[SpanInterval],
    epsilon_ns: int = 0,
) -> tuple[ParallelSiblingCluster, ...]:
    """Rule R5: clock-domain-isolated sweep-line interval overlap clustering."""
    if epsilon_ns < 0:
        msg = f"epsilon_ns must be non-negative, got {epsilon_ns}"
        raise ValueError(msg)

    seen_ids: set[str] = set()
    grouped: dict[tuple[str | None, str], list[SpanInterval]] = {}
    for interval in intervals:
        if interval.event_id in seen_ids:
            msg = f"Duplicate event_id '{interval.event_id}' in sibling intervals"
            raise ValueError(msg)
        seen_ids.add(interval.event_id)
        key = (interval.parent_event_id, interval.clock_domain_id)
        grouped.setdefault(key, []).append(interval)

    clusters: list[ParallelSiblingCluster] = []
    for parent_id, clock_domain_id in sorted(grouped, key=_group_sort_key):
        clusters.extend(
            _sweep_domain_group(
                parent_id,
                clock_domain_id,
                grouped[(parent_id, clock_domain_id)],
                epsilon_ns,
            )
        )
    return tuple(clusters)


def fold_parallel_map(
    intervals: Sequence[SpanInterval],
    max_par_branches: int = 4,
    epsilon_ns: int = 0,
) -> tuple[ParallelMapSummary, ...]:
    """Rule R6: compact homogeneous concurrent fan-out groups (>= max_par_branches)."""
    if max_par_branches < 2:
        msg = f"max_par_branches must be >= 2, got {max_par_branches}"
        raise ValueError(msg)

    by_id = {interval.event_id: interval for interval in intervals}
    clusters = cluster_overlapping_siblings(intervals, epsilon_ns=epsilon_ns)
    summaries: list[ParallelMapSummary] = []

    for cluster in clusters:
        buckets: dict[tuple[str, str], list[SpanInterval]] = {}
        for event_id in cluster.event_ids:
            span = by_id[event_id]
            if span.callee_id is not None and span.signature is not None:
                buckets.setdefault((span.callee_id, span.signature), []).append(span)

        for (callee_id, signature), members in sorted(buckets.items()):
            if len(members) >= max_par_branches:
                summaries.append(
                    ParallelMapSummary(
                        parent_event_id=cluster.parent_event_id,
                        clock_domain_id=cluster.clock_domain_id,
                        callee_id=callee_id,
                        signature=signature,
                        concurrency_count=len(members),
                        backing_ids=tuple(member.event_id for member in members),
                        window_start_ns=min(member.start_ns for member in members),
                        window_end_ns=max(member.end_ns for member in members),
                    )
                )
    return tuple(summaries)


@dataclass(frozen=True, slots=True)
class _CausalPoset:
    """Bitmask-backed strict partial order computed as the transitive closure of a DAG."""

    node_ids: tuple[str, ...]
    anc: tuple[int, ...]
    desc: tuple[int, ...]
    comp: tuple[int, ...]


def _index_nodes(nodes: Sequence[str]) -> tuple[tuple[str, ...], dict[str, int]]:
    if not nodes:
        msg = "Causal poset requires at least one node"
        raise ValueError(msg)

    seen: set[str] = set()
    for node_id in nodes:
        _validate_non_blank(node_id)
        if node_id in seen:
            msg = f"Duplicate node_id '{node_id}' in causal poset"
            raise ValueError(msg)
        seen.add(node_id)

    sorted_ids = tuple(sorted(seen))
    return sorted_ids, {node_id: idx for idx, node_id in enumerate(sorted_ids)}


def _build_dag_adjacency(
    node_count: int,
    index_of: dict[str, int],
    edges: Sequence[tuple[str, str]],
) -> tuple[list[list[int]], list[list[int]], list[int]]:
    adj: list[list[int]] = [[] for _ in range(node_count)]
    rev_adj: list[list[int]] = [[] for _ in range(node_count)]
    indegree = [0] * node_count
    seen_edges: set[tuple[int, int]] = set()

    for src_id, dst_id in edges:
        if src_id not in index_of or dst_id not in index_of:
            msg = f"Edge ({src_id!r}, {dst_id!r}) references unknown node"
            raise ValueError(msg)
        src, dst = index_of[src_id], index_of[dst_id]
        if src == dst:
            msg = f"Self-loop detected on node '{src_id}'"
            raise ValueError(msg)
        if (src, dst) in seen_edges:
            continue
        seen_edges.add((src, dst))
        adj[src].append(dst)
        rev_adj[dst].append(src)
        indegree[dst] += 1

    return adj, rev_adj, indegree


def _topological_order(
    node_count: int,
    adj: list[list[int]],
    indegree: list[int],
) -> list[int]:
    ready: deque[int] = deque(idx for idx, deg in enumerate(indegree) if deg == 0)
    topo: list[int] = []
    while ready:
        current = ready.popleft()
        topo.append(current)
        for nxt in adj[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(topo) != node_count:
        msg = "Causal graph contains a cycle and is not a valid DAG"
        raise ValueError(msg)
    return topo


def _build_causal_poset(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> _CausalPoset:
    node_ids, index_of = _index_nodes(nodes)
    node_count = len(node_ids)
    adj, rev_adj, indegree = _build_dag_adjacency(node_count, index_of, edges)
    topo = _topological_order(node_count, adj, indegree)

    desc = [0] * node_count
    for u in reversed(topo):
        mask = 0
        for v in adj[u]:
            mask |= (1 << v) | desc[v]
        desc[u] = mask

    anc = [0] * node_count
    for v in topo:
        mask = 0
        for u in rev_adj[v]:
            mask |= (1 << u) | anc[u]
        anc[v] = mask

    comp = tuple(anc[i] | desc[i] for i in range(node_count))
    return _CausalPoset(
        node_ids=node_ids,
        anc=tuple(anc),
        desc=tuple(desc),
        comp=comp,
    )


def _find_n_for_pair(
    b: int,
    c: int,
    sub_mask: int,
    poset: _CausalPoset,
) -> tuple[str, str, str, str] | None:
    cand_a = poset.anc[c] & sub_mask & ~poset.comp[b] & ~(1 << b)
    if not cand_a:
        return None
    cand_d = poset.desc[b] & sub_mask & ~poset.comp[c] & ~(1 << c)
    if not cand_d:
        return None

    rem_a = cand_a
    while rem_a:
        bit_a = rem_a & -rem_a
        rem_a ^= bit_a
        a = bit_a.bit_length() - 1
        valid_d = cand_d & ~poset.comp[a]
        if valid_d:
            d = (valid_d & -valid_d).bit_length() - 1
            ids = poset.node_ids
            return (ids[a], ids[b], ids[c], ids[d])
    return None


def _find_induced_n_in_mask(
    sub_mask: int,
    poset: _CausalPoset,
) -> tuple[str, str, str, str] | None:
    rem_b = sub_mask
    while rem_b:
        bit_b = rem_b & -rem_b
        rem_b ^= bit_b
        b = bit_b.bit_length() - 1
        rem_c = poset.desc[b] & sub_mask
        while rem_c:
            bit_c = rem_c & -rem_c
            rem_c ^= bit_c
            c = bit_c.bit_length() - 1
            witness = _find_n_for_pair(b, c, sub_mask, poset)
            if witness is not None:
                return witness
    return None


def find_induced_n_witness(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> tuple[str, str, str, str] | None:
    """Return an exact induced-N witness (a, b, c, d) iff the poset is non-series-parallel."""
    poset = _build_causal_poset(nodes, edges)
    full_mask = (1 << len(poset.node_ids)) - 1
    return _find_induced_n_in_mask(full_mask, poset)


def _connected_components_mask(
    sub_mask: int,
    poset: _CausalPoset,
    *,
    complement: bool,
) -> list[int]:
    """Extract connected components of G_Q (complement=False) or ~G_Q (complement=True)."""
    remaining = sub_mask
    components: list[int] = []
    comp = poset.comp

    while remaining:
        seed = remaining & -remaining
        remaining ^= seed
        component = seed
        frontier = seed
        while frontier:
            bit = frontier & -frontier
            frontier ^= bit
            u = bit.bit_length() - 1
            neighbors = (remaining & ~comp[u]) if complement else (remaining & comp[u])
            if neighbors:
                remaining ^= neighbors
                component |= neighbors
                frontier |= neighbors
        components.append(component)
    return components


def _module_closure(seed_mask: int, sub_mask: int, poset: _CausalPoset) -> int:
    """Compute the smallest poset module in sub_mask containing seed_mask."""
    mod = seed_mask
    anc = poset.anc
    desc = poset.desc

    while True:
        outside = sub_mask ^ mod
        if not outside:
            return sub_mask
        added = 0
        rem = outside
        while rem:
            bit = rem & -rem
            rem ^= bit
            z = bit.bit_length() - 1
            d_inter = desc[z] & mod
            a_inter = anc[z] & mod
            if (d_inter and d_inter != mod) or (a_inter and a_inter != mod):
                added |= bit
        if not added:
            return mod
        mod |= added


def _maximal_proper_modules(sub_mask: int, poset: _CausalPoset) -> list[int]:
    """Partition a PRIME poset into its unique maximal proper modules (Gallai's theorem)."""
    remaining = sub_mask
    modules: list[int] = []

    while remaining:
        seed = remaining & -remaining
        mod = seed
        candidates = remaining ^ seed
        while candidates:
            bit = candidates & -candidates
            candidates ^= bit
            if mod & bit:
                continue
            closure = _module_closure(mod | bit, sub_mask, poset)
            if closure != sub_mask:
                mod = closure
        modules.append(mod)
        remaining &= ~mod
    return modules


def _quotient_cover_edges(
    modules: list[int],
    poset: _CausalPoset,
) -> tuple[tuple[int, int], ...]:
    """Compute the transitive reduction (Hasse cover edges) of the prime quotient poset."""
    count = len(modules)
    reps = [(mod & -mod).bit_length() - 1 for mod in modules]
    q_desc = [0] * count
    q_anc = [0] * count

    for i in range(count):
        rep_i = reps[i]
        desc_i = poset.desc[rep_i]
        for j in range(count):
            if i != j and ((desc_i >> reps[j]) & 1):
                q_desc[i] |= 1 << j
                q_anc[j] |= 1 << i

    edges: list[tuple[int, int]] = []
    for i in range(count):
        rem = q_desc[i]
        while rem:
            bit = rem & -rem
            rem ^= bit
            j = bit.bit_length() - 1
            if (q_desc[i] & q_anc[j]) == 0:
                edges.append((i, j))
    return tuple(edges)


def _order_series_components(
    components: list[int],
    sub_mask: int,
    poset: _CausalPoset,
) -> list[int]:
    return sorted(
        components,
        key=lambda mask: (
            poset.anc[(mask & -mask).bit_length() - 1] & (sub_mask ^ mask)
        ).bit_count(),
    )


def _decompose_subset(sub_mask: int, poset: _CausalPoset) -> PosetCotreeNode:
    if sub_mask & (sub_mask - 1) == 0:
        idx = sub_mask.bit_length() - 1
        return PosetCotreeNode(
            kind=PosetModuleKind.LEAF,
            concurrency_mode=ConcurrencyMode.SEQUENTIAL,
            node_id=poset.node_ids[idx],
        )

    par_parts = _connected_components_mask(sub_mask, poset, complement=False)
    if len(par_parts) > 1:
        return PosetCotreeNode(
            kind=PosetModuleKind.PARALLEL,
            concurrency_mode=ConcurrencyMode.FORK_JOIN,
            children=tuple(_decompose_subset(part, poset) for part in par_parts),
        )

    ser_parts = _connected_components_mask(sub_mask, poset, complement=True)
    if len(ser_parts) > 1:
        ordered = _order_series_components(ser_parts, sub_mask, poset)
        return PosetCotreeNode(
            kind=PosetModuleKind.SERIES,
            concurrency_mode=ConcurrencyMode.SEQUENTIAL,
            children=tuple(_decompose_subset(part, poset) for part in ordered),
        )

    modules = _maximal_proper_modules(sub_mask, poset)
    witness = _find_induced_n_in_mask(sub_mask, poset)
    cover_edges = _quotient_cover_edges(modules, poset)
    return PosetCotreeNode(
        kind=PosetModuleKind.PRIME,
        concurrency_mode=ConcurrencyMode.WEAK_SEQUENCING,
        children=tuple(_decompose_subset(mod, poset) for mod in modules),
        prime_child_edges=cover_edges,
        induced_n_witness=witness,
    )


def decompose_causal_poset(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> PosetCotreeNode:
    """Decompose a causal DAG into its canonical Series / Parallel / Prime modular cotree."""
    poset = _build_causal_poset(nodes, edges)
    full_mask = (1 << len(poset.node_ids)) - 1
    return _decompose_subset(full_mask, poset)


def _prime_reachability(
    child_count: int,
    cover_edges: tuple[tuple[int, int], ...],
) -> list[int]:
    reach = [0] * child_count
    for src, dst in cover_edges:
        reach[src] |= 1 << dst
    for mid in range(child_count):
        mid_bit = 1 << mid
        mid_reach = reach[mid]
        for i in range(child_count):
            if reach[i] & mid_bit:
                reach[i] |= mid_reach
    return reach


def _add_cartesian_pairs(
    target: set[tuple[str, str]],
    earlier: frozenset[str],
    later: frozenset[str],
) -> None:
    for u in earlier:
        for v in later:
            target.add((u, v))


def _collect_series_pairs(child_leaves: list[frozenset[str]]) -> set[tuple[str, str]]:
    cross: set[tuple[str, str]] = set()
    for i, earlier in enumerate(child_leaves):
        for later in child_leaves[i + 1 :]:
            _add_cartesian_pairs(cross, earlier, later)
    return cross


def _collect_prime_pairs(
    child_leaves: list[frozenset[str]],
    cover_edges: tuple[tuple[int, int], ...],
) -> set[tuple[str, str]]:
    cross: set[tuple[str, str]] = set()
    reach = _prime_reachability(len(child_leaves), cover_edges)
    for i, earlier in enumerate(child_leaves):
        for j, later in enumerate(child_leaves):
            if (reach[i] >> j) & 1:
                _add_cartesian_pairs(cross, earlier, later)
    return cross


def _collect_cross_pairs(
    child_leaves: list[frozenset[str]],
    node: PosetCotreeNode,
) -> set[tuple[str, str]]:
    if node.kind is PosetModuleKind.SERIES:
        return _collect_series_pairs(child_leaves)
    if node.kind is PosetModuleKind.PRIME:
        return _collect_prime_pairs(child_leaves, node.prime_child_edges)
    return set()


def _reconstruct_recursive(node: PosetCotreeNode) -> tuple[frozenset[str], set[tuple[str, str]]]:
    if node.kind is PosetModuleKind.LEAF and node.node_id is not None:
        return frozenset((node.node_id,)), set()

    child_leaves: list[frozenset[str]] = []
    all_leaves: set[str] = set()
    pairs: set[tuple[str, str]] = set()

    for child in node.children:
        c_leaves, c_pairs = _reconstruct_recursive(child)
        child_leaves.append(c_leaves)
        all_leaves.update(c_leaves)
        pairs.update(c_pairs)

    pairs.update(_collect_cross_pairs(child_leaves, node))
    return frozenset(all_leaves), pairs


def reconstruct_causal_order(root: PosetCotreeNode) -> frozenset[tuple[str, str]]:
    """Reconstruct the exact strict partial order (u < v pairs) represented by a PosetCotreeNode."""
    _, pairs = _reconstruct_recursive(root)
    return frozenset(pairs)


class CausalTraceEvent(Protocol):
    """Minimal structural contract for causal trace ordering and sibling concurrency grouping."""

    @property
    def event_id(self) -> str: ...

    @property
    def caller_id(self) -> str: ...

    @property
    def callee_id(self) -> str: ...

    @property
    def parent_event_id(self) -> str | None: ...

    @property
    def causal_links(self) -> tuple[str, ...]: ...

    @property
    def clock_domain_id(self) -> str: ...

    @property
    def start_ns(self) -> int: ...

    @property
    def end_ns(self) -> int: ...


def _event_heap_key(eid: str, event: CausalTraceEvent) -> tuple[int, int, str, str, str]:
    return (event.start_ns, event.end_ns, event.caller_id, event.callee_id, eid)


def _collect_in_scope_preds(
    eid: str,
    event: CausalTraceEvent,
    events: Mapping[str, CausalTraceEvent],
) -> set[str]:
    preds: set[str] = {link for link in event.causal_links if link in events and link != eid}
    parent_id = event.parent_event_id
    if parent_id is not None and parent_id in events and parent_id != eid:
        preds.add(parent_id)
    return preds


def _build_trace_dependency_graph(
    events: Mapping[str, CausalTraceEvent],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    dependents: dict[str, list[str]] = {eid: [] for eid in events}
    indegree: dict[str, int] = dict.fromkeys(events, 0)

    for eid, event in events.items():
        preds = _collect_in_scope_preds(eid, event, events)
        indegree[eid] = len(preds)
        for pred in preds:
            dependents[pred].append(eid)

    return dependents, indegree


def _pop_or_break_cycle(
    heap: list[tuple[int, int, str, str, str]],
    events: Mapping[str, CausalTraceEvent],
    emitted: set[str],
) -> str | None:
    while heap:
        _, _, _, _, eid = heapq.heappop(heap)
        if eid not in emitted:
            return eid
    remaining = [eid for eid in events if eid not in emitted]
    if not remaining:
        return None
    return min(remaining, key=lambda eid: _event_heap_key(eid, events[eid]))


def _push_ready_dependents(
    dep_ids: Sequence[str],
    indegree: dict[str, int],
    emitted: set[str],
    events: Mapping[str, CausalTraceEvent],
    heap: list[tuple[int, int, str, str, str]],
) -> None:
    for dep_id in dep_ids:
        if dep_id in emitted:
            continue
        indegree[dep_id] -= 1
        if indegree[dep_id] == 0:
            heapq.heappush(heap, _event_heap_key(dep_id, events[dep_id]))


def order_trace_events[TEvent: CausalTraceEvent](
    events: Mapping[str, TEvent],
) -> list[TEvent]:
    """Deterministically topologically order trace events by parent_event_id and causal_links.

    Dangling predecessor references (not present in ``events``) are safely ignored. Ready events
    are popped via min-heap on ``(start_ns, end_ns, caller_id, callee_id, event_id)``, and any
    causal cycle in unvalidated mappings is broken deterministically under the same key.
    """
    if not events:
        return []

    dependents, indegree = _build_trace_dependency_graph(events)
    heap: list[tuple[int, int, str, str, str]] = [
        _event_heap_key(eid, ev) for eid, ev in events.items() if indegree[eid] == 0
    ]
    heapq.heapify(heap)

    emitted: set[str] = set()
    ordered: list[TEvent] = []
    while len(ordered) < len(events):
        eid = _pop_or_break_cycle(heap, events, emitted)
        if eid is None:
            break
        emitted.add(eid)
        ordered.append(events[eid])
        _push_ready_dependents(dependents[eid], indegree, emitted, events, heap)
    return ordered


def events_overlap(first: CausalTraceEvent, second: CausalTraceEvent) -> bool:
    """Return True iff two events share a clock_domain_id and have overlapping intervals."""
    if first.clock_domain_id != second.clock_domain_id:
        return False
    if first.start_ns == second.start_ns:
        return True
    return max(first.start_ns, second.start_ns) < min(first.end_ns, second.end_ns)


def _can_join_sibling_group(
    candidate: CausalTraceEvent,
    group: Sequence[CausalTraceEvent],
    group_ids: set[str],
    group_links: set[str],
) -> bool:
    anchor = group[0]
    if candidate.parent_event_id != anchor.parent_event_id:
        return False
    if candidate.clock_domain_id != anchor.clock_domain_id:
        return False
    if (set(candidate.causal_links) & group_ids) or (candidate.event_id in group_links):
        return False
    return any(events_overlap(member, candidate) for member in group)


def find_causal_overlap_span(
    ordered: Sequence[CausalTraceEvent],
    start_idx: int,
) -> int:
    """Return exclusive end index of the maximal causally-independent concurrent sibling span.

    Starting at ``start_idx``, extends while subsequent events share ``parent_event_id`` and
    ``clock_domain_id``, overlap at least one member already in the span, and introduce no
    ``causal_links`` dependency with any member already in the span.
    """
    if start_idx < 0 or start_idx >= len(ordered):
        msg = f"start_idx {start_idx} out of bounds for sequence of length {len(ordered)}"
        raise IndexError(msg)

    first = ordered[start_idx]
    group: list[CausalTraceEvent] = [first]
    group_ids: set[str] = {first.event_id}
    group_links: set[str] = set(first.causal_links)

    idx = start_idx + 1
    while idx < len(ordered):
        candidate = ordered[idx]
        if not _can_join_sibling_group(candidate, group, group_ids, group_links):
            break
        group.append(candidate)
        group_ids.add(candidate.event_id)
        group_links.update(candidate.causal_links)
        idx += 1
    return idx


def group_concurrent_siblings[TEvent: CausalTraceEvent](
    ordered: Sequence[TEvent],
) -> list[list[TEvent]]:
    """Partition topologically ordered events into adjacent concurrent sibling groups.

    Every group enforces ``same_parent``, ``same_clock_domain``, overlapping execution
    intervals, and mutual causal independence (no ``causal_links`` edges inside a group).
    """
    if not ordered:
        return []

    groups: list[list[TEvent]] = []
    idx = 0
    while idx < len(ordered):
        end_idx = find_causal_overlap_span(ordered, idx)
        groups.append(list(ordered[idx:end_idx]))
        idx = end_idx
    return groups
