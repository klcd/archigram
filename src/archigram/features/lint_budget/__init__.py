"""Gate 4: Cognitive view budgets, spanning-tree overflow fallback, and orphan detection."""

from archigram.features.lint_budget.gate import (
    CognitiveBudgetAndOrphanGate,
    apply_spanning_tree_edge_fallback,
)

__all__ = [
    "CognitiveBudgetAndOrphanGate",
    "apply_spanning_tree_edge_fallback",
]
