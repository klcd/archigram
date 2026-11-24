"""Zero-LLM CLI Data Plane Driving Adapter (`apply`, `lint`, `diff`, `propose`)."""

from __future__ import annotations

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
    main,
)
from archigram.entrypoints.cli.diff_report import (
    PrDiffSummary,
    ViewRegressionDelta,
    compute_pr_regression_diff,
)
from archigram.entrypoints.cli.pipeline import (
    ApplySyncOutcome,
    CompiledViewArtifact,
    InputFormat,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
    detect_input_format,
    emit_deterministic_dot,
    emit_view_source,
    format_lint_report_text,
    load_canonical_ir,
    load_curation_spec,
    renderer_file_extension,
    sync_compiled_artifacts,
)

__all__ = [
    "EXIT_CLEAN",
    "EXIT_LINT_OR_DRIFT",
    "EXIT_USAGE_ERROR",
    "ApplyOptions",
    "ApplySyncOutcome",
    "CompiledViewArtifact",
    "DiffOptions",
    "InputFormat",
    "LintOptions",
    "PipelineCompilationResult",
    "PrDiffSummary",
    "ViewRegressionDelta",
    "app",
    "compile_and_lint_pipeline",
    "compute_pr_regression_diff",
    "detect_input_format",
    "emit_deterministic_dot",
    "emit_view_source",
    "execute_apply",
    "execute_diff",
    "execute_lint",
    "format_lint_report_text",
    "load_canonical_ir",
    "load_curation_spec",
    "main",
    "renderer_file_extension",
    "sync_compiled_artifacts",
]
