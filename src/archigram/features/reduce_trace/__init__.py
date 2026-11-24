"""Deterministic Interaction Trace & Parallelism Compaction feature slice (R1-R6 + SP-Poset)."""

from __future__ import annotations

from archigram.features.reduce_trace.compactor import (
    TraceCompactionEngine,
    TraceFragment,
    TraceFragmentKind,
    build_fragment_tree_for_siblings,
    build_trace_fragment_tree,
    collect_trace_backing_ids,
)

__all__ = [
    "TraceCompactionEngine",
    "TraceFragment",
    "TraceFragmentKind",
    "build_fragment_tree_for_siblings",
    "build_trace_fragment_tree",
    "collect_trace_backing_ids",
]
