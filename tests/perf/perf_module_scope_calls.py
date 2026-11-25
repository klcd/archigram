"""Performance benchmark for python_ast_v1 module-scope call extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from archigram.domain.ir import EdgeRelation
from archigram.features.ingest_python import PythonAstStructuralIngestor

_COUNT = 300


def test_perf_module_scope_calls(benchmark: Any) -> None:
    """Benchmark ingestion of a module with hundreds of top-level instantiations."""
    lib = "".join(f"class C{i}:\n    pass\n\n" for i in range(_COUNT))
    user = "from .lib import *\n" + "".join(f"V{i} = C{i}()\n" for i in range(_COUNT))
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkgp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "lib.py").write_text(lib, encoding="utf-8")
        (pkg / "user.py").write_text(
            user.replace("import *", f"import {_names()}"), encoding="utf-8"
        )
        ir = benchmark(PythonAstStructuralIngestor().ingest, pkg)
    calls = [e for e in ir.edges if e.relation is EdgeRelation.CALLS]
    assert len(calls) == _COUNT


def _names() -> str:
    return ", ".join(f"C{i}" for i in range(_COUNT))
