"""Performance benchmark suite for R1-R6 + SP-Poset TraceCompactionEngine on 1,000-span traces."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    InteractionTraceIR,
    NodeKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import TraceCompactionConfig
from archigram.features.reduce_trace import (
    TraceCompactionEngine,
    collect_trace_backing_ids,
)

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="otel_perf",
    input_sha256="b" * 64,
)


def _build_1000_span_concurrent_and_looping_trace() -> InteractionTraceIR:
    """Build a 1,000-span trace combining 500 loop iterations, 9x50 fan-out waves, and posets."""
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=pid,
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )
        for pid in ("gateway", "orchestrator", "worker", "cache", "db")
    }
    events: dict[str, TraceEvent] = {}

    events["root_0"] = TraceEvent(
        event_id="root_0",
        caller_id="gateway",
        callee_id="orchestrator",
        signature="POST /v1/execute",
        parent_event_id=None,
        clock_domain_id="hostA:1",
        start_ns=0,
        end_ns=500_000_000,
        provenance=_OBS_PROV,
    )

    # 500 contiguous sequential loop calls under root_0
    cursor = 1_000
    for idx in range(500):
        eid = f"loop_span_{idx:04d}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="orchestrator",
            callee_id="cache",
            signature="GET /cache/key",
            parent_event_id="root_0",
            clock_domain_id="hostA:1",
            start_ns=cursor,
            end_ns=cursor + 400,
            provenance=_OBS_PROV,
        )
        cursor += 1_000

    # 9 waves of 50-way concurrent homogeneous sibling calls = 450 spans
    for wave in range(9):
        wave_parent = f"wave_parent_{wave:02d}"
        wave_start = cursor + wave * 1_000_000
        events[wave_parent] = TraceEvent(
            event_id=wave_parent,
            caller_id="orchestrator",
            callee_id="orchestrator",
            signature=f"dispatch_wave_{wave}",
            parent_event_id="root_0",
            clock_domain_id="hostA:1",
            start_ns=wave_start,
            end_ns=wave_start + 500_000,
            provenance=_OBS_PROV,
        )
        for branch in range(50):
            bid = f"wave_{wave:02d}_branch_{branch:02d}"
            events[bid] = TraceEvent(
                event_id=bid,
                caller_id="orchestrator",
                callee_id="worker",
                signature="process_partition",
                parent_event_id=wave_parent,
                clock_domain_id=f"host_{wave % 3}:1",
                start_ns=wave_start + 1_000 + branch * 10,
                end_ns=wave_start + 250_000,
                provenance=_OBS_PROV,
            )

    # Remaining 40 spans: 10 x 4-node induced-N / recursive chains to hit exact 1,000 spans
    base_extra = cursor + 20_000_000
    for group in range(10):
        g_start = base_extra + group * 100_000
        a_id = f"n_{group}_a"
        b_id = f"n_{group}_b"
        c_id = f"n_{group}_c"
        d_id = f"n_{group}_d"
        events[a_id] = TraceEvent(
            event_id=a_id,
            caller_id="worker",
            callee_id="db",
            signature="write_tx",
            parent_event_id="root_0",
            clock_domain_id="hostA:1",
            start_ns=g_start,
            end_ns=g_start + 1_000,
            provenance=_OBS_PROV,
        )
        events[b_id] = TraceEvent(
            event_id=b_id,
            caller_id="worker",
            callee_id="cache",
            signature="invalidate",
            parent_event_id="root_0",
            clock_domain_id="hostB:2",
            start_ns=g_start + 100,
            end_ns=g_start + 1_100,
            provenance=_OBS_PROV,
        )
        events[c_id] = TraceEvent(
            event_id=c_id,
            caller_id="db",
            callee_id="worker",
            signature="ack_tx",
            parent_event_id="root_0",
            causal_links=(a_id, b_id),
            clock_domain_id="hostA:1",
            start_ns=g_start + 2_000,
            end_ns=g_start + 3_000,
            provenance=_OBS_PROV,
        )
        events[d_id] = TraceEvent(
            event_id=d_id,
            caller_id="cache",
            callee_id="gateway",
            signature="notify_warm",
            parent_event_id="root_0",
            causal_links=(b_id,),
            clock_domain_id="hostB:2",
            start_ns=g_start + 2_100,
            end_ns=g_start + 3_100,
            provenance=_OBS_PROV,
        )

    return InteractionTraceIR(
        trace_id="perf_1000_span_trace",
        participants=participants,
        events=events,
    )


def test_perf_compact_1000_span_concurrent_and_looping_trace(benchmark: Any) -> None:
    trace = _build_1000_span_concurrent_and_looping_trace()
    assert len(trace.events) == 1000

    config = TraceCompactionConfig(
        fold_loops=True,
        fold_loop_with_opt=True,
        cut_recursion=True,
        fold_parallel_siblings=True,
        parallel_epsilon_ns=1_000,
        fold_parallel_map=True,
        max_par_branches=6,
        decompose_series_parallel=True,
        max_call_depth=8,
    )
    engine = TraceCompactionEngine()
    compacted: InteractionTraceIR = benchmark(engine.compact, trace, config)

    assert len(compacted.events) < 80
    assert collect_trace_backing_ids(compacted) == frozenset(trace.events.keys())
