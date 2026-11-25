"""UML 2.5 D2 emitters (State, Activity, Communication, Use Case, Package, Deployment)."""

from __future__ import annotations

from dataclasses import dataclass

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
    D2_CANONICAL_HEADER,
    NEUTRAL_BORDER_HEX,
    TRANSPARENT_FILL,
)
from archigram.features.emit_d2.emitter import (
    build_qualified_paths as _build_qualified_paths,
)
from archigram.features.emit_d2.emitter import (
    build_safe_key_map as _build_safe_key_map,
)
from archigram.features.emit_d2.emitter import (
    format_d2_key as _format_d2_key,
)
from archigram.features.emit_d2.emitter import (
    order_d2_trace_events as _order_trace_events,
)
from archigram.features.emit_d2.emitter import (
    quote_d2_string as _quote_d2_string,
)

_CALL_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex
_IMPORT_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.IMPORTS].stroke_hex
_FORK_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.FORK_JOIN].stroke_hex
_MAX_EDGE_MSGS = 3
_MAX_ACTIVITY_STEPS = 18


def _short_sig(sig: str) -> str:
    base = sig.split("->", 1)[0].strip()
    if "(" in base:
        fn_name = base.split("(", 1)[0].strip().rsplit(".", 1)[-1]
        return f"{fn_name}()" if fn_name else base[:32]
    return base[:36] if base else "call"


def _should_animate(meta: dict[str, str], *, default: bool = True) -> bool:
    override = meta.get("animate_edges")
    if override == "true":
        return True
    if override == "false":
        return False
    return default


def _append_d2_edge(
    endpoints: tuple[str, str],
    label: str,
    stroke_hex: str,
    lines: list[str],
    *,
    dashed: bool = False,
    animated: bool = False,
) -> None:
    src, dst = endpoints
    lines.append(f"{src} -> {dst}: {_quote_d2_string(label)} {{")
    lines.append(f'  style.stroke: "{stroke_hex}"')
    if dashed:
        lines.append("  style.stroke-dash: 5")
    if animated:
        lines.append("  style.animated: true")
    lines.append("}")


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


def _append_state_terminals(lines: list[str]) -> None:
    lines.extend(
        (
            'uml_initial: "●" {',
            "  shape: circle",
            f"  style.fill: {TRANSPARENT_FILL}",
            f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
            "}",
            'uml_final: "◉" {',
            "  shape: circle",
            "  style.double-border: true",
            f"  style.fill: {TRANSPARENT_FILL}",
            f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
            "}",
        )
    )


def _append_state_box(key: str, label: str, lines: list[str]) -> None:
    lines.extend(
        (
            f"{key}: {_quote_d2_string(label)} {{",
            "  style.border-radius: 14",
            f"  style.fill: {TRANSPARENT_FILL}",
            f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
            "}",
        )
    )


def _emit_d2_state_structural(ir: StructuralGraphIR, anim: bool, lines: list[str]) -> str:
    sorted_ids = sorted(ir.nodes)
    key_map, _ = _build_safe_key_map([*sorted_ids, "uml_initial", "uml_final"])
    for sid in sorted_ids:
        _append_state_box(_format_d2_key(key_map[sid]), ir.nodes[sid].label, lines)
    if sorted_ids:
        in_deg = dict.fromkeys(sorted_ids, 0)
        out_deg = dict.fromkeys(sorted_ids, 0)
        for edge in ir.edges:
            in_deg[edge.target] += 1
            out_deg[edge.source] += 1
        starts = [s for s in sorted_ids if in_deg[s] == 0] or [sorted_ids[0]]
        ends = [s for s in sorted_ids if out_deg[s] == 0] or [sorted_ids[-1]]
        for sid in starts[:3]:
            ep = ("uml_initial", _format_d2_key(key_map[sid]))
            _append_d2_edge(ep, "init", _CALL_HEX, lines, animated=anim)
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            ep = (_format_d2_key(key_map[edge.source]), _format_d2_key(key_map[edge.target]))
            _append_d2_edge(ep, edge.relation.value, _CALL_HEX, lines, animated=anim)
        for sid in ends[:3]:
            ep = (_format_d2_key(key_map[sid]), "uml_final")
            _append_d2_edge(ep, "done", _CALL_HEX, lines, animated=anim)
    return "\n".join(lines) + "\n"


def emit_d2_state(ir: CanonicalIR) -> str:
    """Emit a dual-mode UML 2.5 State Machine Diagram in D2."""
    lines = [D2_CANONICAL_HEADER, "", "direction: down"]
    anim = _should_animate(ir.metadata, default=True)
    _append_state_terminals(lines)
    if isinstance(ir, StructuralGraphIR):
        return _emit_d2_state_structural(ir, anim, lines)
    sorted_pids = sorted(ir.participants)
    key_map, _ = _build_safe_key_map([*sorted_pids, "uml_initial", "uml_final"])
    for pid in sorted_pids:
        _append_state_box(_format_d2_key(key_map[pid]), ir.participants[pid].label, lines)
    ordered = _order_trace_events(ir.events)
    if ordered:
        start_ep = ("uml_initial", _format_d2_key(key_map[ordered[0].caller_id]))
        _append_d2_edge(start_ep, "start", _CALL_HEX, lines, animated=anim)
        seen: dict[tuple[str, str], str] = {}
        for ev in ordered:
            seen.setdefault((ev.caller_id, ev.callee_id), _short_sig(ev.signature))
        for (src, dst), trig in seen.items():
            ep = (_format_d2_key(key_map[src]), _format_d2_key(key_map[dst]))
            _append_d2_edge(ep, trig, _CALL_HEX, lines, animated=anim)
        end_ep = (_format_d2_key(key_map[ordered[-1].callee_id]), "uml_final")
        _append_d2_edge(end_ep, "end", _CALL_HEX, lines, animated=anim)
    return "\n".join(lines) + "\n"


def emit_d2_communication(ir: CanonicalIR) -> str:
    """Emit a dual-mode UML 2.5 Communication Diagram with numbered sequence messages in D2."""
    lines = [D2_CANONICAL_HEADER, "", "direction: right"]
    anim = _should_animate(ir.metadata, default=True)
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        key_map, _ = _build_safe_key_map(sorted_ids)
        for sid in sorted_ids:
            k = _format_d2_key(key_map[sid])
            lines.extend(
                (
                    f"{k}: {_quote_d2_string(f':{ir.nodes[sid].label}')} {{",
                    f"  style.fill: {TRANSPARENT_FILL}",
                    f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
                    "}",
                )
            )
        for idx, edge in enumerate(sorted(ir.edges, key=lambda item: item.edge_id), start=1):
            ep = (_format_d2_key(key_map[edge.source]), _format_d2_key(key_map[edge.target]))
            _append_d2_edge(ep, f"{idx}: {edge.relation.value}", _CALL_HEX, lines, animated=anim)
        return "\n".join(lines) + "\n"

    sorted_pids = sorted(ir.participants)
    key_map, _ = _build_safe_key_map(sorted_pids)
    for pid in sorted_pids:
        k = _format_d2_key(key_map[pid])
        lines.extend(
            (
                f"{k}: {_quote_d2_string(f':{ir.participants[pid].label}')} {{",
                f"  style.fill: {TRANSPARENT_FILL}",
                f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
                "}",
            )
        )
    grouped: dict[tuple[str, str], list[str]] = {}
    for seq_no, ev in _number_trace_events(ir):
        msg = f"{seq_no}: {_short_sig(ev.signature)}"
        grouped.setdefault((ev.caller_id, ev.callee_id), []).append(msg)
    for (src, dst), msgs in grouped.items():
        shown = msgs[:_MAX_EDGE_MSGS]
        suffix = f" (+{len(msgs) - _MAX_EDGE_MSGS})" if len(msgs) > _MAX_EDGE_MSGS else ""
        ep = (_format_d2_key(key_map[src]), _format_d2_key(key_map[dst]))
        _append_d2_edge(ep, ", ".join(shown) + suffix, _CALL_HEX, lines, animated=anim)
    return "\n".join(lines) + "\n"


def _append_d2_pill_node(
    node_id: str,
    label: str,
    lines: list[str],
    shape: str | None = None,
) -> None:
    lines.append(f"{node_id}: {_quote_d2_string(label)} {{")
    if shape is not None:
        lines.append(f"  shape: {shape}")
    else:
        lines.append("  style.border-radius: 999")
    lines.append(f"  style.fill: {TRANSPARENT_FILL}")
    lines.append(f'  style.stroke: "{NEUTRAL_BORDER_HEX}"')
    lines.append("}")


def _emit_d2_activity_structural(ir: StructuralGraphIR, anim: bool, lines: list[str]) -> str:
    sorted_ids = sorted(ir.nodes)
    key_map, _ = _build_safe_key_map([*sorted_ids, "act_start", "act_stop"])
    for sid in sorted_ids:
        _append_d2_pill_node(_format_d2_key(key_map[sid]), ir.nodes[sid].label, lines)
    _append_d2_pill_node("act_stop", "◉", lines, shape="circle")
    if sorted_ids:
        in_deg = dict.fromkeys(sorted_ids, 0)
        out_deg = dict.fromkeys(sorted_ids, 0)
        for edge in ir.edges:
            in_deg[edge.target] += 1
            out_deg[edge.source] += 1
        starts = [s for s in sorted_ids if in_deg[s] == 0] or [sorted_ids[0]]
        ends = [s for s in sorted_ids if out_deg[s] == 0] or [sorted_ids[-1]]
        for sid in starts[:3]:
            ep = ("act_start", _format_d2_key(key_map[sid]))
            _append_d2_edge(ep, "start", _CALL_HEX, lines, animated=anim)
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            ep = (_format_d2_key(key_map[edge.source]), _format_d2_key(key_map[edge.target]))
            _append_d2_edge(ep, edge.relation.value, _CALL_HEX, lines, animated=anim)
        for sid in ends[:3]:
            ep = (_format_d2_key(key_map[sid]), "act_stop")
            _append_d2_edge(ep, "done", _CALL_HEX, lines, animated=anim)
    return "\n".join(lines) + "\n"


def _emit_d2_fork_region(
    trace: InteractionTraceIR,
    capped: list[TraceEvent],
    span: tuple[int, int],
    prev_id: str,
    anim: bool,
    lines: list[str],
) -> str:
    start_idx, end_idx = span
    fk, jn = f"fork_{start_idx}", f"join_{start_idx}"
    _append_d2_pill_node(fk, "=== fork ===", lines)
    _append_d2_pill_node(jn, "=== join ===", lines)
    _append_d2_edge((prev_id, fk), "fork", _FORK_HEX, lines, animated=anim)
    for b_idx in range(start_idx, end_idx):
        b_ev = capped[b_idx]
        b_node = f"act_{b_idx}"
        lbl = f"{trace.participants[b_ev.callee_id].label}: {_short_sig(b_ev.signature)}"
        _append_d2_pill_node(b_node, lbl, lines)
        _append_d2_edge((fk, b_node), "branch", _FORK_HEX, lines, animated=anim)
        _append_d2_edge((b_node, jn), "join", _FORK_HEX, lines, animated=anim)
    return jn


def emit_d2_activity(ir: CanonicalIR) -> str:
    """Emit a dual-mode UML 2.5 Activity Diagram in D2."""
    lines = [D2_CANONICAL_HEADER, "", "direction: down"]
    anim = _should_animate(ir.metadata, default=True)
    _append_d2_pill_node("act_start", "●", lines, shape="circle")
    if isinstance(ir, StructuralGraphIR):
        return _emit_d2_activity_structural(ir, anim, lines)

    ordered = _order_trace_events(ir.events)
    capped = ordered[:_MAX_ACTIVITY_STEPS]
    prev_id = "act_start"
    idx = 0
    while idx < len(capped):
        ev = capped[idx]
        overlap_end = idx + 1
        while (
            overlap_end < len(capped)
            and capped[overlap_end].parent_event_id == ev.parent_event_id
            and capped[overlap_end].clock_domain_id == ev.clock_domain_id
            and capped[overlap_end].start_ns < ev.end_ns
        ):
            overlap_end += 1
        if overlap_end - idx > 1:
            prev_id = _emit_d2_fork_region(ir, capped, (idx, overlap_end), prev_id, anim, lines)
            idx = overlap_end
            continue
        loop_cnt = ev.attributes.get("loop_count", "")
        if loop_cnt:
            gid = f"guard_{idx}"
            _append_d2_pill_node(gid, f"loop x{loop_cnt}", lines, shape="diamond")
            _append_d2_edge((prev_id, gid), "guard", _CALL_HEX, lines, animated=anim)
            prev_id = gid
        aid = f"act_{idx}"
        lbl = f"{ir.participants[ev.callee_id].label}: {_short_sig(ev.signature)}"
        _append_d2_pill_node(aid, lbl, lines)
        _append_d2_edge((prev_id, aid), "next", _CALL_HEX, lines, animated=anim)
        prev_id = aid
        idx += 1
    if len(ordered) > _MAX_ACTIVITY_STEPS:
        rem = f"+{len(ordered) - _MAX_ACTIVITY_STEPS} compacted actions"
        _append_d2_pill_node("act_more", rem, lines)
        _append_d2_edge((prev_id, "act_more"), "next", _CALL_HEX, lines, animated=anim)
        prev_id = "act_more"
    _append_d2_pill_node("act_stop", "◉", lines, shape="circle")
    _append_d2_edge((prev_id, "act_stop"), "done", _CALL_HEX, lines, animated=anim)
    return "\n".join(lines) + "\n"


def _render_d2_use_case_body(
    labels: dict[str, str],
    actors: list[str],
    use_cases: list[str],
    edges: list[tuple[str, str]],
    anim: bool,
) -> str:
    lines = [D2_CANONICAL_HEADER, "", "direction: right"]
    all_ids = sorted(labels)
    key_map, _ = _build_safe_key_map([*all_ids, "system_boundary"])
    for aid in actors:
        _append_d2_pill_node(_format_d2_key(key_map[aid]), labels[aid], lines, shape="person")
    if use_cases:
        lines.extend(
            (
                'system_boundary: "System Subject Boundary" {',
                f"  style.fill: {TRANSPARENT_FILL}",
                f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
                "  style.stroke-dash: 4",
            )
        )
        for ucid in use_cases:
            uk = _format_d2_key(key_map[ucid])
            lines.extend(
                (
                    f"  {uk}: {_quote_d2_string(labels[ucid])} {{",
                    "    shape: oval",
                    f"    style.fill: {TRANSPARENT_FILL}",
                    f'    style.stroke: "{NEUTRAL_BORDER_HEX}"',
                    "  }",
                )
            )
        lines.append("}")
    actor_set = set(actors)
    path_of = {
        sid: (
            _format_d2_key(key_map[sid])
            if sid in actor_set
            else f"system_boundary.{_format_d2_key(key_map[sid])}"
        )
        for sid in all_ids
    }
    for src, dst in edges:
        is_actor = src in actor_set
        _append_d2_edge(
            (path_of[src], path_of[dst]),
            "invokes" if is_actor else "<<include>>",
            _CALL_HEX,
            lines,
            dashed=not is_actor,
            animated=anim,
        )
    return "\n".join(lines) + "\n"


def emit_d2_use_case(ir: CanonicalIR) -> str:
    """Emit a dual-mode UML 2.5 Use Case Diagram (`shape: person` -> `shape: oval`) in D2."""
    anim = _should_animate(ir.metadata, default=False)
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
        return _render_d2_use_case_body(labels, actors, use_cases, edges, anim)

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
    return _render_d2_use_case_body(labels, actors, use_cases, edges, anim)


@dataclass(frozen=True, slots=True)
class _D2TreeCtx:
    nodes: dict[str, IRNode]
    children: dict[str, list[str]]
    key_map: dict[str, str]
    mode: str


def _emit_d2_stereo_subtree(
    node: IRNode,
    ctx: _D2TreeCtx,
    depth: int,
    lines: list[str],
) -> None:
    indent = "  " * depth
    inner = "  " * (depth + 1)
    child_ids = ctx.children.get(node.symbol_id, [])
    k = _format_d2_key(ctx.key_map[node.symbol_id])
    stereo = (
        "<<package>>"
        if ctx.mode == "package"
        else ("<<executionEnv>>" if child_ids else "<<artifact>>")
    )
    lines.append(f"{indent}{k}: {_quote_d2_string(f'{stereo} {node.label}')} {{")
    if ctx.mode == "package" and not child_ids:
        lines.append(f"{inner}shape: package")
    elif ctx.mode == "deployment" and not child_ids:
        lines.append(f"{inner}style.double-border: true")
    lines.append(f"{inner}style.fill: {TRANSPARENT_FILL}")
    lines.append(f'{inner}style.stroke: "{NEUTRAL_BORDER_HEX}"')
    for cid in child_ids:
        _emit_d2_stereo_subtree(ctx.nodes[cid], ctx, depth + 1, lines)
    lines.append(f"{indent}}}")


def emit_d2_package_or_deployment(ir: CanonicalIR, mode: str) -> str:
    """Emit a dual-mode UML 2.5 Package or Deployment Diagram in D2."""
    direction = "direction: down" if mode == "package" else "direction: right"
    lines = [D2_CANONICAL_HEADER, "", direction]
    anim = _should_animate(ir.metadata, default=mode == "deployment")
    if isinstance(ir, StructuralGraphIR):
        sorted_ids = sorted(ir.nodes)
        key_map, _ = _build_safe_key_map(sorted_ids)
        children: dict[str, list[str]] = {sid: [] for sid in sorted_ids}
        roots: list[str] = []
        for sid in sorted_ids:
            pid = ir.nodes[sid].parent_id
            if pid is None or pid not in children:
                roots.append(sid)
            else:
                children[pid].append(sid)
        ctx = _D2TreeCtx(nodes=ir.nodes, children=children, key_map=key_map, mode=mode)
        for rid in roots:
            _emit_d2_stereo_subtree(ir.nodes[rid], ctx, 0, lines)
        paths = _build_qualified_paths(ir.nodes, key_map)
        edge_lbl = "<<import>>" if mode == "package" else "<<deploy>>"
        edge_hex = _IMPORT_HEX if mode == "package" else _CALL_HEX
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            _append_d2_edge(
                (paths[edge.source], paths[edge.target]),
                edge_lbl,
                edge_hex,
                lines,
                dashed=True,
                animated=anim,
            )
        return "\n".join(lines) + "\n"

    sorted_pids = sorted(ir.participants)
    key_map, _ = _build_safe_key_map(sorted_pids)
    for pid in sorted_pids:
        k = _format_d2_key(key_map[pid])
        stereo = "<<package>>" if mode == "package" else "<<artifact>>"
        lines.append(f"{k}: {_quote_d2_string(f'{stereo} {ir.participants[pid].label}')} {{")
        lines.append("  shape: package" if mode == "package" else "  style.double-border: true")
        lines.append(f"  style.fill: {TRANSPARENT_FILL}")
        lines.append(f'  style.stroke: "{NEUTRAL_BORDER_HEX}"')
        lines.append("}")
    seen = sorted(
        {(ev.caller_id, ev.callee_id) for ev in ir.events.values() if ev.caller_id != ev.callee_id}
    )
    for src, dst in seen:
        _append_d2_edge(
            (_format_d2_key(key_map[src]), _format_d2_key(key_map[dst])),
            "<<import>>" if mode == "package" else "<<protocol>>",
            _IMPORT_HEX if mode == "package" else _CALL_HEX,
            lines,
            dashed=mode == "package",
            animated=anim,
        )
    return "\n".join(lines) + "\n"
