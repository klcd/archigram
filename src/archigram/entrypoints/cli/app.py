"""CLI Driving Adapter entrypoint (`apply`, `lint`, `diff`, `propose`, `version`)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from archigram.entrypoints.cli.curate_cli import curate_app
from archigram.entrypoints.cli.diff_report import PrDiffSummary, compute_pr_regression_diff
from archigram.entrypoints.cli.markdown_sync import sync_markdown_views
from archigram.entrypoints.cli.pipeline import (
    ApplyCheckReport,
    ApplySyncOutcome,
    InputFormat,
    PipelineCompilationResult,
    build_apply_report,
    compile_and_lint_pipeline,
    format_lint_report_text,
    format_regeneration_command,
    load_canonical_ir,
    load_curation_spec,
    load_mermaid_theme,
    sync_compiled_artifacts,
)
from archigram.entrypoints.cli.triage import TriageOptions, TriageOutcome, execute_triage

if TYPE_CHECKING:
    from archigram.domain.ir import CanonicalIR

EXIT_CLEAN = 0
EXIT_LINT_OR_DRIFT = 1
EXIT_USAGE_ERROR = 2
_TYPER_RUNTIME_TYPES = (Path,)

app = typer.Typer(
    name="archigram",
    help="Deterministic graph projection, concurrency trace compaction, and 5-gate linter.",
    no_args_is_help=True,
)
app.add_typer(curate_app, name="curate")


@dataclass(frozen=True, slots=True)
class ApplyOptions:
    """Configuration options for `archigram apply`."""

    input_path: Path
    spec_path: Path
    out_dir: Path
    input_format: InputFormat = InputFormat.AUTO
    check_only: bool = False
    sync_md: Path | None = None
    mermaid_theme: Path | None = None


@dataclass(frozen=True, slots=True)
class LintOptions:
    """Configuration options for `archigram lint`."""

    input_path: Path
    spec_path: Path
    input_format: InputFormat = InputFormat.AUTO
    output_json: bool = False
    mermaid_theme: Path | None = None


@dataclass(frozen=True, slots=True)
class DiffOptions:
    """Configuration options for `archigram diff`."""

    base_path: Path | None
    head_path: Path
    spec_path: Path
    input_format: InputFormat = InputFormat.AUTO
    output_json: bool = False
    mermaid_theme: Path | None = None


@app.command("version")
def version_cmd() -> None:
    """Print ArchiGram CLI version."""
    from archigram import __version__

    typer.echo(__version__)


def apply_regeneration_command(options: ApplyOptions) -> str:
    """Return the exact `archigram apply` command that regenerates committed artifacts."""
    flags: list[tuple[str, str]] = [
        ("--spec", str(options.spec_path)),
        ("--out-dir", str(options.out_dir)),
    ]
    if options.input_format is not InputFormat.AUTO:
        flags.append(("--format", options.input_format.value))
    if options.sync_md is not None:
        flags.append(("--sync-md", str(options.sync_md)))
    if options.mermaid_theme is not None:
        flags.append(("--mermaid-theme", str(options.mermaid_theme)))
    return format_regeneration_command("apply", options.input_path, flags)


def build_apply_check_report(outcome: ApplySyncOutcome, options: ApplyOptions) -> ApplyCheckReport:
    """Structured (JSON-serializable) `apply` outcome incl. lint report and stale artifacts."""
    return build_apply_report(outcome, regeneration_command=apply_regeneration_command(options))


def _emit_stale_footer(*, lint_passed: bool, regeneration_command: str) -> None:
    if lint_passed:
        typer.echo(f"  Regenerate stale artifacts with: {regeneration_command}")
        return
    typer.echo(
        "  Fix the lint error(s) above first; regeneration refuses to write while lint fails."
    )
    typer.echo(f"  Then regenerate stale artifacts with: {regeneration_command}")


def _emit_apply_summary(outcome: ApplySyncOutcome, options: ApplyOptions) -> None:
    report_text = format_lint_report_text(outcome.compilation.lint_report)
    typer.echo(report_text)
    if options.check_only:
        for missing in outcome.missing_files:
            typer.echo(f"  [STALE] Missing committed diagram file: {missing}")
        for drifted in outcome.drifted_files:
            typer.echo(f"  [STALE] Committed diagram file drifted from spec: {drifted}")
        if outcome.missing_files or outcome.drifted_files:
            _emit_stale_footer(
                lint_passed=outcome.compilation.lint_report.passed,
                regeneration_command=apply_regeneration_command(options),
            )
        if outcome.is_clean:
            typer.echo(
                f"Check passed: {len(outcome.compilation.views)} view(s) up-to-date byte-for-byte."
            )
        return

    for path in outcome.written_files:
        typer.echo(f"  [WROTE] {path}")


def execute_apply(options: ApplyOptions) -> ApplySyncOutcome:
    """Run deterministic `apply` (or `apply --check`) pipeline without Typer framing."""
    observed_ir = load_canonical_ir(options.input_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    theme = load_mermaid_theme(options.spec_path, options.mermaid_theme)
    compilation = compile_and_lint_pipeline(observed_ir, spec, mermaid_theme=theme)
    return sync_compiled_artifacts(
        compilation,
        options.out_dir,
        check_only=options.check_only,
        sync_md_target=options.sync_md,
        markdown_syncer=sync_markdown_views,
    )


def execute_lint(options: LintOptions) -> PipelineCompilationResult:
    """Run deterministic 5-Gate `lint` pipeline without Typer framing."""
    observed_ir = load_canonical_ir(options.input_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    theme = load_mermaid_theme(options.spec_path, options.mermaid_theme)
    return compile_and_lint_pipeline(observed_ir, spec, mermaid_theme=theme)


def _load_optional_base(
    base_path: Path | None,
    input_format: InputFormat,
) -> tuple[CanonicalIR | None, str | None]:
    """Load the baseline IR, or return `(None, reason)` when it is omitted or unusable."""
    if base_path is None:
        return None, "--base was not provided"
    try:
        return load_canonical_ir(base_path, input_format), None
    except (OSError, ValueError, SyntaxError) as exc:
        return None, f"base input '{base_path}' could not be loaded ({type(exc).__name__}: {exc})"


def execute_diff(options: DiffOptions) -> PrDiffSummary:
    """Run deterministic baseline-vs-PR `diff` pipeline without Typer framing.

    `head` and `spec` must load (errors propagate -> usage error). An omitted or unreadable base
    degrades to head-only Gate 1-5 validation reported via `base_available=False`.
    """
    head_ir = load_canonical_ir(options.head_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    theme = load_mermaid_theme(options.spec_path, options.mermaid_theme)
    base_ir, reason = _load_optional_base(options.base_path, options.input_format)
    return compute_pr_regression_diff(
        base_ir,
        head_ir,
        spec,
        base_unavailable_reason=reason,
        mermaid_theme=theme,
    )


@app.command("apply")
def apply_cmd(  # noqa: PLR0913, PLR0917
    input_path: Annotated[
        Path,
        typer.Argument(
            help="Path to input Python directory/.py or topology/trace file (.dot, .json, .scip)."
        ),
    ],
    spec: Annotated[
        Path,
        typer.Option("--spec", "-s", help="Path to CurationSpecV1 YAML (diagram-views.yaml)."),
    ],
    out_dir: Annotated[
        Path,
        typer.Option("--out-dir", "-o", help="Directory where target diagram files are emitted."),
    ],
    input_format: Annotated[
        InputFormat,
        typer.Option(
            "--format",
            "-f",
            help="Input format (auto, ir_json, dot, scip, otlp_json, class_json, python_ast).",
        ),
    ] = InputFormat.AUTO,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Verify committed diagram outputs match freshly compiled outputs byte-for-byte.",
        ),
    ] = False,
    sync_md: Annotated[
        Path | None,
        typer.Option(
            "--sync-md",
            help="Optional Markdown file or directory to sync inline archigram diagram blocks.",
        ),
    ] = None,
    mermaid_theme: Annotated[
        Path | None,
        typer.Option(
            "--mermaid-theme",
            help="Optional path to mermaid-theme.yaml (defaults to sibling of --spec).",
        ),
    ] = None,
) -> None:
    """Compile Canonical IR + CurationSpecV1 into deterministic diagram files (or --check)."""
    options = ApplyOptions(
        input_path=input_path,
        spec_path=spec,
        out_dir=out_dir,
        input_format=input_format,
        check_only=check,
        sync_md=sync_md,
        mermaid_theme=mermaid_theme,
    )
    try:
        outcome = execute_apply(options)
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

    _emit_apply_summary(outcome, options)
    if not outcome.is_clean:
        raise typer.Exit(code=EXIT_LINT_OR_DRIFT)


@app.command("lint")
def lint_cmd(
    input_path: Annotated[
        Path,
        typer.Argument(help="Path to input topology/trace file (.dot, .json, .scip, .puml)."),
    ],
    spec: Annotated[
        Path,
        typer.Option("--spec", "-s", help="Path to CurationSpecV1 YAML (diagram-views.yaml)."),
    ],
    input_format: Annotated[
        InputFormat,
        typer.Option(
            "--format", "-f", help="Input format (auto, ir_json, dot, scip, otlp_json, class_json)."
        ),
    ] = InputFormat.AUTO,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit structured LintReport JSON instead of human text."),
    ] = False,
    mermaid_theme: Annotated[
        Path | None,
        typer.Option(
            "--mermaid-theme",
            help="Optional path to mermaid-theme.yaml (defaults to sibling of --spec).",
        ),
    ] = None,
) -> None:
    """Run all 5 ArchiGram Linter Gates against input topology/trace and curation spec."""
    try:
        compilation = execute_lint(
            LintOptions(
                input_path=input_path,
                spec_path=spec,
                input_format=input_format,
                output_json=output_json,
                mermaid_theme=mermaid_theme,
            )
        )
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

    report = compilation.lint_report
    if output_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(format_lint_report_text(report))

    if not report.passed:
        raise typer.Exit(code=EXIT_LINT_OR_DRIFT)


@app.command("diff")
def diff_cmd(
    head: Annotated[
        Path,
        typer.Option("--head", "--head-input", help="PR head input topology/trace file."),
    ],
    spec: Annotated[
        Path,
        typer.Option("--spec", "-s", help="Path to CurationSpecV1 YAML (diagram-views.yaml)."),
    ],
    base: Annotated[
        Path | None,
        typer.Option(
            "--base",
            "--base-input",
            help=(
                "Baseline input topology/trace file. If omitted or unreadable, only head is "
                "validated (exit status reflects head Gates 1-5)."
            ),
        ),
    ] = None,
    input_format: Annotated[
        InputFormat,
        typer.Option(
            "--format", "-f", help="Input format (auto, ir_json, dot, scip, otlp_json, class_json)."
        ),
    ] = InputFormat.AUTO,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit structured PrDiffSummary JSON instead of PR Markdown."),
    ] = False,
    mermaid_theme: Annotated[
        Path | None,
        typer.Option(
            "--mermaid-theme",
            help="Optional path to mermaid-theme.yaml (defaults to sibling of --spec).",
        ),
    ] = None,
) -> None:
    """Compare baseline vs PR head across views and render PR Markdown regression summary."""
    try:
        summary = execute_diff(
            DiffOptions(
                base_path=base,
                head_path=head,
                spec_path=spec,
                input_format=input_format,
                output_json=output_json,
                mermaid_theme=mermaid_theme,
            )
        )
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

    if not summary.base_available:
        typer.echo(
            f"Warning: base comparison unavailable ({summary.base_unavailable_reason}); "
            "validated head only.",
            err=True,
        )
    if output_json:
        typer.echo(summary.model_dump_json(indent=2))
    else:
        typer.echo(summary.markdown_report)

    if not summary.passed:
        raise typer.Exit(code=EXIT_LINT_OR_DRIFT)


@app.command("propose")
def propose_cmd(
    input_path: Annotated[
        Path,
        typer.Argument(help="Path to input topology/trace file to propose a CurationSpecV1 for."),
    ],
    out_spec: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Optional path to write proposed diagram-views.yaml."),
    ] = None,
    input_format: Annotated[
        InputFormat,
        typer.Option(
            "--format", "-f", help="Input format (auto, ir_json, dot, scip, otlp_json, class_json)."
        ),
    ] = InputFormat.AUTO,
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Maximum 3-turn self-healing repair iterations."),
    ] = 3,
) -> None:
    """Delegate to the MCP/LLM control-plane `run_propose_cli` entrypoint if available."""
    try:
        mcp_mod = importlib.import_module("archigram.entrypoints.mcp")
        runner = getattr(mcp_mod, "run_propose_cli", None)
    except ImportError:
        runner = None

    if not callable(runner):
        typer.echo(
            "Control-plane 'propose' handler (archigram.entrypoints.mcp.run_propose_cli) "
            "is not installed in this environment.",
            err=True,
        )
        raise typer.Exit(code=EXIT_USAGE_ERROR)

    if out_spec is not None:
        exit_code = runner(
            input_path=str(input_path),
            out_spec_path=str(out_spec),
            max_turns=max_turns,
            ir_loader=load_canonical_ir,
        )
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_out = Path(tmp_dir) / "diagram-views.yaml"
            exit_code = runner(
                input_path=str(input_path),
                out_spec_path=str(tmp_out),
                max_turns=max_turns,
                ir_loader=load_canonical_ir,
            )
            if exit_code == 0 and tmp_out.is_file():
                typer.echo(tmp_out.read_text(encoding="utf-8"), nl=False)

    if isinstance(exit_code, int) and exit_code != 0:
        raise typer.Exit(code=exit_code)


def triage_regeneration_command(options: TriageOptions) -> str:
    """Return the exact `archigram triage` command that regenerates committed triage files."""
    flags: list[tuple[str, str]] = [
        ("--spec", str(options.spec_path)),
        ("--view", options.view_id),
        ("--out-dir", str(options.out_dir)),
    ]
    if options.input_format is not InputFormat.AUTO:
        flags.append(("--format", options.input_format.value))
    return format_regeneration_command("triage", options.input_path, flags)


def _emit_triage_summary(outcome: TriageOutcome, options: TriageOptions) -> None:
    st = outcome.stats
    limit_note = (
        "EXCEEDS 5,000 Gitea limit -> SVG comparison required"
        if st.exceeds_gitea_mermaid_limit
        else "within 5,000 Gitea limit"
    )
    typer.echo(format_lint_report_text(outcome.compilation.lint_report))
    typer.echo(
        f"Triage [{st.view_id}]: raw={st.raw_elements} nodes/{st.raw_links} links "
        f"({st.raw_mermaid_chars} Mermaid chars: {limit_note}) -> "
        f"curated={st.curated_elements} nodes/{st.curated_links} links "
        f"({st.curated_mermaid_chars} Mermaid chars) | "
        f"keep={st.kept_count}, remove={st.removed_count}, "
        f"bypass={st.bypassed_count}, collapse={st.collapsed_count}"
    )
    if options.check_only:
        for missing in outcome.missing_files:
            typer.echo(f"  [STALE] Missing triage artifact: {missing}")
        for drifted in outcome.drifted_files:
            typer.echo(f"  [STALE] Drifted triage artifact: {drifted}")
        if outcome.missing_files or outcome.drifted_files:
            _emit_stale_footer(
                lint_passed=outcome.compilation.lint_report.passed,
                regeneration_command=triage_regeneration_command(options),
            )
        return
    for written in outcome.written_files:
        typer.echo(f"  [WROTE] {written}")


@app.command("triage")
def triage_cmd(
    input_path: Annotated[
        Path,
        typer.Argument(help="Path to program-extracted source directory, topology, or trace."),
    ],
    spec: Annotated[
        Path,
        typer.Option("--spec", "-s", help="Path to CurationSpecV1 YAML."),
    ],
    view: Annotated[
        str,
        typer.Option("--view", "-v", help="Target view ID to render 3-stage triage for."),
    ],
    out_dir: Annotated[
        Path,
        typer.Option("--out-dir", "-o", help="Directory for 1_original, 2_triage, 3_curated."),
    ],
    input_format: Annotated[
        InputFormat,
        typer.Option("--format", "-f", help="Input format (auto, python_ast, ir_json, ...)."),
    ] = InputFormat.AUTO,
    check: Annotated[
        bool,
        typer.Option("--check", help="Verify committed 3-stage triage files and SVGs match."),
    ] = False,
) -> None:
    """Render 3-stage comparison (1_original -> 2_triage diff -> 3_curated) as SVGs + Mermaid."""
    options = TriageOptions(
        input_path=input_path,
        spec_path=spec,
        view_id=view,
        out_dir=out_dir,
        input_format=input_format,
        check_only=check,
    )
    try:
        outcome = execute_triage(options)
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

    _emit_triage_summary(outcome, options)
    if not outcome.is_clean:
        raise typer.Exit(code=EXIT_LINT_OR_DRIFT)


def main() -> None:
    """Entrypoint registered in pyproject.toml."""
    app()
