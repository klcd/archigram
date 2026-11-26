"""Performance benchmark suite for native Python AST and runtime call-trace ingestion."""

from __future__ import annotations

import tempfile
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


def test_perf_ingest_function_local_imports(benchmark: Any) -> None:
    """Benchmark AST ingestion of many modules whose imports are all function-local."""
    n = 60
    lines = ["def f() -> None:", *(f"    from pkg import m{i}" for i in range(n))]
    source = "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "main.py").write_text(source, encoding="utf-8")
        for i in range(n):
            (root / "pkg" / f"m{i}.py").write_text("X = 1\n", encoding="utf-8")
        ir = benchmark(_AST_INGESTOR.ingest, root)
    assert len([e for e in ir.edges if e.source == "pkg.main"]) >= n


def test_perf_ingest_type_checking_guarded_imports(benchmark: Any) -> None:
    """Benchmark AST ingestion of a module with many TYPE_CHECKING-only plus runtime imports."""
    n = 60
    lines = ["from typing import TYPE_CHECKING", "if TYPE_CHECKING:"]
    lines += [f"    from pkg import t{i}" for i in range(n)]
    lines += ["else:", *(f"    from pkg import r{i}" for i in range(n))]
    source = "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "main.py").write_text(source, encoding="utf-8")
        for i in range(n):
            (root / "pkg" / f"t{i}.py").write_text("X = 1\n", encoding="utf-8")
            (root / "pkg" / f"r{i}.py").write_text("X = 1\n", encoding="utf-8")
        ir = benchmark(_AST_INGESTOR.ingest, root)
    targets = {e.target for e in ir.edges if e.source == "pkg.main"}
    assert all(f"pkg.r{i}" in targets for i in range(n))
    assert not any(f"pkg.t{i}" in targets for i in range(n))
