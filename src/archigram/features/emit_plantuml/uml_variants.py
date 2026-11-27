"""UML 2.5 PlantUML emitters (State, Activity, Communication, Use Case, Package, Deployment)."""

from __future__ import annotations

from dataclasses import dataclass

from archigram.domain.concurrency import find_causal_overlap_span
from archigram.domain.ir import (
    CanonicalIR,
    EdgeRelation,
    InteractionTraceIR,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    TraceEvent,
)
from archigram.domain.style import (
    CANONICAL_EDGE_STYLES,
    PLANTUML_BEHAVIORAL_SKINPARAMS,
    PLANTUML_ORTHO_SKINPARAMS,
)
from archigram.features.emit_plantuml.emitter import (
    build_alias_map,
    sanitize_plantuml_label,
    sanitize_plantuml_message,
)
from archigram.features.emit_plantuml.emitter import (
    build_puml_children_map as _build_children_map,
)
from archigram.features.emit_plantuml.emitter import (
    order_puml_trace_events as _order_trace_events,
)

_CALL_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex
_IMPORT_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.IMPORTS].stroke_hex
_MAX_EDGE_MSGS = 3
_MAX_ACTIVITY_STEPS = 18


def _short_sig(sig: str) -> str:
    base = sig.split("->", 1)[0].strip()
    if "(" in base:
        fn_name = base.split("(", 1)[0].strip().rsplit(".", 1)[-1]
        return f"{fn_name}()" if fn_name else base[:32]
    return base[:36] if base else "call"


def _clean_action_label(raw: str) -> str:
    cleaned = sanitize_plantuml_label(raw).replace(";", ",").replace("{", "(").replace("}", ")")
    return cleaned if cleaned else "step"


def _number_trace_events(trace: InteractionTraceIR) -> list[tuple[str, TraceEvent]]:
    by_parent: dict[str | None, list[TraceEvent]] = {}
    for ev in _order_trace_events(trace.events):
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


def emit_plantuml_state(ir: CanonicalIR) -> str:
    """Emit a deterministic UML 2.5 State Machine Diagram in PlantUML."""
    lines = ["@startuml", *PLANTUML_BEHAVIORAL_SKINPARAMS]
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        aliases = build_alias_map(sorted_ids, prefix="s")
        for sid in sorted_ids:
            lines.append(
                f'state "{sanitize_plantuml_label(ir.nodes[sid].label)}" as {aliases[sid]}'
            )
        if sorted_ids:
            in_deg = dict.fromkeys(sorted_ids, 0)
            out_deg = dict.fromkeys(sorted_ids, 0)
            for edge in ir.edges:
                in_deg[edge.target] += 1
                out_deg[edge.source] += 1
            starts = [s for s in sorted_ids if in_deg[s] == 0] or [sorted_ids[0]]
            ends = [s for s in sorted_ids if out_deg[s] == 0] or [sorted_ids[-1]]
            lines.extend(f"[*] --> {aliases[s]}" for s in starts[:3])
            for edge in sorted(ir.edges, key=lambda item: item.edge_id):
                msg = sanitize_plantuml_message(edge.relation.value)
                lines.append(
                    f"{aliases[edge.source]} -->[{_CALL_HEX}] {aliases[edge.target]} : {msg}"
                )
            lines.extend(f"{aliases[s]} --> [*]" for s in ends[:3])
        lines.append("@enduml")
        return "\n".join(lines) + "\n"

    sorted_pids = sorted(ir.participants)
    aliases = build_alias_map(sorted_pids, prefix="s")
    for pid in sorted_pids:
        lines.append(
            f'state "{sanitize_plantuml_label(ir.participants[pid].label)}" as {aliases[pid]}'
        )
    ordered = _order_trace_events(ir.events)
    if ordered:
        lines.append(f"[*] --> {aliases[ordered[0].caller_id]}")
        seen: dict[tuple[str, str], str] = {}
        for ev in ordered:
            seen.setdefault(
                (ev.caller_id, ev.callee_id), sanitize_plantuml_message(_short_sig(ev.signature))
            )
        for (src, dst), trig in seen.items():
            lines.append(f"{aliases[src]} -->[{_CALL_HEX}] {aliases[dst]} : {trig}")
        lines.append(f"{aliases[ordered[-1].callee_id]} --> [*]")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def emit_plantuml_communication(ir: CanonicalIR) -> str:
    """Emit a deterministic UML 2.5 Communication Diagram with numbered links in PlantUML."""
    lines = ["@startuml", "!pragma layout elk", *PLANTUML_ORTHO_SKINPARAMS]
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        aliases = build_alias_map(sorted_ids, prefix="o")
        for sid in sorted_ids:
            lines.append(
                f'rectangle ":{sanitize_plantuml_label(ir.nodes[sid].label)}" as {aliases[sid]}'
            )
        for idx, edge in enumerate(sorted(ir.edges, key=lambda item: item.edge_id), start=1):
            msg = sanitize_plantuml_message(f"{idx}: {edge.relation.value}")
            lines.append(f"{aliases[edge.source]} -->[{_CALL_HEX}] {aliases[edge.target]} : {msg}")
        lines.append("@enduml")
        return "\n".join(lines) + "\n"

    sorted_pids = sorted(ir.participants)
    aliases = build_alias_map(sorted_pids, prefix="o")
    for pid in sorted_pids:
        lines.append(
            f'rectangle ":{sanitize_plantuml_label(ir.participants[pid].label)}" as {aliases[pid]}'
        )
    grouped: dict[tuple[str, str], list[str]] = {}
    for seq_no, ev in _number_trace_events(ir):
        grouped.setdefault((ev.caller_id, ev.callee_id), []).append(
            f"{seq_no}: {_short_sig(ev.signature)}"
        )
    for (src, dst), msgs in grouped.items():
        shown = msgs[:_MAX_EDGE_MSGS]
        suffix = f" (+{len(msgs) - _MAX_EDGE_MSGS})" if len(msgs) > _MAX_EDGE_MSGS else ""
        msg = sanitize_plantuml_message(", ".join(shown) + suffix)
        lines.append(f"{aliases[src]} -->[{_CALL_HEX}] {aliases[dst]} : {msg}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def emit_plantuml_activity(ir: CanonicalIR) -> str:
    """Emit a deterministic UML 2.5 Activity Diagram (`start` / `:action;` / `fork` / `stop`)."""
    lines = ["@startuml", *PLANTUML_BEHAVIORAL_SKINPARAMS, "start"]
    if isinstance(ir, StructuralGraphIR):
        for sid in sorted(ir.nodes)[:_MAX_ACTIVITY_STEPS]:
            lines.append(f":{_clean_action_label(ir.nodes[sid].label)};")
        lines.extend(("stop", "@enduml"))
        return "\n".join(lines) + "\n"

    ordered = _order_trace_events(ir.events)
    capped = ordered[:_MAX_ACTIVITY_STEPS]
    idx = 0
    while idx < len(capped):
        ev = capped[idx]
        overlap_end = find_causal_overlap_span(capped, idx)
        if overlap_end - idx > 1:
            for b_idx in range(idx, overlap_end):
                lines.append("fork" if b_idx == idx else "fork again")
                b_ev = capped[b_idx]
                act = _clean_action_label(
                    f"{ir.participants[b_ev.callee_id].label}: {_short_sig(b_ev.signature)}"
                )
                lines.append(f"  :{act};")
            lines.append("end fork")
            idx = overlap_end
            continue
        act = _clean_action_label(
            f"{ir.participants[ev.callee_id].label}: {_short_sig(ev.signature)}"
        )
        lines.append(f":{act};")
        idx += 1
    if len(ordered) > _MAX_ACTIVITY_STEPS:
        lines.append(f":+{len(ordered) - _MAX_ACTIVITY_STEPS} compacted actions;")
    lines.extend(("stop", "@enduml"))
    return "\n".join(lines) + "\n"


def _emit_use_case_body(
    labels: dict[str, str],
    actors: list[str],
    use_cases: list[str],
    edges: list[tuple[str, str]],
) -> str:
    aliases = build_alias_map(labels.keys(), prefix="u")
    lines = ["@startuml", "left to right direction", *PLANTUML_BEHAVIORAL_SKINPARAMS]
    for aid in actors:
        lines.append(f'actor "{sanitize_plantuml_label(labels[aid])}" as {aliases[aid]}')
    if use_cases:
        lines.append('rectangle "System Subject Boundary" as sys_boundary {')
        for ucid in use_cases:
            lines.append(f'  usecase "{sanitize_plantuml_label(labels[ucid])}" as {aliases[ucid]}')
        lines.append("}")
    actor_set = set(actors)
    for src, dst in edges:
        if src in actor_set:
            lines.append(f"{aliases[src]} -->[{_CALL_HEX}] {aliases[dst]} : invokes")
        else:
            lines.append(f"{aliases[src]} ..>[{_CALL_HEX}] {aliases[dst]} : <<include>>")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def emit_plantuml_use_case(ir: CanonicalIR) -> str:
    """Emit a deterministic UML 2.5 Use Case Diagram (`actor` -> `usecase`) in PlantUML."""
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        actors = [
            sid
            for sid in sorted_ids
            if ir.nodes[sid].kind is NodeKind.EXTERNAL
            or any(tok in sid.lower() for tok in ("cli", "mcp", "entrypoint"))
        ]
        if (not actors or len(actors) == len(sorted_ids)) and len(sorted_ids) >= 2:
            in_deg = dict.fromkeys(sorted_ids, 0)
            for e in ir.edges:
                in_deg[e.target] += 1
            roots = [s for s in sorted_ids if in_deg[s] == 0]
            actors = roots[:2] if roots and len(roots) < len(sorted_ids) else [sorted_ids[0]]
        actor_set = set(actors)
        use_cases = [s for s in sorted_ids if s not in actor_set]
        labels = {sid: ir.nodes[sid].label for sid in sorted_ids}
        edges = [(e.source, e.target) for e in sorted(ir.edges, key=lambda item: item.edge_id)]
        return _emit_use_case_body(labels, actors, use_cases, edges)

    ordered = _order_trace_events(ir.events)
    sorted_pids = sorted(ir.participants)
    root_callers = sorted({ev.caller_id for ev in ordered if ev.parent_event_id is None})
    actors = (
        root_callers[:2]
        if root_callers and len(root_callers) < len(sorted_pids)
        else sorted_pids[:1]
    )
    actor_set = set(actors)
    use_cases = [p for p in sorted_pids if p not in actor_set] or sorted_pids
    labels = {pid: ir.participants[pid].label for pid in sorted_pids}
    edges = sorted({(ev.caller_id, ev.callee_id) for ev in ordered if ev.caller_id != ev.callee_id})
    return _emit_use_case_body(labels, actors, use_cases, edges)


@dataclass(frozen=True, slots=True)
class _PumlTreeCtx:
    nodes: dict[str, IRNode]
    children: dict[str, list[str]]
    aliases: dict[str, str]
    mode: str


def _emit_puml_stereo_subtree(sym_id: str, ctx: _PumlTreeCtx, depth: int) -> list[str]:
    indent = "  " * depth
    node = ctx.nodes[sym_id]
    label = sanitize_plantuml_label(node.label)
    alias = ctx.aliases[sym_id]
    child_ids = ctx.children[sym_id]
    if child_ids:
        kw = "package" if ctx.mode == "package" else "node"
        lines = [f'{indent}{kw} "{label}" as {alias} {{']
        for cid in child_ids:
            lines.extend(_emit_puml_stereo_subtree(cid, ctx, depth + 1))
        lines.append(f"{indent}}}")
        return lines
    kw = "package" if ctx.mode == "package" else "artifact"
    return [f'{indent}{kw} "{label}" as {alias}']


def emit_plantuml_package_or_deployment(ir: CanonicalIR, mode: str) -> str:
    """Emit a deterministic UML 2.5 Package or Deployment Diagram in PlantUML."""
    skins = PLANTUML_ORTHO_SKINPARAMS if mode == "package" else PLANTUML_BEHAVIORAL_SKINPARAMS
    lines = ["@startuml", "!pragma layout elk", *skins]
    edge_lbl = "<<import>>" if mode == "package" else "<<deploy>>"
    edge_hex = _IMPORT_HEX if mode == "package" else _CALL_HEX
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        aliases = build_alias_map(sorted_ids, prefix="n")
        roots, children = _build_children_map(ir.nodes)
        ctx = _PumlTreeCtx(nodes=ir.nodes, children=children, aliases=aliases, mode=mode)
        for rid in roots:
            lines.extend(_emit_puml_stereo_subtree(rid, ctx, 0))
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            lines.append(
                f"{aliases[edge.source]} ..>[{edge_hex}] {aliases[edge.target]} : {edge_lbl}"
            )
        lines.append("@enduml")
        return "\n".join(lines) + "\n"

    sorted_pids = sorted(ir.participants)
    aliases = build_alias_map(sorted_pids, prefix="n")
    kw = "package" if mode == "package" else "artifact"
    for pid in sorted_pids:
        lines.append(
            f'{kw} "{sanitize_plantuml_label(ir.participants[pid].label)}" as {aliases[pid]}'
        )
    seen = sorted(
        {(ev.caller_id, ev.callee_id) for ev in ir.events.values() if ev.caller_id != ev.callee_id}
    )
    for src, dst in seen:
        lines.append(f"{aliases[src]} ..>[{edge_hex}] {aliases[dst]} : {edge_lbl}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"
