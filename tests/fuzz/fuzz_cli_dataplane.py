"""Property-based Hypothesis fuzz tests for the Zero-LLM CLI Data Plane."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import DiagnosticSeverity, GateId, LintReport
from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import (
    BudgetRange,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    PruneLeavesRule,
    ReachabilityAssertion,
    SimplifyConfig,
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
from archigram.entrypoints.cli.diff_report import PrDiffSummary, compute_pr_regression_diff
from archigram.entrypoints.cli.pipeline import (
    CompiledViewArtifact,
    InputFormat,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
    detect_input_format,
    emit_deterministic_dot,
    load_canonical_ir,
    load_curation_spec,
    renderer_file_extension,
    resolve_contained_output_path,
    sync_compiled_artifacts,
)
from archigram.entrypoints.cli.triage import (
    TriageOptions,
    compute_triage_stages,
    execute_triage,
)

_RUNNER = CliRunner()
_RENDERERS: tuple[TargetRenderer, ...] = (
    TargetRenderer.MERMAID,
    TargetRenderer.D2,
    TargetRenderer.PLANTUML,
    TargetRenderer.LIKEC4,
    TargetRenderer.DOT,
)


def _obs_stamp() -> ProvenanceStamp:
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="fuzz_cli_dataplane",
        input_sha256="a" * 64,
        file_path="arch.dot",
    )


@st.composite
def _connected_structural_graphs(draw: st.DrawFn) -> StructuralGraphIR:
    node_count = draw(st.integers(min_value=3, max_value=8))
    node_ids = [f"svc_{idx}" for idx in range(node_count)]
    stamp = _obs_stamp()
    nodes = {
        nid: IRNode(
            symbol_id=nid,
            label=f"Service {idx}",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        )
        for idx, nid in enumerate(node_ids)
    }
    edges: list[IREdge] = []
    for idx in range(node_count - 1):
        edges.append(
            IREdge(
                edge_id=f"e_{idx}",
                source=node_ids[idx],
                target=node_ids[idx + 1],
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                weight=float(idx + 1),
                provenance=stamp,
            )
        )
    extra_idx = draw(st.integers(min_value=0, max_value=node_count - 3))
    if extra_idx + 2 < node_count:
        edges.append(
            IREdge(
                edge_id="e_shortcut",
                source=node_ids[extra_idx],
                target=node_ids[extra_idx + 2],
                relation=EdgeRelation.IMPORTS,
                sync_kind=SyncKind.SYNC,
                weight=0.5,
                provenance=stamp,
            )
        )
    return StructuralGraphIR(
        graph_id="fuzz_graph",
        nodes=nodes,
        edges=tuple(edges),
    )


def _build_valid_spec(
    graph: StructuralGraphIR,
    renderer: TargetRenderer,
    *,
    overflow_spanning_tree: bool = False,
) -> CurationSpecV1:
    sorted_ids = sorted(graph.nodes)
    first_id = sorted_ids[0]
    last_id = sorted_ids[-1]
    edge_max = 1 if overflow_spanning_tree else max(20, len(graph.edges) + 5)
    overflow_strategy = "spanning_tree_table" if overflow_spanning_tree else "fail"
    view = DiagramViewSpec(
        id="arch_overview",
        title="Architecture Overview",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=renderer,
        invariants=ViewInvariants(
            protected_nodes=(first_id, last_id),
            preserve_reachability=(ReachabilityAssertion(from_node=first_id, to_node=last_id),),
            node_budget=BudgetRange(min_count=1, max_count=25),
            edge_budget_max=edge_max,
            overflow_strategy=overflow_strategy,
        ),
    )
    return CurationSpecV1(views=(view,))


@given(
    graph=_connected_structural_graphs(),
    renderer=st.sampled_from(_RENDERERS),
    mutation_offset=st.integers(min_value=0, max_value=128),
    use_dot_input=st.booleans(),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_cli_apply_and_check_drift_detection(
    graph: StructuralGraphIR,
    renderer: TargetRenderer,
    mutation_offset: int,
    use_dot_input: bool,
) -> None:
    """`apply` followed by `apply --check` exits 0; mutating any emitted byte exits 1."""
    spec = _build_valid_spec(graph, renderer)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        if use_dot_input:
            input_file = tmp_dir / "input.dot"
            input_file.write_text(emit_deterministic_dot(graph), encoding="utf-8")
        else:
            input_file = tmp_dir / "input.ir.json"
            input_file.write_text(graph.model_dump_json(indent=2), encoding="utf-8")

        spec_file = tmp_dir / "diagram-views.yaml"
        spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
        out_dir = tmp_dir / "out"

        apply_res = _RUNNER.invoke(
            app,
            [
                "apply",
                str(input_file),
                "--spec",
                str(spec_file),
                "--out-dir",
                str(out_dir),
            ],
        )
        assert apply_res.exit_code == EXIT_CLEAN, apply_res.output

        ext = renderer_file_extension(renderer)
        emitted_file = out_dir / f"arch_overview{ext}"
        assert emitted_file.is_file()

        check_clean = _RUNNER.invoke(
            app,
            [
                "apply",
                str(input_file),
                "--spec",
                str(spec_file),
                "--out-dir",
                str(out_dir),
                "--check",
            ],
        )
        assert check_clean.exit_code == EXIT_CLEAN, check_clean.output

        raw_bytes = bytearray(emitted_file.read_bytes())
        assert len(raw_bytes) > 0
        pos = mutation_offset % len(raw_bytes)
        raw_bytes[pos] = 0x21 if raw_bytes[pos] != 0x21 else 0x3F
        emitted_file.write_bytes(bytes(raw_bytes))

        check_mutated = _RUNNER.invoke(
            app,
            [
                "apply",
                str(input_file),
                "--spec",
                str(spec_file),
                "--out-dir",
                str(out_dir),
                "--check",
            ],
        )
        assert check_mutated.exit_code == EXIT_LINT_OR_DRIFT


@given(
    graph=_connected_structural_graphs(),
    renderer=st.sampled_from(_RENDERERS),
    inject_bad_glob=st.booleans(),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_cli_lint_json_parity_with_exit_code(
    graph: StructuralGraphIR,
    renderer: TargetRenderer,
    inject_bad_glob: bool,
) -> None:
    """`lint --json` emits valid JSON LintReport whose `passed` boolean matches exit_code == 0."""
    base_spec = _build_valid_spec(graph, renderer)
    if inject_bad_glob:
        bad_rule = PruneLeavesRule(
            id="bad_zero_match_glob",
            node_id_globs=("nonexistent_ghost_*",),
            direction="sink",
        )
        spec = base_spec.model_copy(update={"global_abstractions": (bad_rule,)})
    else:
        spec = base_spec

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        input_file = tmp_dir / "input.json"
        input_file.write_text(graph.model_dump_json(), encoding="utf-8")
        spec_file = tmp_dir / "diagram-views.yaml"
        spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")

        lint_res = _RUNNER.invoke(
            app,
            [
                "lint",
                str(input_file),
                "--spec",
                str(spec_file),
                "--json",
            ],
        )
        parsed_report = LintReport.model_validate_json(lint_res.stdout)
        expected_code = EXIT_CLEAN if parsed_report.passed else EXIT_LINT_OR_DRIFT
        assert lint_res.exit_code == expected_code
        assert parsed_report.passed is (not inject_bad_glob)


@given(
    graph=_connected_structural_graphs(),
    renderer=st.sampled_from(_RENDERERS),
    break_head_protected=st.booleans(),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_cli_diff_identical_vs_broken_protected_node(
    graph: StructuralGraphIR,
    renderer: TargetRenderer,
    break_head_protected: bool,
) -> None:
    """`diff` on identical inputs exits 0; breaking a protected node in head exits 1."""
    spec = _build_valid_spec(graph, renderer)
    sorted_ids = sorted(graph.nodes)
    last_id = sorted_ids[-1]

    if break_head_protected:
        remaining_nodes = {k: v for k, v in graph.nodes.items() if k != last_id}
        remaining_edges = tuple(e for e in graph.edges if last_id not in {e.source, e.target})
        head_graph = StructuralGraphIR(
            graph_id=graph.graph_id,
            nodes=remaining_nodes,
            edges=remaining_edges,
        )
    else:
        head_graph = graph

    summary = compute_pr_regression_diff(graph, head_graph, spec)
    assert summary.passed is (not break_head_protected)
    assert summary.has_regressions is break_head_protected

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        base_file = tmp_dir / "base.json"
        head_file = tmp_dir / "head.json"
        spec_file = tmp_dir / "diagram-views.yaml"
        base_file.write_text(graph.model_dump_json(), encoding="utf-8")
        head_file.write_text(head_graph.model_dump_json(), encoding="utf-8")
        spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")

        diff_md_res = _RUNNER.invoke(
            app,
            [
                "diff",
                "--base",
                str(base_file),
                "--head",
                str(head_file),
                "--spec",
                str(spec_file),
            ],
        )
        expected_exit = EXIT_LINT_OR_DRIFT if break_head_protected else EXIT_CLEAN
        assert diff_md_res.exit_code == expected_exit
        assert "ArchiGram PR Architectural & Topological Diff" in diff_md_res.stdout

        diff_json_res = _RUNNER.invoke(
            app,
            [
                "diff",
                "--base",
                str(base_file),
                "--head",
                str(head_file),
                "--spec",
                str(spec_file),
                "--json",
            ],
        )
        assert diff_json_res.exit_code == expected_exit
        parsed_diff = PrDiffSummary.model_validate_json(diff_json_res.stdout)
        assert parsed_diff.passed is (not break_head_protected)


@given(
    graph=_connected_structural_graphs(),
    renderer=st.sampled_from(_RENDERERS),
)
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_spanning_tree_overflow_and_trace_pipeline(
    graph: StructuralGraphIR,
    renderer: TargetRenderer,
) -> None:
    """Spanning-tree overflow emits companion `.overflow.md` and trace views compile cleanly."""
    spec = _build_valid_spec(graph, renderer, overflow_spanning_tree=True)
    comp = compile_and_lint_pipeline(graph, spec)
    assert comp.lint_report.passed
    art = comp.views["arch_overview"]
    if len(graph.edges) > 1:
        assert art.overflow_markdown != ""
        assert "Companion overflow table" in art.diagram_source

    stamp = _obs_stamp()
    trace = InteractionTraceIR(
        trace_id="t1",
        participants={
            "p_api": TraceParticipant(
                participant_id="p_api",
                symbol_id="svc.api",
                label="API",
                provenance=stamp,
            ),
            "p_db": TraceParticipant(
                participant_id="p_db",
                symbol_id="svc.db",
                label="DB",
                provenance=stamp,
            ),
        },
        events={
            "ev_1": TraceEvent(
                event_id="ev_1",
                caller_id="p_api",
                callee_id="p_db",
                signature="SELECT 1",
                clock_domain_id="clk_1",
                start_ns=10,
                end_ns=50,
                provenance=stamp,
            )
        },
    )
    trace_spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="trace_view",
                title="Trace View",
                ir_mode=IRMode.INTERACTION_TRACE,
                target_renderer=renderer,
            ),
        )
    )
    trace_comp = compile_and_lint_pipeline(trace, trace_spec)
    assert trace_comp.lint_report.passed


@given(
    fmt_choice=st.sampled_from(
        [
            InputFormat.DOT,
            InputFormat.SCIP,
            InputFormat.OTLP_JSON,
            InputFormat.CLASS_JSON,
            InputFormat.IR_JSON,
        ]
    )
)
@settings(max_examples=15, deadline=None)
def test_fuzz_multi_format_ingestion_and_usage_errors(fmt_choice: InputFormat) -> None:
    """Auto-detection and explicit `--format` load all 5 input formats and reject bad paths."""
    if fmt_choice is InputFormat.DOT:
        payload = 'digraph G { "a" -> "b"; }'
    elif fmt_choice is InputFormat.SCIP:
        payload = json.dumps(
            {
                "documents": [
                    {
                        "relative_path": "app.py",
                        "symbols": [{"symbol": "pkg/A#", "kind": "Class"}],
                    }
                ]
            }
        )
    elif fmt_choice is InputFormat.OTLP_JSON:
        payload = json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "11" * 16,
                                        "spanId": "22" * 8,
                                        "name": "GET /health",
                                        "startTimeUnixNano": "100",
                                        "endTimeUnixNano": "200",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
    elif fmt_choice is InputFormat.CLASS_JSON:
        payload = json.dumps(
            {
                "classes": [
                    {"id": "pkg.Base", "name": "Base", "kind": "class"},
                    {
                        "id": "pkg.Child",
                        "name": "Child",
                        "kind": "class",
                        "bases": ["pkg.Base"],
                    },
                ]
            }
        )
    else:
        stamp = _obs_stamp()
        ir = StructuralGraphIR(
            nodes={
                "n1": IRNode(
                    symbol_id="n1",
                    label="N1",
                    kind=NodeKind.SERVICE,
                    provenance=stamp,
                )
            }
        )
        payload = ir.model_dump_json()

    detected = detect_input_format(payload, None, InputFormat.AUTO)
    assert detected is fmt_choice
    loaded = load_canonical_ir(payload, fmt_choice)
    assert loaded.schema_version == "1.0"

    bad_res = _RUNNER.invoke(
        app,
        ["lint", "/nonexistent/missing_input.dot", "--spec", "/nonexistent/missing.yaml"],
    )
    assert bad_res.exit_code == EXIT_USAGE_ERROR
    assert load_curation_spec("schema_version: '1.0'\nviews: []\n").schema_version == "1.0"


_TRAVERSAL_VIEW_IDS = st.one_of(
    st.sampled_from(
        [
            "../../README",
            "../outside",
            "..\\outside",
            "nested/view",
            "nested\\view",
            "/etc/passwd",
            ".",
            "..",
            ".hidden",
            "a..b",
            "trailing.",
            "view\x00id",
            "view\nid",
            "C:drive",
        ]
    ),
    st.text(alphabet="/\\.abcABC01_\x00\n:", min_size=1, max_size=16).filter(
        lambda s: "/" in s or "\\" in s or ".." in s or "\x00" in s or "\n" in s or ":" in s
    ),
)

_SAFE_VIEW_IDS = st.from_regex(r"[a-z][a-z0-9_-]{1,12}", fullmatch=True)


def _minimal_graph() -> StructuralGraphIR:
    stamp = _obs_stamp()
    return StructuralGraphIR(
        graph_id="contain_graph",
        nodes={
            "svc_a": IRNode(
                symbol_id="svc_a", label="Service A", kind=NodeKind.SERVICE, provenance=stamp
            ),
            "svc_b": IRNode(
                symbol_id="svc_b", label="Service B", kind=NodeKind.SERVICE, provenance=stamp
            ),
        },
        edges=(
            IREdge(
                edge_id="e0",
                source="svc_a",
                target="svc_b",
                relation=EdgeRelation.CALLS,
                sync_kind=SyncKind.SYNC,
                provenance=stamp,
            ),
        ),
    )


def _tamper_compilation_view_id(
    comp: PipelineCompilationResult,
    bad_id: str,
) -> PipelineCompilationResult:
    orig_art = next(iter(comp.views.values()))
    tampered_view = orig_art.view.model_copy(update={"id": bad_id})
    tampered_art = CompiledViewArtifact(
        view=tampered_view,
        curated_ir=orig_art.curated_ir,
        emitted_ir=orig_art.emitted_ir,
        diagram_source=orig_art.diagram_source,
        file_extension=orig_art.file_extension,
        overflow_markdown=orig_art.overflow_markdown,
    )
    return PipelineCompilationResult(
        observed_ir=comp.observed_ir,
        spec=comp.spec.model_copy(update={"views": (tampered_view,)}),
        views={bad_id: tampered_art},
        lint_report=comp.lint_report,
        extra_diagnostics=comp.extra_diagnostics,
    )


@given(
    bad_id=_TRAVERSAL_VIEW_IDS,
    renderer=st.sampled_from(_RENDERERS),
    check_only=st.booleans(),
)
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_apply_and_triage_block_traversal_view_ids(
    bad_id: str,
    renderer: TargetRenderer,
    check_only: bool,
) -> None:
    """Traversal view IDs fail at spec parsing, CLI apply/triage, and tampered pipeline sync."""
    graph = _minimal_graph()
    valid_spec = _build_valid_spec(graph, renderer)
    comp = compile_and_lint_pipeline(graph, valid_spec)
    tampered_comp = _tamper_compilation_view_id(comp, bad_id)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        out_dir = tmp_dir / "sub" / "out"
        sentinel = tmp_dir / "README"
        sentinel.write_text("SENTINEL", encoding="utf-8")

        with pytest.raises(ValueError):
            sync_compiled_artifacts(tampered_comp, out_dir, check_only=check_only)
        with pytest.raises(ValueError):
            compute_triage_stages(graph, valid_spec, bad_id)

        input_file = tmp_dir / "input.ir.json"
        input_file.write_text(graph.model_dump_json(), encoding="utf-8")
        valid_spec_file = tmp_dir / "valid.yaml"
        valid_spec_file.write_text(dump_curation_spec_yaml(valid_spec), encoding="utf-8")
        bad_spec_file = tmp_dir / "bad.yaml"
        bad_payload = valid_spec.model_dump(mode="json")
        bad_payload["views"][0]["id"] = bad_id
        bad_spec_file.write_text(yaml.safe_dump(bad_payload, sort_keys=False), encoding="utf-8")

        with pytest.raises(ValueError):
            execute_triage(
                TriageOptions(
                    input_path=input_file,
                    spec_path=valid_spec_file,
                    view_id=bad_id,
                    out_dir=out_dir,
                    check_only=check_only,
                )
            )

        apply_args = [
            "apply",
            str(input_file),
            "--spec",
            str(bad_spec_file),
            "--out-dir",
            str(out_dir),
        ]
        if check_only:
            apply_args.append("--check")
        assert _RUNNER.invoke(app, apply_args).exit_code == EXIT_USAGE_ERROR

        if "\x00" not in bad_id:
            triage_args = [
                "triage",
                str(input_file),
                "--spec",
                str(valid_spec_file),
                "--view",
                bad_id,
                "--out-dir",
                str(out_dir),
            ]
            if check_only:
                triage_args.append("--check")
            assert _RUNNER.invoke(app, triage_args).exit_code == EXIT_USAGE_ERROR

        assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
        assert not out_dir.exists()


@given(
    safe_id=_SAFE_VIEW_IDS,
    renderer=st.sampled_from(_RENDERERS),
    check_only=st.booleans(),
    escape_target=st.sampled_from(["diagram", "overflow", "svg", "triage_d2", "triage_svg"]),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_output_containment_blocks_symlink_and_path_escapes(
    safe_id: str,
    renderer: TargetRenderer,
    check_only: bool,
    escape_target: str,
) -> None:
    """Symlinks inside out_dir pointing outside cannot be read or overwritten by apply or triage."""
    graph = _minimal_graph()
    is_overflow = escape_target == "overflow"
    view = DiagramViewSpec(
        id=safe_id,
        title="Containment View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=renderer,
        render_svg=escape_target == "svg",
        invariants=ViewInvariants(
            node_budget=BudgetRange(min_count=1, max_count=10),
            edge_budget_max=0 if is_overflow else 20,
            overflow_strategy="spanning_tree_table" if is_overflow else "fail",
        ),
    )
    spec = CurationSpecV1(views=(view,))
    comp = compile_and_lint_pipeline(graph, spec)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        outside_secret = tmp_dir / "outside_secret.txt"
        outside_secret.write_text("UNTOUCHED", encoding="utf-8")

        ext = renderer_file_extension(renderer)
        symlink_names = {
            "diagram": f"{safe_id}{ext}",
            "overflow": f"{safe_id}.overflow.md",
            "svg": f"{safe_id}{ext}.svg",
            "triage_d2": f"{safe_id}.1_original.d2",
            "triage_svg": f"{safe_id}.1_original.d2.svg",
        }
        (out_dir / symlink_names[escape_target]).symlink_to(outside_secret)

        with pytest.raises(ValueError):
            resolve_contained_output_path(out_dir, f"../{safe_id}{ext}")

        if escape_target in {"diagram", "overflow", "svg"}:
            with pytest.raises(ValueError):
                sync_compiled_artifacts(comp, out_dir, check_only=check_only)
        else:
            input_file = tmp_dir / "input.ir.json"
            input_file.write_text(graph.model_dump_json(), encoding="utf-8")
            spec_file = tmp_dir / "spec.yaml"
            spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
            with pytest.raises(ValueError):
                execute_triage(
                    TriageOptions(
                        input_path=input_file,
                        spec_path=spec_file,
                        view_id=safe_id,
                        out_dir=out_dir,
                        check_only=check_only,
                    )
                )

        assert outside_secret.read_text(encoding="utf-8") == "UNTOUCHED"


def _build_cycle_inducing_dag(mid_count: int) -> StructuralGraphIR:
    stamp = _obs_stamp()
    node_ids = ["part_head", *(f"mid_{idx}" for idx in range(mid_count)), "part_tail"]
    nodes = {
        nid: IRNode(
            symbol_id=nid,
            label=nid.replace("_", " ").title(),
            kind=NodeKind.SERVICE,
            provenance=stamp,
        )
        for nid in node_ids
    }
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=node_ids[idx],
            target=node_ids[idx + 1],
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        )
        for idx in range(len(node_ids) - 1)
    )
    return StructuralGraphIR(graph_id="cycle_dag", nodes=nodes, edges=edges)


def _build_cycle_inducing_spec(
    renderer: TargetRenderer,
    *,
    condense_sccs: bool,
    transitive_reduction: bool,
    rule_in_global: bool,
) -> CurationSpecV1:
    rule = CollapsePartitionRule(
        id="collapse_cycle_rule",
        target_node_id="part_super",
        label="Partition Supernode",
        member_globs=("part_*",),
    )
    cycle_view = DiagramViewSpec(
        id="cycle_view",
        title="Cycle Inducing View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=renderer,
        rules=() if rule_in_global else (rule,),
        simplify=SimplifyConfig(
            condense_sccs=condense_sccs,
            transitive_reduction=transitive_reduction,
        ),
        invariants=ViewInvariants(
            forbid_new_cycles=True,
            node_budget=BudgetRange(min_count=1, max_count=20),
            edge_budget_max=40,
        ),
    )
    views: list[DiagramViewSpec] = [cycle_view]
    if not rule_in_global:
        views.append(
            DiagramViewSpec(
                id="valid_sibling",
                title="Valid Sibling View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderer,
                invariants=ViewInvariants(
                    forbid_new_cycles=True,
                    node_budget=BudgetRange(min_count=1, max_count=20),
                    edge_budget_max=40,
                ),
            )
        )
    return CurationSpecV1(
        global_abstractions=(rule,) if rule_in_global else (),
        views=tuple(views),
    )


@given(
    mid_count=st.integers(min_value=1, max_value=5),
    renderer=st.sampled_from(_RENDERERS),
    condense_sccs=st.booleans(),
    transitive_reduction=st.booleans(),
    rule_in_global=st.booleans(),
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_fuzz_collapse_induced_cycle_fails_pipeline_and_blocks_apply(
    mid_count: int,
    renderer: TargetRenderer,
    condense_sccs: bool,
    transitive_reduction: bool,
    rule_in_global: bool,
) -> None:
    """`collapse_partition` inducing a cycle fails with INDUCED_PARTITION_CYCLE and blocks apply."""
    graph = _build_cycle_inducing_dag(mid_count)
    spec = _build_cycle_inducing_spec(
        renderer,
        condense_sccs=condense_sccs,
        transitive_reduction=transitive_reduction,
        rule_in_global=rule_in_global,
    )

    comp = compile_and_lint_pipeline(graph, spec)
    assert not comp.lint_report.passed
    assert "cycle_view" not in comp.views
    if not rule_in_global:
        assert "valid_sibling" in comp.views

    cycle_diags = [d for d in comp.lint_report.diagnostics if d.code == "INDUCED_PARTITION_CYCLE"]
    assert len(cycle_diags) == 1
    diag = cycle_diags[0]
    assert diag.gate is GateId.GATE_3_TOPOLOGY_POSET
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.rule_id == "collapse_cycle_rule"
    assert diag.view_id == "cycle_view"
    assert len(diag.witness_path) >= 3

    with pytest.raises(ValueError, match="INDUCED_PARTITION_CYCLE"):
        compute_triage_stages(graph, spec, "cycle_view")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        input_file = tmp_dir / "input.ir.json"
        input_file.write_text(graph.model_dump_json(), encoding="utf-8")
        spec_file = tmp_dir / "diagram-views.yaml"
        spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
        out_dir = tmp_dir / "out"

        lint_res = _RUNNER.invoke(
            app,
            ["lint", str(input_file), "--spec", str(spec_file), "--json"],
        )
        assert lint_res.exit_code == EXIT_LINT_OR_DRIFT
        parsed_report = LintReport.model_validate_json(lint_res.stdout)
        assert not parsed_report.passed
        assert any(d.code == "INDUCED_PARTITION_CYCLE" for d in parsed_report.diagnostics)

        apply_res = _RUNNER.invoke(
            app,
            ["apply", str(input_file), "--spec", str(spec_file), "--out-dir", str(out_dir)],
        )
        assert apply_res.exit_code == EXIT_LINT_OR_DRIFT
        assert not out_dir.exists() or not any(out_dir.rglob("*"))
