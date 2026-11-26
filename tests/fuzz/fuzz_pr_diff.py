"""Property-based Hypothesis fuzz tests for AG-05: architectural changes in PR review (`diff`).

Proves: (1) newly broken vs pre-existing vs restored declared reachability is distinguished for
both `REACHABILITY_NOT_OBSERVED` and `REACHABILITY_SEVERED` in structured fields and Markdown,
(2) bulk edge churn never floods or outranks a broken declared dependency, and (3) an unavailable
base degrades to a head-only 5-Gate validation with an accurate exit status.
"""

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
    BypassVerticesRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import (
    EXIT_CLEAN,
    EXIT_LINT_OR_DRIFT,
    EXIT_USAGE_ERROR,
    app,
)
from archigram.entrypoints.cli.diff_report import (
    PrDiffSummary,
    RelationshipStatus,
    compute_pr_regression_diff,
)

_RUNNER = CliRunner()
_DECLARED_CODES = {"REACHABILITY_NOT_OBSERVED", "REACHABILITY_SEVERED"}
_SECTION = "### Declared Relationship Changes & Affected Views"
_VIEW_IDS = ("view_a", "view_b")


def _stamp() -> ProvenanceStamp:
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="fuzz_pr_diff",
        input_sha256="c" * 64,
        file_path="arch.json",
    )


def _node(symbol_id: str) -> IRNode:
    return IRNode(symbol_id=symbol_id, label=symbol_id, kind=NodeKind.SERVICE, provenance=_stamp())


def _edge(edge_id: str, src: str, dst: str, relation: EdgeRelation) -> IREdge:
    return IREdge(
        edge_id=edge_id,
        source=src,
        target=dst,
        relation=relation,
        sync_kind=SyncKind.SYNC,
        provenance=_stamp(),
    )


def _chain_ids(chain_len: int) -> list[str]:
    return [f"svc/n{idx}" for idx in range(chain_len)]


def _graph(
    chain_len: int,
    *,
    mode: str,
    broken: bool,
    bulk: int = 0,
    bulk_relation: EdgeRelation = EdgeRelation.CALLS,
) -> StructuralGraphIR:
    """Declared chain n0..nK; `broken` removes the last edge or flips the n1->n2 relation."""
    ids = _chain_ids(chain_len)
    nodes = {sid: _node(sid) for sid in ids}
    edges: list[IREdge] = []
    for idx in range(chain_len - 1):
        relation = EdgeRelation.CALLS
        if broken and mode == "severed" and idx == 1:
            relation = EdgeRelation.IMPORTS
        if broken and mode == "not_observed" and idx == chain_len - 2:
            continue
        edges.append(_edge(f"e{idx}", ids[idx], ids[idx + 1], relation))
    bulk_ids = [f"bulk/b{idx}" for idx in range(bulk + 1)] if bulk else []
    for bid in bulk_ids:
        nodes[bid] = _node(bid)
    for idx in range(bulk):
        edges.append(_edge(f"bulk{idx}", bulk_ids[idx], bulk_ids[idx + 1], bulk_relation))
    return StructuralGraphIR(graph_id="fuzz_pr_diff", nodes=nodes, edges=tuple(edges))


def _spec(chain_len: int, *, mode: str, preserve: bool = False) -> CurationSpecV1:
    ids = _chain_ids(chain_len)
    assertion = ReachabilityAssertion(from_node=ids[0], to_node=ids[-1])
    invariants = ViewInvariants(
        require_reachability=() if preserve else (assertion,),
        preserve_reachability=(assertion,) if preserve else (),
        node_budget=BudgetRange(min_count=1, max_count=500),
        edge_budget_max=1000,
    )
    rules = (
        (
            BypassVerticesRule(
                id="bypass_n1",
                node_id_globs=("svc/n1",),
                preserve_edge_relations=("calls",),
            ),
        )
        if mode == "severed"
        else ()
    )
    views = tuple(
        DiagramViewSpec(
            id=view_id,
            title=f"View {view_id}",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=renderer,
            rules=rules,
            invariants=invariants,
        )
        for view_id, renderer in zip(
            _VIEW_IDS, (TargetRenderer.MERMAID, TargetRenderer.D2), strict=True
        )
    )
    return CurationSpecV1(views=views)


def _broken_label(chain_len: int) -> str:
    ids = _chain_ids(chain_len)
    return f"{ids[0]} -> {ids[-1]}"


_MODES = st.sampled_from(["not_observed", "severed"])


def _assert_view_deltas(
    summary: PrDiffSummary,
    status: RelationshipStatus,
    label: str,
) -> None:
    for delta in summary.view_deltas:
        newly = status is RelationshipStatus.NEWLY_BROKEN
        assert delta.newly_severed_reachability == ((label,) if newly else ())
        assert delta.newly_broken_reachability == delta.newly_severed_reachability
        pre = status is RelationshipStatus.PRE_EXISTING
        assert delta.pre_existing_reachability_failures == ((label,) if pre else ())
        restored = status is RelationshipStatus.RESTORED
        assert delta.restored_reachability == ((label,) if restored else ())
        assert delta.has_regression is newly
        if pre:
            assert delta.pre_existing_error_count >= 1
            assert delta.new_error_count == 0


@given(
    chain_len=st.integers(min_value=4, max_value=8),
    mode=_MODES,
    base_broken=st.booleans(),
    head_broken=st.booleans(),
)
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_pr_diff_newly_broken_vs_pre_existing_vs_restored(
    chain_len: int,
    mode: str,
    base_broken: bool,
    head_broken: bool,
) -> None:
    """Both reachability codes classify as newly broken / pre-existing / restored per view."""
    spec = _spec(chain_len, mode=mode)
    base = _graph(chain_len, mode=mode, broken=base_broken)
    head = _graph(chain_len, mode=mode, broken=head_broken)
    label = _broken_label(chain_len)

    summary = compute_pr_regression_diff(base, head, spec)
    again = compute_pr_regression_diff(base, head, spec)
    assert summary == again
    assert summary.markdown_report == again.markdown_report
    assert summary.base_available is True
    assert summary.base_unavailable_reason is None
    # Only a newly broken relationship regresses; one already broken on base is pre-existing.
    newly_broken = head_broken and not base_broken
    assert summary.passed is (not newly_broken)
    assert summary.has_regressions is newly_broken

    if base_broken is head_broken:
        expected_status = RelationshipStatus.PRE_EXISTING if head_broken else None
    else:
        expected_status = (
            RelationshipStatus.NEWLY_BROKEN if head_broken else RelationshipStatus.RESTORED
        )

    if not (base_broken or head_broken):
        assert summary.declared_changes == ()
        assert summary.affected_view_ids == ()
        return

    assert expected_status is not None
    assert {c.status for c in summary.declared_changes} == {expected_status}
    assert {c.view_id for c in summary.declared_changes} == set(_VIEW_IDS)
    assert summary.affected_view_ids == _VIEW_IDS
    expected_code = (
        "REACHABILITY_NOT_OBSERVED" if mode == "not_observed" else "REACHABILITY_SEVERED"
    )
    assert {c.code for c in summary.declared_changes} == {expected_code}

    tag = {
        RelationshipStatus.NEWLY_BROKEN: "[NEWLY BROKEN]",
        RelationshipStatus.PRE_EXISTING: "[PRE-EXISTING]",
        RelationshipStatus.RESTORED: "[RESTORED]",
    }[expected_status]
    md = summary.markdown_report
    assert _SECTION in md
    assert tag in md
    for other_tag in {"[NEWLY BROKEN]", "[PRE-EXISTING]", "[RESTORED]"} - {tag}:
        assert other_tag not in md
    assert md.count(f"`{label}`") >= len(_VIEW_IDS)
    for view_id in _VIEW_IDS:
        assert f"`{view_id}`" in md

    _assert_view_deltas(summary, expected_status, label)
    if expected_status is RelationshipStatus.NEWLY_BROKEN:
        assert summary.new_errors
        assert summary.new_errors[0].code in _DECLARED_CODES
    if expected_status is RelationshipStatus.PRE_EXISTING:
        assert not [e for e in summary.new_errors if e.code in _DECLARED_CODES]


@given(
    chain_len=st.integers(min_value=4, max_value=7),
    declared=st.sampled_from(["require", "severed_require", "severed_preserve"]),
    bulk=st.integers(min_value=15, max_value=50),
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_pr_diff_high_churn_prioritizes_declared_dependency(
    chain_len: int,
    declared: str,
    bulk: int,
) -> None:
    """30-100 bulk edge changes never bury the one broken declared dependency."""
    mode = "not_observed" if declared == "require" else "severed"
    spec = _spec(chain_len, mode=mode, preserve=declared == "severed_preserve")
    base = _graph(chain_len, mode=mode, broken=False, bulk=bulk, bulk_relation=EdgeRelation.CALLS)
    head = _graph(chain_len, mode=mode, broken=True, bulk=bulk, bulk_relation=EdgeRelation.IMPORTS)
    label = _broken_label(chain_len)

    summary = compute_pr_regression_diff(base, head, spec)
    assert not summary.passed

    broken = [c for c in summary.declared_changes if c.status is RelationshipStatus.NEWLY_BROKEN]
    assert {c.view_id for c in broken} == set(_VIEW_IDS)
    assert {c.label for c in broken} == {label}
    assert summary.affected_view_ids == _VIEW_IDS
    if declared == "require":
        last_edge = f"{_chain_ids(chain_len)[-2]} -[calls]-> {_chain_ids(chain_len)[-1]}"
        assert all(last_edge in c.removed_edges for c in broken)
    else:
        assert all("svc/n1 -[calls]-> svc/n2" in c.removed_edges for c in broken)
        assert all("svc/n1 -[imports]-> svc/n2" in c.added_edges for c in broken)
        kinds = {c.kind for c in broken}
        assert kinds == {
            "preserve_reachability" if declared == "severed_preserve" else "require_reachability"
        }

    churn = summary.edge_churn
    assert churn.edges_added >= bulk
    assert churn.edges_removed >= bulk
    assert churn.declared_edges_changed >= 1
    assert churn.bulk_edges_changed >= 2 * bulk - 2 * len(_VIEW_IDS) * 2
    assert summary.new_errors
    assert summary.new_errors[0].code in _DECLARED_CODES

    md = summary.markdown_report
    lines = md.splitlines()
    assert len(lines) < 45
    assert not any("bulk/b" in line for line in lines)
    section = md.index(_SECTION)
    first_break = md.index("[NEWLY BROKEN]")
    assert section < first_break < md.index("| View |")
    assert first_break < md.index("Raw edge churn")
    assert md.index("| View |") < md.index("Gate 1-5 Error Diagnostics")
    assert "Gate 1-5 Error Diagnostics" in md
    assert "bulk non-declared" in md
    for view_id in _VIEW_IDS:
        assert f"`{view_id}`" in md.split("| View |")[0]


def _write_inputs(
    tmp: Path,
    head: StructuralGraphIR,
    spec: CurationSpecV1,
) -> tuple[Path, Path]:
    head_file = tmp / "head.json"
    spec_file = tmp / "diagram-views.yaml"
    head_file.write_text(head.model_dump_json(), encoding="utf-8")
    spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
    return head_file, spec_file


@given(
    chain_len=st.integers(min_value=4, max_value=8),
    mode=_MODES,
    head_broken=st.booleans(),
    reason=st.one_of(st.none(), st.text(min_size=1, max_size=40)),
)
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_pr_diff_unavailable_base_direct(
    chain_len: int,
    mode: str,
    head_broken: bool,
    reason: str | None,
) -> None:
    """`compute_pr_regression_diff(None, ...)` validates head and reports base unavailable."""
    spec = _spec(chain_len, mode=mode)
    head = _graph(chain_len, mode=mode, broken=head_broken)
    summary = compute_pr_regression_diff(None, head, spec, base_unavailable_reason=reason)

    assert summary.base_available is False
    assert summary.base_unavailable_reason
    assert summary.head_passed is (not head_broken)
    assert summary.passed is (not head_broken)
    assert summary.has_regressions is head_broken
    assert summary.base_error_count == 0
    assert summary.resolved_errors == ()
    assert summary.edge_churn.edges_added == 0
    assert "Base comparison unavailable" in summary.markdown_report
    assert _SECTION in summary.markdown_report
    assert len(summary.view_deltas) == len(_VIEW_IDS)
    if head_broken:
        assert {c.status for c in summary.declared_changes} == {RelationshipStatus.FAILING}
        assert "[FAILING]" in summary.markdown_report
        assert summary.new_errors[0].code in _DECLARED_CODES
        assert summary.head_error_count == len(summary.new_errors)
    else:
        assert summary.declared_changes == ()
        assert summary.new_errors == ()
    for status in ("[NEWLY BROKEN]", "[PRE-EXISTING]"):
        assert status not in summary.markdown_report
    assert summary == compute_pr_regression_diff(None, head, spec, base_unavailable_reason=reason)


@given(
    chain_len=st.integers(min_value=4, max_value=6),
    mode=_MODES,
    head_broken=st.booleans(),
    base_variant=st.sampled_from(["omitted", "missing", "garbage"]),
    as_json=st.booleans(),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_pr_diff_cli_unavailable_base_exit_status(
    chain_len: int,
    mode: str,
    head_broken: bool,
    base_variant: str,
    as_json: bool,
) -> None:
    """Omitted/missing/garbage `--base` still validates head; exit mirrors head pass/fail."""
    spec = _spec(chain_len, mode=mode)
    head = _graph(chain_len, mode=mode, broken=head_broken)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        head_file, spec_file = _write_inputs(tmp, head, spec)
        args = ["diff", "--head", str(head_file), "--spec", str(spec_file)]
        if base_variant == "missing":
            args += ["--base", str(tmp / "does-not-exist.json")]
        elif base_variant == "garbage":
            bad = tmp / "base.json"
            bad.write_text("{not valid json", encoding="utf-8")
            args += ["--base", str(bad)]
        if as_json:
            args.append("--json")

        result = _RUNNER.invoke(app, args)
        expected = EXIT_LINT_OR_DRIFT if head_broken else EXIT_CLEAN
        assert result.exit_code == expected, result.output
        assert "base comparison unavailable" in result.stderr.lower()
        if as_json:
            parsed = PrDiffSummary.model_validate_json(result.stdout)
            assert parsed.base_available is False
            assert parsed.base_unavailable_reason
            assert parsed.passed is (not head_broken)
            assert parsed.head_passed is (not head_broken)
        else:
            assert "Base comparison unavailable" in result.stdout
            assert "ArchiGram PR Architectural & Topological Diff" in result.stdout
        if base_variant == "omitted":
            assert "--base was not provided" in result.stderr
        else:
            assert "could not be loaded" in result.stderr

        # Missing/invalid head or spec is still a usage error, never silently degraded.
        no_head = _RUNNER.invoke(app, ["diff", "--spec", str(spec_file)])
        assert no_head.exit_code == EXIT_USAGE_ERROR
        no_spec = _RUNNER.invoke(app, ["diff", "--head", str(head_file)])
        assert no_spec.exit_code == EXIT_USAGE_ERROR
        gone_head = _RUNNER.invoke(
            app, ["diff", "--head", str(tmp / "gone.json"), "--spec", str(spec_file)]
        )
        assert gone_head.exit_code == EXIT_USAGE_ERROR
        gone_spec = _RUNNER.invoke(
            app, ["diff", "--head", str(head_file), "--spec", str(tmp / "gone.yaml")]
        )
        assert gone_spec.exit_code == EXIT_USAGE_ERROR
