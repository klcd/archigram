"""Deterministic LikeC4 DSL emitter for StructuralGraphIR and InteractionTraceIR."""

from __future__ import annotations

import heapq
import re
from typing import TYPE_CHECKING

from archigram.domain.ir import (
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    TraceEvent,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from archigram.domain.ir import CanonicalIR

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")

_LIKEC4_RESERVED: frozenset[str] = frozenset(
    {
        "all",
        "custom",
        "deployment",
        "deployments",
        "description",
        "dynamic",
        "element",
        "exclude",
        "export",
        "extend",
        "false",
        "for",
        "group",
        "import",
        "include",
        "instance",
        "is",
        "it",
        "kind",
        "link",
        "metadata",
        "model",
        "navigateTo",
        "node",
        "null",
        "par",
        "parallel",
        "predicate",
        "properties",
        "relationship",
        "sequence",
        "shape",
        "specification",
        "style",
        "tag",
        "technology",
        "this",
        "title",
        "to",
        "true",
        "variant",
        "view",
        "views",
        "where",
        "with",
    }
)


def _escape_likec4_string(raw: str) -> str:
    """Sanitize and escape a string label into single-quoted LikeC4 literal."""
    cleaned = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in cleaned).strip()
    if not cleaned:
        return "'unnamed'"
    escaped = cleaned.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _make_safe_ident(raw_id: str) -> str:
    """Derive a safe LikeC4 identifier token matching [a-zA-Z_][a-zA-Z0-9_]*."""
    slug = _SLUG_RE.sub("_", raw_id).strip("_")
    if not slug:
        slug = "elem"
    elif slug[0].isdigit():
        slug = f"n_{slug}"
    if slug.lower() in _LIKEC4_RESERVED:
        slug = f"n_{slug}"
    return slug


def _build_safe_ident_map(
    raw_ids: Iterable[str],
) -> tuple[dict[str, str], set[str]]:
    """Map raw IDs to collision-free, keyword-safe LikeC4 identifiers deterministically."""
    used_lower: set[str] = set()
    ident_map: dict[str, str] = {}
    for raw_id in sorted(raw_ids):
        base = _make_safe_ident(raw_id)
        candidate = base
        counter = 1
        while candidate.lower() in used_lower:
            candidate = f"{base}_{counter}"
            counter += 1
        used_lower.add(candidate.lower())
        ident_map[raw_id] = candidate
    return ident_map, used_lower


def _build_fqn_map(nodes: dict[str, IRNode], ident_map: dict[str, str]) -> dict[str, str]:
    """Build fully-qualified dot-separated paths for all nodes in the containment hierarchy."""
    fqn_map: dict[str, str] = {}

    def resolve(sym_id: str) -> str:
        if sym_id in fqn_map:
            return fqn_map[sym_id]
        node = nodes[sym_id]
        ident = ident_map[sym_id]
        if node.parent_id is None or node.parent_id not in nodes:
            fqn_map[sym_id] = ident
        else:
            fqn_map[sym_id] = f"{resolve(node.parent_id)}.{ident}"
        return fqn_map[sym_id]

    for sym_id in sorted(nodes):
        resolve(sym_id)
    return fqn_map


def _emit_specification(kinds: Iterable[str]) -> list[str]:
    """Emit specification block with sorted element kinds."""
    lines: list[str] = ["specification {"]
    for kind in sorted(kinds):
        lines.append(f"  element {kind}")
    lines.append("}")
    return lines


def _is_ancestor_or_self(ancestor_id: str, descendant_id: str, nodes: dict[str, IRNode]) -> bool:
    """Return True if ancestor_id is descendant_id or an ancestor in containment."""
    if ancestor_id == descendant_id:
        return True
    curr = nodes.get(descendant_id)
    while curr is not None and curr.parent_id is not None:
        if curr.parent_id == ancestor_id:
            return True
        curr = nodes.get(curr.parent_id)
    return False


def _emit_node_tree(
    sym_id: str,
    nodes: dict[str, IRNode],
    ident_map: dict[str, str],
    children: dict[str | None, list[str]],
    indent: str,
    lines: list[str],
) -> None:
    """Emit a node and its nested children recursively within model { ... }."""
    node = nodes[sym_id]
    ident = ident_map[sym_id]
    label_str = _escape_likec4_string(node.label)
    node_children = children.get(sym_id, [])
    if not node_children:
        lines.append(f"{indent}{ident} = {node.kind.value} {label_str}")
        return
    lines.append(f"{indent}{ident} = {node.kind.value} {label_str} {{")
    child_indent = f"{indent}  "
    for child_id in sorted(node_children):
        _emit_node_tree(child_id, nodes, ident_map, children, child_indent, lines)
    lines.append(f"{indent}}}")


def _emit_structural_edges(
    edges: tuple[IREdge, ...],
    nodes: dict[str, IRNode],
    fqn_map: dict[str, str],
    lines: list[str],
) -> None:
    """Emit deterministic forward relationship edges with zero chained or backward arrows."""
    valid_edges: list[IREdge] = []
    for edge in edges:
        if edge.source not in fqn_map or edge.target not in fqn_map:
            continue
        if _is_ancestor_or_self(edge.source, edge.target, nodes) or _is_ancestor_or_self(
            edge.target, edge.source, nodes
        ):
            continue
        valid_edges.append(edge)

    sorted_edges = sorted(
        valid_edges,
        key=lambda e: (
            fqn_map[e.source],
            fqn_map[e.target],
            e.relation.value,
            e.edge_id,
        ),
    )
    for edge in sorted_edges:
        src_fqn = fqn_map[edge.source]
        dst_fqn = fqn_map[edge.target]
        label = edge.attributes.get("label") or edge.relation.value
        label_str = _escape_likec4_string(label)
        lines.append(f"  {src_fqn} -> {dst_fqn} {label_str}")


def _emit_structural_graph(graph: StructuralGraphIR) -> str:
    """Serialize a StructuralGraphIR to deterministic LikeC4 DSL."""
    all_kinds = sorted(
        {k.value for k in NodeKind} | {node.kind.value for node in graph.nodes.values()}
    )
    lines = _emit_specification(all_kinds)
    lines.append("")
    lines.append("model {")

    ident_map, _ = _build_safe_ident_map(graph.nodes.keys())
    fqn_map = _build_fqn_map(graph.nodes, ident_map)

    children: dict[str | None, list[str]] = {}
    for sym_id in sorted(graph.nodes):
        p = graph.nodes[sym_id].parent_id
        norm_p = p if (p is not None and p in graph.nodes) else None
        children.setdefault(norm_p, []).append(sym_id)

    roots = children.get(None, [])
    for root_id in sorted(roots):
        _emit_node_tree(root_id, graph.nodes, ident_map, children, "  ", lines)

    if graph.edges and roots:
        lines.append("")
    _emit_structural_edges(graph.edges, graph.nodes, fqn_map, lines)

    lines.append("}")
    lines.append("")
    lines.append("views {")
    lines.append("  view index {")
    lines.append("    include *")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _order_trace_events(events: dict[str, TraceEvent]) -> list[TraceEvent]:
    """Topologically order causal DAG events deterministically by timestamp and event_id."""
    dependents: dict[str, list[str]] = {eid: [] for eid in events}
    indegree: dict[str, int] = dict.fromkeys(events, 0)

    for eid, event in events.items():
        preds: set[str] = set(event.causal_links)
        if event.parent_event_id is not None:
            preds.add(event.parent_event_id)
        indegree[eid] = len(preds)
        for pred in preds:
            dependents[pred].append(eid)

    heap: list[tuple[int, int, str, str, str]] = [
        (ev.start_ns, ev.end_ns, ev.caller_id, ev.callee_id, eid)
        for eid, ev in events.items()
        if indegree[eid] == 0
    ]
    heapq.heapify(heap)

    ordered: list[TraceEvent] = []
    while heap:
        _, _, _, _, eid = heapq.heappop(heap)
        ordered.append(events[eid])
        for dep_id in dependents[eid]:
            indegree[dep_id] -= 1
            if indegree[dep_id] == 0:
                dep = events[dep_id]
                heapq.heappush(
                    heap,
                    (dep.start_ns, dep.end_ns, dep.caller_id, dep.callee_id, dep_id),
                )
    return ordered


def _group_concurrent_siblings(
    ordered: list[TraceEvent],
) -> list[list[TraceEvent]]:
    """Group adjacent causally-independent sibling events with overlapping intervals."""
    if not ordered:
        return []
    groups: list[list[TraceEvent]] = [[ordered[0]]]
    group_ids: set[str] = {ordered[0].event_id}
    group_max_end = ordered[0].end_ns

    for event in ordered[1:]:
        current = groups[-1]
        same_parent = event.parent_event_id == current[0].parent_event_id
        same_clock = event.clock_domain_id == current[0].clock_domain_id
        independent = not (set(event.causal_links) & group_ids)
        overlaps = event.start_ns < group_max_end
        if same_parent and same_clock and independent and overlaps:
            current.append(event)
            group_ids.add(event.event_id)
            group_max_end = max(group_max_end, event.end_ns)
        else:
            groups.append([event])
            group_ids = {event.event_id}
            group_max_end = event.end_ns
    return groups


def _emit_trace_step(
    event: TraceEvent,
    ident_map: dict[str, str],
    indent: str,
    lines: list[str],
) -> None:
    """Emit a single caller -> callee 'signature' step."""
    caller = ident_map[event.caller_id]
    callee = ident_map[event.callee_id]
    sig_str = _escape_likec4_string(event.signature)
    lines.append(f"{indent}{caller} -> {callee} {sig_str}")


def _emit_dynamic_view(
    trace: InteractionTraceIR,
    ident_map: dict[str, str],
    lines: list[str],
) -> None:
    """Emit dynamic sequence view with parallel blocks for concurrent events."""
    view_id = _make_safe_ident(f"trace_{trace.trace_id}")
    lines.append("views {")
    lines.append(f"  dynamic view {view_id} {{")
    lines.append("    variant sequence")

    if trace.events:
        lines.append("")
        ordered = _order_trace_events(trace.events)
        for group in _group_concurrent_siblings(ordered):
            if len(group) == 1:
                _emit_trace_step(group[0], ident_map, "    ", lines)
            else:
                lines.append("    parallel {")
                for event in group:
                    _emit_trace_step(event, ident_map, "      ", lines)
                lines.append("    }")

    lines.append("  }")
    lines.append("}")


def _emit_interaction_trace(trace: InteractionTraceIR) -> str:
    """Serialize an InteractionTraceIR to deterministic LikeC4 dynamic sequence view."""
    kinds = sorted(
        {k.value for k in NodeKind} | {p.kind.value for p in trace.participants.values()}
    )
    lines = _emit_specification(kinds)
    lines.append("")
    lines.append("model {")

    ident_map, _ = _build_safe_ident_map(trace.participants.keys())
    for pid in sorted(trace.participants):
        p = trace.participants[pid]
        ident = ident_map[pid]
        label_str = _escape_likec4_string(p.label)
        lines.append(f"  {ident} = {p.kind.value} {label_str}")

    lines.append("}")
    lines.append("")
    _emit_dynamic_view(trace, ident_map, lines)
    return "\n".join(lines) + "\n"


class LikeC4Emitter:
    """Deterministic LikeC4 diagram emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target diagram syntax identifier ('likec4')."""
        return "likec4"

    def emit(self, ir: CanonicalIR) -> str:
        """Serialize a CanonicalIR (StructuralGraphIR or InteractionTraceIR) to LikeC4 DSL."""
        if isinstance(ir, StructuralGraphIR):
            return _emit_structural_graph(ir)
        return _emit_interaction_trace(ir)
