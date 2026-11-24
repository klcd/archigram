"""ArchiGram MCP Control Plane Server exposing digest, 5-Gate lint, and view preview tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer as FastMCP

from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.spec import parse_curation_spec_yaml
from archigram.entrypoints.mcp.propose_loop import (
    CuratedViewsPreviewReport,
    evaluate_curation_spec,
    make_schema_error_diagnostic,
    parse_canonical_ir_json,
)
from archigram.features.digest import GraphDigestGenerator

_DIGEST_GENERATOR = GraphDigestGenerator()
_CHARS_PER_TOKEN = 3
_MIN_TOKEN_BUDGET = 64
_MAX_TOKEN_BUDGET = 4_000


def _make_ir_error_diagnostic(exc: Exception) -> LintDiagnostic:
    err_text = str(exc).strip() or "Invalid CanonicalIR JSON payload."
    return LintDiagnostic(
        gate=GateId.GATE_1_GROUNDING,
        severity=DiagnosticSeverity.ERROR,
        code="INVALID_CANONICAL_IR_JSON",
        message=f"Failed to parse or validate CanonicalIR JSON: {err_text}",
        offending_symbols=("ir_mode",),
        repair_hint=(
            "Provide a valid CanonicalIR JSON object with schema_version='1.0' and "
            "ir_mode='structural_graph' or 'interaction_trace'."
        ),
    )


def _clamp_digest_to_tokens(markdown: str, max_tokens: int) -> str:
    clamped_tokens = max(_MIN_TOKEN_BUDGET, min(max_tokens, _MAX_TOKEN_BUDGET))
    max_chars = min(12_000, clamped_tokens * _CHARS_PER_TOKEN)
    if len(markdown) <= max_chars:
        return markdown
    suffix = f"\n... [digest truncated to stay under {clamped_tokens}-token budget]"
    keep = max(0, max_chars - len(suffix))
    return markdown[:keep] + suffix


def get_ir_digest(ir_json: str, max_tokens: int = 3800) -> str:
    """Validate CanonicalIR JSON and return a compact (<4,000-token) Markdown digest."""
    ir = parse_canonical_ir_json(ir_json)
    clamped_tokens = max(_MIN_TOKEN_BUDGET, min(max_tokens, _MAX_TOKEN_BUDGET))
    max_items = max(3, min(25, clamped_tokens // 180))
    digest_md = _DIGEST_GENERATOR.to_compact_markdown(ir, max_items_per_section=max_items)
    return _clamp_digest_to_tokens(digest_md, clamped_tokens)


def validate_curation_spec(ir_json: str, spec_yaml: str) -> str:
    """Parse CurationSpecV1 YAML, run reductions + Gates 1-5, and return JSON LintReport."""
    try:
        ir = parse_canonical_ir_json(ir_json)
    except Exception as exc:
        report = LintReport.from_diagnostics((_make_ir_error_diagnostic(exc),))
        return report.model_dump_json(indent=2)

    try:
        spec = parse_curation_spec_yaml(spec_yaml)
    except Exception as exc:
        report = LintReport.from_diagnostics((make_schema_error_diagnostic(exc),))
        return report.model_dump_json(indent=2)

    preview_report = evaluate_curation_spec(ir, spec)
    return preview_report.lint_report.model_dump_json(indent=2)


def preview_curated_views(ir_json: str, spec_yaml: str) -> str:
    """Compile every view in CurationSpecV1 and return JSON counts, lint report, and diagrams."""
    try:
        ir = parse_canonical_ir_json(ir_json)
    except Exception as exc:
        report = LintReport.from_diagnostics((_make_ir_error_diagnostic(exc),))
        out = CuratedViewsPreviewReport(passed=False, lint_report=report, views=())
        return out.model_dump_json(indent=2)

    try:
        spec = parse_curation_spec_yaml(spec_yaml)
    except Exception as exc:
        report = LintReport.from_diagnostics((make_schema_error_diagnostic(exc),))
        out = CuratedViewsPreviewReport(passed=False, lint_report=report, views=())
        return out.model_dump_json(indent=2)

    preview_report = evaluate_curation_spec(ir, spec)
    return preview_report.model_dump_json(indent=2)


def create_mcp_server(name: str = "archigram") -> FastMCP:
    """Construct the deterministic ArchiGram MCP server exposing digest, lint, and preview tools."""
    server = FastMCP(name)
    server.add_tool(
        get_ir_digest,
        name="get_ir_digest",
        description=(
            "Validate CanonicalIR JSON, run GraphDigestGenerator, and return a compact "
            "(<4,000-token) Markdown digest (Louvain clusters, PageRank hubs, leaf sinks, "
            "wrapper chains, and R5/R6 concurrent fanout hotspots)."
        ),
    )
    server.add_tool(
        validate_curation_spec,
        name="validate_curation_spec",
        description=(
            "Parse CurationSpecV1 YAML, execute reductions + Gates 1-5, and return a "
            "structured JSON LintReport with passed, offending_symbols, witness_path, "
            "and actionable repair_hints."
        ),
    )
    server.add_tool(
        preview_curated_views,
        name="preview_curated_views",
        description=(
            "Compile every view in CurationSpecV1, returning JSON with per-view "
            "node/edge/branch counts, LintReport summary, and deterministically emitted "
            ".mmd / .d2 / .puml / .c4 / .dot diagram text."
        ),
    )
    return server


def run_mcp_stdio_server() -> None:
    """Run the ArchiGram MCP server over stdio."""
    server = create_mcp_server()
    server.run(transport="stdio")
