"""Pure domain helpers shared across UML 2.5 diagram emitters (D2, Mermaid, PlantUML)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archigram.domain.concurrency import order_trace_events
from archigram.domain.ir import NodeKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from archigram.domain.ir import (
        InteractionTraceIR,
        IRNode,
        StructuralGraphIR,
        TraceEvent,
    )

_ACTOR_TOKENS: tuple[str, ...] = ("cli", "mcp", "entrypoint")


def shorten_signature(sig: str) -> str:
    """Shorten a Python or RPC call signature for compact UML edge labels."""
    base = sig.split("->", 1)[0].strip()
    if "(" in base:
        fn_name = base.split("(", 1)[0].strip().rsplit(".", 1)[-1]
        return f"{fn_name}()" if fn_name else base[:32]
    return base[:36] if base else "call"


def number_trace_events(trace: InteractionTraceIR) -> list[tuple[str, TraceEvent]]:
    """Assign hierarchical UML Communication Diagram sequence numbers (`1`, `1.1`, `2`)."""
    by_parent: dict[str | None, list[TraceEvent]] = {}
    for ev in order_trace_events(trace.events):
        parent = ev.parent_event_id if ev.parent_event_id in trace.events else None
        by_parent.setdefault(parent, []).append(ev)

    numbered: list[tuple[str, TraceEvent]] = []
    visited: set[str] = set()

    def _walk(parent_id: str | None, prefix: str) -> None:
        for idx, child in enumerate(by_parent.get(parent_id, ()), start=1):
            if child.event_id in visited:
                continue
            visited.add(child.event_id)
            seq_no = f"{prefix}.{idx}" if prefix else str(idx)
            numbered.append((seq_no, child))
            _walk(child.event_id, seq_no)

    _walk(None, "")
    return numbered


def structural_start_end_nodes(ir: StructuralGraphIR) -> tuple[list[str], list[str]]:
    """Select deterministic start and end node IDs for State and Activity structural graphs."""
    sorted_ids = sorted(ir.nodes)
    if not sorted_ids:
        return [], []
    in_deg = dict.fromkeys(sorted_ids, 0)
    out_deg = dict.fromkeys(sorted_ids, 0)
    for edge in ir.edges:
        in_deg[edge.target] += 1
        out_deg[edge.source] += 1
    starts = [sid for sid in sorted_ids if in_deg[sid] == 0] or [sorted_ids[0]]
    ends = [sid for sid in sorted_ids if out_deg[sid] == 0] or [sorted_ids[-1]]
    return starts, ends


def partition_use_case_structural(ir: StructuralGraphIR) -> tuple[list[str], list[str]]:
    """Split `StructuralGraphIR` node IDs into driving Actors vs inside-boundary Use Cases."""
    sorted_ids = sorted(ir.nodes)
    if len(sorted_ids) <= 1:
        return [], sorted_ids
    actors = [
        sid
        for sid in sorted_ids
        if ir.nodes[sid].kind is NodeKind.EXTERNAL
        or any(tok in sid.lower() for tok in _ACTOR_TOKENS)
    ]
    if not actors or len(actors) == len(sorted_ids):
        in_deg = dict.fromkeys(sorted_ids, 0)
        for edge in ir.edges:
            in_deg[edge.target] += 1
        roots = [sid for sid in sorted_ids if in_deg[sid] == 0]
        actors = roots[:2] if roots and len(roots) < len(sorted_ids) else [sorted_ids[0]]
    actor_set = set(actors)
    use_cases = [sid for sid in sorted_ids if sid not in actor_set]
    return actors, use_cases


def partition_use_case_trace(trace: InteractionTraceIR) -> tuple[list[str], list[str]]:
    """Split `InteractionTraceIR` participant IDs into driving Actors vs Use Cases."""
    sorted_pids = sorted(trace.participants)
    ordered = order_trace_events(trace.events)
    root_callers = sorted({ev.caller_id for ev in ordered if ev.parent_event_id is None})
    actors = (
        root_callers[:2]
        if root_callers and len(root_callers) < len(sorted_pids)
        else sorted_pids[:1]
    )
    actor_set = set(actors)
    use_cases = [pid for pid in sorted_pids if pid not in actor_set] or sorted_pids
    return actors, use_cases


def build_structural_hierarchy(
    nodes: Mapping[str, IRNode],
) -> tuple[list[str], dict[str, list[str]]]:
    """Partition structural nodes into sorted root IDs and sorted parent-to-children lists."""
    sorted_ids = sorted(nodes)
    children: dict[str, list[str]] = {sid: [] for sid in sorted_ids}
    roots: list[str] = []
    for sid in sorted_ids:
        parent_id = nodes[sid].parent_id
        if parent_id is None or parent_id not in children:
            roots.append(sid)
        else:
            children[parent_id].append(sid)
    return roots, children
