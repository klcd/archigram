"""Performance benchmarks for R5 sibling overlap sweep-line and 100-node SP-Poset decomposition."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.concurrency import (
    ParallelSiblingCluster,
    PosetCotreeNode,
    PosetModuleKind,
    SpanInterval,
    cluster_overlapping_siblings,
    decompose_causal_poset,
    group_concurrent_siblings,
    order_trace_events,
)
from archigram.domain.ir import EventStatus, SyncKind, TraceEvent


def _build_1000_sibling_spans() -> list[SpanInterval]:
    """Build 1,000 realistic sibling spans across 4 clock domains and 25 fan-out waves."""
    spans: list[SpanInterval] = []
    domains = ("host-a:101", "host-b:202", "host-c:303", "host-d:404")
    for idx in range(1000):
        wave = idx // 10
        offset_in_wave = idx % 10
        domain = domains[wave % len(domains)]
        parent = f"req_{wave // 5:02d}"
        start_ns = wave * 1_000_000 + offset_in_wave * 20_000
        end_ns = start_ns + 250_000
        spans.append(
            SpanInterval(
                event_id=f"span_{idx:04d}",
                parent_event_id=parent,
                clock_domain_id=domain,
                start_ns=start_ns,
                end_ns=end_ns,
                callee_id=f"svc_{offset_in_wave % 3}",
                signature=f"GET /v1/shard/{offset_in_wave % 2}",
            )
        )
    return spans


def _build_100_node_fork_join_dag() -> tuple[list[str], list[tuple[str, str]]]:
    """Build a 100-node nested Series-Parallel fork-join DAG (1 entry + 9 x 11-node stages)."""
    nodes: list[str] = ["entry"]
    edges: list[tuple[str, str]] = []
    prev_join = "entry"

    for stage in range(9):
        join_id = f"s{stage}_join"
        nodes.append(join_id)

        # 4 two-step serial branches in parallel (8 nodes)
        for branch in range(4):
            first = f"s{stage}_b{branch}_step1"
            second = f"s{stage}_b{branch}_step2"
            nodes.extend((first, second))
            edges.append((prev_join, first))
            edges.append((first, second))
            edges.append((second, join_id))

        # 2 single-step parallel branches (2 nodes)
        for fast in range(2):
            leaf = f"s{stage}_fast{fast}"
            nodes.append(leaf)
            edges.append((prev_join, leaf))
            edges.append((leaf, join_id))

        prev_join = join_id

    return nodes, edges


def _count_leaves_and_primes(node: PosetCotreeNode) -> tuple[int, int]:
    if node.kind is PosetModuleKind.LEAF:
        return 1, 0
    leaf_total = 0
    prime_total = 1 if node.kind is PosetModuleKind.PRIME else 0
    for child in node.children:
        c_leaves, c_primes = _count_leaves_and_primes(child)
        leaf_total += c_leaves
        prime_total += c_primes
    return leaf_total, prime_total


def test_perf_cluster_overlapping_siblings_1000_spans(benchmark: Any) -> None:
    spans = _build_1000_sibling_spans()
    clusters: tuple[ParallelSiblingCluster, ...] = benchmark(
        cluster_overlapping_siblings,
        spans,
        1_000,
    )
    assert len(spans) == 1000
    assert len(clusters) > 0
    assert sum(len(cluster.event_ids) for cluster in clusters) == 1000


def test_perf_decompose_causal_poset_100_node_fork_join_dag(benchmark: Any) -> None:
    nodes, edges = _build_100_node_fork_join_dag()
    cotree: PosetCotreeNode = benchmark(decompose_causal_poset, nodes, edges)
    leaf_count, prime_count = _count_leaves_and_primes(cotree)
    assert len(nodes) == 100
    assert leaf_count == 100
    assert prime_count == 0
    assert cotree.kind is PosetModuleKind.SERIES


def _build_1000_skewed_causal_trace_events() -> dict[str, TraceEvent]:
    """Build 1,000 TraceEvents across 4 skewed clock domains with parents and causal_links."""
    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_concurrency",
        input_sha256="b" * 64,
    )
    domains = ("host-a:101", "host-b:202", "host-c:303", "host-d:404")
    skew_ns = (0, 500_000, -150_000, 1_250_000)
    events: dict[str, TraceEvent] = {}

    for idx in range(1000):
        eid = f"te_{idx:04d}"
        wave = idx // 8
        slot = idx % 8
        domain_idx = (wave + (slot // 4)) % len(domains)
        parent_id = f"te_{wave * 8:04d}" if slot > 0 else None
        links: tuple[str, ...] = ()
        if slot in {2, 6}:
            links = (f"te_{idx - 1:04d}",)
        elif wave > 0 and slot == 0:
            links = (f"te_{(wave - 1) * 8 + 7:04d}",)

        raw_start = wave * 100_000 + slot * 5_000 + skew_ns[domain_idx]
        start_ns = max(0, raw_start)
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"svc_{slot % 4}",
            callee_id=f"svc_{(slot + 1) % 4}",
            signature=f"call_{slot % 3}()",
            parent_event_id=parent_id,
            causal_links=links,
            clock_domain_id=domains[domain_idx],
            start_ns=start_ns,
            end_ns=start_ns + 40_000,
            sync_kind=SyncKind.FORK_JOIN_BRANCH if slot % 2 == 1 else SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=stamp,
        )
    return events


def test_perf_order_trace_events_1000_skewed_causal_events(benchmark: Any) -> None:
    events = _build_1000_skewed_causal_trace_events()
    ordered: list[TraceEvent] = benchmark(order_trace_events, events)
    assert len(ordered) == 1000
    pos = {ev.event_id: idx for idx, ev in enumerate(ordered)}
    for idx, ev in enumerate(ordered):
        if ev.parent_event_id is not None:
            assert pos[ev.parent_event_id] < idx
        for link in ev.causal_links:
            assert pos[link] < idx


def test_perf_group_concurrent_siblings_1000_skewed_causal_events(benchmark: Any) -> None:
    ordered = order_trace_events(_build_1000_skewed_causal_trace_events())
    groups: list[list[TraceEvent]] = benchmark(group_concurrent_siblings, ordered)
    assert sum(len(group) for group in groups) == 1000
    assert any(len(group) > 1 for group in groups)
    for group in groups:
        anchor = group[0]
        ids = {ev.event_id for ev in group}
        assert all(ev.clock_domain_id == anchor.clock_domain_id for ev in group)
        assert all(ev.parent_event_id == anchor.parent_event_id for ev in group)
        assert all(not (set(ev.causal_links) & ids) for ev in group)
