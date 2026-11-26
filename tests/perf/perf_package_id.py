"""Performance benchmark for pyproject-derived root package ID resolution."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from archigram.features.ingest_python import PythonAstStructuralIngestor


def test_perf_ingest_renamed_package_with_pyproject(benchmark: Any) -> None:
    """Benchmark ingestion of a renamed package copy resolved via pyproject.toml."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "case_a_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        for i in range(30):
            (pkg / f"m{i}.py").write_text(f"def f{i}() -> int:\n    return {i}\n", encoding="utf-8")
        (root / "pyproject.toml").write_text('[project]\nname = "realpkg"\n', encoding="utf-8")
        ir = benchmark(PythonAstStructuralIngestor().ingest, pkg)
    assert "realpkg.m0" in ir.nodes
    assert "case_a_pkg" not in ir.nodes
