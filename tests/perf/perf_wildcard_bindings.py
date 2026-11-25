"""Performance benchmark for wildcard-import binding resolution in python_ast_v1."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from archigram.features.ingest_python import PythonAstStructuralIngestor

_N_MODULES = 40
_N_NAMES = 25


def _write_wildcard_facade(root: Path) -> None:
    """Write a package whose __init__ wildcard-imports many modules with many names."""
    root.mkdir(parents=True)
    lines: list[str] = []
    for m in range(_N_MODULES):
        body = "".join(f"def fn_{m}_{i}() -> int:\n    return {i}\n\n" for i in range(_N_NAMES))
        (root / f"mod{m}.py").write_text(body, encoding="utf-8")
        lines.append(f"from .mod{m} import *")
    (root / "__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    calls = "".join(f"    fn_{m}_0()\n" for m in range(_N_MODULES))
    imports = ", ".join(f"fn_{m}_0" for m in range(_N_MODULES))
    (root / "user.py").write_text(
        f"from pkg import {imports}\n\ndef go() -> None:\n{calls}", encoding="utf-8"
    )


def test_perf_wildcard_facade_ingestion(benchmark: Any) -> None:
    """Benchmark ingestion of a wildcard-heavy facade package."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pkg"
        _write_wildcard_facade(root)
        ir = benchmark(PythonAstStructuralIngestor().ingest, root)
    assert sum(1 for e in ir.edges if e.source == "pkg.user.go") >= _N_MODULES
