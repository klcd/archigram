"""Performance benchmark suite for Compact Graph & Concurrent Trace Digest Generator."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    EventStatus,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.features.digest import GraphDigestGenerator

_BENCH_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_digest_fixture",
    input_sha256="b" * 64,
    file_path="src/cluster.py",
    line_span=(1, 100),
)


def _build_500_node_structural_graph(node_count: int = 500) -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(node_count):
        cluster_idx = idx // 25
        sym_id = f"pkg.cluster_{cluster_idx:02d}.node_{idx:03d}"
        parent_id = (
            f"pkg.cluster_{cluster_idx:02d}.node_{cluster_idx * 25:03d}" if idx % 25 != 0 else None
        )
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=f"Node {idx}",
            kind=NodeKind.SERVICE if idx % 10 != 9 else NodeKind.FUNCTION,
            parent_id=parent_id,
            provenance=_BENCH_STAMP,
        )

    sym_ids = list(nodes)
    edges: list[IREdge] = []
    edge_idx = 0
    for idx in range(node_count):
        # Leave every 10th node (idx % 10 == 9) as a pure leaf sink (out_degree == 0)
        if idx % 10 == 9:
            continue
        # Intra-cluster dense edge + leaf sink logging edge
        cluster_base = (idx // 25) * 25
        intra_target = cluster_base + ((idx + 1) % 25)
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=sym_ids[idx],
                target=sym_ids[intra_target],
                relation=EdgeRelation.CALLS,
                weight=3.0,
                provenance=_BENCH_STAMP,
            )
        )
        edge_idx += 1

        sink_idx = (idx // 10) * 10 + 9
        if sink_idx < node_count:
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=sym_ids[idx],
                    target=sym_ids[sink_idx],
                    relation=EdgeRelation.CALLS,
                    weight=1.0,
                    provenance=_BENCH_STAMP,
                )
            )
            edge_idx += 1

    return StructuralGraphIR(
        graph_id="bench_structural_500",
        nodes=nodes,
        edges=tuple(edges),
    )


def _build_1000_event_concurrent_trace(event_count: int = 1000) -> InteractionTraceIR:
    participant_count = 16
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.worker_{idx}",
            label=f"Worker {idx}",
            kind=NodeKind.SERVICE,
            provenance=_BENCH_STAMP,
        )
        for idx in range(participant_count)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(event_count):
        eid = f"evt_{idx:04d}"
        group_root_idx = (idx // 10) * 10
        parent_id = f"evt_{group_root_idx:04d}" if idx > group_root_idx else None
        # Create overlapping concurrent sibling spans within each 10-event group
        is_fanout_group = (group_root_idx // 10) % 2 == 0
        start_ns = group_root_idx * 1000 if is_fanout_group else idx * 1000
        end_ns = start_ns + 800
        is_recursive = idx % 25 == 0
        caller_id = f"p_{idx % participant_count}"
        callee_id = caller_id if is_recursive else f"p_{(idx + 1) % participant_count}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=caller_id,
            callee_id=callee_id,
            signature=f"op_{idx % 5}()",
            parent_event_id=parent_id,
            clock_domain_id=f"clk_{group_root_idx % 4}",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.FORK_JOIN_BRANCH if is_fanout_group else SyncKind.SYNC,
            status=EventStatus.ERROR if idx % 40 == 0 else EventStatus.OK,
            provenance=_BENCH_STAMP,
        )

    return InteractionTraceIR(
        trace_id="bench_trace_1000",
        participants=participants,
        events=events,
    )


_GENERATOR = GraphDigestGenerator()
_STRUCTURAL_500 = _build_500_node_structural_graph(500)
_TRACE_1000 = _build_1000_event_concurrent_trace(1000)


def test_digest_structural_500_nodes_benchmark(benchmark: Any) -> None:
    """Benchmark StructuralDigest + compact Markdown generation on a 500-node graph."""

    def _run() -> tuple[int, str]:
        digest = _GENERATOR.digest_structural(_STRUCTURAL_500)
        return digest.node_count, digest.to_compact_markdown()

    count, markdown = benchmark(_run)
    assert count == 500
    assert 0 < len(markdown) <= 12_000


def test_digest_trace_1000_events_benchmark(benchmark: Any) -> None:
    """Benchmark TraceDigest + compact Markdown generation on a 1,000-event concurrent trace."""

    def _run() -> tuple[int, str]:
        digest = _GENERATOR.digest_trace(_TRACE_1000)
        return digest.event_count, digest.to_compact_markdown()

    count, markdown = benchmark(_run)
    assert count == 1000
    assert 0 < len(markdown) <= 12_000
