"""Benchmarks for Gate 3 trace partial-order validation on large compacted traces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    InteractionTraceIR,
    NodeKind,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import (
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
)
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.reduce_trace import TraceCompactionEngine

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture

_OBS = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf-trace-guarantees",
    input_sha256="d" * 64,
)
_VIEW_ID = "trace-view"
_PHASES = 24
_ITERATIONS = 10


def _event(
    eid: str,
    idx: int,
    *,
    parent: str | None,
    links: tuple[str, ...] = (),
    sig: str,
    sync: SyncKind = SyncKind.SYNC,
) -> TraceEvent:
    return TraceEvent(
        event_id=eid,
        caller_id="p0",
        callee_id="p1",
        signature=sig,
        parent_event_id=parent,
        causal_links=links,
        clock_domain_id="clk0",
        start_ns=idx * 10,
        end_ns=idx * 10 + 5,
        sync_kind=sync,
        provenance=_OBS,
    )


def _build_observed() -> InteractionTraceIR:
    """24 phases x (setup + 10 loop iterations + async spawn + join) = 336 events."""
    events: dict[str, TraceEvent] = {"root": _event("root", 0, parent=None, sig="root()")}
    idx = 1
    prev_join = "root"
    for phase in range(_PHASES):
        setup = f"p{phase}_setup"
        events[setup] = _event(
            setup, idx, parent="root", links=(prev_join,), sig=f"setup_{phase}()"
        )
        idx += 1
        last_iter = setup
        for it in range(_ITERATIONS):
            eid = f"p{phase}_it{it}"
            events[eid] = _event(eid, idx, parent="root", links=(setup,), sig=f"work_{phase}()")
            idx += 1
            last_iter = eid
        spawn = f"p{phase}_async"
        events[spawn] = _event(
            spawn, idx, parent=setup, sig=f"fire_{phase}()", sync=SyncKind.ASYNC_FIRE_FORGET
        )
        idx += 1
        join = f"p{phase}_join"
        events[join] = _event(join, idx, parent="root", links=(last_iter,), sig=f"join_{phase}()")
        idx += 1
        prev_join = join
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"svc/{pid}",
            label=pid,
            kind=NodeKind.SERVICE,
            provenance=_OBS,
        )
        for pid in ("p0", "p1")
    }
    return InteractionTraceIR(participants=participants, events=events)


def _spec() -> CurationSpecV1:
    view = DiagramViewSpec(
        id=_VIEW_ID,
        title="Trace View",
        ir_mode=IRMode.INTERACTION_TRACE,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(preserve_partial_order=True),
    )
    return CurationSpecV1(views=(view,))


def test_perf_gate3_validates_large_compacted_trace(benchmark: BenchmarkFixture) -> None:
    """Gate 3 ordering contract on a 300+ event trace compacted by loop folding."""
    observed = _build_observed()
    config = TraceCompactionConfig(
        fold_loops=True,
        fold_parallel_siblings=False,
        fold_parallel_map=False,
        decompose_series_parallel=False,
        max_call_depth=None,
    )
    curated = TraceCompactionEngine().compact(observed, config)
    spec = _spec()
    gate = TopologicalInvariantGate()

    report = benchmark(gate.check, observed, spec, {_VIEW_ID: curated})

    assert len(observed.events) > 300
    assert len(curated.events) < len(observed.events) // 2
    assert report.passed is True


def test_perf_gate3_rejects_identity_trace_with_severed_edges(benchmark: BenchmarkFixture) -> None:
    """Uncompacted 300+ event trace with one dropped link: single minimal LOST_EDGE report."""
    observed = _build_observed()
    victim = "p12_join"
    severed = observed.events[victim].model_copy(update={"causal_links": ()})
    curated = observed.model_copy(update={"events": {**observed.events, victim: severed}})
    spec = _spec()
    gate = TopologicalInvariantGate()

    report = benchmark(gate.check, observed, spec, {_VIEW_ID: curated})

    assert report.passed is False
    assert {d.code for d in report.diagnostics} == {"PARTIAL_ORDER_LOST_EDGE"}
