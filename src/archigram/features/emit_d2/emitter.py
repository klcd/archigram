"""Deterministic D2 emitter for StructuralGraphIR (ELK) and InteractionTraceIR."""

from __future__ import annotations

import heapq
import re
from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import (
    ClassMember,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from archigram.domain.ir import CanonicalIR

_ELK_HEADER = "vars: {\n  d2-config: {\n    layout-engine: elk\n  }\n}"
_UNQUOTED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAMPLE_BACKING_LIMIT = 5

_D2_RESERVED_KEYWORDS: frozenset[str] = frozenset(
    {
        "3d",
        "animated",
        "bold",
        "border-radius",
        "class",
        "classes",
        "constraint",
        "d2-config",
        "description",
        "direction",
        "double-border",
        "fill",
        "filled",
        "font",
        "font-color",
        "font-size",
        "grid-columns",
        "grid-gap",
        "grid-rows",
        "height",
        "horizontal-gap",
        "icon",
        "italic",
        "label",
        "layers",
        "left",
        "link",
        "multiple",
        "near",
        "null",
        "opacity",
        "scenarios",
        "sequence_diagram",
        "shadow",
        "shape",
        "source-arrowhead",
        "sql_table",
        "steps",
        "stroke",
        "stroke-dash",
        "stroke-width",
        "style",
        "suspend",
        "target-arrowhead",
        "text-transform",
        "title",
        "tooltip",
        "top",
        "underline",
        "unsuspend",
        "vars",
        "vertical-gap",
        "width",
    }
)

_NODE_KIND_SHAPES: dict[NodeKind, str] = {
    NodeKind.DATABASE: "cylinder",
    NodeKind.TABLE: "cylinder",
    NodeKind.QUEUE: "queue",
}


def _sanitize_control_chars(value: str) -> str:
    """Replace non-standard ASCII control characters while keeping normal whitespace."""
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if code < 0x20 and ch not in {"\n", "\r", "\t"}:
            continue
        if code == 0x7F:
            continue
        out.append(ch)
    return "".join(out)


def _quote_d2_string(value: str) -> str:
    """Return a single-line D2 double-quoted string with full escaping."""
    cleaned = _sanitize_control_chars(value)
    escaped = (
        cleaned.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _format_d2_key(key: str) -> str:
    """Return an unquoted D2 key if strictly safe, otherwise a D2 double-quoted key."""
    if _UNQUOTED_KEY_RE.match(key) and key.lower() not in _D2_RESERVED_KEYWORDS:
        return key
    return _quote_d2_string(key)


def _build_safe_key_map(raw_ids: Iterable[str]) -> tuple[dict[str, str], set[str]]:
    """Map raw IDs to collision-free, keyword-safe D2 keys in deterministic order."""
    used_lower: set[str] = set()
    key_map: dict[str, str] = {}
    for raw_id in sorted(raw_ids):
        candidate = f"node_{raw_id}" if raw_id.lower() in _D2_RESERVED_KEYWORDS else raw_id
        if candidate.lower() in used_lower:
            suffix = 1
            while f"{candidate}_{suffix}".lower() in used_lower:
                suffix += 1
            candidate = f"{candidate}_{suffix}"
        used_lower.add(candidate.lower())
        key_map[raw_id] = candidate
    return key_map, used_lower


def _build_provenance_tooltip(node: IRNode) -> str | None:
    """Build deterministic provenance tooltip text for DERIVED_DETERMINISTIC nodes."""
    if node.provenance.grade is not ProvenanceGrade.DERIVED_DETERMINISTIC:
        return None
    rule_id = node.provenance.rule_id or "unknown_rule"
    backing = node.provenance.backing_ids
    total = len(backing)
    sample = ", ".join(backing[:_SAMPLE_BACKING_LIMIT])
    noun = "backing node" if total == 1 else "backing nodes"
    ellipsis = ", ..." if total > _SAMPLE_BACKING_LIMIT else ""
    return f"Derived via {rule_id} ({total} {noun}: {sample}{ellipsis})"


def _format_class_member(member: ClassMember) -> tuple[str, str]:
    """Format a ClassMember into a visibility-prefixed D2 class key and optional type."""
    vis = member.visibility.value
    mods = f"{' '.join(member.modifiers)} " if member.modifiers else ""
    generics = f"[{', '.join(member.generic_params)}]" if member.generic_params else ""
    sig = member.signature.strip()
    if not member.is_method:
        return f"{vis}{mods}{member.name}{generics}", sig
    if sig.startswith("("):
        params, arrow, ret = sig.partition("->")
        return f"{vis}{mods}{member.name}{generics}{params.strip()}", (ret.strip() if arrow else "")
    return f"{vis}{mods}{member.name}{generics}()", sig


def _emit_class_members(lines: list[str], members: tuple[ClassMember, ...], indent: str) -> None:
    """Emit D2 class compartment fields and methods deterministically without key collisions."""
    used_keys: set[str] = set()
    for member in members:
        raw_key, raw_val = _format_class_member(member)
        key = raw_key
        if key.lower() in used_keys:
            idx = 1
            while f"{raw_key}_{idx}".lower() in used_keys:
                idx += 1
            key = f"{raw_key}_{idx}"
        used_keys.add(key.lower())
        if raw_val:
            lines.append(f"{indent}{_quote_d2_string(key)}: {_quote_d2_string(raw_val)}")
        else:
            lines.append(f"{indent}{_quote_d2_string(key)}")


def _emit_structural_node(
    node: IRNode,
    key_map: dict[str, str],
    children_by_parent: dict[str | None, list[str]],
    nodes: dict[str, IRNode],
    depth: int,
    lines: list[str],
) -> None:
    """Recursively emit a D2 node or nested container block."""
    indent = "  " * depth
    child_ids = children_by_parent.get(node.symbol_id, [])
    is_class = (
        node.kind in {NodeKind.CLASS, NodeKind.INTERFACE} and bool(node.members) and not child_ids
    )
    shape = "class" if is_class else _NODE_KIND_SHAPES.get(node.kind)
    tooltip = _build_provenance_tooltip(node)
    header = f"{indent}{_format_d2_key(key_map[node.symbol_id])}: {_quote_d2_string(node.label)}"
    if not child_ids and shape is None and tooltip is None:
        lines.append(header)
        return

    inner = "  " * (depth + 1)
    lines.append(f"{header} {{")
    if shape is not None:
        lines.append(f"{inner}shape: {shape}")
    if tooltip is not None:
        lines.append(f"{inner}tooltip: {_quote_d2_string(tooltip)}")
    if is_class:
        _emit_class_members(lines, node.members, inner)
    for child_id in child_ids:
        _emit_structural_node(nodes[child_id], key_map, children_by_parent, nodes, depth + 1, lines)
    lines.append(f"{indent}}}")


def _build_qualified_paths(nodes: dict[str, IRNode], key_map: dict[str, str]) -> dict[str, str]:
    """Compute full hierarchical dot-qualified D2 paths for every node in the graph."""
    paths: dict[str, str] = {}

    def _resolve(symbol_id: str) -> str:
        if symbol_id in paths:
            return paths[symbol_id]
        segment = _format_d2_key(key_map[symbol_id])
        parent_id = nodes[symbol_id].parent_id
        full = segment if parent_id is None else f"{_resolve(parent_id)}.{segment}"
        paths[symbol_id] = full
        return full

    for sid in sorted(nodes):
        _resolve(sid)
    return paths


def _emit_structural_edge(edge: IREdge, paths: dict[str, str], lines: list[str]) -> None:
    """Emit a single structural edge using full hierarchical D2 qualified paths."""
    src = paths[edge.source]
    dst = paths[edge.target]
    stmt = f"{src} -> {dst}: {_quote_d2_string(edge.relation.value)}"
    if edge.sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        lines.append(f"{stmt} {{")
        lines.append("  style.stroke-dash: 5")
        lines.append("}")
        return
    lines.append(stmt)


def _emit_structural_graph(graph: StructuralGraphIR) -> str:
    """Serialize a StructuralGraphIR into deterministic D2 with ELK layout and containers."""
    lines: list[str] = [_ELK_HEADER]
    if not graph.nodes:
        return "\n".join(lines) + "\n"

    key_map, _ = _build_safe_key_map(graph.nodes.keys())
    children_by_parent: dict[str | None, list[str]] = {}
    for sid in sorted(graph.nodes):
        parent_id = graph.nodes[sid].parent_id
        children_by_parent.setdefault(parent_id, []).append(sid)

    lines.append("")
    for root_id in children_by_parent.get(None, []):
        _emit_structural_node(
            graph.nodes[root_id], key_map, children_by_parent, graph.nodes, 0, lines
        )

    if graph.edges:
        paths = _build_qualified_paths(graph.nodes, key_map)
        sorted_edges = sorted(
            graph.edges,
            key=lambda e: (e.source, e.target, e.relation.value, e.sync_kind.value, e.edge_id),
        )
        lines.append("")
        for edge in sorted_edges:
            _emit_structural_edge(edge, paths, lines)

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
                    heap, (dep.start_ns, dep.end_ns, dep.caller_id, dep.callee_id, dep_id)
                )
    return ordered


def _group_concurrent_siblings(ordered: list[TraceEvent]) -> list[list[TraceEvent]]:
    """Group adjacent causally-independent sibling events with overlapping intervals."""
    if not ordered:
        return []
    groups: list[list[TraceEvent]] = [[ordered[0]]]
    group_ids: set[str] = {ordered[0].event_id}
    group_max_end = ordered[0].end_ns

    for event in ordered[1:]:
        current = groups[-1]
        same_parent = event.parent_event_id == current[0].parent_event_id
        independent = not (set(event.causal_links) & group_ids)
        overlaps = event.start_ns < group_max_end
        if same_parent and independent and overlaps:
            current.append(event)
            group_ids.add(event.event_id)
            group_max_end = max(group_max_end, event.end_ns)
        else:
            groups.append([event])
            group_ids = {event.event_id}
            group_max_end = event.end_ns
    return groups


def _emit_trace_message(
    event: TraceEvent,
    key_map: dict[str, str],
    indent: str,
    lines: list[str],
) -> None:
    """Emit a single D2 sequence diagram message line (and async dash style if needed)."""
    caller = _format_d2_key(key_map[event.caller_id])
    callee = _format_d2_key(key_map[event.callee_id])
    stmt = f"{indent}{caller} -> {callee}: {_quote_d2_string(event.signature)}"
    if event.sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        lines.append(f"{stmt} {{")
        lines.append(f"{indent}  style.stroke-dash: 5")
        lines.append(f"{indent}}}")
        return
    lines.append(stmt)


def _allocate_group_key(used_lower: set[str], group_index: int) -> tuple[str, int]:
    """Allocate a deterministic sequence-group key that never collides with participants."""
    idx = group_index
    candidate = f"concurrent_group_{idx}"
    while candidate.lower() in used_lower:
        idx += 1
        candidate = f"concurrent_group_{idx}"
    used_lower.add(candidate.lower())
    return candidate, idx + 1


def _emit_interaction_trace(trace: InteractionTraceIR) -> str:
    """Serialize an InteractionTraceIR into a deterministic D2 sequence diagram."""
    lines: list[str] = [_ELK_HEADER, "", "shape: sequence_diagram"]
    key_map, used_lower = _build_safe_key_map(trace.participants.keys())

    if trace.participants:
        lines.append("")
    for pid in sorted(trace.participants):
        participant = trace.participants[pid]
        p_key = _format_d2_key(key_map[pid])
        p_label = _quote_d2_string(participant.label)
        p_shape = _NODE_KIND_SHAPES.get(participant.kind)
        if p_shape is not None:
            lines.append(f"{p_key}: {p_label} {{")
            lines.append(f"  shape: {p_shape}")
            lines.append("}")
        else:
            lines.append(f"{p_key}: {p_label}")

    if trace.events:
        lines.append("")
        ordered = _order_trace_events(trace.events)
        group_index = 0
        for group in _group_concurrent_siblings(ordered):
            if len(group) == 1:
                _emit_trace_message(group[0], key_map, "", lines)
                continue
            group_key, group_index = _allocate_group_key(used_lower, group_index)
            lines.append(f"{group_key}: {_quote_d2_string(f'Concurrent ({len(group)})')} {{")
            for event in group:
                _emit_trace_message(event, key_map, "  ", lines)
            lines.append("}")

    return "\n".join(lines) + "\n"


class D2Emitter:
    """Deterministic D2 diagram emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target diagram syntax identifier ('d2')."""
        return "d2"

    def emit(self, ir: CanonicalIR) -> str:
        """Serialize a CanonicalIR (StructuralGraphIR or InteractionTraceIR) to D2."""
        if isinstance(ir, StructuralGraphIR):
            return _emit_structural_graph(ir)
        return _emit_interaction_trace(ir)
