"""Performance benchmark suite for Curation Spec v1 YAML parsing, validation, and diagnostics."""

from __future__ import annotations

from typing import Any

from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.spec import (
    BudgetRange,
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    SimplifyConfig,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
    dump_curation_spec_yaml,
    export_curation_spec_json_schema,
    parse_curation_spec_yaml,
)


def _build_multi_view_spec(view_count: int = 12) -> CurationSpecV1:
    global_rules = (
        PruneLeavesRule(
            id="global_prune_telemetry",
            node_id_globs=("pkg.telemetry.*", "pkg.logging.*"),
            direction="sink",
        ),
        BypassVerticesRule(
            id="global_bypass_adapters",
            node_id_globs=("pkg.adapter.*", "pkg.dto.*"),
            preserve_edge_relations=("calls", "reads_data", "writes_data"),
            preserve_concurrency_kind=True,
            max_fanout_product=25,
        ),
        CollapsePartitionRule(
            id="global_collapse_billing",
            target_node_id="supernode.billing",
            label="Billing Subsystem",
            summary="Invoices, ledger, and payment webhooks",
            member_globs=("pkg.billing.*",),
            exclude_ids=("pkg.billing.gateway",),
        ),
    )

    renderers = tuple(TargetRenderer)
    views: list[DiagramViewSpec] = []
    for idx in range(view_count):
        is_trace = idx % 3 == 0
        scope = (
            FullScope()
            if idx % 2 == 0
            else KHopNeighborhoodScope(
                focal_nodes=(f"svc.core_{idx}", f"svc.api_{idx}"),
                upstream_depth=2,
                downstream_depth=3,
            )
        )
        views.append(
            DiagramViewSpec(
                id=f"view_{idx}",
                title=f"Architecture Projection {idx}",
                ir_mode=IRMode.INTERACTION_TRACE if is_trace else IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderers[idx % len(renderers)],
                rules=(
                    PruneLeavesRule(
                        id=f"v{idx}_prune_helpers",
                        node_id_globs=(f"pkg.v{idx}.helper.*",),
                        direction="both",
                    ),
                    BypassVerticesRule(
                        id=f"v{idx}_bypass_wrappers",
                        node_id_globs=(f"pkg.v{idx}.wrapper.*",),
                        preserve_edge_relations=("calls", "async_spawn"),
                        preserve_concurrency_kind=True,
                        max_fanout_product=16,
                    ),
                ),
                scope=scope,
                simplify=SimplifyConfig(condense_sccs=True, transitive_reduction=True),
                trace_compaction=TraceCompactionConfig(
                    fold_loops=True,
                    fold_loop_with_opt=True,
                    cut_recursion=True,
                    fold_parallel_siblings=True,
                    parallel_epsilon_ns=1_000_000,
                    fold_parallel_map=True,
                    max_par_branches=6,
                    decompose_series_parallel=True,
                    max_call_depth=8,
                ),
                invariants=ViewInvariants(
                    protected_nodes=(f"svc.core_{idx}", "db.primary"),
                    preserve_reachability=(
                        ReachabilityAssertion(
                            from_node=f"svc.api_{idx}",
                            to_node="db.primary",
                            via_relations=("calls", "writes_data"),
                        ),
                    ),
                    preserve_partial_order=True,
                    forbid_new_cycles=True,
                    node_budget=BudgetRange(min_count=4, max_count=25),
                    edge_budget_max=40,
                    overflow_strategy="fail" if idx % 2 == 0 else "spanning_tree_table",
                ),
            )
        )

    return CurationSpecV1(
        global_abstractions=global_rules,
        views=tuple(views),
        metadata={"owner": "architecture-platform", "repo": "archigram"},
    )


def _build_diagnostics_batch(count: int = 200) -> tuple[LintDiagnostic, ...]:
    gates = tuple(GateId)
    return tuple(
        LintDiagnostic(
            gate=gates[idx % len(gates)],
            severity=DiagnosticSeverity.ERROR if idx % 5 == 0 else DiagnosticSeverity.WARNING,
            code=f"LINT_{idx % 10:03d}",
            message=f"Diagnostic finding {idx} on curated view",
            rule_id=f"rule_{idx % 8}",
            view_id=f"view_{idx % 12}",
            offending_symbols=(f"svc.node_{idx}", f"svc.node_{idx + 1}"),
            witness_path=(f"svc.entry_{idx % 4}", f"svc.mid_{idx % 6}", f"svc.sink_{idx % 3}"),
            repair_hint="Replace prune_leaves with bypass_vertices or adjust node_budget.",
        )
        for idx in range(count)
    )


_MULTI_VIEW_SPEC = _build_multi_view_spec(12)
_MULTI_VIEW_YAML = dump_curation_spec_yaml(_MULTI_VIEW_SPEC)
_DIAGNOSTICS_BATCH = _build_diagnostics_batch(200)


def test_parse_multi_view_curation_spec_yaml(benchmark: Any) -> None:
    parsed = benchmark(parse_curation_spec_yaml, _MULTI_VIEW_YAML)
    assert len(parsed.views) == 12
    assert len(parsed.global_abstractions) == 3


def test_dump_multi_view_curation_spec_yaml(benchmark: Any) -> None:
    dumped = benchmark(dump_curation_spec_yaml, _MULTI_VIEW_SPEC)
    assert "schema_version: '1.0'" in dumped or 'schema_version: "1.0"' in dumped


def test_export_curation_spec_json_schema(benchmark: Any) -> None:
    schema = benchmark(export_curation_spec_json_schema)
    assert schema.get("additionalProperties") is False


def test_lint_report_aggregate_200_diagnostics(benchmark: Any) -> None:
    report = benchmark(LintReport.from_diagnostics, _DIAGNOSTICS_BATCH)
    assert report.passed is False
    assert report.error_count == 40
    assert report.warning_count == 160
