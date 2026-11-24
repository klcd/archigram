"""Performance benchmarks for self-hosted ArchiGram multi-level dogfood compilation & sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archigram.entrypoints.cli.app import ApplyOptions, execute_apply

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ARCHIGRAM = _REPO_ROOT / "src" / "archigram"
_DOCS_ARCH = _REPO_ROOT / "docs" / "architecture"
_DIAGRAMS_DIR = _DOCS_ARCH / "diagrams"
_DIAGRAM_SPEC = _DOCS_ARCH / "diagram-views.yaml"
_TRACE_SPEC = _DOCS_ARCH / "trace-views.yaml"
_TRACE_SNAPSHOT = _DOCS_ARCH / "traces" / "pipeline-and-repair.ir.json"


def test_perf_self_hosted_structural_apply_check(benchmark: Any) -> None:
    """Benchmark full self-hosted L0/L1/L2 compilation, 5-Gate linting, and `--sync-md --check`."""
    opts = ApplyOptions(
        input_path=_SRC_ARCHIGRAM,
        spec_path=_DIAGRAM_SPEC,
        out_dir=_DIAGRAMS_DIR,
        check_only=True,
        sync_md=_DOCS_ARCH,
    )

    def _run() -> bool:
        return execute_apply(opts).is_clean

    assert benchmark(_run)


def test_perf_self_hosted_trace_apply_check(benchmark: Any) -> None:
    """Benchmark full self-hosted L3 trace compaction, 5-Gate linting, and `--sync-md --check`."""
    opts = ApplyOptions(
        input_path=_TRACE_SNAPSHOT,
        spec_path=_TRACE_SPEC,
        out_dir=_DIAGRAMS_DIR,
        check_only=True,
        sync_md=_DOCS_ARCH,
    )

    def _run() -> bool:
        return execute_apply(opts).is_clean

    assert benchmark(_run)
