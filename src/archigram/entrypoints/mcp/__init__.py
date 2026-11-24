"""ArchiGram MCP Control Plane Server and Bounded 3-Turn Lint-Repair Loop."""

from __future__ import annotations

from archigram.entrypoints.mcp.propose_loop import (
    CompiledViewPreview,
    CuratedViewsPreviewReport,
    DeterministicHeuristicProposer,
    ProposeResult,
    RuleProposerProtocol,
    evaluate_curation_spec,
    repair_curation_spec_from_diagnostics,
    run_propose_cli,
    run_propose_loop,
)
from archigram.entrypoints.mcp.server import (
    FastMCP,
    create_mcp_server,
    get_ir_digest,
    preview_curated_views,
    run_mcp_stdio_server,
    validate_curation_spec,
)

__all__ = [
    "CompiledViewPreview",
    "CuratedViewsPreviewReport",
    "DeterministicHeuristicProposer",
    "FastMCP",
    "ProposeResult",
    "RuleProposerProtocol",
    "create_mcp_server",
    "evaluate_curation_spec",
    "get_ir_digest",
    "preview_curated_views",
    "repair_curation_spec_from_diagnostics",
    "run_mcp_stdio_server",
    "run_propose_cli",
    "run_propose_loop",
    "validate_curation_spec",
]
