"""Deterministic Mermaid v12 Emitter for Flowchart ELK, OOP Class, and Sequence par/loop."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from archigram.domain.ir import (
    CanonicalIR,
    ClassMember,
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)

_ELK_FRONTMATTER = "---\nconfig:\n  layout: elk\n---"
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

_OOP_RELATIONS: frozenset[EdgeRelation] = frozenset(
    {
        EdgeRelation.INHERITS,
        EdgeRelation.IMPLEMENTS,
        EdgeRelation.COMPOSES,
        EdgeRelation.AGGREGATES,
    }
)

_CLASS_ARROWS: dict[EdgeRelation, str] = {
    EdgeRelation.INHERITS: "<|--",
    EdgeRelation.IMPLEMENTS: "<|..",
    EdgeRelation.COMPOSES: "*--",
    EdgeRelation.AGGREGATES: "o--",
}

_STEREOTYPES: dict[NodeKind, str] = {
    NodeKind.INTERFACE: "<<interface>>",
    NodeKind.ENUM: "<<enumeration>>",
}


def sanitize_mermaid_text(raw: str) -> str:
    """Escape semicolons, quotes, brackets, and control characters for Mermaid v12 labels."""
    text = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.replace("#", "#35;")
    text = text.replace('"', "#quot;")
    text = text.replace(";", "#59;")
    text = text.replace("[", "#91;").replace("]", "#93;")
    text = text.replace("{", "#123;").replace("}", "#125;")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return cleaned if cleaned else "unnamed"


def sanitize_sequence_text(raw: str) -> str:
    """Sanitize sequence diagram message and block header text (removing raw colons/semicolons)."""
    return sanitize_mermaid_text(raw).replace(":", "#58;")


def _make_safe_alias(prefix: str, index: int, raw_id: str) -> str:
    """Build a collision-free Mermaid identifier guaranteed never to match reserved keywords."""
    slug = _SLUG_RE.sub("_", raw_id).strip("_").lower()[:16]
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:6]
    if slug:
        return f"{prefix}_{index}_{slug}_{digest}"
    return f"{prefix}_{index}_{digest}"


def build_alias_map(ids: list[str], prefix: str = "n") -> dict[str, str]:
    """Map sorted raw identifiers to collision-free deterministic Mermaid aliases."""
    return {raw_id: _make_safe_alias(prefix, idx, raw_id) for idx, raw_id in enumerate(sorted(ids))}


def _is_class_diagram_mode(ir: StructuralGraphIR) -> bool:
    """Determine if a StructuralGraphIR should emit an OOP classDiagram instead of flowchart."""
    if ir.metadata.get("diagram_variant") == "class":
        return True
    if any(bool(node.members) for node in ir.nodes.values()):
        return True
    return any(edge.relation in _OOP_RELATIONS for edge in ir.edges)


_RESERVED_TOKENS: frozenset[str] = frozenset(
    {"end", "subgraph", "class", "par", "loop", "and", "participant"}
)


def _sanitize_token(raw: str) -> str:
    """Sanitize an identifier token inside a class member or generic parameter."""
    token = _SLUG_RE.sub("_", raw).strip("_")
    if not token:
        return "T"
    if token.lower() in _RESERVED_TOKENS:
        return f"{token}_"
    return token


def _format_class_member(member: ClassMember) -> str:
    """Format a ClassMember compartment line with visibility, generics, signature, and modifiers."""
    vis = member.visibility.value
    name = _sanitize_token(member.name)
    generics = (
        f"~{','.join(_sanitize_token(g) for g in member.generic_params)}~"
        if member.generic_params
        else ""
    )
    raw_sig = member.signature.strip()
    sig = (
        sanitize_mermaid_text(raw_sig).replace("(", " ").replace(")", " ").strip()
        if raw_sig
        else ""
    )
    mods = f" {' '.join(_sanitize_token(m) for m in member.modifiers)}" if member.modifiers else ""
    if member.is_method:
        if "->" in sig:
            params_part, ret_part = sig.split("->", 1)
            params_clean = params_part.strip()
            ret_clean = _sanitize_token(ret_part.strip())
            return f"    {vis}{name}{generics}({params_clean}) {ret_clean}{mods}"
        return f"    {vis}{name}{generics}({sig}){mods}"
    if sig:
        return f"    {vis}{sig} {name}{generics}{mods}"
    return f"    {vis}{name}{generics}{mods}"


def _format_class_header(node: IRNode, alias: str) -> str:
    """Format a Mermaid class declaration line with optional generic params and quoted label."""
    raw_generics = node.attributes.get("generic_params", "")
    generic_suffix = ""
    if raw_generics.strip():
        tokens = [_sanitize_token(part) for part in raw_generics.split(",") if part.strip()]
        if tokens:
            generic_suffix = f"~{','.join(tokens)}~"
    label = sanitize_mermaid_text(node.label)
    return f'  class {alias}{generic_suffix}["{label}"]'


def _emit_class_node(node: IRNode, alias: str) -> list[str]:
    """Emit a single class block in a Mermaid classDiagram."""
    header = _format_class_header(node, alias)
    stereotype = _STEREOTYPES.get(node.kind)
    if not node.members and stereotype is None:
        return [header]
    lines = [f"{header} {{"]
    if stereotype is not None:
        lines.append(f"    {stereotype}")
    for member in node.members:
        lines.append(_format_class_member(member))
    lines.append("  }")
    return lines


def _emit_class_edge(edge: IREdge, aliases: dict[str, str]) -> str:
    """Emit a typed UML relationship line in a Mermaid classDiagram."""
    src = aliases[edge.source]
    tgt = aliases[edge.target]
    label = sanitize_sequence_text(edge.relation.value)
    if edge.relation in _CLASS_ARROWS:
        arrow = _CLASS_ARROWS[edge.relation]
        return f"  {tgt} {arrow} {src} : {label}"
    if edge.sync_kind is SyncKind.ASYNC_FIRE_FORGET or edge.relation is EdgeRelation.IMPORTS:
        return f"  {src} ..> {tgt} : {label}"
    return f"  {src} --> {tgt} : {label}"


def _emit_class_diagram(ir: StructuralGraphIR) -> str:
    """Emit a deterministic Mermaid v12 OOP classDiagram with ELK layout frontmatter."""
    sorted_ids = sorted(ir.nodes)
    aliases = build_alias_map(sorted_ids, prefix="c")
    lines: list[str] = [_ELK_FRONTMATTER, "classDiagram"]
    for sym_id in sorted_ids:
        lines.extend(_emit_class_node(ir.nodes[sym_id], aliases[sym_id]))
    for edge in sorted(ir.edges, key=lambda item: item.edge_id):
        lines.append(_emit_class_edge(edge, aliases))
    return "\n".join(lines) + "\n"


def _build_children_map(nodes: dict[str, IRNode]) -> tuple[list[str], dict[str, list[str]]]:
    """Partition nodes into sorted root IDs and sorted parent->children adjacency lists."""
    roots: list[str] = []
    children: dict[str, list[str]] = {sym_id: [] for sym_id in nodes}
    for sym_id in sorted(nodes):
        parent_id = nodes[sym_id].parent_id
        if parent_id is None:
            roots.append(sym_id)
        else:
            children[parent_id].append(sym_id)
    return roots, children


def _emit_flowchart_subtree(
    sym_id: str,
    nodes: dict[str, IRNode],
    children: dict[str, list[str]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Recursively emit a flowchart node or nested subgraph container."""
    indent = "  " * depth
    node = nodes[sym_id]
    alias = aliases[sym_id]
    label = sanitize_mermaid_text(node.label)
    child_ids = children[sym_id]
    if not child_ids:
        return [f'{indent}{alias}["{label}"]']
    lines = [f'{indent}subgraph {alias}["{label}"]']
    for child_id in child_ids:
        lines.extend(_emit_flowchart_subtree(child_id, nodes, children, aliases, depth + 1))
    lines.append(f"{indent}end")
    return lines


def _emit_flowchart_edge(edge: IREdge, aliases: dict[str, str]) -> str:
    """Emit a styled flowchart edge line sorted by edge_id."""
    src = aliases[edge.source]
    tgt = aliases[edge.target]
    label = sanitize_mermaid_text(edge.relation.value)
    if edge.sync_kind is SyncKind.FORK_JOIN_BRANCH or edge.relation is EdgeRelation.FORK_JOIN:
        arrow = "==>"
    elif edge.sync_kind is SyncKind.ASYNC_FIRE_FORGET or edge.relation in {
        EdgeRelation.IMPORTS,
        EdgeRelation.ASYNC_SPAWN,
    }:
        arrow = "-.->"
    else:
        arrow = "-->"
    return f'  {src} {arrow}|"{label}"| {tgt}'


def _emit_flowchart(ir: StructuralGraphIR) -> str:
    """Emit a deterministic Mermaid v12 ELK flowchart TD diagram."""
    sorted_ids = sorted(ir.nodes)
    aliases = build_alias_map(sorted_ids, prefix="n")
    roots, children = _build_children_map(ir.nodes)
    lines: list[str] = [_ELK_FRONTMATTER, "flowchart TD"]
    for root_id in roots:
        lines.extend(_emit_flowchart_subtree(root_id, ir.nodes, children, aliases, depth=1))
    for edge in sorted(ir.edges, key=lambda item: item.edge_id):
        lines.append(_emit_flowchart_edge(edge, aliases))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _SingleStep:
    """Single non-overlapping sequence event step."""

    event: TraceEvent


@dataclass(frozen=True, slots=True)
class _ParStep:
    """Concurrent overlapping sibling events grouped into a par ... and ... end fragment."""

    branches: tuple[TraceEvent, ...]


@dataclass(frozen=True, slots=True)
class _LoopStep:
    """Contiguous identical sibling events grouped into a loop [N times] ... end fragment."""

    events: tuple[TraceEvent, ...]


type _SequenceStep = _SingleStep | _ParStep | _LoopStep


def _events_overlap(first: TraceEvent, second: TraceEvent) -> bool:
    """Check whether two events in the same clock domain have overlapping time intervals."""
    if first.clock_domain_id != second.clock_domain_id:
        return False
    if first.start_ns == second.start_ns:
        return True
    return max(first.start_ns, second.start_ns) < min(first.end_ns, second.end_ns)


def _cluster_parallel_siblings(siblings: list[TraceEvent]) -> list[list[TraceEvent]]:
    """Group sorted sibling events into temporal overlap clusters per clock domain."""
    clusters: list[list[TraceEvent]] = []
    for event in siblings:
        if clusters and any(_events_overlap(member, event) for member in clusters[-1]):
            clusters[-1].append(event)
        else:
            clusters.append([event])
    return clusters


def _can_merge_into_loop(
    first: TraceEvent,
    second: TraceEvent,
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> bool:
    """Determine if two consecutive non-overlapping events form a repeatable loop step."""
    if children_by_parent.get(first.event_id) or children_by_parent.get(second.event_id):
        return False
    return (
        first.caller_id == second.caller_id
        and first.callee_id == second.callee_id
        and first.signature == second.signature
        and first.sync_kind == second.sync_kind
    )


def _group_sibling_steps(
    siblings: list[TraceEvent],
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> list[_SequenceStep]:
    """Partition sorted sibling events into par blocks, loop blocks, and single steps."""
    clusters = _cluster_parallel_siblings(siblings)
    steps: list[_SequenceStep] = []
    idx = 0
    while idx < len(clusters):
        cluster = clusters[idx]
        if len(cluster) > 1:
            steps.append(_ParStep(branches=tuple(cluster)))
            idx += 1
            continue
        run = [cluster[0]]
        next_idx = idx + 1
        while (
            next_idx < len(clusters)
            and len(clusters[next_idx]) == 1
            and _can_merge_into_loop(run[-1], clusters[next_idx][0], children_by_parent)
        ):
            run.append(clusters[next_idx][0])
            next_idx += 1
        if len(run) >= 2:
            steps.append(_LoopStep(events=tuple(run)))
        else:
            steps.append(_SingleStep(event=run[0]))
        idx = next_idx
    return steps


def _emit_event_invocation(
    event: TraceEvent,
    children_by_parent: dict[str | None, list[TraceEvent]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit a single event call arrow, any nested child steps, and return arrow if synchronous."""
    indent = "  " * depth
    caller = aliases[event.caller_id]
    callee = aliases[event.callee_id]
    sig = sanitize_sequence_text(event.signature)
    child_events = children_by_parent.get(event.event_id, [])
    if event.sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        lines = [f"{indent}{caller}-){callee}: {sig}"]
        if child_events:
            lines.extend(_emit_sibling_list(child_events, children_by_parent, aliases, depth))
        return lines
    lines = [f"{indent}{caller}->>+{callee}: {sig}"]
    if child_events:
        lines.extend(_emit_sibling_list(child_events, children_by_parent, aliases, depth))
    lines.append(f"{indent}{callee}-->>-{caller}: return {sig}")
    return lines


def _emit_sibling_list(
    siblings: list[TraceEvent],
    children_by_parent: dict[str | None, list[TraceEvent]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit grouped sequence steps (par, loop, single) at the requested indentation depth."""
    indent = "  " * depth
    lines: list[str] = []
    for step in _group_sibling_steps(siblings, children_by_parent):
        if isinstance(step, _SingleStep):
            lines.extend(_emit_event_invocation(step.event, children_by_parent, aliases, depth))
        elif isinstance(step, _LoopStep):
            count = len(step.events)
            sig_label = sanitize_sequence_text(step.events[0].signature)
            lines.append(f"{indent}loop [{count} times] {sig_label}")
            lines.extend(
                _emit_event_invocation(step.events[0], children_by_parent, aliases, depth + 1)
            )
            lines.append(f"{indent}end")
        else:
            for b_idx, branch_event in enumerate(step.branches):
                keyword = "par" if b_idx == 0 else "and"
                b_label = sanitize_sequence_text(branch_event.signature)
                lines.append(f"{indent}{keyword} {b_label}")
                lines.extend(
                    _emit_event_invocation(branch_event, children_by_parent, aliases, depth + 1)
                )
            lines.append(f"{indent}end")
    return lines


def _format_participant_line(participant: TraceParticipant, alias: str) -> str:
    """Format a sequenceDiagram participant declaration with safe alias and quoted label."""
    label = sanitize_mermaid_text(participant.label)
    return f'  participant {alias} as "{label}"'


def _emit_sequence_diagram(ir: InteractionTraceIR) -> str:
    """Emit a deterministic Mermaid v12 sequenceDiagram with par/loop fragments and async arrows."""
    sorted_pids = sorted(ir.participants)
    aliases = build_alias_map(sorted_pids, prefix="p")
    lines: list[str] = ["sequenceDiagram"]
    for pid in sorted_pids:
        lines.append(_format_participant_line(ir.participants[pid], aliases[pid]))

    children_by_parent: dict[str | None, list[TraceEvent]] = {}
    for event in sorted(ir.events.values(), key=lambda item: (item.start_ns, item.event_id)):
        children_by_parent.setdefault(event.parent_event_id, []).append(event)

    root_events = children_by_parent.get(None, [])
    lines.extend(_emit_sibling_list(root_events, children_by_parent, aliases, depth=1))
    return "\n".join(lines) + "\n"


class MermaidEmitter:
    """Deterministic Mermaid v12 emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target format identifier 'mermaid'."""
        return "mermaid"

    def emit(self, ir: CanonicalIR) -> str:
        """Emit deterministic Mermaid v12 syntax from a StructuralGraphIR or InteractionTraceIR."""
        if isinstance(ir, StructuralGraphIR):
            if _is_class_diagram_mode(ir):
                return _emit_class_diagram(ir)
            return _emit_flowchart(ir)
        return _emit_sequence_diagram(ir)
