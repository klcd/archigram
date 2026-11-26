"""Hypothesis fuzz suite for AG-02: actionable, distinguishable CI failure diagnostics.

One CI log (`archigram apply --check`) must let a reader tell apart a missing symbol, a
missing observed path, a path severed by curation, and a stale committed artifact, while
check mode leaves every file and directory untouched.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

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
    BypassVerticesRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    KHopNeighborhoodScope,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.app import (
    EXIT_LINT_OR_DRIFT,
    ApplyOptions,
    app,
    build_apply_check_report,
    execute_apply,
)
from archigram.entrypoints.cli.pipeline import (
    ApplyCheckReport,
    InputFormat,
    format_lint_report_text,
)

_RUNNER = CliRunner()
_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_ci_diagnostics",
    input_sha256="c" * 64,
    file_path="arch.json",
)
_IDENT = st.from_regex(r"[a-z][a-z0-9_]{2,8}", fullmatch=True)
_TEXT = st.from_regex(r"[a-z][a-z0-9_ ]{1,24}", fullmatch=True)


def _chain(length: int) -> tuple[StructuralGraphIR, list[str]]:
    ids = [f"svc_{idx}" for idx in range(length)]
    graph = StructuralGraphIR(
        graph_id="ci_diag",
        nodes={
            nid: IRNode(symbol_id=nid, label=nid, kind=NodeKind.SERVICE, provenance=_STAMP)
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
            for idx in range(length - 1)
        ),
    )
    return graph, ids


def _view(view_id: str, **kwargs: object) -> DiagramViewSpec:
    return DiagramViewSpec.model_validate(
        {
            "id": view_id,
            "title": view_id,
            "ir_mode": IRMode.STRUCTURAL_GRAPH,
            "target_renderer": TargetRenderer.MERMAID,
            **kwargs,
        }
    )


def _ok_view() -> DiagramViewSpec:
    return _view("v_ok")


def _failing_views(ids: list[str], ghost: str, mid: int) -> tuple[DiagramViewSpec, ...]:
    first, last = ids[0], ids[-1]
    path = ReachabilityAssertion(from_node=first, to_node=last)
    return (
        _view("v_unknown", invariants=ViewInvariants(protected_nodes=(ghost,))),
        _view(
            "v_notobs",
            invariants=ViewInvariants(
                require_reachability=(ReachabilityAssertion(from_node=last, to_node=first),)
            ),
        ),
        _view(
            "v_scope",
            scope=KHopNeighborhoodScope(focal_nodes=(first,), upstream_depth=0, downstream_depth=1),
            invariants=ViewInvariants(preserve_reachability=(path,)),
        ),
        _view(
            "v_rule",
            rules=(
                BypassVerticesRule(
                    id="drop_mid",
                    node_id_globs=(ids[mid],),
                    preserve_edge_relations=("imports",),
                ),
            ),
            invariants=ViewInvariants(preserve_reachability=(path,)),
        ),
    )


def _write_inputs(root: Path, graph: StructuralGraphIR, spec: CurationSpecV1) -> tuple[Path, Path]:
    input_file = root / "input.ir.json"
    input_file.write_text(graph.model_dump_json(indent=2), encoding="utf-8")
    spec_file = root / "diagram-views.yaml"
    spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
    return input_file, spec_file


def _snapshot(root: Path) -> dict[str, tuple[bool, bytes, int]]:
    """Return (is_dir, content, mtime_ns) for every path under root (including dirs)."""
    snap: dict[str, tuple[bool, bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        is_dir = path.is_dir()
        snap[path.relative_to(root).as_posix()] = (
            is_dir,
            b"" if is_dir else path.read_bytes(),
            path.stat().st_mtime_ns,
        )
    return snap


def _error_blocks(output: str) -> dict[str, list[str]]:
    """Group CI log lines by the `[ERROR] CODE (...)` header that introduced them."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        header = re.match(r"\s+\[ERROR\] (\w+) \((.*?)\)", line)
        if header:
            current = blocks.setdefault(f"{header.group(1)}|{header.group(2)}", [])
            current.append(line)
        elif current is not None and line.startswith("    "):
            current.append(line)
        else:
            current = None
    return blocks


def _block_for(blocks: dict[str, list[str]], code: str, view_id: str) -> list[str]:
    matches = [v for k, v in blocks.items() if k.startswith(f"{code}|") and f"view={view_id}" in k]
    assert len(matches) == 1, (code, view_id, list(blocks))
    return matches[0]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    length=st.integers(min_value=5, max_value=8),
    mid_offset=st.integers(min_value=0, max_value=100),
    ghost=_IDENT,
    stale_is_missing=st.booleans(),
    explicit_format=st.booleans(),
    use_sync_md=st.booleans(),
)
def test_single_ci_log_distinguishes_all_four_failure_causes(
    length: int,
    mid_offset: int,
    ghost: str,
    stale_is_missing: bool,
    explicit_format: bool,
    use_sync_md: bool,
) -> None:
    """One `apply --check` log shows missing symbol, no observed path, severed path, and stale."""
    graph, ids = _chain(length)
    mid = 1 + (mid_offset % (length - 2))
    ghost_id = f"ghost_{ghost}"
    full_spec = CurationSpecV1(views=(_ok_view(), *_failing_views(ids, ghost_id, mid)))
    ok_spec = CurationSpecV1(views=(_ok_view(),))

    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str)
        input_file, spec_file = _write_inputs(root, graph, full_spec)
        ok_spec_file = root / "ok-views.yaml"
        ok_spec_file.write_text(dump_curation_spec_yaml(ok_spec), encoding="utf-8")
        out_dir = root / "out"
        docs_dir = root / "docs"
        docs_dir.mkdir()
        (docs_dir / "README.md").write_text("# Docs without diagram blocks\n", encoding="utf-8")

        write = _RUNNER.invoke(
            app,
            ["apply", str(input_file), "--spec", str(ok_spec_file), "--out-dir", str(out_dir)],
        )
        assert write.exit_code == 0, write.output
        stale_file = out_dir / "v_ok.mmd"
        assert stale_file.is_file()
        if stale_is_missing:
            stale_file.unlink()
        else:
            stale_file.write_bytes(stale_file.read_bytes() + b"%% hand edit\n")

        args = ["apply", str(input_file), "--spec", str(spec_file), "--out-dir", str(out_dir)]
        expected_command = f"archigram apply {input_file} --spec {spec_file} --out-dir {out_dir}"
        if explicit_format:
            args += ["--format", "ir_json"]
            expected_command += " --format ir_json"
        if use_sync_md:
            args += ["--sync-md", str(docs_dir)]
            expected_command += f" --sync-md {docs_dir}"
        args.append("--check")

        before = _snapshot(root)
        result = _RUNNER.invoke(app, args)
        assert _snapshot(root) == before  # check mode never touches the disk
        assert result.exit_code == EXIT_LINT_OR_DRIFT
        _assert_four_causes(result.output, ids, ghost_id, mid, stale_is_missing)
        assert "Regenerate stale artifacts with:" not in result.output  # lint fails first
        assert f"Then regenerate stale artifacts with: {expected_command}" in result.output
        assert "Fix the lint error(s) above first" in result.output


def _assert_four_causes(
    output: str,
    ids: list[str],
    ghost_id: str,
    mid: int,
    stale_is_missing: bool,
) -> None:
    blocks = _error_blocks(output)
    first, last = ids[0], ids[-1]

    unknown = _block_for(blocks, "UNKNOWN_SYMBOL_ID", "v_unknown")
    assert any(f"Symbols: {ghost_id}" in line for line in unknown)
    assert any("Hint:" in line for line in unknown)
    assert not any("Witness:" in line for line in unknown)

    not_observed = _block_for(blocks, "REACHABILITY_NOT_OBSERVED", "v_notobs")
    assert any(f"Endpoints: {last} -> {first}" in line for line in not_observed)
    assert any("over any relation" in line for line in not_observed)
    assert not any("Witness:" in line for line in not_observed)  # no fabricated witness

    witness = f"Witness: {' -> '.join(ids)}"
    scoped = _block_for(blocks, "REACHABILITY_SEVERED", "v_scope")
    assert any(f"Endpoints: {first} -> {last}" in line for line in scoped)
    assert any(witness in line for line in scoped)
    assert any("Bottlenecks:" in line for line in scoped)
    assert any("k_hop_neighborhood" in line and "excluded node(s)" in line for line in scoped)
    assert not any("rule=" in line for line in scoped[:1])

    ruled = _block_for(blocks, "REACHABILITY_SEVERED", "v_rule")
    assert "rule=drop_mid" in ruled[0]
    assert any(f"Endpoints: {first} -> {last}" in line for line in ruled)
    assert any(witness in line for line in ruled)
    assert any(f"Bottlenecks: {ids[mid]}" in line for line in ruled)
    assert any("bypass_vertices 'drop_mid'" in line for line in ruled)

    kind = (
        "Missing committed diagram file" if stale_is_missing else "Committed diagram file drifted"
    )
    stale_lines = [ln for ln in output.splitlines() if "[STALE]" in ln and "v_ok.mmd" in ln]
    assert len(stale_lines) == 1
    assert kind in stale_lines[0]


def _stage_committed_output(
    root: Path,
    input_file: Path,
    out_dir: Path,
    ok_spec: CurationSpecV1,
    scenario: str,
    mutation_offset: int,
) -> None:
    """Pre-populate `out_dir` with committed artifacts, then break them per scenario."""
    if scenario == "no_out_dir":
        return
    ok_file = root / "ok.yaml"
    ok_file.write_text(dump_curation_spec_yaml(ok_spec), encoding="utf-8")
    res = _RUNNER.invoke(
        app, ["apply", str(input_file), "--spec", str(ok_file), "--out-dir", str(out_dir)]
    )
    assert res.exit_code == 0, res.output
    ok_file.unlink()
    emitted = out_dir / "v_ok.mmd"
    if scenario == "missing_file":
        emitted.unlink()
    elif scenario == "drifted_file":
        raw = bytearray(emitted.read_bytes())
        pos = mutation_offset % len(raw)
        raw[pos] = 0x21 if raw[pos] != 0x21 else 0x3F
        emitted.write_bytes(bytes(raw))


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    length=st.integers(min_value=5, max_value=8),
    scenario=st.sampled_from(
        ("pass", "lint_failure", "missing_file", "drifted_file", "no_out_dir")
    ),
    mutation_offset=st.integers(min_value=0, max_value=512),
    use_sync_md=st.booleans(),
)
def test_apply_check_never_touches_files_or_directories(
    length: int,
    scenario: str,
    mutation_offset: int,
    use_sync_md: bool,
) -> None:
    """`apply --check` leaves bytes, mtimes, and directory trees identical in every outcome."""
    graph, ids = _chain(length)
    ok_spec = CurationSpecV1(views=(_ok_view(),))
    failing = _failing_views(ids, "ghost_x", 1)[1]  # REACHABILITY_NOT_OBSERVED
    spec = CurationSpecV1(views=(_ok_view(), failing)) if scenario == "lint_failure" else ok_spec

    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str)
        input_file, spec_file = _write_inputs(root, graph, spec)
        out_dir = root / "out"
        docs_dir = root / "docs"
        docs_dir.mkdir()
        (docs_dir / "notes.md").write_text("plain notes\n", encoding="utf-8")

        _stage_committed_output(root, input_file, out_dir, ok_spec, scenario, mutation_offset)

        args = ["apply", str(input_file), "--spec", str(spec_file), "--out-dir", str(out_dir)]
        if use_sync_md:
            args += ["--sync-md", str(docs_dir)]
        before = _snapshot(root)
        result = _RUNNER.invoke(app, [*args, "--check"])
        assert _snapshot(root) == before
        if scenario == "no_out_dir":
            assert not out_dir.exists()
        expected_exit = 0 if scenario == "pass" else EXIT_LINT_OR_DRIFT
        assert result.exit_code == expected_exit, result.output
        if scenario == "pass":
            assert "[STALE]" not in result.output
            assert "regenerate" not in result.output.lower()
        else:
            assert (scenario == "lint_failure") == ("REACHABILITY_NOT_OBSERVED" in result.output)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    length=st.integers(min_value=5, max_value=8),
    mid_offset=st.integers(min_value=0, max_value=100),
    stale_is_missing=st.booleans(),
)
def test_structured_json_preserves_exact_diagnostic_fields(
    length: int,
    mid_offset: int,
    stale_is_missing: bool,
) -> None:
    """`lint --json` and the structured apply report keep witness, rule, and stale fields."""
    graph, ids = _chain(length)
    mid = 1 + (mid_offset % (length - 2))
    spec = CurationSpecV1(views=(_ok_view(), *_failing_views(ids, "ghost_z", mid)))

    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str)
        input_file, spec_file = _write_inputs(root, graph, spec)
        res = _RUNNER.invoke(app, ["lint", str(input_file), "--spec", str(spec_file), "--json"])
        assert res.exit_code == EXIT_LINT_OR_DRIFT
        raw = json.loads(res.stdout)
        expected_keys = {
            "gate",
            "severity",
            "code",
            "message",
            "rule_id",
            "view_id",
            "offending_symbols",
            "witness_path",
            "repair_hint",
        }
        assert all(set(diag) == expected_keys for diag in raw["diagnostics"])
        report = LintReport.model_validate(raw)
        by_view = {(d.view_id, d.code): d for d in report.diagnostics}

        unknown = by_view[("v_unknown", "UNKNOWN_SYMBOL_ID")]
        assert unknown.code == "UNKNOWN_SYMBOL_ID"
        assert unknown.offending_symbols == ("ghost_z",)
        assert unknown.witness_path == ()

        not_observed = next(d for d in raw["diagnostics"] if d["view_id"] == "v_notobs")
        assert not_observed["code"] == "REACHABILITY_NOT_OBSERVED"
        assert not_observed["witness_path"] == []  # never fabricated
        assert not_observed["offending_symbols"] == [ids[-1], ids[0]]
        assert not_observed["gate"] == GateId.GATE_3_TOPOLOGY_POSET.value
        assert not_observed["severity"] == DiagnosticSeverity.ERROR.value

        scoped = by_view[("v_scope", "REACHABILITY_SEVERED")]
        assert scoped.code == "REACHABILITY_SEVERED"
        assert scoped.witness_path == tuple(ids)
        assert scoped.rule_id is None
        assert "k_hop_neighborhood" in scoped.message

        ruled = by_view[("v_rule", "REACHABILITY_SEVERED")]
        assert ruled.code == "REACHABILITY_SEVERED"
        assert ruled.witness_path == tuple(ids)
        assert ruled.rule_id == "drop_mid"
        assert ruled.offending_symbols == (ids[mid],)

        _assert_structured_apply_report(root, input_file, stale_is_missing)


def _assert_structured_apply_report(root: Path, input_file: Path, stale_is_missing: bool) -> None:
    ok_spec_file = root / "ok.yaml"
    ok_spec_file.write_text(
        dump_curation_spec_yaml(CurationSpecV1(views=(_ok_view(),))), encoding="utf-8"
    )
    out_dir = root / "out"
    write_opts = ApplyOptions(input_path=input_file, spec_path=ok_spec_file, out_dir=out_dir)
    assert execute_apply(write_opts).is_clean
    emitted = out_dir / "v_ok.mmd"
    if stale_is_missing:
        emitted.unlink()
    else:
        emitted.write_text("drifted\n", encoding="utf-8")

    check_opts = ApplyOptions(
        input_path=input_file,
        spec_path=ok_spec_file,
        out_dir=out_dir,
        input_format=InputFormat.IR_JSON,
        check_only=True,
    )
    before = _snapshot(root)
    outcome = execute_apply(check_opts)
    assert _snapshot(root) == before
    report = build_apply_check_report(outcome, check_opts)
    assert ApplyCheckReport.model_validate_json(report.model_dump_json()) == report
    assert report.passed is True
    assert report.is_clean is False
    assert (report.missing_files if stale_is_missing else report.drifted_files) == (str(emitted),)
    assert (report.drifted_files if stale_is_missing else report.missing_files) == ()
    assert report.regeneration_command == (
        f"archigram apply {input_file} --spec {ok_spec_file} --out-dir {out_dir} --format ir_json"
    )


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    length=st.integers(min_value=3, max_value=7),
    explicit_format=st.booleans(),
)
def test_triage_check_names_missing_files_and_regeneration_command(
    length: int,
    explicit_format: bool,
) -> None:
    """`triage --check` lists every missing artifact, prints the regen command, writes nothing."""
    graph, _ids = _chain(length)
    spec = CurationSpecV1(views=(_ok_view(),))
    with tempfile.TemporaryDirectory() as tmp_str:
        root = Path(tmp_str)
        input_file, spec_file = _write_inputs(root, graph, spec)
        out_dir = root / "triage"
        args = [
            "triage",
            str(input_file),
            "--spec",
            str(spec_file),
            "--view",
            "v_ok",
            "--out-dir",
            str(out_dir),
        ]
        expected = (
            f"archigram triage {input_file} --spec {spec_file} --view v_ok --out-dir {out_dir}"
        )
        if explicit_format:
            args += ["--format", "ir_json"]
            expected += " --format ir_json"
        before = _snapshot(root)
        result = _RUNNER.invoke(app, [*args, "--check"])
        assert _snapshot(root) == before
        assert not out_dir.exists()
        assert result.exit_code == EXIT_LINT_OR_DRIFT
        for stem in ("v_ok.1_original.d2", "v_ok.2_triage.d2", "v_ok.3_curated.d2"):
            assert f"[STALE] Missing triage artifact: {out_dir / stem}" in result.output
        assert f"[STALE] Missing triage artifact: {out_dir / 'v_ok.3_curated.mmd'}" in result.output
        assert f"Regenerate stale artifacts with: {expected}" in result.output


@st.composite
def _diagnostics(draw: st.DrawFn) -> LintDiagnostic:
    code = draw(
        st.sampled_from(
            ("UNKNOWN_SYMBOL_ID", "REACHABILITY_NOT_OBSERVED", "REACHABILITY_SEVERED", "OTHER")
        )
    )
    symbols = tuple(draw(st.lists(_IDENT, min_size=0, max_size=4, unique=True)))
    witness = tuple(draw(st.lists(_IDENT, min_size=0, max_size=6)))
    if code == "REACHABILITY_NOT_OBSERVED":
        witness = ()  # a missing observed path never carries a witness
        symbols = ("end_a", "end_b")
    return LintDiagnostic(
        gate=draw(st.sampled_from(list(GateId))),
        severity=draw(st.sampled_from(list(DiagnosticSeverity))),
        code=code,
        message=draw(_TEXT),
        rule_id=draw(st.none() | _IDENT),
        view_id=draw(st.none() | _IDENT),
        offending_symbols=symbols,
        witness_path=witness,
        repair_hint=draw(_TEXT),
    )


@settings(max_examples=120, deadline=None)
@given(diags=st.lists(_diagnostics(), min_size=0, max_size=8))
def test_format_lint_report_text_renders_witness_only_when_present(
    diags: list[LintDiagnostic],
) -> None:
    """Every diagnostic shows severity/code/gate/view/rule/hint; Witness only if non-empty."""
    report = LintReport.from_diagnostics(diags)
    text = format_lint_report_text(report)
    lines = text.splitlines()

    assert text == format_lint_report_text(report)
    assert text.count("    Hint: ") == len(diags)
    assert sum(1 for ln in lines if ln.startswith("    Witness: ")) == sum(
        1 for d in diags if d.witness_path
    )
    headers = [ln for ln in lines if re.match(r"  \[(ERROR|WARNING)\] ", ln)]
    assert len(headers) == len(diags)
    for header, diag in zip(headers, diags, strict=True):
        assert f"[{diag.severity.value.upper()}] {diag.code} ({diag.gate.value}" in header
        assert (f"view={diag.view_id}" in header) is (diag.view_id is not None)
        assert (f"rule={diag.rule_id}" in header) is (diag.rule_id is not None)
        assert header.endswith(diag.message)
    for diag in diags:
        if diag.witness_path:
            assert f"    Witness: {' -> '.join(diag.witness_path)}" in lines
        if diag.code == "REACHABILITY_NOT_OBSERVED":
            assert (
                f"    Endpoints: {diag.offending_symbols[0]} -> {diag.offending_symbols[1]}"
                in lines
            )
