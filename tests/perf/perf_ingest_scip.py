"""Performance benchmark suite for SCIP JSON & static fork-join ingestion."""

from __future__ import annotations

import json
from typing import Any

from archigram.domain.ir import EdgeRelation, SyncKind
from archigram.features.ingest_scip import ScipStructuralIngestor


def _build_single_doc_fixture(
    d_idx: int,
    classes_per_doc: int,
    methods_per_class: int,
    all_methods: list[tuple[str, str, int]],
) -> dict[str, Any]:
    pkg = f"service_{d_idx}"
    mod_name = f"handler_{d_idx}"
    rel_path = f"src/{pkg}/{mod_name}.py"
    mod_sym = f"scip-python python {pkg} 1.0.0 {mod_name}/"

    symbols: list[dict[str, Any]] = [
        {"symbol": mod_sym, "display_name": mod_name, "kind": "module"}
    ]
    occurrences: list[dict[str, Any]] = [
        {"range": [0, 0, 500, 0], "symbol": mod_sym, "symbol_roles": 1}
    ]

    line_cursor = 5
    for c_idx in range(classes_per_doc):
        cls_name = f"Worker_{d_idx}_{c_idx}"
        cls_sym = f"scip-python python {pkg} 1.0.0 {mod_name}/{cls_name}#"
        cls_start = line_cursor
        cls_end = cls_start + methods_per_class * 10 + 4

        symbols.append(
            {
                "symbol": cls_sym,
                "display_name": cls_name,
                "kind": "class",
                "enclosing_symbol": mod_sym,
            }
        )
        occurrences.append(
            {"range": [cls_start, 0, cls_end, 0], "symbol": cls_sym, "symbol_roles": 1}
        )

        m_cursor = cls_start + 2
        for m_idx in range(methods_per_class):
            m_name = f"step_{m_idx}"
            m_sym = f"scip-python python {pkg} 1.0.0 {mod_name}/{cls_name}#{m_name}()."
            m_start = m_cursor
            m_end = m_start + 6
            m_cursor = m_end + 2

            symbols.append(
                {
                    "symbol": m_sym,
                    "display_name": m_name,
                    "kind": "method",
                    "enclosing_symbol": cls_sym,
                }
            )
            occurrences.append(
                {"range": [m_start, 4, m_end, 20], "symbol": m_sym, "symbol_roles": 1}
            )
            all_methods.append((m_sym, rel_path, m_start + 1))

        line_cursor = cls_end + 2

    return {
        "relative_path": rel_path,
        "symbols": symbols,
        "occurrences": occurrences,
    }


def _build_400_symbol_scip_export(
    doc_count: int = 10,
    classes_per_doc: int = 8,
    methods_per_class: int = 4,
) -> str:
    """Build a 400-symbol multi-document SCIP JSON export with fork-join regions."""
    all_methods: list[tuple[str, str, int]] = []
    documents = [
        _build_single_doc_fixture(d_idx, classes_per_doc, methods_per_class, all_methods)
        for d_idx in range(doc_count)
    ]
    annotations: list[dict[str, Any]] = []
    doc_by_path = {d["relative_path"]: d for d in documents}
    total_methods = len(all_methods)

    for idx, (caller_sym, rel_path, call_line) in enumerate(all_methods):
        doc_occ = doc_by_path[rel_path]["occurrences"]
        callee_a = all_methods[(idx + 1) % total_methods][0]
        callee_b = all_methods[(idx + 2) % total_methods][0]
        doc_occ.append(
            {"range": [call_line, 8, call_line, 24], "symbol": callee_a, "symbol_roles": 8}
        )
        if idx % 8 == 0:
            doc_occ.append(
                {
                    "range": [call_line + 1, 8, call_line + 1, 24],
                    "symbol": callee_b,
                    "symbol_roles": 8,
                }
            )
            annotations.append(
                {
                    "caller": caller_sym,
                    "targets": [callee_a, callee_b],
                    "file_path": rel_path,
                    "mode": "fork_join",
                    "idiom": "asyncio.gather",
                    "parallel_region_id": f"par_gather_{idx}",
                }
            )
        elif idx % 15 == 0:
            annotations.append(
                {
                    "caller": caller_sym,
                    "callee": callee_a,
                    "file_path": rel_path,
                    "range": [call_line, 8, call_line, 24],
                    "mode": "async_fire_forget",
                    "idiom": "asyncio.create_task",
                }
            )

    return json.dumps(
        {
            "schema": "scip.graph-export.v1",
            "graph_id": "bench_scip_400",
            "documents": documents,
            "concurrency_annotations": annotations,
        }
    )


_SCIP_400_FIXTURE = _build_400_symbol_scip_export(10, 8, 4)
_INGESTOR = ScipStructuralIngestor()


def test_ingest_400_symbol_scip_export_benchmark(benchmark: Any) -> None:
    ir = benchmark(_INGESTOR.ingest, _SCIP_400_FIXTURE)
    # 10 docs * (1 module + 8 classes + 32 methods) = 410 symbols
    assert len(ir.nodes) == 410
    fork_join_edges = [
        e
        for e in ir.edges
        if e.relation is EdgeRelation.FORK_JOIN and e.sync_kind is SyncKind.FORK_JOIN_BRANCH
    ]
    assert len(fork_join_edges) == 80
