"""Performance benchmark suite for native Python AST and runtime call-trace ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archigram.features.ingest_python import (
    PythonAstStructuralIngestor,
    capture_python_trace,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARCHIGRAM_SRC = _REPO_ROOT / "src" / "archigram"
_ARCHIGRAM_DOMAIN = _ARCHIGRAM_SRC / "domain"
_AST_INGESTOR = PythonAstStructuralIngestor()


def _multi_hundred_call_workload() -> object:
    """Execute a deterministic multi-hundred-call workload across real ArchiGram modules."""
    return _AST_INGESTOR.ingest(_ARCHIGRAM_DOMAIN)


def test_perf_ingest_archigram_codebase_ast(benchmark: Any) -> None:
    """Benchmark native Python AST ingestion across ArchiGram's full src/archigram tree."""
    ir = benchmark(_AST_INGESTOR.ingest, _ARCHIGRAM_SRC)
    assert "archigram" in ir.nodes
    assert "archigram.domain.ir" in ir.nodes
    assert len(ir.nodes) >= 100
    assert len(ir.edges) >= 100


def test_perf_capture_python_trace_multi_hundred_calls(benchmark: Any) -> None:
    """Benchmark PEP 669 sys.monitoring runtime call tracing over a multi-hundred-call workload."""
    trace = benchmark(
        capture_python_trace,
        _multi_hundred_call_workload,
        trace_id="perf_archigram_domain_ingest",
        module_prefixes=("archigram",),
        granularity="module",
        max_events=2000,
    )
    assert len(trace.participants) >= 2
    assert len(trace.events) >= 200
