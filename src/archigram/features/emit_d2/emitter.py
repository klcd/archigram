"""Deterministic D2 emitter for StructuralGraphIR (ELK) and InteractionTraceIR."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade
from archigram.domain.concurrency import (
    group_concurrent_siblings,
    order_trace_events,
)
from archigram.domain.ir import (
    ClassMember,
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
)
from archigram.domain.style import (
    D2_CANONICAL_HEADER,
    NEUTRAL_BORDER_HEX,
    TRANSPARENT_FILL,
    LinePattern,
    UmlArrowheadKind,
    resolve_edge_animated,
    resolve_edge_visual_style,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from archigram.domain.ir import CanonicalIR

_UNQUOTED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAMPLE_BACKING_LIMIT = 5
_MAX_D2_TRACE_STEPS = 12

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

_OOP_CLASS_KINDS: frozenset[NodeKind] = frozenset(
    {NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM}
)


@dataclass(frozen=True, slots=True)
class _D2StructuralCtx:
    """Shared structural emission context to keep recursive signatures compact."""

    key_map: dict[str, str]
    children_by_parent: dict[str | None, list[str]]
    nodes: dict[str, IRNode]
    force_class: bool


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


def _is_class_shape(node: IRNode, has_children: bool, force_class: bool) -> bool:
    """Return True when a node is drawn as a UML class compartment instead of a box."""
    return (
        not has_children and node.kind in _OOP_CLASS_KINDS and (bool(node.members) or force_class)
    )


def class_shaped_node_ids(graph: StructuralGraphIR) -> frozenset[str]:
    """IDs of the nodes `emit` draws as class compartments, whose `style.stroke` is not a border."""
    parents = {node.parent_id for node in graph.nodes.values() if node.parent_id is not None}
    force_class = graph.metadata.get("diagram_variant") == "class"
    return frozenset(
        sid
        for sid, node in graph.nodes.items()
        if _is_class_shape(node, sid in parents, force_class)
    )


def _emit_structural_node(
    node: IRNode,
    ctx: _D2StructuralCtx,
    depth: int,
    lines: list[str],
) -> None:
    """Recursively emit a D2 node or nested container block with transparent fill."""
    indent = "  " * depth
    child_ids = ctx.children_by_parent.get(node.symbol_id, [])
    is_class = _is_class_shape(node, bool(child_ids), ctx.force_class)
    shape = "class" if is_class else _NODE_KIND_SHAPES.get(node.kind)
    tooltip = _build_provenance_tooltip(node)
    header = (
        f"{indent}{_format_d2_key(ctx.key_map[node.symbol_id])}: {_quote_d2_string(node.label)}"
    )
    inner = "  " * (depth + 1)
    lines.append(f"{header} {{")
    if shape is not None:
        lines.append(f"{inner}shape: {shape}")
    if not is_class:
        lines.append(f"{inner}style.fill: {TRANSPARENT_FILL}")
        lines.append(f'{inner}style.stroke: "{NEUTRAL_BORDER_HEX}"')
    if tooltip is not None:
        lines.append(f"{inner}tooltip: {_quote_d2_string(tooltip)}")
    if is_class and node.members:
        _emit_class_members(lines, node.members, inner)
    for child_id in child_ids:
        _emit_structural_node(ctx.nodes[child_id], ctx, depth + 1, lines)
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


def _append_d2_uml_arrowhead(arrowhead: UmlArrowheadKind, lines: list[str]) -> None:
    """Append UML generalization, realization, composition, or aggregation arrowheads in D2."""
    if arrowhead is UmlArrowheadKind.HOLLOW_TRIANGLE:
        lines.extend(
            (
                "  target-arrowhead: {",
                "    shape: triangle",
                "    style.filled: false",
                "  }",
            )
        )
    elif arrowhead is UmlArrowheadKind.FILLED_DIAMOND:
        lines.extend(
            (
                "  source-arrowhead: {",
                "    shape: diamond",
                "    style.filled: true",
                "  }",
            )
        )
    elif arrowhead is UmlArrowheadKind.HOLLOW_DIAMOND:
        lines.extend(
            (
                "  source-arrowhead: {",
                "    shape: diamond",
                "    style.filled: false",
                "  }",
            )
        )


def _emit_structural_edge(
    edge: IREdge,
    paths: dict[str, str],
    animate_override: str | None,
    lines: list[str],
) -> None:
    """Emit a structural edge with edge-only semantic stroke, animation, and UML arrowheads."""
    src = paths[edge.source]
    dst = paths[edge.target]
    visual = resolve_edge_visual_style(edge.relation, edge.sync_kind)
    stmt = f"{src} -> {dst}: {_quote_d2_string(edge.relation.value)}"
    lines.append(f"{stmt} {{")
    lines.append(f'  style.stroke: "{visual.stroke_hex}"')
    if visual.line_pattern is LinePattern.DASHED:
        lines.append("  style.stroke-dash: 5")
    elif visual.line_pattern is LinePattern.BOLD:
        lines.append("  style.stroke-width: 3")
    if resolve_edge_animated(visual, animate_override):
        lines.append("  style.animated: true")
    _append_d2_uml_arrowhead(visual.uml_arrowhead, lines)
    lines.append("}")


def _emit_structural_graph(graph: StructuralGraphIR) -> str:
    """Serialize a StructuralGraphIR into deterministic D2 with ELK layout and containers."""
    lines: list[str] = [D2_CANONICAL_HEADER]
    if not graph.nodes:
        return "\n".join(lines) + "\n"

    key_map, _ = _build_safe_key_map(graph.nodes.keys())
    children_by_parent: dict[str | None, list[str]] = {}
    for sid in sorted(graph.nodes):
        parent_id = graph.nodes[sid].parent_id
        children_by_parent.setdefault(parent_id, []).append(sid)

    ctx = _D2StructuralCtx(
        key_map=key_map,
        children_by_parent=children_by_parent,
        nodes=graph.nodes,
        force_class=graph.metadata.get("diagram_variant") == "class",
    )
    lines.append("")
    for root_id in children_by_parent.get(None, []):
        _emit_structural_node(graph.nodes[root_id], ctx, 0, lines)

    if graph.edges:
        paths = _build_qualified_paths(graph.nodes, key_map)
        animate_override = graph.metadata.get("animate_edges")
        sorted_edges = sorted(
            graph.edges,
            key=lambda e: (e.source, e.target, e.relation.value, e.sync_kind.value, e.edge_id),
        )
        lines.append("")
        for edge in sorted_edges:
            _emit_structural_edge(edge, paths, animate_override, lines)

    return "\n".join(lines) + "\n"


def _emit_d2_call_edge(
    endpoints: tuple[str, str],
    label: str,
    sync_kind: SyncKind,
    animate_override: str | None,
    lines: list[str],
    indent: str = "",
) -> None:
    """Emit a D2 runtime call edge with semantic stroke, dash/width, and flowing animation."""
    caller_key, callee_key = endpoints
    visual = resolve_edge_visual_style(EdgeRelation.CALLS, sync_kind)
    stmt = f"{indent}{caller_key} -> {callee_key}: {_quote_d2_string(label)}"
    lines.append(f"{stmt} {{")
    lines.append(f'{indent}  style.stroke: "{visual.stroke_hex}"')
    if visual.line_pattern is LinePattern.DASHED:
        lines.append(f"{indent}  style.stroke-dash: 5")
    elif visual.line_pattern is LinePattern.BOLD:
        lines.append(f"{indent}  style.stroke-width: 3")
    if resolve_edge_animated(visual, animate_override):
        lines.append(f"{indent}  style.animated: true")
    lines.append(f"{indent}}}")


def _allocate_group_key(used_lower: set[str], group_index: int) -> tuple[str, int]:
    """Allocate a deterministic sequence-group key that never collides with participants."""
    idx = group_index
    candidate = f"concurrent_group_{idx}"
    while candidate.lower() in used_lower:
        idx += 1
        candidate = f"concurrent_group_{idx}"
    used_lower.add(candidate.lower())
    return candidate, idx + 1


def _emit_trace_participants(
    trace: InteractionTraceIR,
    key_map: dict[str, str],
    lines: list[str],
) -> None:
    """Emit transparent, neutral-bordered participant declarations in deterministic order."""
    if trace.participants:
        lines.append("")
    for pid in sorted(trace.participants):
        participant = trace.participants[pid]
        p_key = _format_d2_key(key_map[pid])
        p_label = _quote_d2_string(participant.label)
        p_shape = _NODE_KIND_SHAPES.get(participant.kind)
        lines.append(f"{p_key}: {p_label} {{")
        if p_shape is not None:
            lines.append(f"  shape: {p_shape}")
        lines.append(f"  style.fill: {TRANSPARENT_FILL}")
        lines.append(f'  style.stroke: "{NEUTRAL_BORDER_HEX}"')
        lines.append("}")


@dataclass(frozen=True, slots=True)
class _StepEdge:
    """Coalesced first-activation causal transition emitted inside a D2 `steps` frame."""

    caller_id: str
    callee_id: str
    signature: str
    sync_kind: SyncKind
    call_count: int


def _chunk_step_waves(waves: list[list[_StepEdge]]) -> list[list[_StepEdge]]:
    """Bound D2 multiboard frame count to `_MAX_D2_TRACE_STEPS` while preserving causal order."""
    if len(waves) <= _MAX_D2_TRACE_STEPS:
        return waves
    chunked: list[list[_StepEdge]] = []
    total = len(waves)
    for bucket in range(_MAX_D2_TRACE_STEPS):
        start = (bucket * total) // _MAX_D2_TRACE_STEPS
        end = ((bucket + 1) * total) // _MAX_D2_TRACE_STEPS
        merged = [edge for wave in waves[start:end] for edge in wave]
        if merged:
            chunked.append(merged)
    return chunked


def _collect_unique_step_waves(ordered: list[TraceEvent]) -> list[list[_StepEdge]]:
    """Extract ordered causal activation waves without duplicate parallel edges."""
    has_cross = any(ev.caller_id != ev.callee_id for ev in ordered)
    pair_counts: dict[tuple[str, str], int] = {}
    for ev in ordered:
        if not (has_cross and ev.caller_id == ev.callee_id):
            pair = (ev.caller_id, ev.callee_id)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    seen_pairs: set[tuple[str, str]] = set()
    waves: list[list[_StepEdge]] = []
    for group in group_concurrent_siblings(ordered):
        wave: list[_StepEdge] = []
        for ev in group:
            pair = (ev.caller_id, ev.callee_id)
            if (has_cross and ev.caller_id == ev.callee_id) or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            wave.append(
                _StepEdge(
                    caller_id=ev.caller_id,
                    callee_id=ev.callee_id,
                    signature=ev.signature,
                    sync_kind=ev.sync_kind,
                    call_count=pair_counts[pair],
                )
            )
        if wave:
            waves.append(wave)
    return _chunk_step_waves(waves)


def _emit_trace_steps(trace: InteractionTraceIR) -> str:
    """Serialize an InteractionTraceIR into a multi-frame D2 `steps` architecture playback."""
    lines: list[str] = [D2_CANONICAL_HEADER, "", "direction: right"]
    key_map, _ = _build_safe_key_map(trace.participants.keys())
    animate_override = trace.metadata.get("animate_edges")
    _emit_trace_participants(trace, key_map, lines)

    waves = _collect_unique_step_waves(order_trace_events(trace.events)) if trace.events else []
    if waves:
        lines.append("")
        lines.append("steps: {")
        for idx, wave in enumerate(waves, start=1):
            lines.append(f"  step_{idx}: {{")
            for step_edge in wave:
                label = (
                    f"{step_edge.signature} (x{step_edge.call_count})"
                    if step_edge.call_count > 1
                    else step_edge.signature
                )
                endpoints = (
                    _format_d2_key(key_map[step_edge.caller_id]),
                    _format_d2_key(key_map[step_edge.callee_id]),
                )
                _emit_d2_call_edge(
                    endpoints,
                    label,
                    step_edge.sync_kind,
                    animate_override,
                    lines,
                    indent="    ",
                )
            lines.append("  }")
        lines.append("}")
    return "\n".join(lines) + "\n"


def _emit_interaction_trace(trace: InteractionTraceIR) -> str:
    """Serialize an InteractionTraceIR into a deterministic dual-mode D2 sequence diagram."""
    if trace.metadata.get("diagram_variant") == "steps":
        return _emit_trace_steps(trace)
    lines: list[str] = [D2_CANONICAL_HEADER, "", "shape: sequence_diagram"]
    key_map, used_lower = _build_safe_key_map(trace.participants.keys())
    animate_override = trace.metadata.get("animate_edges")
    _emit_trace_participants(trace, key_map, lines)

    if trace.events:
        lines.append("")
        ordered = order_trace_events(trace.events)
        group_index = 0
        for group in group_concurrent_siblings(ordered):
            if len(group) == 1:
                ev = group[0]
                _emit_d2_call_edge(
                    (_format_d2_key(key_map[ev.caller_id]), _format_d2_key(key_map[ev.callee_id])),
                    ev.signature,
                    ev.sync_kind,
                    animate_override,
                    lines,
                )
                continue
            group_key, group_index = _allocate_group_key(used_lower, group_index)
            lines.append(f"{group_key}: {_quote_d2_string(f'Concurrent ({len(group)})')} {{")
            for ev in group:
                _emit_d2_call_edge(
                    (_format_d2_key(key_map[ev.caller_id]), _format_d2_key(key_map[ev.callee_id])),
                    ev.signature,
                    ev.sync_kind,
                    animate_override,
                    lines,
                    indent="  ",
                )
            lines.append("}")

    return "\n".join(lines) + "\n"


def _dispatch_d2_variant(ir: CanonicalIR, variant: str) -> str | None:
    """Dispatch non-default UML 2.5 projection variants for D2Emitter."""
    from archigram.features.emit_d2 import uml_variants as uv

    if variant in {"package", "deployment"}:
        return uv.emit_d2_package_or_deployment(ir, variant)
    dispatch = {
        "state": uv.emit_d2_state,
        "communication": uv.emit_d2_communication,
        "activity": uv.emit_d2_activity,
        "use_case": uv.emit_d2_use_case,
    }
    fn = dispatch.get(variant)
    return fn(ir) if fn is not None else None


class D2Emitter:
    """Deterministic D2 diagram emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target diagram syntax identifier ('d2')."""
        return "d2"

    def emit(self, ir: CanonicalIR) -> str:
        """Serialize a CanonicalIR (StructuralGraphIR or InteractionTraceIR) to D2."""
        variant = ir.metadata.get("diagram_variant", "auto")
        dispatched = _dispatch_d2_variant(ir, variant)
        if dispatched is not None:
            return dispatched
        if isinstance(ir, StructuralGraphIR):
            return _emit_structural_graph(ir)
        return _emit_interaction_trace(ir)


quote_d2_string = _quote_d2_string
format_d2_key = _format_d2_key
build_safe_key_map = _build_safe_key_map
build_qualified_paths = _build_qualified_paths
build_provenance_tooltip = _build_provenance_tooltip
order_d2_trace_events = order_trace_events
