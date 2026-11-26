"""Performance benchmarks for AG-02 actionable CI diagnostics (attribution + rendering + check)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import DiagnosticSeverity, GateId, LintDiagnostic, LintReport
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
)
from archigram.domain.spec import (
    BudgetRange,
    CurationSpecV1,
    DiagramViewSpec,
    GlobalAbstractionRule,
    IRMode,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import ApplyOptions, build_apply_check_report, execute_apply
from archigram.entrypoints.cli.pipeline import format_lint_report_text
from archigram.features.lint_topology import TopologicalInvariantGate

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_ci_diagnostics",
    input_sha256="d" * 64,
)
_CHAIN_LEN = 200
_RULE_COUNT = 100


def _chain_graph() -> StructuralGraphIR:
    ids = [f"svc/n{idx:03d}" for idx in range(_CHAIN_LEN)]
    return StructuralGraphIR(
        nodes={
            nid: IRNode(symbol_id=nid, label=nid, kind=NodeKind.MODULE, provenance=_STAMP)
            for nid in ids
        },
        edges=tuple(
            IREdge(
                edge_id=f"e{idx}",
                source=ids[idx],
                target=ids[idx + 1],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                provenance=_STAMP,
            )
            for idx in range(_CHAIN_LEN - 1)
        ),
    )


def test_perf_severed_path_rule_attribution_200_node_chain(benchmark: Any) -> None:
    """Benchmark Gate 3 severed-path attribution across 100 view rules on a 200-node witness."""
    observed = _chain_graph()
    ids = sorted(observed.nodes)
    cut = ids[100]
    curated = StructuralGraphIR(
        nodes={nid: node for nid, node in observed.nodes.items() if nid != cut},
        edges=tuple(e for e in observed.edges if cut not in {e.source, e.target}),
    )
    rules: tuple[GlobalAbstractionRule, ...] = tuple(
        PruneLeavesRule(id=f"rule_{idx:03d}", node_id_globs=(f"svc/n{idx:03d}",))
        for idx in range(_RULE_COUNT + 1)
    )
    view = DiagramViewSpec(
        id="perf_view",
        title="Perf View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        rules=rules,
        invariants=ViewInvariants(
            preserve_reachability=(ReachabilityAssertion(from_node=ids[0], to_node=ids[-1]),)
        ),
    )
    spec = CurationSpecV1(views=(view,))
    gate = TopologicalInvariantGate()

    report = benchmark(lambda: gate.check(observed, spec, {"perf_view": curated}))
    severed = [d for d in report.diagnostics if d.code == "REACHABILITY_SEVERED"]
    assert len(severed) == 1
    assert severed[0].rule_id == "rule_100"
    assert severed[0].witness_path == tuple(ids)


def test_perf_format_lint_report_text_500_diagnostics(benchmark: Any) -> None:
    """Benchmark human CLI rendering of 500 mixed severed / not-observed / unknown diagnostics."""
    diags: list[LintDiagnostic] = []
    for idx in range(500):
        severed = idx % 3 == 0
        diags.append(
            LintDiagnostic(
                gate=GateId.GATE_3_TOPOLOGY_POSET,
                severity=DiagnosticSeverity.ERROR,
                code="REACHABILITY_SEVERED" if severed else "REACHABILITY_NOT_OBSERVED",
                message=f"diagnostic {idx}",
                view_id=f"view_{idx}",
                rule_id=f"rule_{idx}" if severed else None,
                offending_symbols=(f"a{idx}", f"z{idx}"),
                witness_path=tuple(f"n{idx}_{hop}" for hop in range(8)) if severed else (),
                repair_hint="repair",
            )
        )
    report = LintReport.from_diagnostics(diags)

    text = benchmark(lambda: format_lint_report_text(report))
    assert text.count("    Hint: ") == 500
    assert text.count("    Witness: ") == len([d for d in diags if d.witness_path])


def test_perf_apply_check_report_stale_detection(benchmark: Any) -> None:
    """Benchmark `apply --check` stale detection + structured report on a 200-node graph."""
    graph = _chain_graph()
    view = DiagramViewSpec(
        id="perf_ok",
        title="Perf OK",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(
            node_budget=BudgetRange(min_count=1, max_count=500),
            edge_budget_max=500,
        ),
    )
    spec = CurationSpecV1(views=(view,))
    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str)
        input_file = root / "input.ir.json"
        input_file.write_text(graph.model_dump_json(), encoding="utf-8")
        spec_file = root / "views.yaml"
        spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
        options = ApplyOptions(
            input_path=input_file,
            spec_path=spec_file,
            out_dir=root / "out",
            check_only=True,
        )

        def _check() -> bool:
            outcome = execute_apply(options)
            report = build_apply_check_report(outcome, options)
            return (
                len(report.missing_files) == 1 and "archigram apply" in report.regeneration_command
            )

        assert benchmark(_check)
        assert not (root / "out").exists()
