"""Hypothesis property-based fuzz suite for R5/R6 concurrency clustering and SP-Poset cotrees."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from archigram.domain import NonBlankStr, ProvenanceGrade, ProvenanceStamp, validate_non_blank
from archigram.domain.concurrency import (
    ParallelSiblingCluster,
    PosetCotreeNode,
    PosetModuleKind,
    SpanInterval,
    cluster_overlapping_siblings,
    decompose_causal_poset,
    events_overlap,
    find_causal_overlap_span,
    find_induced_n_witness,
    fold_parallel_map,
    group_concurrent_siblings,
    order_trace_events,
    reconstruct_causal_order,
)
from archigram.domain.ir import EventStatus, SyncKind, TraceEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

_FUZZ_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_concurrency",
    input_sha256="a" * 64,
)


def _reference_transitive_closure(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> frozenset[tuple[str, str]]:
    """Independent BFS transitive closure oracle for cross-checking bitmask algorithms."""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        adj[src].append(dst)

    pairs: set[tuple[str, str]] = set()
    for start in nodes:
        visited: set[str] = set()
        queue: deque[str] = deque(adj[start])
        while queue:
            curr = queue.popleft()
            if curr in visited:
                continue
            visited.add(curr)
            pairs.add((start, curr))
            queue.extend(adj[curr])
    return frozenset(pairs)


def _reference_overlap_components(
    intervals: Sequence[SpanInterval],
    epsilon_ns: int,
) -> set[tuple[str | None, str, frozenset[str]]]:
    """Independent O(n^2) connected-components oracle for R5 interval overlap."""
    adj: dict[str, set[str]] = {span.event_id: set() for span in intervals}
    by_id = {span.event_id: span for span in intervals}

    for i, left in enumerate(intervals):
        for right in intervals[i + 1 :]:
            same_group = (
                left.parent_event_id == right.parent_event_id
                and left.clock_domain_id == right.clock_domain_id
            )
            overlaps = max(left.start_ns, right.start_ns) < min(left.end_ns, right.end_ns) - (
                epsilon_ns
            )
            if same_group and overlaps:
                adj[left.event_id].add(right.event_id)
                adj[right.event_id].add(left.event_id)

    visited: set[str] = set()
    components: set[tuple[str | None, str, frozenset[str]]] = set()
    for span in intervals:
        if span.event_id in visited:
            continue
        comp: set[str] = set()
        queue: deque[str] = deque([span.event_id])
        while queue:
            curr = queue.popleft()
            if curr in comp:
                continue
            comp.add(curr)
            queue.extend(adj[curr] - comp)
        visited.update(comp)
        if len(comp) >= 2:
            rep = by_id[span.event_id]
            components.add((rep.parent_event_id, rep.clock_domain_id, frozenset(comp)))
    return components


def _contains_prime(node: PosetCotreeNode) -> bool:
    if node.kind is PosetModuleKind.PRIME:
        return True
    return any(_contains_prime(child) for child in node.children)


def _collect_leaves(node: PosetCotreeNode) -> list[str]:
    if node.kind is PosetModuleKind.LEAF and node.node_id is not None:
        return [node.node_id]
    leaves: list[str] = []
    for child in node.children:
        leaves.extend(_collect_leaves(child))
    return leaves


@st.composite
def random_span_intervals(draw: st.DrawFn) -> list[SpanInterval]:
    """Generate sibling spans across multiple clock domains, parents, and durations."""
    count = draw(st.integers(min_value=0, max_value=24))
    domains = draw(st.lists(st.sampled_from(["hostA:1", "hostB:2", "hostC:3"]), min_size=1))
    parents = [None, "root_0", "root_1"]
    callees = ["svc_inventory", "svc_pricing", "svc_fraud"]
    sigs = ["GET /item/{id}", "POST /check"]

    spans: list[SpanInterval] = []
    for idx in range(count):
        start_ns = draw(st.integers(min_value=0, max_value=120))
        duration = draw(st.integers(min_value=0, max_value=45))
        spans.append(
            SpanInterval(
                event_id=f"ev_{idx:02d}",
                parent_event_id=draw(st.sampled_from(parents)),
                clock_domain_id=draw(st.sampled_from(domains)),
                start_ns=start_ns,
                end_ns=start_ns + duration,
                callee_id=draw(st.sampled_from(callees)),
                signature=draw(st.sampled_from(sigs)),
            )
        )
    return spans


@st.composite
def random_causal_dags(draw: st.DrawFn) -> tuple[list[str], list[tuple[str, str]]]:
    """Generate arbitrary finite DAGs over 1..12 nodes with varied edge densities."""
    node_count = draw(st.integers(min_value=1, max_value=12))
    nodes = [f"n{i:02d}" for i in range(node_count)]
    possible_edges = [
        (nodes[i], nodes[j]) for i in range(node_count) for j in range(i + 1, node_count)
    ]
    if not possible_edges:
        return nodes, []
    density = draw(st.floats(min_value=0.0, max_value=0.75))
    edges: list[tuple[str, str]] = []
    for edge in possible_edges:
        if draw(st.floats(min_value=0.0, max_value=1.0)) < density:
            edges.append(edge)
    return nodes, edges


@st.composite
def random_series_parallel_dags(draw: st.DrawFn) -> tuple[list[str], list[tuple[str, str]]]:
    """Generate guaranteed N-free Series-Parallel DAGs recursively."""
    counter = [0]

    def _build(depth: int) -> tuple[list[str], list[tuple[str, str]]]:
        if depth <= 0 or draw(st.booleans()):
            node_id = f"sp_{counter[0]:02d}"
            counter[0] += 1
            return [node_id], []

        branch_count = draw(st.integers(min_value=2, max_value=3))
        parts = [_build(depth - 1) for _ in range(branch_count)]
        is_series = draw(st.booleans())
        nodes = [n for p_nodes, _ in parts for n in p_nodes]
        edges = [e for _, p_edges in parts for e in p_edges]
        if is_series:
            for idx in range(len(parts) - 1):
                for u in parts[idx][0]:
                    for v in parts[idx + 1][0]:
                        edges.append((u, v))
        return nodes, edges

    return _build(draw(st.integers(min_value=0, max_value=3)))


@settings(max_examples=120)
@given(
    durations=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=20),
    gaps=st.lists(st.integers(min_value=0, max_value=20), min_size=20, max_size=20),
    epsilon_ns=st.integers(min_value=0, max_value=10),
)
def test_disjoint_sequential_siblings_never_cluster(
    durations: list[int],
    gaps: list[int],
    epsilon_ns: int,
) -> None:
    cursor = 0
    intervals: list[SpanInterval] = []
    for idx, dur in enumerate(durations):
        start = cursor
        end = start + dur
        intervals.append(
            SpanInterval(
                event_id=f"seq_{idx:02d}",
                parent_event_id="parent_root",
                clock_domain_id="hostA:1",
                start_ns=start,
                end_ns=end,
            )
        )
        cursor = end + gaps[idx]

    clusters = cluster_overlapping_siblings(intervals, epsilon_ns=epsilon_ns)
    assert clusters == ()


@settings(max_examples=120)
@given(
    count=st.integers(min_value=2, max_value=16),
    base_start=st.integers(min_value=0, max_value=1000),
    overlap_width=st.integers(min_value=1, max_value=200),
    epsilon_ns=st.integers(min_value=0, max_value=50),
    left_offsets=st.lists(st.integers(min_value=0, max_value=100), min_size=16, max_size=16),
    right_offsets=st.lists(st.integers(min_value=0, max_value=100), min_size=16, max_size=16),
)
def test_mutually_overlapping_same_clock_siblings_share_single_cluster(
    count: int,
    base_start: int,
    overlap_width: int,
    epsilon_ns: int,
    left_offsets: list[int],
    right_offsets: list[int],
) -> None:
    # Every interval contains [core_start, core_end] with core width = overlap_width + epsilon_ns.
    core_start = base_start + 100
    core_end = core_start + overlap_width + epsilon_ns
    intervals = [
        SpanInterval(
            event_id=f"par_{idx:02d}",
            parent_event_id="parent_root",
            clock_domain_id="hostA:1",
            start_ns=core_start - left_offsets[idx],
            end_ns=core_end + right_offsets[idx],
        )
        for idx in range(count)
    ]

    clusters = cluster_overlapping_siblings(intervals, epsilon_ns=epsilon_ns)
    assert len(clusters) == 1
    assert set(clusters[0].event_ids) == {span.event_id for span in intervals}
    assert clusters[0].clock_domain_id == "hostA:1"


@settings(max_examples=120)
@given(
    count=st.integers(min_value=2, max_value=15),
    start_ns=st.integers(min_value=0, max_value=500),
    duration=st.integers(min_value=10, max_value=500),
)
def test_distinct_clock_domains_never_cross_cluster(
    count: int,
    start_ns: int,
    duration: int,
) -> None:
    intervals = [
        SpanInterval(
            event_id=f"iso_{idx:02d}",
            parent_event_id="parent_root",
            clock_domain_id=f"host_{idx}:pid1",
            start_ns=start_ns,
            end_ns=start_ns + duration,
        )
        for idx in range(count)
    ]
    assert cluster_overlapping_siblings(intervals, epsilon_ns=0) == ()


@settings(max_examples=150)
@given(
    intervals=random_span_intervals(),
    epsilon_ns=st.integers(min_value=0, max_value=25),
    perm=st.data(),
)
def test_sweep_line_matches_quadratic_oracle_and_is_order_invariant(
    intervals: list[SpanInterval],
    epsilon_ns: int,
    perm: st.DataObject,
) -> None:
    clusters: tuple[ParallelSiblingCluster, ...] = cluster_overlapping_siblings(
        intervals,
        epsilon_ns=epsilon_ns,
    )
    observed = {(c.parent_event_id, c.clock_domain_id, frozenset(c.event_ids)) for c in clusters}
    expected = _reference_overlap_components(intervals, epsilon_ns=epsilon_ns)
    assert observed == expected

    shuffled = perm.draw(st.permutations(intervals))
    assert cluster_overlapping_siblings(shuffled, epsilon_ns=epsilon_ns) == clusters


@settings(max_examples=100)
@given(
    intervals=random_span_intervals(),
    max_par_branches=st.integers(min_value=2, max_value=6),
    epsilon_ns=st.integers(min_value=0, max_value=10),
)
def test_r6_parallel_map_preserves_backing_ids_and_domain_isolation(
    intervals: list[SpanInterval],
    max_par_branches: int,
    epsilon_ns: int,
) -> None:
    by_id = {span.event_id: span for span in intervals}
    summaries = fold_parallel_map(
        intervals,
        max_par_branches=max_par_branches,
        epsilon_ns=epsilon_ns,
    )

    for summary in summaries:
        assert summary.concurrency_count >= max_par_branches
        assert summary.concurrency_count == len(summary.backing_ids)
        assert len(set(summary.backing_ids)) == summary.concurrency_count
        for eid in summary.backing_ids:
            span = by_id[eid]
            assert span.parent_event_id == summary.parent_event_id
            assert span.clock_domain_id == summary.clock_domain_id
            assert span.callee_id == summary.callee_id
            assert span.signature == summary.signature


@settings(max_examples=200)
@given(dag=random_causal_dags(), perm=st.data())
def test_grabowski_valdes_tarjan_lawler_theorem_and_lossless_reachability(
    dag: tuple[list[str], list[tuple[str, str]]],
    perm: st.DataObject,
) -> None:
    nodes, edges = dag
    cotree = decompose_causal_poset(nodes, edges)
    witness = find_induced_n_witness(nodes, edges)
    has_prime = _contains_prime(cotree)

    # Grabowski / Valdes-Tarjan-Lawler exact N-free characterization:
    assert has_prime == (witness is not None)

    # Lossless causal reachability preservation (exact bijective equality):
    expected_closure = _reference_transitive_closure(nodes, edges)
    reconstructed = reconstruct_causal_order(cotree)
    assert reconstructed == expected_closure

    # Exact leaf partition (every node appears once):
    leaves = _collect_leaves(cotree)
    assert len(leaves) == len(nodes)
    assert set(leaves) == set(nodes)

    # If a witness exists, verify it is an exact induced-N (a<c, b<c, b<d; no other relations):
    if witness is not None:
        a, b, c, d = witness
        assert len({a, b, c, d}) == 4
        assert (a, c) in expected_closure
        assert (b, c) in expected_closure
        assert (b, d) in expected_closure
        for x, y in ((a, b), (b, a), (c, d), (d, c), (a, d), (d, a)):
            assert (x, y) not in expected_closure

    # Canonical permutation invariance and JSON roundtrip:
    shuffled_nodes = perm.draw(st.permutations(nodes))
    shuffled_edges = perm.draw(st.permutations(edges))
    assert decompose_causal_poset(shuffled_nodes, shuffled_edges) == cotree
    assert find_induced_n_witness(shuffled_nodes, shuffled_edges) == witness
    assert PosetCotreeNode.model_validate_json(cotree.model_dump_json()) == cotree


@settings(max_examples=120)
@given(dag=random_series_parallel_dags())
def test_constructed_series_parallel_dags_never_produce_prime_nodes(
    dag: tuple[list[str], list[tuple[str, str]]],
) -> None:
    nodes, edges = dag
    cotree = decompose_causal_poset(nodes, edges)
    assert not _contains_prime(cotree)
    assert find_induced_n_witness(nodes, edges) is None
    assert reconstruct_causal_order(cotree) == _reference_transitive_closure(nodes, edges)


def _make_chain_or_parallel_module(
    prefix: str,
    size: int,
    is_chain: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    nodes = [f"{prefix}_{idx}" for idx in range(size)]
    if not is_chain or size < 2:
        return nodes, []
    return nodes, [(nodes[i], nodes[i + 1]) for i in range(size - 1)]


@settings(max_examples=120)
@given(
    sizes=st.tuples(
        st.integers(min_value=1, max_value=3),
        st.integers(min_value=1, max_value=3),
        st.integers(min_value=1, max_value=3),
        st.integers(min_value=1, max_value=3),
    ),
    chains=st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans()),
    add_entry=st.booleans(),
    add_parallel_sibling=st.booleans(),
)
def test_embedded_induced_n_modules_always_yield_prime_and_exact_witness(
    sizes: tuple[int, int, int, int],
    chains: tuple[bool, bool, bool, bool],
    add_entry: bool,
    add_parallel_sibling: bool,
) -> None:
    mod_a, edges_a = _make_chain_or_parallel_module("a", sizes[0], chains[0])
    mod_b, edges_b = _make_chain_or_parallel_module("b", sizes[1], chains[1])
    mod_c, edges_c = _make_chain_or_parallel_module("c", sizes[2], chains[2])
    mod_d, edges_d = _make_chain_or_parallel_module("d", sizes[3], chains[3])

    nodes = [*mod_a, *mod_b, *mod_c, *mod_d]
    edges: list[tuple[str, str]] = [*edges_a, *edges_b, *edges_c, *edges_d]
    for src_mod, dst_mod in ((mod_a, mod_c), (mod_b, mod_c), (mod_b, mod_d)):
        edges.extend((u, v) for u in src_mod for v in dst_mod)

    if add_entry:
        nodes.append("entry")
        edges.extend(("entry", u) for u in (*mod_a, *mod_b))
    if add_parallel_sibling:
        nodes.append("independent_bg")

    cotree = decompose_causal_poset(nodes, edges)
    witness = find_induced_n_witness(nodes, edges)
    expected_closure = _reference_transitive_closure(nodes, edges)

    assert _contains_prime(cotree)
    assert witness is not None
    assert reconstruct_causal_order(cotree) == expected_closure

    a, b, c, d = witness
    assert (a, c) in expected_closure
    assert (b, c) in expected_closure
    assert (b, d) in expected_closure
    for x, y in ((a, b), (b, a), (c, d), (d, c), (a, d), (d, a)):
        assert (x, y) not in expected_closure


@st.composite
def random_skewed_causal_trace_events(
    draw: st.DrawFn,
    *,
    allow_dangling: bool = True,
) -> dict[str, TraceEvent]:
    """Generate TraceEvent DAGs with skewed clocks, causal_links, and zero-dur spans."""
    count = draw(st.integers(min_value=0, max_value=18))
    if count == 0:
        return {}

    clocks = ("host_a:1", "host_b:2", "host_c:3")
    participants = ("api", "auth", "orders", "billing", "db")
    event_ids = [f"tr_{idx:02d}" for idx in range(count)]
    skew_by_clock = {clk: draw(st.integers(min_value=-200, max_value=500)) for clk in clocks}

    events: dict[str, TraceEvent] = {}
    for idx, eid in enumerate(event_ids):
        earlier = event_ids[:idx]
        parent_id = draw(st.sampled_from(earlier)) if earlier and draw(st.booleans()) else None
        links: list[str] = (
            draw(st.lists(st.sampled_from(earlier), max_size=3, unique=True)) if earlier else []
        )
        if allow_dangling and draw(st.booleans()):
            links.append(f"dangling_link_{idx}")
        if allow_dangling and parent_id is None and draw(st.integers(0, 4)) == 0:
            parent_id = f"dangling_parent_{idx}"

        clock = draw(st.sampled_from(clocks))
        raw_start = draw(st.integers(min_value=0, max_value=120)) + skew_by_clock[clock]
        start_ns = max(0, raw_start)
        dur = 0 if draw(st.integers(0, 5)) == 0 else draw(st.integers(min_value=1, max_value=80))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=draw(st.sampled_from(participants)),
            callee_id=draw(st.sampled_from(participants)),
            signature=f"op_{idx % 4}()",
            parent_event_id=parent_id,
            causal_links=tuple(links),
            clock_domain_id=clock,
            start_ns=start_ns,
            end_ns=start_ns + dur,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=EventStatus.OK,
            provenance=_FUZZ_STAMP,
        )
    return events


def _assert_topological_order(
    ordered: Sequence[TraceEvent],
    events: dict[str, TraceEvent],
) -> None:
    pos = {ev.event_id: idx for idx, ev in enumerate(ordered)}
    assert len(ordered) == len(events)
    assert set(pos) == set(events)
    for idx, ev in enumerate(ordered):
        if ev.parent_event_id is not None and ev.parent_event_id in events:
            assert pos[ev.parent_event_id] < idx
        for link in ev.causal_links:
            if link in events:
                assert pos[link] < idx


def _assert_group_invariants(group: Sequence[TraceEvent]) -> None:
    anchor = group[0]
    member_ids = {ev.event_id for ev in group}
    for ev in group:
        assert ev.parent_event_id == anchor.parent_event_id
        assert ev.clock_domain_id == anchor.clock_domain_id
        assert ev.parent_event_id not in member_ids
        assert not (set(ev.causal_links) & member_ids)
    if len(group) >= 2:
        # Every member after the first overlaps at least one earlier member in the group.
        for idx in range(1, len(group)):
            assert any(events_overlap(group[prev], group[idx]) for prev in range(idx))


@settings(max_examples=150)
@given(events=random_skewed_causal_trace_events(allow_dangling=True), perm=st.data())
def test_order_trace_events_honors_dag_skewed_clocks_and_is_order_invariant(
    events: dict[str, TraceEvent],
    perm: st.DataObject,
) -> None:
    ordered = order_trace_events(events)
    _assert_topological_order(ordered, events)

    shuffled_keys = perm.draw(st.permutations(list(events.keys())))
    shuffled = {key: events[key] for key in shuffled_keys}
    assert order_trace_events(shuffled) == ordered


@settings(max_examples=100)
@given(
    cycle_len=st.integers(min_value=2, max_value=6),
    extra_count=st.integers(min_value=0, max_value=4),
    perm=st.data(),
)
def test_order_trace_events_cycle_safe_and_deterministic(
    cycle_len: int,
    extra_count: int,
    perm: st.DataObject,
) -> None:
    events: dict[str, TraceEvent] = {}
    for idx in range(cycle_len):
        eid = f"cyc_{idx}"
        prev_id = f"cyc_{(idx - 1) % cycle_len}"
        start_ns = perm.draw(st.integers(min_value=0, max_value=200))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="svc_a",
            callee_id="svc_b",
            signature="call()",
            causal_links=(prev_id,),
            clock_domain_id=f"clk_{idx % 2}",
            start_ns=start_ns,
            end_ns=start_ns + 20,
            provenance=_FUZZ_STAMP,
        )
    for idx in range(extra_count):
        eid = f"tail_{idx}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="svc_b",
            callee_id="svc_c",
            signature="after()",
            causal_links=("cyc_0",),
            clock_domain_id="clk_0",
            start_ns=idx * 5,
            end_ns=idx * 5 + 10,
            provenance=_FUZZ_STAMP,
        )

    ordered = order_trace_events(events)
    assert len(ordered) == len(events)
    assert {ev.event_id for ev in ordered} == set(events)

    shuffled_keys = perm.draw(st.permutations(list(events.keys())))
    shuffled = {key: events[key] for key in shuffled_keys}
    assert order_trace_events(shuffled) == ordered


@settings(max_examples=150)
@given(events=random_skewed_causal_trace_events(allow_dangling=False))
def test_group_concurrent_siblings_and_span_enforce_causality_and_clock_isolation(
    events: dict[str, TraceEvent],
) -> None:
    ordered = order_trace_events(events)
    groups = group_concurrent_siblings(ordered)

    flattened = [ev for group in groups for ev in group]
    assert flattened == ordered

    cursor = 0
    for group in groups:
        assert len(group) >= 1
        end_idx = find_causal_overlap_span(ordered, cursor)
        assert ordered[cursor:end_idx] == group
        _assert_group_invariants(group)
        cursor = end_idx
    assert cursor == len(ordered)


@settings(max_examples=80)
@given(
    start_a=st.integers(min_value=0, max_value=200),
    dur_a=st.integers(min_value=20, max_value=100),
    dur_b=st.integers(min_value=20, max_value=100),
    same_clock=st.booleans(),
    has_causal_link=st.booleans(),
)
def test_causal_link_or_clock_mismatch_prevents_concurrent_merge(
    start_a: int,
    dur_a: int,
    dur_b: int,
    same_clock: bool,
    has_causal_link: bool,
) -> None:
    ev_a = TraceEvent(
        event_id="ev_a",
        caller_id="api",
        callee_id="svc_a",
        signature="step_a()",
        clock_domain_id="host_0",
        start_ns=start_a,
        end_ns=start_a + dur_a,
        provenance=_FUZZ_STAMP,
    )
    ev_b = TraceEvent(
        event_id="ev_b",
        caller_id="api",
        callee_id="svc_b",
        signature="step_b()",
        causal_links=("ev_a",) if has_causal_link else (),
        clock_domain_id="host_0" if same_clock else "host_1",
        start_ns=start_a + 1,
        end_ns=start_a + 1 + dur_b,
        provenance=_FUZZ_STAMP,
    )
    ordered = order_trace_events({"ev_b": ev_b, "ev_a": ev_a})
    groups = group_concurrent_siblings(ordered)

    if same_clock and not has_causal_link:
        assert len(groups) == 1
        assert [ev.event_id for ev in groups[0]] == ["ev_a", "ev_b"]
    else:
        assert len(groups) == 2
        assert [ev.event_id for ev in groups[0]] == ["ev_a"]
        assert [ev.event_id for ev in groups[1]] == ["ev_b"]


@settings(max_examples=120)
@given(raw=st.text(min_size=0, max_size=24))
def test_validate_non_blank_and_non_blank_str_roundtrip(raw: str) -> None:
    adapter: TypeAdapter[str] = TypeAdapter(NonBlankStr)
    if raw.strip():
        assert validate_non_blank(raw) == raw
        assert adapter.validate_python(raw) == raw
    else:
        with pytest.raises(ValueError):
            validate_non_blank(raw)
        with pytest.raises(ValidationError):
            adapter.validate_python(raw)
