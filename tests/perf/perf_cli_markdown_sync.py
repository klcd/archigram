"""Performance benchmarks for Python directory auto-ingestion & Markdown inline diagram sync."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from archigram.domain.spec import (
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    PruneLeavesRule,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import ApplyOptions, execute_apply


def _build_synthetic_pkg_and_guides(root: Path) -> tuple[Path, Path, Path, Path]:
    pkg_dir = root / "src" / "benchpkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(
        "from __future__ import annotations\n",
        encoding="utf-8",
    )
    for idx in range(8):
        prev_import = f"from .mod_{idx - 1} import Cls{idx - 1}\n" if idx > 0 else ""
        base_expr = f"(Cls{idx - 1})" if idx > 0 else ""
        (pkg_dir / f"mod_{idx}.py").write_text(
            f"from __future__ import annotations\n{prev_import}\n"
            f"class Cls{idx}{base_expr}:\n"
            f"    def step_{idx}(self) -> int:\n"
            f"        return {idx}\n",
            encoding="utf-8",
        )

    invariants = ViewInvariants(
        node_budget=BudgetRange(min_count=1, max_count=25),
        edge_budget_max=50,
    )
    spec = CurationSpecV1(
        global_abstractions=(
            PruneLeavesRule(
                id="prune_pkg_root",
                node_id_globs=("benchpkg",),
                direction="both",
            ),
        ),
        views=(
            DiagramViewSpec(
                id="bench_mermaid",
                title="Benchmark Mermaid View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="bench_d2",
                title="Benchmark D2 View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="bench_plantuml",
                title="Benchmark PlantUML View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.PLANTUML,
                invariants=invariants,
            ),
        ),
    )
    spec_path = root / "diagram-views.yaml"
    spec_path.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")

    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for doc_idx in range(4):
        (docs_dir / f"guide_{doc_idx}.md").write_text(
            f"# Architecture Guide {doc_idx}\n\n"
            "<!-- archigram:start bench_mermaid -->\n"
            "```mermaid\nplaceholder\n```\n"
            "<!-- archigram:end bench_mermaid -->\n\n"
            "<!-- archigram:start foreign_view -->\n"
            "```mermaid\nforeign\n```\n"
            "<!-- archigram:end foreign_view -->\n\n"
            "<!-- archigram:start bench_d2 -->\n"
            "```d2\nplaceholder\n```\n"
            "<!-- archigram:end bench_d2 -->\n\n"
            "<!-- archigram:start bench_plantuml -->\n"
            "```plantuml\nplaceholder\n```\n"
            "<!-- archigram:end bench_plantuml -->\n",
            encoding="utf-8",
        )
    out_dir = root / "out"
    return pkg_dir, spec_path, out_dir, docs_dir


def test_perf_execute_apply_python_dir_and_markdown_sync(benchmark: Any) -> None:
    """Benchmark end-to-end `execute_apply` with Python directory auto-ingestion and `--sync-md`."""
    with tempfile.TemporaryDirectory() as tmp_str:
        pkg_dir, spec_path, out_dir, docs_dir = _build_synthetic_pkg_and_guides(Path(tmp_str))
        write_opts = ApplyOptions(
            input_path=pkg_dir,
            spec_path=spec_path,
            out_dir=out_dir,
            check_only=False,
            sync_md=docs_dir,
        )
        check_opts = ApplyOptions(
            input_path=pkg_dir,
            spec_path=spec_path,
            out_dir=out_dir,
            check_only=True,
            sync_md=docs_dir,
        )

        def _run_apply_and_check() -> bool:
            write_outcome = execute_apply(write_opts)
            check_outcome = execute_apply(check_opts)
            return write_outcome.is_clean and check_outcome.is_clean

        assert benchmark(_run_apply_and_check)
