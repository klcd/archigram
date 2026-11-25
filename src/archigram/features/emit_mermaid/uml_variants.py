"""UML 2.5 Mermaid emitters (State, Activity, Communication, Use Case, Package, Deployment)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    TraceEvent,
)
from archigram.domain.style import (
    CANONICAL_EDGE_STYLES,
    DEFAULT_MERMAID_THEME,
    MermaidThemeConfig,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_CALL_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.CALLS].stroke_hex
_IMPORT_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.IMPORTS].stroke_hex
_FORK_HEX = CANONICAL_EDGE_STYLES[EdgeRelation.FORK_JOIN].stroke_hex
_MAX_EDGE_MSGS = 3
_MAX_ACTIVITY_STEPS = 18


def _clean_text(raw: str) -> str:
    """Sanitize text for Mermaid quoted labels without semicolons or raw brackets."""
    text = " ".join(raw.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    text = text.replace("#", "#35;").replace('"', "#quot;").replace(";", "#59;")
    text = text.replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return cleaned if cleaned else "unnamed"


def _clean_state_trigger(raw: str) -> str:
    """Sanitize a Mermaid `stateDiagram-v2` transition label (`s0 --> s1 : trigger`)."""
    cleaned = " ".join(raw.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    for bad in ('"', ";", ":", "{", "}", "[", "]", "#"):
        cleaned = cleaned.replace(bad, "")
    printable = "".join(ch if ch.isprintable() else " " for ch in cleaned).strip()
    return printable[:48] if printable else "step"


def _short_sig(sig: str) -> str:
    """Shorten a Python or RPC call signature for compact diagram edge labels."""
    base = sig.split("->", 1)[0].strip()
    if "(" in base:
        fn_name = base.split("(", 1)[0].strip().rsplit(".", 1)[-1]
        return f"{fn_name}()" if fn_name else base[:32]
    return base[:36] if base else "call"


def _alias_map(ids: Sequence[str], prefix: str) -> dict[str, str]:
    return {raw_id: f"{prefix}{idx}" for idx, raw_id in enumerate(sorted(set(ids)))}


def _ordered_events(trace: InteractionTraceIR) -> list[TraceEvent]:
    return sorted(trace.events.values(), key=lambda ev: (ev.start_ns, ev.end_ns, ev.event_id))


def _number_trace_events(trace: InteractionTraceIR) -> list[tuple[str, TraceEvent]]:
    """Assign hierarchical UML Communication Diagram sequence numbers (`1`, `1.1`, `2`)."""
    by_parent: dict[str | None, list[TraceEvent]] = {}
    for ev in _ordered_events(trace):
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


def emit_mermaid_state_structural(
    ir: StructuralGraphIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a Gitea-compatible Mermaid `stateDiagram-v2` from `StructuralGraphIR`."""
    sorted_ids = sorted(ir.nodes)
    aliases = _alias_map(sorted_ids, "s")
    lines = [theme.state_frontmatter(), "stateDiagram-v2", "  direction TB"]
    for sid in sorted_ids:
        label = _clean_state_trigger(ir.nodes[sid].label)
        lines.append(f'  state "{label}" as {aliases[sid]}')
    if sorted_ids:
        in_deg = dict.fromkeys(sorted_ids, 0)
        out_deg = dict.fromkeys(sorted_ids, 0)
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            in_deg[edge.target] += 1
            out_deg[edge.source] += 1
        starts = [sid for sid in sorted_ids if in_deg[sid] == 0] or [sorted_ids[0]]
        ends = [sid for sid in sorted_ids if out_deg[sid] == 0] or [sorted_ids[-1]]
        for sid in starts[:3]:
            lines.append(f"  [*] --> {aliases[sid]}")
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            trig = _clean_state_trigger(edge.relation.value)
            lines.append(f"  {aliases[edge.source]} --> {aliases[edge.target]} : {trig}")
        for sid in ends[:3]:
            lines.append(f"  {aliases[sid]} --> [*]")
    return "\n".join(lines) + "\n"


def emit_mermaid_state_trace(
    trace: InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a Gitea-compatible Mermaid `stateDiagram-v2` from `InteractionTraceIR`."""
    sorted_pids = sorted(trace.participants)
    aliases = _alias_map(sorted_pids, "s")
    lines = [theme.state_frontmatter(), "stateDiagram-v2", "  direction TB"]
    for pid in sorted_pids:
        label = _clean_state_trigger(trace.participants[pid].label)
        lines.append(f'  state "{label}" as {aliases[pid]}')
    ordered = _ordered_events(trace)
    if ordered:
        lines.append(f"  [*] --> {aliases[ordered[0].caller_id]}")
        seen_pairs: dict[tuple[str, str], str] = {}
        for ev in ordered:
            pair = (ev.caller_id, ev.callee_id)
            if pair not in seen_pairs:
                seen_pairs[pair] = _clean_state_trigger(_short_sig(ev.signature))
        for (src, dst), trig in seen_pairs.items():
            lines.append(f"  {aliases[src]} --> {aliases[dst]} : {trig}")
        lines.append(f"  {aliases[ordered[-1].callee_id]} --> [*]")
    elif sorted_pids:
        lines.append(f"  [*] --> {aliases[sorted_pids[0]]}")
        lines.append(f"  {aliases[sorted_pids[0]]} --> [*]")
    return "\n".join(lines) + "\n"


def emit_mermaid_communication_trace(
    trace: InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Communication Diagram with numbered sequence messages in Mermaid."""
    sorted_pids = sorted(trace.participants)
    aliases = _alias_map(sorted_pids, "o")
    lines = [theme.structural_frontmatter(), "flowchart LR", f"  {theme.default_classdef()}"]
    for pid in sorted_pids:
        label = _clean_text(trace.participants[pid].label)
        lines.append(f'  {aliases[pid]}[":{label}"]')
    grouped: dict[tuple[str, str], list[str]] = {}
    for seq_no, ev in _number_trace_events(trace):
        grouped.setdefault((ev.caller_id, ev.callee_id), []).append(
            f"{seq_no}: {_short_sig(ev.signature)}"
        )
    edge_count = 0
    for (src, dst), msgs in grouped.items():
        shown = msgs[:_MAX_EDGE_MSGS]
        suffix = f" (+{len(msgs) - _MAX_EDGE_MSGS})" if len(msgs) > _MAX_EDGE_MSGS else ""
        label = _clean_text(", ".join(shown) + suffix)
        lines.append(f'  {aliases[src]} -->|"{label}"| {aliases[dst]}')
        edge_count += 1
    if edge_count:
        indices = ",".join(str(i) for i in range(edge_count))
        lines.append(
            f"  linkStyle {indices} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"


def emit_mermaid_communication_structural(
    ir: StructuralGraphIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Communication Diagram with numbered links from `StructuralGraphIR`."""
    sorted_ids = sorted(ir.nodes)
    aliases = _alias_map(sorted_ids, "o")
    lines = [theme.structural_frontmatter(), "flowchart LR", f"  {theme.default_classdef()}"]
    for sid in sorted_ids:
        lines.append(f'  {aliases[sid]}[":{_clean_text(ir.nodes[sid].label)}"]')
    sorted_edges = sorted(ir.edges, key=lambda item: item.edge_id)
    for idx, edge in enumerate(sorted_edges, start=1):
        label = _clean_text(f"{idx}: {edge.relation.value}")
        lines.append(f'  {aliases[edge.source]} -->|"{label}"| {aliases[edge.target]}')
    if sorted_edges:
        indices = ",".join(str(i) for i in range(len(sorted_edges)))
        lines.append(
            f"  linkStyle {indices} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"


def _emit_mermaid_fork_region(
    trace: InteractionTraceIR,
    capped: list[TraceEvent],
    span: tuple[int, int],
    prev_id: str,
    node_lines: list[str],
    edges: list[tuple[str, str, str]],
) -> str:
    start_idx, end_idx = span
    fork_id, join_id = f"fk{start_idx}", f"jn{start_idx}"
    node_lines.append(f'  {fork_id}["=== fork ==="]')
    node_lines.append(f'  {join_id}["=== join ==="]')
    edges.append((prev_id, fork_id, _FORK_HEX))
    for b_idx in range(start_idx, end_idx):
        b_ev = capped[b_idx]
        b_node = f"a{b_idx}"
        b_lbl = _clean_text(
            f"{trace.participants[b_ev.callee_id].label}: {_short_sig(b_ev.signature)}"
        )
        node_lines.append(f'  {b_node}("{b_lbl}")')
        edges.append((fork_id, b_node, _FORK_HEX))
        edges.append((b_node, join_id, _FORK_HEX))
    return join_id


def _emit_activity_trace_nodes(
    ordered: list[TraceEvent],
    trace: InteractionTraceIR,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Build UML Activity nodes (actions + loop diamonds + fork/join bars) and transitions."""
    node_lines = ['  act_start((" "))']
    edges: list[tuple[str, str, str]] = []
    prev_id = "act_start"
    capped = ordered[:_MAX_ACTIVITY_STEPS]
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
            prev_id = _emit_mermaid_fork_region(
                trace, capped, (idx, overlap_end), prev_id, node_lines, edges
            )
            idx = overlap_end
            continue
        loop_count = ev.attributes.get("loop_count", "")
        if loop_count:
            guard_id = f"g{idx}"
            node_lines.append(f'  {guard_id}{{"loop x{_clean_text(loop_count)}"}}')
            edges.append((prev_id, guard_id, _CALL_HEX))
            prev_id = guard_id
        act_id = f"a{idx}"
        step_lbl = _clean_text(
            f"{trace.participants[ev.callee_id].label}: {_short_sig(ev.signature)}"
        )
        node_lines.append(f'  {act_id}("{step_lbl}")')
        edges.append((prev_id, act_id, _CALL_HEX))
        prev_id = act_id
        idx += 1
    if len(ordered) > _MAX_ACTIVITY_STEPS:
        rem_id = "a_more"
        node_lines.append(f'  {rem_id}("+{len(ordered) - _MAX_ACTIVITY_STEPS} compacted actions")')
        edges.append((prev_id, rem_id, _CALL_HEX))
        prev_id = rem_id
    node_lines.append('  act_stop(((" ")))')
    edges.append((prev_id, "act_stop", _CALL_HEX))
    return node_lines, edges


def emit_mermaid_activity_trace(
    trace: InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Activity Diagram (`flowchart TD`) from `InteractionTraceIR`."""
    lines = [theme.structural_frontmatter(), "flowchart TD", f"  {theme.default_classdef()}"]
    node_lines, edges = _emit_activity_trace_nodes(_ordered_events(trace), trace)
    lines.extend(node_lines)
    grouped_styles: dict[str, list[str]] = {}
    for edge_idx, (src, dst, hex_color) in enumerate(edges):
        lines.append(f"  {src} --> {dst}")
        spec = f"stroke:{hex_color},stroke-width:{theme.edge_stroke_width_px}"
        grouped_styles.setdefault(spec, []).append(str(edge_idx))
    for spec, idxs in grouped_styles.items():
        lines.append(f"  linkStyle {','.join(idxs)} {spec}")
    return "\n".join(lines) + "\n"


def emit_mermaid_activity_structural(
    ir: StructuralGraphIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Activity workflow Diagram (`flowchart TD`) from `StructuralGraphIR`."""
    sorted_ids = sorted(ir.nodes)
    aliases = _alias_map(sorted_ids, "a")
    lines = [
        theme.structural_frontmatter(),
        "flowchart TD",
        f"  {theme.default_classdef()}",
        '  act_start((" "))',
    ]
    for sid in sorted_ids:
        lines.append(f'  {aliases[sid]}("{_clean_text(ir.nodes[sid].label)}")')
    lines.append('  act_stop(((" ")))')
    edge_count = 0
    if sorted_ids:
        in_deg = dict.fromkeys(sorted_ids, 0)
        out_deg = dict.fromkeys(sorted_ids, 0)
        for edge in ir.edges:
            in_deg[edge.target] += 1
            out_deg[edge.source] += 1
        starts = [sid for sid in sorted_ids if in_deg[sid] == 0] or [sorted_ids[0]]
        ends = [sid for sid in sorted_ids if out_deg[sid] == 0] or [sorted_ids[-1]]
        for sid in starts[:3]:
            lines.append(f"  act_start --> {aliases[sid]}")
            edge_count += 1
        for edge in sorted(ir.edges, key=lambda item: item.edge_id):
            lines.append(f"  {aliases[edge.source]} --> {aliases[edge.target]}")
            edge_count += 1
        for sid in ends[:3]:
            lines.append(f"  {aliases[sid]} --> act_stop")
            edge_count += 1
    if edge_count:
        idxs = ",".join(str(i) for i in range(edge_count))
        lines.append(
            f"  linkStyle {idxs} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"


def _partition_actors_and_use_cases(ir: StructuralGraphIR) -> tuple[list[str], list[str]]:
    """Split `StructuralGraphIR` nodes into driving Actors vs inside-boundary Use Cases."""
    sorted_ids = sorted(ir.nodes)
    if len(sorted_ids) <= 1:
        return [], sorted_ids
    in_deg = dict.fromkeys(sorted_ids, 0)
    for edge in ir.edges:
        in_deg[edge.target] += 1
    actors = [
        sid
        for sid in sorted_ids
        if ir.nodes[sid].kind is NodeKind.EXTERNAL
        or "cli" in sid.lower()
        or "mcp" in sid.lower()
        or "entrypoint" in sid.lower()
    ]
    if not actors or len(actors) == len(sorted_ids):
        roots = [sid for sid in sorted_ids if in_deg[sid] == 0]
        actors = roots[:2] if roots and len(roots) < len(sorted_ids) else [sorted_ids[0]]
    actor_set = set(actors)
    use_cases = [sid for sid in sorted_ids if sid not in actor_set]
    return actors, use_cases


def emit_mermaid_use_case_structural(
    ir: StructuralGraphIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Use Case Diagram (`flowchart LR`) from `StructuralGraphIR`."""
    actors, use_cases = _partition_actors_and_use_cases(ir)
    aliases = _alias_map(sorted(ir.nodes), "u")
    lines = [theme.structural_frontmatter(), "flowchart LR", f"  {theme.default_classdef()}"]
    for aid in actors:
        lines.append(f'  {aliases[aid]}["<<actor>> {_clean_text(ir.nodes[aid].label)}"]')
    if use_cases:
        lines.append('  subgraph sys_boundary["System Subject Boundary"]')
        for ucid in use_cases:
            lines.append(f'    {aliases[ucid]}(["{_clean_text(ir.nodes[ucid].label)}"])')
        lines.append("  end")
        lines.append(f"  style sys_boundary {theme.subgraph_style_attrs()}")
    actor_set = set(actors)
    sorted_edges = sorted(ir.edges, key=lambda item: item.edge_id)
    for edge in sorted_edges:
        if edge.source in actor_set:
            lines.append(f'  {aliases[edge.source]} -->|"invokes"| {aliases[edge.target]}')
        else:
            lines.append(f'  {aliases[edge.source]} -.->|"<<include>>"| {aliases[edge.target]}')
    if sorted_edges:
        idxs = ",".join(str(i) for i in range(len(sorted_edges)))
        lines.append(
            f"  linkStyle {idxs} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"


def emit_mermaid_use_case_trace(
    trace: InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Use Case Diagram (`flowchart LR`) from `InteractionTraceIR`."""
    ordered = _ordered_events(trace)
    root_callers = sorted({ev.caller_id for ev in ordered if ev.parent_event_id is None})
    sorted_pids = sorted(trace.participants)
    actors = (
        root_callers[:2]
        if root_callers and len(root_callers) < len(sorted_pids)
        else sorted_pids[:1]
    )
    actor_set = set(actors)
    use_cases = [pid for pid in sorted_pids if pid not in actor_set] or sorted_pids
    aliases = _alias_map(sorted_pids, "u")
    lines = [theme.structural_frontmatter(), "flowchart LR", f"  {theme.default_classdef()}"]
    for aid in actors:
        lines.append(f'  {aliases[aid]}["<<actor>> {_clean_text(trace.participants[aid].label)}"]')
    lines.append('  subgraph sys_boundary["System Subject Boundary"]')
    for ucid in use_cases:
        lines.append(f'    {aliases[ucid]}(["{_clean_text(trace.participants[ucid].label)}"])')
    lines.append("  end")
    lines.append(f"  style sys_boundary {theme.subgraph_style_attrs()}")
    seen: set[tuple[str, str]] = set()
    for ev in ordered:
        pair = (ev.caller_id, ev.callee_id)
        if pair in seen or ev.caller_id == ev.callee_id:
            continue
        seen.add(pair)
        arrow = '-->|"invokes"|' if ev.caller_id in actor_set else '-.->|"<<include>>"|'
        lines.append(f"  {aliases[ev.caller_id]} {arrow} {aliases[ev.callee_id]}")
    if seen:
        idxs = ",".join(str(i) for i in range(len(seen)))
        lines.append(
            f"  linkStyle {idxs} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"


def _emit_stereotyped_tree(
    sym_id: str,
    nodes: dict[str, IRNode],
    children: dict[str, list[str]],
    aliases: dict[str, str],
    stereotypes: tuple[str, str],
    depth: int = 1,
) -> list[str]:
    indent = "  " * depth
    node = nodes[sym_id]
    container_stereo, leaf_stereo = stereotypes
    child_ids = children[sym_id]
    label = _clean_text(node.label)
    if not child_ids:
        return [f'{indent}{aliases[sym_id]}["{leaf_stereo} {label}"]']
    lines = [f'{indent}subgraph {aliases[sym_id]}["{container_stereo} {label}"]']
    for cid in child_ids:
        lines.extend(_emit_stereotyped_tree(cid, nodes, children, aliases, stereotypes, depth + 1))
    lines.append(f"{indent}end")
    return lines


def _emit_stereotyped_structural(
    ir: StructuralGraphIR,
    direction: str,
    stereotypes: tuple[str, str],
    edge_stereotype: str,
    edge_hex: str,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    sorted_ids = sorted(ir.nodes)
    aliases = _alias_map(sorted_ids, "n")
    roots: list[str] = []
    children: dict[str, list[str]] = {sid: [] for sid in sorted_ids}
    for sid in sorted_ids:
        pid = ir.nodes[sid].parent_id
        if pid is None or pid not in children:
            roots.append(sid)
        else:
            children[pid].append(sid)
    sub_style = theme.subgraph_style_attrs()
    lines = [
        theme.structural_frontmatter(),
        f"flowchart {direction}",
        f"  {theme.default_classdef()}",
    ]
    for rid in roots:
        lines.extend(_emit_stereotyped_tree(rid, ir.nodes, children, aliases, stereotypes))
    for sid in sorted_ids:
        if children[sid]:
            lines.append(f"  style {aliases[sid]} {sub_style}")
    sorted_edges: list[IREdge] = sorted(ir.edges, key=lambda item: item.edge_id)
    for edge in sorted_edges:
        lines.append(f'  {aliases[edge.source]} -.->|"{edge_stereotype}"| {aliases[edge.target]}')
    if sorted_edges:
        idxs = ",".join(str(i) for i in range(len(sorted_edges)))
        lines.append(
            f"  linkStyle {idxs} stroke:{edge_hex},"
            f"stroke-width:{theme.edge_stroke_width_px},stroke-dasharray:{theme.dashed_edge_array}"
        )
    return "\n".join(lines) + "\n"


def emit_mermaid_package(
    ir: StructuralGraphIR | InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Package Diagram (`<<package>>` + `<<import>>` dependencies)."""
    if isinstance(ir, StructuralGraphIR):
        return _emit_stereotyped_structural(
            ir, "TD", ("<<package>>", "<<package>>"), "<<import>>", _IMPORT_HEX, theme
        )
    sorted_pids = sorted(ir.participants)
    aliases = _alias_map(sorted_pids, "p")
    lines = [theme.structural_frontmatter(), "flowchart TD", f"  {theme.default_classdef()}"]
    for pid in sorted_pids:
        lines.append(f'  {aliases[pid]}["<<package>> {_clean_text(ir.participants[pid].label)}"]')
    seen = sorted(
        {(ev.caller_id, ev.callee_id) for ev in ir.events.values() if ev.caller_id != ev.callee_id}
    )
    for src, dst in seen:
        lines.append(f'  {aliases[src]} -.->|"<<import>>"| {aliases[dst]}')
    if seen:
        idxs = ",".join(str(i) for i in range(len(seen)))
        lines.append(
            f"  linkStyle {idxs} stroke:{_IMPORT_HEX},"
            f"stroke-width:{theme.edge_stroke_width_px},stroke-dasharray:{theme.dashed_edge_array}"
        )
    return "\n".join(lines) + "\n"


def emit_mermaid_deployment(
    ir: StructuralGraphIR | InteractionTraceIR,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit a UML 2.5 Deployment Diagram (`<<executionEnv>>` nodes + `<<artifact>>` targets)."""
    if isinstance(ir, StructuralGraphIR):
        return _emit_stereotyped_structural(
            ir, "LR", ("<<executionEnv>>", "<<artifact>>"), "<<deploy>>", _CALL_HEX, theme
        )
    sorted_pids = sorted(ir.participants)
    aliases = _alias_map(sorted_pids, "d")
    domains: dict[str, list[str]] = {}
    for ev in _ordered_events(ir):
        domains.setdefault(ev.clock_domain_id, [])
        if ev.callee_id not in domains[ev.clock_domain_id]:
            domains[ev.clock_domain_id].append(ev.callee_id)
    assigned = {pid for members in domains.values() for pid in members}
    for pid in sorted_pids:
        if pid not in assigned:
            domains.setdefault("default_runtime", []).append(pid)
    sub_style = theme.subgraph_style_attrs()
    lines = [theme.structural_frontmatter(), "flowchart LR", f"  {theme.default_classdef()}"]
    for d_idx, (dom_id, members) in enumerate(sorted(domains.items())):
        env_alias = f"env{d_idx}"
        lines.append(f'  subgraph {env_alias}["<<executionEnv>> {_clean_text(dom_id)}"]')
        for pid in sorted(set(members)):
            lines.append(
                f'    {aliases[pid]}["<<artifact>> {_clean_text(ir.participants[pid].label)}"]'
            )
        lines.append("  end")
        lines.append(f"  style {env_alias} {sub_style}")
    seen = sorted(
        {(ev.caller_id, ev.callee_id) for ev in ir.events.values() if ev.caller_id != ev.callee_id}
    )
    for src, dst in seen:
        lines.append(f'  {aliases[src]} -->|"<<protocol>>"| {aliases[dst]}')
    if seen:
        idxs = ",".join(str(i) for i in range(len(seen)))
        lines.append(
            f"  linkStyle {idxs} stroke:{_CALL_HEX},stroke-width:{theme.edge_stroke_width_px}"
        )
    return "\n".join(lines) + "\n"
