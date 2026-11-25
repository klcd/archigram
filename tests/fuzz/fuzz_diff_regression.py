"""Property-based fuzz tests: `diff` regresses only on newly introduced diagnostics."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from archigram.domain import ProvenanceGrade, ProvenanceStamp
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
    IRMode,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import EXIT_CLEAN, EXIT_LINT_OR_DRIFT, app
from archigram.entrypoints.cli.diff_report import compute_pr_regression_diff

_RUNNER = CliRunner()


def _chain_graph(node_count: int) -> StructuralGraphIR:
    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="fuzz_diff_regression",
        input_sha256="c" * 64,
        file_path="arch.dot",
    )
    ids = [f"svc_{idx}" for idx in range(node_count)]
    nodes = {
        nid: IRNode(symbol_id=nid, label=nid, kind=NodeKind.SERVICE, provenance=stamp)
        for nid in ids
    }
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=ids[idx],
            target=ids[idx + 1],
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            weight=1.0,
            provenance=stamp,
        )
        for idx in range(node_count - 1)
    )
    return StructuralGraphIR(graph_id="fuzz_diff", nodes=nodes, edges=edges)


def _spec_with_unmet_min_budget(min_count: int) -> CurationSpecV1:
    """Spec whose node budget floor exceeds the graph size, so every compile has an error."""
    view = DiagramViewSpec(
        id="arch_overview",
        title="Architecture Overview",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.MERMAID,
        invariants=ViewInvariants(
            node_budget=BudgetRange(min_count=min_count, max_count=25),
            edge_budget_max=50,
        ),
    )
    return CurationSpecV1(global_abstractions=(), views=(view,))


@given(
    node_count=st.integers(min_value=2, max_value=8),
    floor_extra=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_diff_preexisting_errors_do_not_regress(
    node_count: int,
    floor_extra: int,
) -> None:
    """Errors present on both base and head never set has_regression nor fail the exit code."""
    base = _chain_graph(node_count)
    head = base  # zero-churn commit: every base error persists unchanged on head
    spec = _spec_with_unmet_min_budget(node_count + floor_extra)

    summary = compute_pr_regression_diff(base, head, spec)
    assert summary.base_error_count > 0
    assert summary.head_error_count > 0
    assert summary.has_regressions is False
    assert summary.passed is True
    assert all(not delta.has_regression for delta in summary.view_deltas)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        (tmp_dir / "base.json").write_text(base.model_dump_json(), encoding="utf-8")
        (tmp_dir / "head.json").write_text(head.model_dump_json(), encoding="utf-8")
        (tmp_dir / "spec.yaml").write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
        result = _RUNNER.invoke(
            app,
            [
                "diff",
                "--base",
                str(tmp_dir / "base.json"),
                "--head",
                str(tmp_dir / "head.json"),
                "--spec",
                str(tmp_dir / "spec.yaml"),
            ],
        )
        assert result.exit_code == EXIT_CLEAN


@given(node_count=st.integers(min_value=3, max_value=8))
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_diff_newly_introduced_errors_regress(node_count: int) -> None:
    """An error absent on base but present on head sets has_regression."""
    base = _chain_graph(node_count)
    head = _chain_graph(node_count - 1)
    # Floor equals base size: base meets it, head (one node fewer) violates it.
    spec = _spec_with_unmet_min_budget(node_count)

    summary = compute_pr_regression_diff(base, head, spec)
    assert summary.base_error_count == 0
    assert summary.head_error_count > 0
    assert summary.has_regressions is True
    assert summary.passed is False
    assert any(delta.has_regression for delta in summary.view_deltas)
    assert EXIT_LINT_OR_DRIFT == 1
