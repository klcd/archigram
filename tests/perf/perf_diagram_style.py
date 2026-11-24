"""Performance benchmarks for canonical edge style resolution and luminance calculations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from archigram.domain.ir import EdgeRelation, SyncKind
from archigram.domain.style import relative_luminance, resolve_edge_visual_style

if TYPE_CHECKING:
    from collections.abc import Callable


def test_perf_resolve_edge_styles_batch_1000(benchmark: Callable[..., Any]) -> None:
    """Benchmark resolving 1,000 (EdgeRelation, SyncKind) visual styles."""
    pairs = [(rel, sk) for _ in range(31) for rel in EdgeRelation for sk in SyncKind][:1000]

    def _run() -> int:
        total = 0
        for rel, sk in pairs:
            style = resolve_edge_visual_style(rel, sk)
            total += len(style.stroke_hex)
        return total

    result = benchmark(_run)
    assert result == 7000


def test_perf_relative_luminance_palette(benchmark: Callable[..., Any]) -> None:
    """Benchmark WCAG relative luminance evaluation across 500 hex lookups."""
    palette = [resolve_edge_visual_style(rel).stroke_hex for rel in EdgeRelation] * 46

    def _run() -> float:
        acc = 0.0
        for color in palette[:500]:
            acc += relative_luminance(color)
        return acc

    total = benchmark(_run)
    assert total > 0.0


def test_perf_emit_d2_steps_animated_trace_200_events(benchmark: Callable[..., Any]) -> None:
    """Benchmark D2 multiboard animated `steps` trace emission over 200 runtime events."""
    from archigram.domain import ProvenanceGrade, ProvenanceStamp
    from archigram.domain.ir import (
        EventStatus,
        InteractionTraceIR,
        NodeKind,
        TraceEvent,
        TraceParticipant,
    )
    from archigram.features.emit_d2 import D2Emitter

    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="perf_diagram_style",
        input_sha256="d" * 64,
    )
    participants = {
        f"svc_{idx}": TraceParticipant(
            participant_id=f"svc_{idx}",
            symbol_id=f"svc_{idx}",
            label=f"Service {idx}",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        )
        for idx in range(16)
    }
    events = {
        f"ev_{idx}": TraceEvent(
            event_id=f"ev_{idx}",
            caller_id=f"svc_{idx % 15}",
            callee_id=f"svc_{(idx + 1) % 16}",
            signature=f"handle_{idx % 10}()",
            clock_domain_id="host_0",
            start_ns=idx * 100,
            end_ns=idx * 100 + 80,
            status=EventStatus.OK,
            provenance=stamp,
        )
        for idx in range(200)
    }
    trace = InteractionTraceIR(
        trace_id="perf_steps",
        participants=participants,
        events=events,
        metadata={"diagram_variant": "steps"},
    )
    emitter = D2Emitter()
    rendered = benchmark(emitter.emit, trace)
    assert "\nsteps: {\n" in rendered
    assert "style.animated: true" in rendered
