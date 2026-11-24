"""Gates 1 & 2: Closed-world referential grounding, glob integrity, and boundary/leaf safety."""

from __future__ import annotations

from archigram.features.lint_grounding.gates import (
    Gate1GroundingGate,
    Gate2BoundaryLeafGate,
    GroundingAndBoundaryGate,
    collect_observed_symbols,
    match_symbols_for_globs,
    matches_glob,
    run_gate_1_grounding,
    run_gate_2_boundary_leaf,
)

__all__ = [
    "Gate1GroundingGate",
    "Gate2BoundaryLeafGate",
    "GroundingAndBoundaryGate",
    "collect_observed_symbols",
    "match_symbols_for_globs",
    "matches_glob",
    "run_gate_1_grounding",
    "run_gate_2_boundary_leaf",
]
