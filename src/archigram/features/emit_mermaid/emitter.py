"""Deterministic Mermaid Emitter compatible with Gitea (Mermaid v10+) and Mermaid v11/v12."""

from __future__ import annotations

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
from archigram.domain.style import (
    DEFAULT_STROKE_WIDTH_PX,
    MERMAID_DEFAULT_CLASSDEF,
    MERMAID_SEQUENCE_FRONTMATTER,
    MERMAID_STRUCTURAL_FRONTMATTER,
    MERMAID_SUBGRAPH_STYLE_ATTRS,
    LinePattern,
    resolve_edge_visual_style,
)

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
    """Escape semicolons, quotes, brackets, and control characters for Mermaid quoted labels."""
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


def _make_safe_alias(prefix: str, index: int) -> str:
    """Build a compact collision-free Mermaid identifier (`n0`, `c0`, `p0`)."""
    return f"{prefix}{index}"


def build_alias_map(ids: list[str], prefix: str = "n") -> dict[str, str]:
    """Map sorted raw identifiers to compact deterministic Mermaid aliases."""
    return {raw_id: _make_safe_alias(prefix, idx) for idx, raw_id in enumerate(sorted(set(ids)))}


_OOP_NODE_KINDS: frozenset[NodeKind] = frozenset(
    {
        NodeKind.CLASS,
        NodeKind.INTERFACE,
        NodeKind.ENUM,
    }
)


def _is_class_diagram_mode(ir: StructuralGraphIR) -> bool:
    """Determine if a StructuralGraphIR should emit an OOP classDiagram instead of flowchart."""
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


_RESERVED_TOKENS: frozenset[str] = frozenset(
    {"end", "subgraph", "class", "par", "loop", "and", "participant"}
)

_CLASS_SIG_STRIP_TRANS = str.maketrans(
    {
        "\r": " ",
        "\n": " ",
        "\t": " ",
        '"': " ",
        "'": " ",
        ";": " ",
        "{": " ",
        "}": " ",
        "(": " ",
        ")": " ",
        "#": " ",
        "~": " ",
    }
)


def _sanitize_token(raw: str) -> str:
    """Sanitize an identifier token inside a class member or generic parameter."""
    token = _SLUG_RE.sub("_", raw).strip("_")
    if not token:
        return "T"
    if token.lower() in _RESERVED_TOKENS:
        return f"{token}_"
    return token


def _brackets_balanced(text: str, opener: str, closer: str) -> bool:
    """Return True when opener/closer brackets in `text` are properly balanced."""
    depth = 0
    for ch in text:
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _sanitize_class_sig(raw: str) -> str:
    """Sanitize a class member signature without semicolon entities for Mermaid classDiagram."""
    cleaned = raw.replace("->", " ").translate(_CLASS_SIG_STRIP_TRANS)
    cleaned = re.sub(r",\s*\.\.\.", "", cleaned).replace("...", "")
    if "[" in cleaned or "]" in cleaned:
        rep = "~" if _brackets_balanced(cleaned, "[", "]") else "_"
        cleaned = cleaned.replace("[", rep).replace("]", rep)
    if "<" in cleaned or ">" in cleaned:
        rep = "~" if _brackets_balanced(cleaned, "<", ">") else "_"
        cleaned = cleaned.replace("<", rep).replace(">", rep)
    printable = "".join(ch if ch.isprintable() else " " for ch in cleaned)
    return " ".join(printable.split())


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
    mods = f" {' '.join(_sanitize_token(m) for m in member.modifiers)}" if member.modifiers else ""
    if member.is_method:
        if "->" in raw_sig:
            params_part, ret_part = raw_sig.split("->", 1)
            params_clean = _sanitize_class_sig(params_part)
            ret_clean = _sanitize_class_sig(ret_part) or "None"
            return f"    {vis}{name}{generics}({params_clean}) {ret_clean}{mods}"
        return f"    {vis}{name}{generics}({_sanitize_class_sig(raw_sig)}){mods}"
    sig_clean = _sanitize_class_sig(raw_sig)
    if sig_clean:
        return f"    {vis}{sig_clean} {name}{generics}{mods}"
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
    visual = resolve_edge_visual_style(edge.relation, edge.sync_kind)
    if visual.line_pattern is LinePattern.DASHED:
        return f"  {src} ..> {tgt} : {label}"
    return f"  {src} --> {tgt} : {label}"


def _emit_class_diagram(ir: StructuralGraphIR) -> str:
    """Emit a deterministic Mermaid OOP classDiagram compatible with Gitea and Mermaid v10+."""
    parent_ids = {n.parent_id for n in ir.nodes.values() if n.parent_id is not None}
    incident_ids = {e.source for e in ir.edges} | {e.target for e in ir.edges}
    visible_ids = [
        sid
        for sid in sorted(ir.nodes)
        if not (
            sid in parent_ids
            and sid not in incident_ids
            and ir.nodes[sid].kind not in _OOP_NODE_KINDS
        )
    ] or sorted(ir.nodes)
    aliases = build_alias_map(visible_ids, prefix="c")
    lines: list[str] = [
        MERMAID_STRUCTURAL_FRONTMATTER,
        "classDiagram",
    ]
    for sym_id in visible_ids:
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


def _coalesce_link_styles(style_specs: list[str]) -> list[str]:
    """Group edge indices sharing identical stroke attributes into compact `linkStyle` lines."""
    grouped: dict[str, list[str]] = {}
    for edge_idx, spec in enumerate(style_specs):
        grouped.setdefault(spec, []).append(str(edge_idx))
    return [f"  linkStyle {','.join(indices)} {spec}" for spec, indices in grouped.items()]


def _emit_flowchart_edge_and_style(
    edge: IREdge,
    edge_idx: int,
    aliases: dict[str, str],
    animate_override: str | None = None,
) -> tuple[list[str], str]:
    """Emit flowchart edge line(s) (opt-in v11.4+ animation) and stroke style spec."""
    src = aliases[edge.source]
    tgt = aliases[edge.target]
    label = sanitize_mermaid_text(edge.relation.value)
    visual = resolve_edge_visual_style(edge.relation, edge.sync_kind)
    if visual.line_pattern is LinePattern.BOLD:
        arrow = "==>"
        style_spec = f"stroke:{visual.stroke_hex},stroke-width:2.5px"
    elif visual.line_pattern is LinePattern.DASHED:
        arrow = "-.->"
        style_spec = (
            f"stroke:{visual.stroke_hex},"
            f"stroke-width:{DEFAULT_STROKE_WIDTH_PX},stroke-dasharray:5 5"
        )
    else:
        arrow = "-->"
        style_spec = f"stroke:{visual.stroke_hex},stroke-width:{DEFAULT_STROKE_WIDTH_PX}"
    if animate_override == "true":
        edge_id = f"e{edge_idx}"
        return [
            f'  {src} {edge_id}@{arrow}|"{label}"| {tgt}',
            f"  {edge_id}@{{ animate: true }}",
        ], style_spec
    return [f'  {src} {arrow}|"{label}"| {tgt}'], style_spec


def _emit_flowchart(ir: StructuralGraphIR) -> str:
    """Emit a deterministic ELK orthogonal flowchart TD diagram."""
    sorted_ids = sorted(ir.nodes)
    aliases = build_alias_map(sorted_ids, prefix="n")
    roots, children = _build_children_map(ir.nodes)
    animate_override = ir.metadata.get("animate_edges")
    lines: list[str] = [
        MERMAID_STRUCTURAL_FRONTMATTER,
        "flowchart TD",
        f"  {MERMAID_DEFAULT_CLASSDEF}",
    ]
    for root_id in roots:
        lines.extend(_emit_flowchart_subtree(root_id, ir.nodes, children, aliases, depth=1))
    for sym_id in sorted_ids:
        if children[sym_id]:
            lines.append(f"  style {aliases[sym_id]} {MERMAID_SUBGRAPH_STYLE_ATTRS}")
    style_specs: list[str] = []
    for edge_idx, edge in enumerate(sorted(ir.edges, key=lambda item: item.edge_id)):
        edge_lines, style_spec = _emit_flowchart_edge_and_style(
            edge,
            edge_idx,
            aliases,
            animate_override,
        )
        lines.extend(edge_lines)
        style_specs.append(style_spec)
    lines.extend(_coalesce_link_styles(style_specs))
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
    lines.append(f"{indent}{callee}-->>-{caller}: return")
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
    lines: list[str] = [MERMAID_SEQUENCE_FRONTMATTER, "sequenceDiagram"]
    for pid in sorted_pids:
        lines.append(_format_participant_line(ir.participants[pid], aliases[pid]))

    children_by_parent: dict[str | None, list[TraceEvent]] = {}
    for event in sorted(ir.events.values(), key=lambda item: (item.start_ns, item.event_id)):
        children_by_parent.setdefault(event.parent_event_id, []).append(event)

    root_events = children_by_parent.get(None, [])
    lines.extend(_emit_sibling_list(root_events, children_by_parent, aliases, depth=1))
    return "\n".join(lines) + "\n"


def _dispatch_mermaid_variant(ir: CanonicalIR, variant: str) -> str | None:
    """Dispatch non-default UML 2.5 projection variants for MermaidEmitter."""
    from archigram.features.emit_mermaid import uml_variants as uv

    if variant == "package":
        return uv.emit_mermaid_package(ir)
    if variant == "deployment":
        return uv.emit_mermaid_deployment(ir)
    if isinstance(ir, StructuralGraphIR):
        structural_map = {
            "state": uv.emit_mermaid_state_structural,
            "communication": uv.emit_mermaid_communication_structural,
            "activity": uv.emit_mermaid_activity_structural,
            "use_case": uv.emit_mermaid_use_case_structural,
        }
        fn_s = structural_map.get(variant)
        return fn_s(ir) if fn_s is not None else None
    trace_map = {
        "state": uv.emit_mermaid_state_trace,
        "communication": uv.emit_mermaid_communication_trace,
        "activity": uv.emit_mermaid_activity_trace,
        "use_case": uv.emit_mermaid_use_case_trace,
    }
    fn_t = trace_map.get(variant)
    return fn_t(ir) if fn_t is not None else None


class MermaidEmitter:
    """Deterministic Mermaid emitter implementing DiagramEmitterPort."""

    @property
    def target_format(self) -> str:
        """Return target format identifier 'mermaid'."""
        return "mermaid"

    def emit(self, ir: CanonicalIR) -> str:
        """Emit deterministic Mermaid syntax from a StructuralGraphIR or InteractionTraceIR."""
        variant = ir.metadata.get("diagram_variant", "auto")
        dispatched = _dispatch_mermaid_variant(ir, variant)
        if dispatched is not None:
            return dispatched
        if isinstance(ir, StructuralGraphIR):
            if _is_class_diagram_mode(ir):
                return _emit_class_diagram(ir)
            return _emit_flowchart(ir)
        return _emit_sequence_diagram(ir)
