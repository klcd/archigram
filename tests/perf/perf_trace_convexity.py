"""Performance benchmarks for convexity-guarded trace compaction."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.concurrency import decompose_causal_poset
from archigram.domain.ir import InteractionTraceIR, NodeKind, TraceEvent, TraceParticipant
from archigram.domain.spec import TraceCompactionConfig
from archigram.features.reduce_trace import TraceCompactionEngine, collect_trace_backing_ids

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf-convexity",
    input_sha256="d" * 64,
)


def _participants() -> dict[str, TraceParticipant]:
    return {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc.{pid}",
            label=pid,
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )
        for pid in ("client", "worker", "ext")
    }


def _build_cyclic_trap_trace() -> InteractionTraceIR:
    """Build a 240+ span trace with non-convex bridges across loops, fanouts, and recursion."""
    events: dict[str, TraceEvent] = {
        "root": TraceEvent(
            event_id="root",
            caller_id="client",
            callee_id="worker",
            signature="run()",
            parent_event_id=None,
            clock_domain_id="clk0",
            start_ns=0,
            end_ns=10_000_000,
            provenance=_OBS_PROV,
        )
    }

    # 10 blocks x 10 loop iterations separated by external cross-iteration bridges = 110 spans
    cursor = 1_000
    for blk in range(10):
        for it in range(10):
            eid = f"lp_{blk}_{it}"
            links = (f"lp_ext_{blk - 1}",) if blk > 0 and it == 0 else ()
            events[eid] = TraceEvent(
                event_id=eid,
                caller_id="worker",
                callee_id="client",
                signature="step()",
                parent_event_id="root",
                causal_links=links,
                clock_domain_id="clk0",
                start_ns=cursor,
                end_ns=cursor + 400,
                provenance=_OBS_PROV,
            )
            cursor += 1_000
        ext_id = f"lp_ext_{blk}"
        events[ext_id] = TraceEvent(
            event_id=ext_id,
            caller_id="ext",
            callee_id="ext",
            signature="bridge()",
            parent_event_id=None,
            causal_links=(f"lp_{blk}_9",),
            clock_domain_id="clk0",
            start_ns=cursor,
            end_ns=cursor + 200,
            provenance=_OBS_PROV,
        )
        cursor += 1_000

    # 3 fanout waves of 40 parallel branches with interleaved external bridges = 123 spans
    for wave in range(3):
        base = cursor + wave * 100_000
        ext_w = f"fan_ext_{wave}"
        events[ext_w] = TraceEvent(
            event_id=ext_w,
            caller_id="ext",
            callee_id="ext",
            signature="fan_bridge()",
            parent_event_id=None,
            causal_links=(f"fan_{wave}_19",),
            clock_domain_id="clk0",
            start_ns=base + 50,
            end_ns=base + 150,
            provenance=_OBS_PROV,
        )
        for branch in range(40):
            bid = f"fan_{wave}_{branch}"
            events[bid] = TraceEvent(
                event_id=bid,
                caller_id="worker",
                callee_id="ext",
                signature="map_item()",
                parent_event_id="root",
                causal_links=(ext_w,) if branch >= 20 else (),
                clock_domain_id="clk0",
                start_ns=base + branch,
                end_ns=base + 50_000 + branch,
                provenance=_OBS_PROV,
            )

    return InteractionTraceIR(
        trace_id="perf_cyclic_traps",
        participants=_participants(),
        events=events,
    )


def _build_1000_span_no_link_fast_path_trace() -> InteractionTraceIR:
    """Build a 1,000-span pure parent_event_id tree to verify zero fast-path regression."""
    events: dict[str, TraceEvent] = {
        "root": TraceEvent(
            event_id="root",
            caller_id="client",
            callee_id="worker",
            signature="run()",
            parent_event_id=None,
            clock_domain_id="clk0",
            start_ns=0,
            end_ns=10_000_000,
            provenance=_OBS_PROV,
        )
    }
    for idx in range(999):
        t0 = 1_000 + idx * 1_000
        events[f"it_{idx:04d}"] = TraceEvent(
            event_id=f"it_{idx:04d}",
            caller_id="worker",
            callee_id="client",
            signature="step()",
            parent_event_id="root",
            clock_domain_id="clk0",
            start_ns=t0,
            end_ns=t0 + 400,
            provenance=_OBS_PROV,
        )
    return InteractionTraceIR(
        trace_id="perf_no_links_fast_path",
        participants=_participants(),
        events=events,
    )


def test_perf_compact_cyclic_trap_loops_and_fanouts(benchmark: Any) -> None:
    trace = _build_cyclic_trap_trace()
    config = TraceCompactionConfig(
        fold_loops=True,
        fold_loop_with_opt=True,
        cut_recursion=True,
        fold_parallel_siblings=True,
        parallel_epsilon_ns=1_000,
        fold_parallel_map=True,
        max_par_branches=4,
        decompose_series_parallel=True,
        max_call_depth=6,
    )
    engine = TraceCompactionEngine()
    compacted: InteractionTraceIR = benchmark(engine.compact, trace, config)

    edges = [
        (src, eid)
        for eid, ev in compacted.events.items()
        for src in (
            ev.causal_links
            if ev.parent_event_id is None
            else (ev.parent_event_id, *ev.causal_links)
        )
        if src in compacted.events
    ]
    decompose_causal_poset(sorted(compacted.events.keys()), sorted(set(edges)))
    assert collect_trace_backing_ids(compacted) == frozenset(trace.events.keys())


def test_perf_compact_no_links_fast_path_1000_spans(benchmark: Any) -> None:
    trace = _build_1000_span_no_link_fast_path_trace()
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
        max_call_depth=6,
    )
    engine = TraceCompactionEngine()
    compacted: InteractionTraceIR = benchmark(engine.compact, trace, config)

    assert len(compacted.events) == 2
    assert collect_trace_backing_ids(compacted) == frozenset(trace.events.keys())
