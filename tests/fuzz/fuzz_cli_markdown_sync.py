"""Property-based Hypothesis fuzz tests for Python auto-ingestion & Markdown inline diagram sync."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from archigram.domain.ir import StructuralGraphIR
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
from archigram.entrypoints.cli.app import (
    EXIT_CLEAN,
    EXIT_LINT_OR_DRIFT,
    EXIT_USAGE_ERROR,
    ApplyOptions,
    DiffOptions,
    LintOptions,
    app,
    execute_apply,
    execute_diff,
    execute_lint,
)
from archigram.entrypoints.cli.markdown_sync import (
    format_fenced_diagram_block,
    render_synced_markdown_text,
    renderer_fence_language,
    sync_markdown_views,
)
from archigram.entrypoints.cli.pipeline import (
    InputFormat,
    compile_and_lint_pipeline,
    load_canonical_ir,
)

_RUNNER = CliRunner()
_RENDERERS: tuple[TargetRenderer, ...] = (
    TargetRenderer.MERMAID,
    TargetRenderer.D2,
    TargetRenderer.PLANTUML,
    TargetRenderer.LIKEC4,
    TargetRenderer.DOT,
)
_LOWER_IDENT = st.from_regex(r"^[a-z][a-z0-9]{2,7}$", fullmatch=True)
_UPPER_IDENT = st.from_regex(r"^[A-Z][a-zA-Z0-9]{2,7}$", fullmatch=True)


@st.composite
def _synthetic_python_pkg_and_spec(
    draw: st.DrawFn,
) -> tuple[str, dict[str, str], CurationSpecV1, TargetRenderer, TargetRenderer]:
    pkg = draw(_LOWER_IDENT)
    cls_names = draw(st.lists(_UPPER_IDENT, min_size=2, max_size=2, unique=True))
    base_cls, svc_cls = f"{cls_names[0]}Core", f"{cls_names[1]}Service"
    renderer_a = draw(st.sampled_from(_RENDERERS))
    renderer_b = draw(st.sampled_from(_RENDERERS))

    files = {
        f"{pkg}/__init__.py": (
            "from __future__ import annotations\n"
            f"from .core import {base_cls}\n"
            f"from .service import {svc_cls}\n"
            f"__all__ = [{base_cls!r}, {svc_cls!r}]\n"
        ),
        f"{pkg}/core.py": (
            "from __future__ import annotations\n\n"
            f"class {base_cls}:\n"
            "    def ping(self) -> int:\n"
            "        return 1\n"
        ),
        f"{pkg}/service.py": (
            "from __future__ import annotations\n"
            f"from .core import {base_cls}\n\n"
            f"class {svc_cls}({base_cls}):\n"
            "    def run(self) -> int:\n"
            "        return self.ping()\n"
        ),
    }
    invariants = ViewInvariants(
        node_budget=BudgetRange(min_count=1, max_count=25),
        edge_budget_max=50,
    )
    spec = CurationSpecV1(
        global_abstractions=(
            PruneLeavesRule(
                id="prune_pkg_init",
                node_id_globs=(pkg,),
                direction="both",
            ),
        ),
        views=(
            DiagramViewSpec(
                id="view_primary",
                title="Primary Architecture View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderer_a,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="view_secondary",
                title="Secondary Architecture View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=renderer_b,
                invariants=invariants,
            ),
        ),
    )
    return pkg, files, spec, renderer_a, renderer_b


def _write_pkg_files(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    first_pkg = next(iter(files)).split("/", 1)[0]
    return root / first_pkg


def _build_guide_markdown(
    intro_line: str,
    foreign_content: str,
    outro_line: str,
) -> str:
    return (
        f"# Architecture Guide\n\n"
        f"{intro_line}\n\n"
        f"<!-- archigram:start view_primary -->\n"
        f"```mermaid\n"
        f"stale_placeholder_primary\n"
        f"```\n"
        f"<!-- archigram:end view_primary -->\n\n"
        f"## Foreign Trace Section\n\n"
        f"<!-- archigram:start foreign_l3_trace -->\n"
        f"{foreign_content}\n"
        f"<!-- archigram:end foreign_l3_trace -->\n\n"
        f"## Secondary View Section\n\n"
        f"<!-- archigram:start view_secondary -->\n"
        f"stale_secondary\n"
        f"<!-- archigram:end view_secondary -->\n\n"
        f"{outro_line}\n"
    )


def _assert_foreign_edit_clean_and_block_mutation_drifts(
    pkg_dir: Path,
    spec_path: Path,
    out_dir: Path,
    sync_target: Path,
    guide_md: Path,
    mutation_spec: tuple[str, str, int, TargetRenderer],
) -> None:
    synced_text, foreign_body, mutate_offset, renderer_a = mutation_spec
    foreign_block_old = (
        f"<!-- archigram:start foreign_l3_trace -->\n"
        f"{foreign_body}\n"
        f"<!-- archigram:end foreign_l3_trace -->"
    )
    foreign_block_new = (
        f"<!-- archigram:start foreign_l3_trace -->\n"
        f"{foreign_body}_updated_foreign\n"
        f"<!-- archigram:end foreign_l3_trace -->"
    )
    edited_prose_md = (
        synced_text.replace(foreign_block_old, foreign_block_new, 1)
        + "\nFooter note outside markers.\n"
    )
    guide_md.write_text(edited_prose_md, encoding="utf-8")
    check_foreign_edit = execute_apply(
        ApplyOptions(
            input_path=pkg_dir,
            spec_path=spec_path,
            out_dir=out_dir,
            check_only=True,
            sync_md=sync_target,
        )
    )
    assert check_foreign_edit.is_clean

    primary_art = check_foreign_edit.compilation.views["view_primary"]
    expected_fence = format_fenced_diagram_block(primary_art)
    assert expected_fence in edited_prose_md
    pos = mutate_offset % len(primary_art.diagram_source)
    orig_ch = primary_art.diagram_source[pos]
    replacement_ch = "X" if orig_ch != "X" else "Y"
    mutated_diag = (
        primary_art.diagram_source[:pos] + replacement_ch + primary_art.diagram_source[pos + 1 :]
    )
    mutated_fence = f"```{renderer_fence_language(renderer_a)}\n{mutated_diag.rstrip(chr(10))}\n```"
    drifted_md = edited_prose_md.replace(expected_fence, mutated_fence, 1)
    guide_md.write_text(drifted_md, encoding="utf-8")

    drifted_cli = _RUNNER.invoke(
        app,
        [
            "apply",
            str(pkg_dir),
            "--spec",
            str(spec_path),
            "--out-dir",
            str(out_dir),
            "--sync-md",
            str(sync_target),
            "--check",
        ],
    )
    assert drifted_cli.exit_code == EXIT_LINT_OR_DRIFT


@given(
    bundle=_synthetic_python_pkg_and_spec(),
    intro_text=st.from_regex(r"^[A-Za-z0-9 _.,-]{3,40}$", fullmatch=True),
    foreign_body=st.from_regex(r"^[A-Za-z0-9_ ->:]{4,40}$", fullmatch=True),
    outro_text=st.from_regex(r"^[A-Za-z0-9 _.,-]{3,40}$", fullmatch=True),
    mutate_offset=st.integers(min_value=0, max_value=64),
    sync_as_dir=st.booleans(),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_directory_auto_ingestion_and_markdown_sync_lifecycle(
    bundle: tuple[str, dict[str, str], CurationSpecV1, TargetRenderer, TargetRenderer],
    intro_text: str,
    foreign_body: str,
    outro_text: str,
    mutate_offset: int,
    sync_as_dir: bool,
) -> None:
    """Directory auto-ingestion + `--sync-md` is idempotent, preserves foreign blocks, & checks."""
    pkg, files, spec, renderer_a, renderer_b = bundle
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        pkg_dir = _write_pkg_files(tmp_dir / "src", files)
        spec_path = tmp_dir / "diagram-views.yaml"
        spec_path.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
        out_dir = tmp_dir / "out"
        docs_dir = tmp_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        guide_md = docs_dir / "GUIDE.md"
        guide_md.write_text(
            _build_guide_markdown(intro_text, foreign_body, outro_text),
            encoding="utf-8",
        )
        sync_target = docs_dir if sync_as_dir else guide_md

        ir_auto = load_canonical_ir(pkg_dir, InputFormat.AUTO)
        ir_explicit = load_canonical_ir(pkg_dir, InputFormat.PYTHON_AST)
        assert isinstance(ir_auto, StructuralGraphIR)
        assert ir_auto == ir_explicit
        assert f"{pkg}.core" in ir_auto.nodes
        assert isinstance(load_canonical_ir(pkg_dir / "core.py"), StructuralGraphIR)

        opts = ApplyOptions(
            input_path=pkg_dir,
            spec_path=spec_path,
            out_dir=out_dir,
            check_only=False,
            sync_md=sync_target,
        )
        outcome_first = execute_apply(opts)
        assert outcome_first.is_clean
        assert guide_md in outcome_first.synced_md_files
        synced_bytes = guide_md.read_bytes()
        synced_text = synced_bytes.decode("utf-8")

        assert intro_text in synced_text
        assert outro_text in synced_text
        assert (
            f"<!-- archigram:start foreign_l3_trace -->\n"
            f"{foreign_body}\n"
            f"<!-- archigram:end foreign_l3_trace -->"
        ) in synced_text
        assert f"```{renderer_fence_language(renderer_a)}\n" in synced_text
        assert f"```{renderer_fence_language(renderer_b)}\n" in synced_text

        outcome_second = execute_apply(opts)
        assert outcome_second.is_clean
        assert outcome_second.synced_md_files == ()
        assert guide_md.read_bytes() == synced_bytes

        check_res = _RUNNER.invoke(
            app,
            [
                "apply",
                str(pkg_dir),
                "--spec",
                str(spec_path),
                "--out-dir",
                str(out_dir),
                "--sync-md",
                str(sync_target),
                "--check",
            ],
        )
        assert check_res.exit_code == EXIT_CLEAN, check_res.output

        _assert_foreign_edit_clean_and_block_mutation_drifts(
            pkg_dir,
            spec_path,
            out_dir,
            sync_target,
            guide_md,
            (synced_text, foreign_body, mutate_offset, renderer_a),
        )


@given(
    bundle=_synthetic_python_pkg_and_spec(),
)
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_fuzz_cli_lint_diff_propose_on_python_directory_and_trace_json(
    bundle: tuple[str, dict[str, str], CurationSpecV1, TargetRenderer, TargetRenderer],
) -> None:
    """`lint`, `diff`, and `propose` work seamlessly on Python directories and Python trace JSON."""
    _pkg, files, spec, _ra, _rb = bundle
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        pkg_dir = _write_pkg_files(tmp_dir / "src", files)
        spec_path = tmp_dir / "diagram-views.yaml"
        spec_path.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")

        lint_outcome = execute_lint(LintOptions(input_path=pkg_dir, spec_path=spec_path))
        assert lint_outcome.lint_report.passed

        diff_outcome = execute_diff(
            DiffOptions(base_path=pkg_dir, head_path=pkg_dir, spec_path=spec_path)
        )
        assert diff_outcome.passed

        proposed_spec_path = tmp_dir / "proposed.yaml"
        propose_res = _RUNNER.invoke(
            app,
            [
                "propose",
                str(pkg_dir),
                "--out",
                str(proposed_spec_path),
            ],
        )
        assert propose_res.exit_code == EXIT_CLEAN, propose_res.output
        assert proposed_spec_path.is_file()

        # Declarative python_trace JSON auto-detection
        trace_payload = json.dumps(
            {
                "trace_id": "cli_trace_auto",
                "events": [
                    {
                        "event_id": "ev_01",
                        "caller": "pkg.service",
                        "callee": "pkg.core",
                        "signature": "ping()",
                        "start_ns": 10,
                        "end_ns": 20,
                    }
                ],
            }
        )
        trace_ir = load_canonical_ir(trace_payload, InputFormat.AUTO)
        assert trace_ir.ir_mode == "interaction_trace"

        # Empty directory raises FileNotFoundError
        empty_dir = tmp_dir / "empty_pkg"
        empty_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            load_canonical_ir(empty_dir, InputFormat.AUTO)


@given(
    bad_case=st.sampled_from(
        [
            "unclosed_start",
            "orphan_end",
            "nested_start",
            "mismatched_end",
            "unknown_strict",
        ]
    ),
    view_name=_LOWER_IDENT,
)
@settings(max_examples=20, deadline=None)
def test_fuzz_malformed_markdown_markers_raise_value_error(
    bad_case: str,
    view_name: str,
) -> None:
    """Unclosed, orphan, nested, or mismatched Markdown sync markers raise ValueError cleanly."""
    other_name = f"{view_name}_other"
    if bad_case == "unclosed_start":
        md = f"# Doc\n<!-- archigram:start {view_name} -->\n```mermaid\ngraph LR\n```\n"
    elif bad_case == "orphan_end":
        md = f"# Doc\n<!-- archigram:end {view_name} -->\n"
    elif bad_case == "nested_start":
        md = (
            f"<!-- archigram:start {view_name} -->\n"
            f"<!-- archigram:start {other_name} -->\n"
            f"<!-- archigram:end {other_name} -->\n"
            f"<!-- archigram:end {view_name} -->\n"
        )
    elif bad_case == "mismatched_end":
        md = f"<!-- archigram:start {view_name} -->\ncontent\n<!-- archigram:end {other_name} -->\n"
    else:
        md = (
            f"<!-- archigram:start {other_name} -->\ncontent\n<!-- archigram:end {other_name} -->\n"
        )

    strict = bad_case == "unknown_strict"
    with pytest.raises(ValueError):
        render_synced_markdown_text(
            md,
            {},
            strict_unknown_views=strict,
        )

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        bad_md_file = tmp_path / "broken.md"
        bad_md_file.write_text(md, encoding="utf-8")
        with pytest.raises(ValueError):
            sync_markdown_views({}, bad_md_file, strict_unknown_views=strict)

        if not strict:
            # Verify CLI apply --sync-md translates ValueError to EXIT_USAGE_ERROR (2)
            py_file = tmp_path / "mod.py"
            py_file.write_text("class A:\n    pass\n", encoding="utf-8")
            ir = load_canonical_ir(py_file)
            spec = CurationSpecV1(
                views=(
                    DiagramViewSpec(
                        id=view_name,
                        title="Test",
                        ir_mode=IRMode.STRUCTURAL_GRAPH,
                        target_renderer=TargetRenderer.MERMAID,
                        invariants=ViewInvariants(
                            node_budget=BudgetRange(min_count=1, max_count=10),
                        ),
                    ),
                )
            )
            comp = compile_and_lint_pipeline(ir, spec)
            assert comp.lint_report.passed
            spec_file = tmp_path / "spec.yaml"
            spec_file.write_text(dump_curation_spec_yaml(spec), encoding="utf-8")
            cli_res = _RUNNER.invoke(
                app,
                [
                    "apply",
                    str(py_file),
                    "--spec",
                    str(spec_file),
                    "--out-dir",
                    str(tmp_path / "out"),
                    "--sync-md",
                    str(bad_md_file),
                ],
            )
            assert cli_res.exit_code == EXIT_USAGE_ERROR
