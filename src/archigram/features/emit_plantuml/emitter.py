"""Deterministic PlantUML emitter for teoz concurrent sequences, OOP classes, and ELK views."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from archigram.domain.concurrency import events_overlap, order_trace_events
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
from archigram.domain.style import (
    PLANTUML_ORTHO_SKINPARAMS,
    PLANTUML_SEQUENCE_SKINPARAMS,
    LinePattern,
    resolve_edge_visual_style,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

_OOP_RELATIONS: frozenset[EdgeRelation] = frozenset(
    {
        EdgeRelation.INHERITS,
        EdgeRelation.IMPLEMENTS,
        EdgeRelation.COMPOSES,
        EdgeRelation.AGGREGATES,
    }
)

_CLASS_KEYWORDS: dict[NodeKind, str] = {
    NodeKind.INTERFACE: "interface",
    NodeKind.ENUM: "enum",
}

_COMPONENT_LEAF_KEYWORDS: dict[NodeKind, str] = {
    NodeKind.DATABASE: "database",
    NodeKind.TABLE: "database",
    NodeKind.QUEUE: "queue",
    NodeKind.PACKAGE: "package",
    NodeKind.MODULE: "package",
    NodeKind.CONTAINER: "rectangle",
}

_PARTICIPANT_KEYWORDS: dict[NodeKind, str] = {
    NodeKind.QUEUE: "queue",
    NodeKind.DATABASE: "database",
    NodeKind.TABLE: "database",
}

_RESERVED_TOKENS: frozenset[str] = frozenset(
    {
        "as",
        "class",
        "component",
        "database",
        "else",
        "end",
        "enum",
        "hide",
        "interface",
        "loop",
        "package",
        "par",
        "participant",
        "queue",
        "rectangle",
        "remove",
        "skinparam",
    }
)


def _strip_control_and_newlines(raw: str) -> str:
    """Normalize whitespace and strip non-printable control characters."""
    normalized = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in normalized)
    return cleaned.rstrip("\\").strip()


def sanitize_plantuml_label(raw: str) -> str:
    """Sanitize text for double-quoted PlantUML labels without breaking quotes or lines."""
    cleaned = _strip_control_and_newlines(raw).replace('"', "'")
    return cleaned if cleaned else "unnamed"


def sanitize_plantuml_message(raw: str) -> str:
    """Sanitize sequence message or edge label text for single-line PlantUML statements."""
    cleaned = _strip_control_and_newlines(raw).replace('"', "'")
    return cleaned if cleaned else "unnamed"


def _sanitize_member_text(raw: str) -> str:
    """Sanitize class member signature fragments to avoid brace or line-continuation breaks."""
    cleaned = _strip_control_and_newlines(raw).replace('"', "'")
    return cleaned.replace("{", "(").replace("}", ")")


def _sanitize_token(raw: str) -> str:
    """Sanitize an identifier token for generics or member names."""
    token = _SLUG_RE.sub("_", raw).strip("_")
    if not token:
        return "T"
    if token.lower() in _RESERVED_TOKENS:
        return f"{token}_"
    return token


def _sanitize_tag(raw: str) -> str:
    """Normalize a PlantUML $tag identifier without a leading dollar sign."""
    stripped = raw.strip().lstrip("$")
    token = _SLUG_RE.sub("_", stripped).strip("_")
    return token


def _make_safe_alias(prefix: str, index: int, raw_id: str) -> str:
    """Build a deterministic, collision-free, keyword-safe PlantUML alias."""
    slug = _SLUG_RE.sub("_", raw_id).strip("_").lower()[:16]
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:6]
    if slug:
        return f"{prefix}_{index}_{slug}_{digest}"
    return f"{prefix}_{index}_{digest}"


def build_alias_map(ids: Iterable[str], prefix: str = "n") -> dict[str, str]:
    """Map sorted raw identifiers to collision-free deterministic PlantUML aliases."""
    return {raw_id: _make_safe_alias(prefix, idx, raw_id) for idx, raw_id in enumerate(sorted(ids))}


_OOP_NODE_KINDS: frozenset[NodeKind] = frozenset(
    {
        NodeKind.CLASS,
        NodeKind.INTERFACE,
        NodeKind.ENUM,
    }
)


def _is_class_diagram_mode(ir: StructuralGraphIR) -> bool:
    """Determine whether StructuralGraphIR should emit an OOP class diagram."""
    variant = ir.metadata.get("diagram_variant", "auto")
    if variant == "class":
        return True
    if variant == "architecture":
        return False
    if any(bool(node.members) for node in ir.nodes.values()):
        return True
    if not any(node.kind in _OOP_NODE_KINDS for node in ir.nodes.values()):
        return False
    return any(edge.relation in _OOP_RELATIONS for edge in ir.edges)


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


def _extract_node_tags(node: IRNode) -> list[str]:
    """Extract sorted sanitized $tag names from node attributes."""
    raw_combined = ",".join(
        val for key in ("tag", "tags") if (val := node.attributes.get(key, "").strip())
    )
    if not raw_combined:
        return []
    tags = {_sanitize_tag(part) for part in raw_combined.split(",")}
    return sorted(tag for tag in tags if tag)


def _format_member_modifiers(modifiers: tuple[str, ...]) -> str:
    """Format PlantUML member modifiers ({static}, {abstract}, or clean tokens)."""
    if not modifiers:
        return ""
    parts: list[str] = []
    for raw_mod in modifiers:
        token = _sanitize_token(raw_mod)
        lowered = token.lower()
        if lowered in {"static", "abstract"}:
            parts.append(f"{{{lowered}}}")
        else:
            parts.append(token)
    return " ".join(parts) + " "


def _format_class_member(member: ClassMember, indent: str) -> str:
    """Format a ClassMember compartment line with + - # ~ visibility and generics."""
    vis = member.visibility.value
    mods = _format_member_modifiers(member.modifiers)
    name = _sanitize_token(member.name)
    generics = (
        f"<{', '.join(_sanitize_token(g) for g in member.generic_params)}>"
        if member.generic_params
        else ""
    )
    sig = _sanitize_member_text(member.signature)
    if member.is_method:
        return _format_method_member(indent, vis, mods, name, generics, sig)
    if sig:
        clean_type = sig.replace("(", "[").replace(")", "]")
        return f"{indent}{vis}{mods}{name}{generics} : {clean_type}"
    return f"{indent}{vis}{mods}{name}{generics}"


def _format_method_member(
    indent: str,
    vis: str,
    mods: str,
    name: str,
    generics: str,
    sig: str,
) -> str:
    """Format a method ClassMember line into valid PlantUML method compartment syntax."""
    if "->" in sig:
        params_part, ret_part = sig.split("->", 1)
        params = params_part.strip().removeprefix("(").removesuffix(")").strip()
        ret = ret_part.strip()
        if ret:
            return f"{indent}{vis}{mods}{name}{generics}({params}) : {ret}"
        return f"{indent}{vis}{mods}{name}{generics}({params})"
    if sig.startswith("(") and sig.endswith(")"):
        return f"{indent}{vis}{mods}{name}{generics}{sig}"
    return f"{indent}{vis}{mods}{name}{generics}({sig})"


def _format_class_header(node: IRNode, alias: str) -> str:
    """Format a class/interface/enum declaration header with generics and tags."""
    keyword = _CLASS_KEYWORDS.get(node.kind, "class")
    raw_generics = node.attributes.get("generic_params", "")
    generic_suffix = ""
    if raw_generics.strip():
        tokens = [_sanitize_token(part) for part in raw_generics.split(",") if part.strip()]
        if tokens:
            generic_suffix = f"<{', '.join(tokens)}>"
    label = sanitize_plantuml_label(node.label)
    tags = _extract_node_tags(node)
    tag_suffix = f" {' '.join(f'${tag}' for tag in tags)}" if tags else ""
    return f'{keyword} "{label}{generic_suffix}" as {alias}{tag_suffix}'


def _emit_class_subtree(
    sym_id: str,
    nodes: dict[str, IRNode],
    children: dict[str, list[str]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit a class/interface/enum block or nested package container in Mode B."""
    indent = "  " * depth
    node = nodes[sym_id]
    alias = aliases[sym_id]
    child_ids = children[sym_id]
    if child_ids:
        pkg_label = sanitize_plantuml_label(node.label)
        lines = [f'{indent}package "{pkg_label}" as {alias} {{']
        for child_id in child_ids:
            lines.extend(_emit_class_subtree(child_id, nodes, children, aliases, depth + 1))
        lines.append(f"{indent}}}")
        return lines

    header = f"{indent}{_format_class_header(node, alias)}"
    if not node.members:
        return [header]
    lines = [f"{header} {{"]
    inner_indent = "  " * (depth + 1)
    for member in node.members:
        lines.append(_format_class_member(member, inner_indent))
    lines.append(f"{indent}}}")
    return lines


_REVERSED_CLASS_ARROWS: dict[EdgeRelation, str] = {
    EdgeRelation.INHERITS: "<|--",
    EdgeRelation.IMPLEMENTS: "<|..",
}

_DIRECT_CLASS_ARROWS: dict[EdgeRelation, str] = {
    EdgeRelation.COMPOSES: "*--",
    EdgeRelation.AGGREGATES: "o--",
}


def _emit_styled_structural_edge(edge: IREdge, aliases: dict[str, str]) -> str:
    """Emit a UML relationship arrow with edge-only semantic `[#rrggbb]` stroke color."""
    src = aliases[edge.source]
    tgt = aliases[edge.target]
    label = sanitize_plantuml_message(edge.relation.value)
    visual = resolve_edge_visual_style(edge.relation, edge.sync_kind)
    color = visual.stroke_hex
    if edge.relation in _REVERSED_CLASS_ARROWS:
        return f"{tgt} {_REVERSED_CLASS_ARROWS[edge.relation]}[{color}] {src} : {label}"
    if edge.relation in _DIRECT_CLASS_ARROWS:
        return f"{src} {_DIRECT_CLASS_ARROWS[edge.relation]}[{color}] {tgt} : {label}"
    if visual.line_pattern is LinePattern.BOLD:
        return f"{src} -[{color},bold]-> {tgt} : {label}"
    if visual.line_pattern is LinePattern.DASHED:
        return f"{src} ..>[{color}] {tgt} : {label}"
    return f"{src} -->[{color}] {tgt} : {label}"


def _emit_class_diagram(ir: StructuralGraphIR) -> str:
    """Emit a deterministic PlantUML OOP class diagram (Mode B) with orthogonal routing."""
    sorted_ids = sorted(ir.nodes)
    aliases = build_alias_map(sorted_ids, prefix="c")
    roots, children = _build_children_map(ir.nodes)
    lines: list[str] = ["@startuml", "!pragma layout elk", *PLANTUML_ORTHO_SKINPARAMS]
    for root_id in roots:
        lines.extend(_emit_class_subtree(root_id, ir.nodes, children, aliases, depth=0))
    sorted_edges = sorted(
        ir.edges,
        key=lambda e: (e.source, e.target, e.relation.value, e.sync_kind.value, e.edge_id),
    )
    for edge in sorted_edges:
        lines.append(_emit_styled_structural_edge(edge, aliases))
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def _emit_component_subtree(
    sym_id: str,
    nodes: dict[str, IRNode],
    children: dict[str, list[str]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Recursively emit nested package/rectangle containers and component leaves."""
    indent = "  " * depth
    node = nodes[sym_id]
    alias = aliases[sym_id]
    label = sanitize_plantuml_label(node.label)
    child_ids = children[sym_id]
    if child_ids:
        container_kw = (
            "package" if node.kind in {NodeKind.PACKAGE, NodeKind.MODULE} else "rectangle"
        )
        lines = [f'{indent}{container_kw} "{label}" as {alias} {{']
        for child_id in child_ids:
            lines.extend(_emit_component_subtree(child_id, nodes, children, aliases, depth + 1))
        lines.append(f"{indent}}}")
        return lines
    leaf_kw = _COMPONENT_LEAF_KEYWORDS.get(node.kind, "component")
    return [f'{indent}{leaf_kw} "{label}" as {alias}']


def _emit_component_diagram(ir: StructuralGraphIR) -> str:
    """Emit a deterministic PlantUML ELK component/architecture view (Mode C)."""
    sorted_ids = sorted(ir.nodes)
    aliases = build_alias_map(sorted_ids, prefix="n")
    roots, children = _build_children_map(ir.nodes)
    lines: list[str] = ["@startuml", "!pragma layout elk", *PLANTUML_ORTHO_SKINPARAMS]
    for root_id in roots:
        lines.extend(_emit_component_subtree(root_id, ir.nodes, children, aliases, depth=0))
    sorted_edges = sorted(
        ir.edges,
        key=lambda e: (e.source, e.target, e.relation.value, e.sync_kind.value, e.edge_id),
    )
    for edge in sorted_edges:
        lines.append(_emit_styled_structural_edge(edge, aliases))
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _SingleStep:
    """Single sequence event step."""

    event: TraceEvent


@dataclass(frozen=True, slots=True)
class _ParStep:
    """Overlapping same-clock-domain sibling spans grouped into par ... else ... end."""

    branches: tuple[TraceEvent, ...]


@dataclass(frozen=True, slots=True)
class _LoopStep:
    """Contiguous identical sibling calls grouped into loop N times ... end."""

    events: tuple[TraceEvent, ...]


@dataclass(frozen=True, slots=True)
class _AnchorStep:
    """Concurrent cross-branch sibling events sharing start_ns emitted with teoz & anchors."""

    events: tuple[TraceEvent, ...]


type _SequenceStep = _SingleStep | _ParStep | _LoopStep | _AnchorStep


def _can_join_cluster(
    event: TraceEvent,
    cluster_idx: int | None,
    clusters: list[list[TraceEvent]],
) -> bool:
    """Check whether event overlaps its domain's active cluster without breaking causality."""
    if cluster_idx is None:
        return False
    cluster = clusters[cluster_idx]
    if not any(events_overlap(member, event) for member in cluster):
        return False
    if not event.causal_links:
        return True
    links = set(event.causal_links)
    for later_cluster in clusters[cluster_idx:]:
        if any(member.event_id in links for member in later_cluster):
            return False
    return True


def _cluster_parallel_siblings(siblings: list[TraceEvent]) -> list[list[TraceEvent]]:
    """Group causally independent overlapping same-clock-domain sibling spans."""
    clusters: list[list[TraceEvent]] = []
    last_by_domain: dict[str, int] = {}
    for event in siblings:
        c_idx = last_by_domain.get(event.clock_domain_id)
        if _can_join_cluster(event, c_idx, clusters) and c_idx is not None:
            clusters[c_idx].append(event)
        else:
            clusters.append([event])
            last_by_domain[event.clock_domain_id] = len(clusters) - 1
    return clusters


def _can_merge_into_loop(
    first: TraceEvent,
    second: TraceEvent,
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> bool:
    """Determine if two consecutive same-domain non-concurrent calls form a repeatable loop."""
    if children_by_parent.get(first.event_id) or children_by_parent.get(second.event_id):
        return False
    if first.clock_domain_id != second.clock_domain_id or first.start_ns == second.start_ns:
        return False
    return (
        first.caller_id == second.caller_id
        and first.callee_id == second.callee_id
        and first.signature == second.signature
        and first.sync_kind == second.sync_kind
    )


def _can_share_anchor(
    anchor_run: list[TraceEvent],
    candidate: TraceEvent,
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> bool:
    """Check whether candidate can be anchored in parallel via & at the same start_ns."""
    if candidate.start_ns != anchor_run[0].start_ns:
        return False
    if children_by_parent.get(anchor_run[0].event_id) or children_by_parent.get(candidate.event_id):
        return False
    seen_ids = {ev.event_id for ev in anchor_run}
    return not (set(candidate.causal_links) & seen_ids)


def _starts_anchor_pair(
    clusters: list[list[TraceEvent]],
    idx: int,
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> bool:
    """Return True if singleton cluster at idx shares start_ns with the next singleton."""
    if idx + 1 >= len(clusters) or len(clusters[idx + 1]) != 1:
        return False
    return _can_share_anchor([clusters[idx][0]], clusters[idx + 1][0], children_by_parent)


def _collect_singleton_step(
    clusters: list[list[TraceEvent]],
    start_idx: int,
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> tuple[_SequenceStep, int]:
    """Consume consecutive singleton clusters into _AnchorStep, _LoopStep, or _SingleStep."""
    first = clusters[start_idx][0]
    anchor_run = [first]
    next_idx = start_idx + 1
    while (
        next_idx < len(clusters)
        and len(clusters[next_idx]) == 1
        and _can_share_anchor(anchor_run, clusters[next_idx][0], children_by_parent)
    ):
        anchor_run.append(clusters[next_idx][0])
        next_idx += 1
    if len(anchor_run) >= 2:
        return _AnchorStep(events=tuple(anchor_run)), next_idx

    loop_run = [first]
    next_idx = start_idx + 1
    while (
        next_idx < len(clusters)
        and len(clusters[next_idx]) == 1
        and not _starts_anchor_pair(clusters, next_idx, children_by_parent)
        and _can_merge_into_loop(loop_run[-1], clusters[next_idx][0], children_by_parent)
    ):
        loop_run.append(clusters[next_idx][0])
        next_idx += 1
    if len(loop_run) >= 2:
        return _LoopStep(events=tuple(loop_run)), next_idx
    return _SingleStep(event=first), start_idx + 1


def _group_sibling_steps(
    siblings: list[TraceEvent],
    children_by_parent: dict[str | None, list[TraceEvent]],
) -> list[_SequenceStep]:
    """Partition sibling events into par, anchor, loop, and single sequence steps."""
    clusters = _cluster_parallel_siblings(siblings)
    steps: list[_SequenceStep] = []
    idx = 0
    while idx < len(clusters):
        cluster = clusters[idx]
        if len(cluster) > 1:
            steps.append(_ParStep(branches=tuple(cluster)))
            idx += 1
            continue
        step, idx = _collect_singleton_step(clusters, idx, children_by_parent)
        steps.append(step)
    return steps


def _emit_event_invocation(
    event: TraceEvent,
    children_by_parent: dict[str | None, list[TraceEvent]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit a single call arrow, nested child steps, and return arrow for sync calls."""
    indent = "  " * depth
    caller = aliases[event.caller_id]
    callee = aliases[event.callee_id]
    sig = sanitize_plantuml_message(event.signature)
    child_events = children_by_parent.get(event.event_id, [])
    if event.sync_kind is SyncKind.ASYNC_FIRE_FORGET:
        lines = [f"{indent}{caller} ->> {callee} : {sig}"]
        if child_events:
            lines.extend(_emit_sibling_list(child_events, children_by_parent, aliases, depth))
        return lines
    lines = [f"{indent}{caller} -> {callee} : {sig}"]
    if child_events:
        lines.extend(_emit_sibling_list(child_events, children_by_parent, aliases, depth))
    lines.append(f"{indent}{callee} --> {caller} : return {sig}")
    return lines


def _emit_anchor_step(
    step: _AnchorStep,
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit concurrent same-start_ns events using teoz '&' parallel anchors."""
    indent = "  " * depth
    lines: list[str] = []
    sync_events: list[TraceEvent] = []
    for idx, event in enumerate(step.events):
        prefix = "& " if idx > 0 else ""
        caller = aliases[event.caller_id]
        callee = aliases[event.callee_id]
        sig = sanitize_plantuml_message(event.signature)
        arrow = "->>" if event.sync_kind is SyncKind.ASYNC_FIRE_FORGET else "->"
        lines.append(f"{indent}{prefix}{caller} {arrow} {callee} : {sig}")
        if event.sync_kind is not SyncKind.ASYNC_FIRE_FORGET:
            sync_events.append(event)
    for idx, event in enumerate(sync_events):
        prefix = "& " if idx > 0 else ""
        caller = aliases[event.caller_id]
        callee = aliases[event.callee_id]
        sig = sanitize_plantuml_message(event.signature)
        lines.append(f"{indent}{prefix}{callee} --> {caller} : return {sig}")
    return lines


def _emit_par_step(
    step: _ParStep,
    children_by_parent: dict[str | None, list[TraceEvent]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit a PlantUML par ... else ... end fragment for overlapping same-domain spans."""
    indent = "  " * depth
    lines: list[str] = []
    for idx, branch in enumerate(step.branches):
        kw = "par" if idx == 0 else "else"
        label = sanitize_plantuml_message(branch.signature)
        lines.append(f"{indent}{kw} {label}")
        lines.extend(_emit_event_invocation(branch, children_by_parent, aliases, depth + 1))
    lines.append(f"{indent}end")
    return lines


def _emit_sibling_list(
    siblings: list[TraceEvent],
    children_by_parent: dict[str | None, list[TraceEvent]],
    aliases: dict[str, str],
    depth: int,
) -> list[str]:
    """Emit grouped sequence steps (par, anchor, loop, single) at requested indentation."""
    indent = "  " * depth
    lines: list[str] = []
    for step in _group_sibling_steps(siblings, children_by_parent):
        if isinstance(step, _SingleStep):
            lines.extend(_emit_event_invocation(step.event, children_by_parent, aliases, depth))
        elif isinstance(step, _AnchorStep):
            lines.extend(_emit_anchor_step(step, aliases, depth))
        elif isinstance(step, _LoopStep):
            count = len(step.events)
            lines.append(f"{indent}loop {count} times")
            lines.extend(
                _emit_event_invocation(step.events[0], children_by_parent, aliases, depth + 1)
            )
            lines.append(f"{indent}end")
        else:
            lines.extend(_emit_par_step(step, children_by_parent, aliases, depth))
    return lines


def _format_participant_line(participant: TraceParticipant, alias: str) -> str:
    """Format a deterministic PlantUML sequence participant declaration."""
    keyword = _PARTICIPANT_KEYWORDS.get(participant.kind, "participant")
    label = sanitize_plantuml_label(participant.label)
    return f'{keyword} "{label}" as {alias}'


def _emit_sequence_diagram(ir: InteractionTraceIR) -> str:
    """Emit a deterministic PlantUML teoz concurrent sequence diagram (Mode A)."""
    sorted_pids = sorted(ir.participants)
    aliases = build_alias_map(sorted_pids, prefix="p")
    lines: list[str] = ["@startuml", "!pragma teoz true", *PLANTUML_SEQUENCE_SKINPARAMS]
    for pid in sorted_pids:
        lines.append(_format_participant_line(ir.participants[pid], aliases[pid]))

    children_by_parent: dict[str | None, list[TraceEvent]] = {}
    for event in order_trace_events(ir.events):
        children_by_parent.setdefault(event.parent_event_id, []).append(event)

    root_events = children_by_parent.get(None, [])
    lines.extend(_emit_sibling_list(root_events, children_by_parent, aliases, depth=0))
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def _dispatch_plantuml_variant(ir: CanonicalIR, variant: str) -> str | None:
    """Dispatch non-default UML 2.5 projection variants for PlantUmlEmitter."""
    from archigram.features.emit_plantuml import uml_variants as uv

    if variant in {"package", "deployment"}:
        return uv.emit_plantuml_package_or_deployment(ir, variant)
    dispatch = {
        "state": uv.emit_plantuml_state,
        "communication": uv.emit_plantuml_communication,
        "activity": uv.emit_plantuml_activity,
        "use_case": uv.emit_plantuml_use_case,
    }
    fn = dispatch.get(variant)
    return fn(ir) if fn is not None else None


class PlantUmlEmitter:
    """Deterministic PlantUML emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target format identifier 'plantuml'."""
        return "plantuml"

    def emit(self, ir: CanonicalIR) -> str:
        """Emit deterministic PlantUML syntax from StructuralGraphIR or InteractionTraceIR."""
        variant = ir.metadata.get("diagram_variant", "auto")
        dispatched = _dispatch_plantuml_variant(ir, variant)
        if dispatched is not None:
            return dispatched
        if isinstance(ir, StructuralGraphIR):
            if _is_class_diagram_mode(ir):
                return _emit_class_diagram(ir)
            return _emit_component_diagram(ir)
        return _emit_sequence_diagram(ir)


build_puml_children_map = _build_children_map
order_puml_trace_events = order_trace_events
