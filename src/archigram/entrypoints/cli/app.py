"""CLI Driving Adapter entrypoint (`apply`, `lint`, `diff`, `propose`, `version`)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from archigram.entrypoints.cli.diff_report import PrDiffSummary, compute_pr_regression_diff
from archigram.entrypoints.cli.pipeline import (
    ApplySyncOutcome,
    InputFormat,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
    format_lint_report_text,
    load_canonical_ir,
    load_curation_spec,
    sync_compiled_artifacts,
)

EXIT_CLEAN = 0
EXIT_LINT_OR_DRIFT = 1
EXIT_USAGE_ERROR = 2
_TYPER_RUNTIME_TYPES = (Path,)

app = typer.Typer(
    name="archigram",
    help="Deterministic graph projection, concurrency trace compaction, and 5-gate linter.",
    no_args_is_help=True,
)


@dataclass(frozen=True, slots=True)
class ApplyOptions:
    """Configuration options for `archigram apply`."""

    input_path: Path
    spec_path: Path
    out_dir: Path
    input_format: InputFormat = InputFormat.AUTO
    check_only: bool = False
    sync_md: Path | None = None


@dataclass(frozen=True, slots=True)
class LintOptions:
    """Configuration options for `archigram lint`."""

    input_path: Path
    spec_path: Path
    input_format: InputFormat = InputFormat.AUTO
    output_json: bool = False


@dataclass(frozen=True, slots=True)
class DiffOptions:
    """Configuration options for `archigram diff`."""

    base_path: Path
    head_path: Path
    spec_path: Path
    input_format: InputFormat = InputFormat.AUTO
    output_json: bool = False


@app.command("version")
def version_cmd() -> None:
    """Print ArchiGram CLI version."""
    from archigram import __version__

    typer.echo(__version__)


def _emit_apply_summary(outcome: ApplySyncOutcome, *, check_only: bool) -> None:
    report_text = format_lint_report_text(outcome.compilation.lint_report)
    typer.echo(report_text)
    if check_only:
        for missing in outcome.missing_files:
            typer.echo(f"  [STALE] Missing committed diagram file: {missing}")
        for drifted in outcome.drifted_files:
            typer.echo(f"  [STALE] Committed diagram file drifted from spec: {drifted}")
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
    compilation = compile_and_lint_pipeline(observed_ir, spec)
    return sync_compiled_artifacts(
        compilation,
        options.out_dir,
        check_only=options.check_only,
        sync_md_target=options.sync_md,
    )


def execute_lint(options: LintOptions) -> PipelineCompilationResult:
    """Run deterministic 5-Gate `lint` pipeline without Typer framing."""
    observed_ir = load_canonical_ir(options.input_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    return compile_and_lint_pipeline(observed_ir, spec)


def execute_diff(options: DiffOptions) -> PrDiffSummary:
    """Run deterministic baseline-vs-PR `diff` pipeline without Typer framing."""
    base_ir = load_canonical_ir(options.base_path, options.input_format)
    head_ir = load_canonical_ir(options.head_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    return compute_pr_regression_diff(base_ir, head_ir, spec)


@app.command("apply")
def apply_cmd(
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
) -> None:
    """Compile Canonical IR + CurationSpecV1 into deterministic diagram files (or --check)."""
    try:
        outcome = execute_apply(
            ApplyOptions(
                input_path=input_path,
                spec_path=spec,
                out_dir=out_dir,
                input_format=input_format,
                check_only=check,
                sync_md=sync_md,
            )
        )
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

    _emit_apply_summary(outcome, check_only=check)
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
) -> None:
    """Run all 5 ArchiGram Linter Gates against input topology/trace and curation spec."""
    try:
        compilation = execute_lint(
            LintOptions(
                input_path=input_path,
                spec_path=spec,
                input_format=input_format,
                output_json=output_json,
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
    base: Annotated[
        Path,
        typer.Option("--base", "--base-input", help="Baseline input topology/trace file."),
    ],
    head: Annotated[
        Path,
        typer.Option("--head", "--head-input", help="PR head input topology/trace file."),
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
        typer.Option("--json", help="Emit structured PrDiffSummary JSON instead of PR Markdown."),
    ] = False,
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
            )
        )
    except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Input/Usage Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc

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
        )
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_out = Path(tmp_dir) / "diagram-views.yaml"
            exit_code = runner(
                input_path=str(input_path),
                out_spec_path=str(tmp_out),
                max_turns=max_turns,
            )
            if exit_code == 0 and tmp_out.is_file():
                typer.echo(tmp_out.read_text(encoding="utf-8"), nl=False)

    if isinstance(exit_code, int) and exit_code != 0:
        raise typer.Exit(code=exit_code)


def main() -> None:
    """Entrypoint registered in pyproject.toml."""
    app()
