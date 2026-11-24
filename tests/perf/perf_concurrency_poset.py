"""Performance benchmarks for R5 sibling overlap sweep-line and 100-node SP-Poset decomposition."""

from __future__ import annotations

from typing import Any

from archigram.domain.concurrency import (
    ParallelSiblingCluster,
    PosetCotreeNode,
    PosetModuleKind,
    SpanInterval,
    cluster_overlapping_siblings,
    decompose_causal_poset,
)


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
