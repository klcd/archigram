"""Hypothesis property-based fuzz suite for Deterministic Mermaid v12 Emitter."""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    ClassMember,
    EdgeRelation,
    EventStatus,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
    Visibility,
)
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.ports.contracts import DiagramEmitterPort

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_emit_mermaid",
    input_sha256="a" * 64,
)

_ADVERSARIAL_TOKENS: tuple[str, ...] = (
    "End",
    "end",
    "END",
    "subgraph",
    "class",
    "par",
    "loop",
    "and",
    "participant",
    'svc["inject"]; end; subgraph hack',
    "foo(); bar();",
    "path/to/module:fn(x, y)",
    'quote"semi;bracket[x]{y}',
    "line1\nline2\r\nline3\ttab",
    "Generic<T, K>",
)

_ADVERSARIAL_TEXT = st.one_of(
    st.sampled_from(_ADVERSARIAL_TOKENS),
    st.text(
        alphabet=(
            'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_;:"()/[]{}<>-\n\t '
        ),
        min_size=1,
        max_size=28,
    ).filter(lambda s: bool(s.strip())),
)

_FLOWCHART_RELATIONS: tuple[EdgeRelation, ...] = (
    EdgeRelation.IMPORTS,
    EdgeRelation.CALLS,
    EdgeRelation.WRITES_DATA,
    EdgeRelation.READS_DATA,
    EdgeRelation.ASSOCIATES,
    EdgeRelation.ASYNC_SPAWN,
    EdgeRelation.FORK_JOIN,
)

_OOP_RELATIONS: tuple[EdgeRelation, ...] = (
    EdgeRelation.INHERITS,
    EdgeRelation.IMPLEMENTS,
    EdgeRelation.COMPOSES,
    EdgeRelation.AGGREGATES,
    EdgeRelation.ASSOCIATES,
    EdgeRelation.IMPORTS,
)


@st.composite
def adversarial_class_member_strategy(draw: st.DrawFn) -> ClassMember:
    """Generate arbitrary ClassMember instances with adversarial names, signatures, and generics."""
    return ClassMember(
        name=draw(_ADVERSARIAL_TEXT),
        visibility=draw(st.sampled_from(list(Visibility))),
        signature=draw(
            st.sampled_from(
                [
                    "",
                    "(x: int, y: str) -> bool",
                    "dict[str, list[int]]",
                    'bad";inject() -> Result[T]',
                ]
            )
        ),
        is_method=draw(st.booleans()),
        modifiers=tuple(draw(st.lists(_ADVERSARIAL_TEXT, max_size=2))),
        generic_params=tuple(draw(st.lists(_ADVERSARIAL_TEXT, max_size=2))),
    )


@st.composite
def flowchart_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate hierarchical StructuralGraphIR guaranteed to trigger Mode B (flowchart TD)."""
    count = draw(st.integers(min_value=1, max_value=14))
    raw_ids = draw(
        st.lists(
            _ADVERSARIAL_TEXT,
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    nodes: dict[str, IRNode] = {}
    for idx, sym_id in enumerate(raw_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = raw_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=draw(_ADVERSARIAL_TEXT),
            kind=draw(
                st.sampled_from(
                    [
                        NodeKind.SERVICE,
                        NodeKind.MODULE,
                        NodeKind.PACKAGE,
                        NodeKind.CONTAINER,
                        NodeKind.DATABASE,
                    ]
                )
            ),
            parent_id=parent_id,
            members=(),
            provenance=_STAMP,
        )

    edge_count = draw(st.integers(min_value=0, max_value=18))
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        edges.append(
            IREdge(
                edge_id=f"e_{e_idx}_{draw(_ADVERSARIAL_TEXT)}",
                source=draw(st.sampled_from(raw_ids)),
                target=draw(st.sampled_from(raw_ids)),
                relation=draw(st.sampled_from(_FLOWCHART_RELATIONS)),
                sync_kind=draw(st.sampled_from(list(SyncKind))),
                provenance=_STAMP,
            )
        )
    return StructuralGraphIR(
        graph_id=draw(_ADVERSARIAL_TEXT),
        nodes=nodes,
        edges=tuple(edges),
        metadata={"diagram_variant": "flowchart"},
    )


@st.composite
def class_diagram_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate OOP StructuralGraphIR guaranteed to trigger Mode A (classDiagram)."""
    count = draw(st.integers(min_value=1, max_value=10))
    raw_ids = draw(
        st.lists(
            _ADVERSARIAL_TEXT,
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    nodes: dict[str, IRNode] = {}
    for sym_id in raw_ids:
        has_generics = draw(st.booleans())
        attrs = {"generic_params": "T, K"} if has_generics else {}
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=draw(_ADVERSARIAL_TEXT),
            kind=draw(st.sampled_from([NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM])),
            members=tuple(
                draw(st.lists(adversarial_class_member_strategy(), min_size=1, max_size=3))
            ),
            attributes=attrs,
            provenance=_STAMP,
        )

    edge_count = draw(st.integers(min_value=0, max_value=14))
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        edges.append(
            IREdge(
                edge_id=f"ce_{e_idx}",
                source=draw(st.sampled_from(raw_ids)),
                target=draw(st.sampled_from(raw_ids)),
                relation=draw(st.sampled_from(_OOP_RELATIONS)),
                sync_kind=draw(st.sampled_from(list(SyncKind))),
                provenance=_STAMP,
            )
        )
    use_meta_flag = draw(st.booleans())
    meta = {"diagram_variant": "class"} if use_meta_flag else {}
    return StructuralGraphIR(
        graph_id=draw(_ADVERSARIAL_TEXT),
        nodes=nodes,
        edges=tuple(edges),
        metadata=meta,
    )


@st.composite
def interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate InteractionTraceIR with concurrent overlapping spans, repeated loops, and async."""
    p_count = draw(st.integers(min_value=2, max_value=6))
    raw_pids = draw(
        st.lists(
            _ADVERSARIAL_TEXT,
            min_size=p_count,
            max_size=p_count,
            unique=True,
        )
    )
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{idx}",
            label=draw(_ADVERSARIAL_TEXT),
            kind=NodeKind.SERVICE,
            provenance=_STAMP,
        )
        for idx, pid in enumerate(raw_pids)
    }

    events: dict[str, TraceEvent] = {}
    par_branches = draw(st.integers(min_value=2, max_value=4))
    for b_idx in range(par_branches):
        eid = f"par_evt_{b_idx}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=raw_pids[0],
            callee_id=raw_pids[b_idx % len(raw_pids)],
            signature=draw(_ADVERSARIAL_TEXT),
            parent_event_id=None,
            clock_domain_id="clock_0",
            start_ns=100 + b_idx * 10,
            end_ns=300 + b_idx * 10,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=EventStatus.OK,
            provenance=_STAMP,
        )

    loop_repeats = draw(st.integers(min_value=2, max_value=5))
    loop_sig = draw(_ADVERSARIAL_TEXT)
    loop_sync = draw(st.sampled_from(list(SyncKind)))
    for l_idx in range(loop_repeats):
        eid = f"loop_evt_{l_idx}"
        start = 1000 + l_idx * 100
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=raw_pids[0],
            callee_id=raw_pids[1],
            signature=loop_sig,
            parent_event_id=None,
            clock_domain_id="clock_0",
            start_ns=start,
            end_ns=start + 40,
            sync_kind=loop_sync,
            status=EventStatus.OK,
            provenance=_STAMP,
        )

    events["async_child"] = TraceEvent(
        event_id="async_child",
        caller_id=raw_pids[1],
        callee_id=raw_pids[0],
        signature=draw(_ADVERSARIAL_TEXT),
        parent_event_id="par_evt_0",
        clock_domain_id="clock_0",
        start_ns=120,
        end_ns=150,
        sync_kind=SyncKind.ASYNC_FIRE_FORGET,
        status=EventStatus.OK,
        provenance=_STAMP,
    )

    return InteractionTraceIR(
        trace_id=draw(_ADVERSARIAL_TEXT),
        participants=participants,
        events=events,
    )


def _assert_no_unescaped_semicolons_in_quoted_labels(mermaid_src: str) -> None:
    """Verify every quoted label contains zero raw semicolons after entity removal."""
    for quoted in re.findall(r'"([^"]*)"', mermaid_src):
        stripped = quoted.replace("#59;", "").replace("#quot;", "").replace("#35;", "")
        stripped = (
            stripped.replace("#91;", "")
            .replace("#93;", "")
            .replace("#123;", "")
            .replace("#125;", "")
            .replace("#58;", "")
        )
        assert ";" not in stripped


def _assert_balanced_mermaid_blocks(mermaid_src: str) -> None:
    """Verify every subgraph/par/loop block opener is closed by a matching standalone 'end' line."""
    open_blocks = 0
    end_lines = 0
    for raw_line in mermaid_src.splitlines():
        line = raw_line.strip()
        if line.startswith(("subgraph ", "par ", "loop ")):
            open_blocks += 1
        elif line == "end":
            end_lines += 1
    assert open_blocks == end_lines


@settings(max_examples=50)
@given(graph=flowchart_graph_strategy())
def test_flowchart_emission_determinism_and_syntax_invariants(
    graph: StructuralGraphIR,
) -> None:
    from archigram.domain.style import (
        MERMAID_DEFAULT_CLASSDEF,
        MERMAID_STRUCTURAL_FRONTMATTER,
    )
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    emitter = MermaidEmitter()
    assert isinstance(emitter, DiagramEmitterPort)
    assert emitter.target_format == "mermaid"

    first = emitter.emit(graph)
    permuted_nodes = dict(reversed(list(graph.nodes.items())))
    permuted_edges = tuple(reversed(graph.edges))
    permuted_graph = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=permuted_nodes,
        edges=permuted_edges,
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted_graph)

    assert first == second
    assert first.startswith(f"{MERMAID_STRUCTURAL_FRONTMATTER}\nflowchart TD\n")
    assert MERMAID_DEFAULT_CLASSDEF in first
    assert "curve: linear" in first
    assert "defaultRenderer: elk" in first
    if graph.edges:
        assert re.search(r"\n  linkStyle 0(?:,\d+)* stroke:#", first) is not None
    assert MermaidSyntaxValidator().validate(first).valid
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    _assert_balanced_mermaid_blocks(first)
    assert re.search(r"\bsubgraph[ \t]+end\b", first, re.IGNORECASE) is None


@settings(max_examples=50)
@given(graph=class_diagram_graph_strategy())
def test_class_diagram_emission_determinism_and_syntax_invariants(
    graph: StructuralGraphIR,
) -> None:
    from archigram.domain.style import MERMAID_STRUCTURAL_FRONTMATTER
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    emitter = MermaidEmitter()
    first = emitter.emit(graph)
    permuted_graph = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(reversed(list(graph.nodes.items()))),
        edges=tuple(reversed(graph.edges)),
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted_graph)

    assert first == second
    assert first.startswith(f"{MERMAID_STRUCTURAL_FRONTMATTER}\nclassDiagram\n")
    assert "classDef " not in first
    assert MermaidSyntaxValidator().validate(first).valid
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    assert re.search(r"\bclass[ \t]+end\b", first, re.IGNORECASE) is None


@settings(max_examples=50)
@given(trace=interaction_trace_strategy())
def test_sequence_diagram_par_loop_and_end_aliasing_invariants(
    trace: InteractionTraceIR,
) -> None:
    from archigram.domain.style import MERMAID_SEQUENCE_FRONTMATTER
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    emitter = MermaidEmitter()
    first = emitter.emit(trace)
    permuted_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    second = emitter.emit(permuted_trace)

    assert first == second
    assert first.startswith(f"{MERMAID_SEQUENCE_FRONTMATTER}\nsequenceDiagram\n")
    assert "mirrorActors: false" in first
    assert "actorBkg: transparent" not in first
    assert MermaidSyntaxValidator().validate(first).valid
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    _assert_balanced_mermaid_blocks(first)
    assert re.search(r"\bparticipant[ \t]+end\b", first, re.IGNORECASE) is None
    assert "\n  par " in first
    assert "\n  and " in first
    assert "\n  loop [" in first
    assert "-)" in first


def _build_demo_participants(count: int) -> tuple[list[str], dict[str, TraceParticipant]]:
    pids = [f"p_{idx}" for idx in range(count)]
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{idx}",
            label=f"Svc{idx}",
            kind=NodeKind.SERVICE,
            provenance=_STAMP,
        )
        for idx, pid in enumerate(pids)
    }
    return pids, participants


@st.composite
def skewed_causal_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate traces with skewed/inverted timestamps across causal_links and parent_event_id."""
    pids, participants = _build_demo_participants(draw(st.integers(min_value=2, max_value=4)))
    count = draw(st.integers(min_value=2, max_value=7))
    events: dict[str, TraceEvent] = {}
    by_parent: dict[str | None, list[str]] = {None: []}
    for idx in range(count):
        eid = f"ev_{idx}"
        # Deliberately invert timestamps so predecessors have later timestamps than dependents.
        base_start = (count - idx) * 1000 + draw(st.integers(min_value=0, max_value=200))
        duration = draw(st.integers(min_value=10, max_value=300))
        parent_id: str | None = None
        causal_links: tuple[str, ...] = ()
        if idx > 0:
            if draw(st.booleans()):
                parent_id = draw(st.sampled_from([f"ev_{j}" for j in range(idx)]))
                siblings = by_parent.get(parent_id, [])
                if siblings and draw(st.booleans()):
                    causal_links = tuple(
                        draw(
                            st.lists(
                                st.sampled_from(siblings),
                                min_size=1,
                                max_size=min(2, len(siblings)),
                                unique=True,
                            )
                        )
                    )
            else:
                causal_links = tuple(
                    draw(
                        st.lists(
                            st.sampled_from([f"ev_{j}" for j in range(idx)]),
                            min_size=1,
                            max_size=min(2, idx),
                            unique=True,
                        )
                    )
                )
        by_parent.setdefault(parent_id, []).append(eid)
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=pids[idx % len(pids)],
            callee_id=pids[(idx + 1) % len(pids)],
            signature=f"step_{idx:02d}()",
            parent_event_id=parent_id,
            causal_links=causal_links,
            clock_domain_id=f"clock_{draw(st.integers(min_value=0, max_value=2))}",
            start_ns=base_start,
            end_ns=base_start + duration,
            sync_kind=draw(st.sampled_from([SyncKind.SYNC, SyncKind.ASYNC_FIRE_FORGET])),
            status=EventStatus.OK,
            provenance=_STAMP,
        )
    return InteractionTraceIR(trace_id="skewed_causal", participants=participants, events=events)


@st.composite
def causally_linked_overlap_strategy(draw: st.DrawFn) -> tuple[InteractionTraceIR, bool]:
    """Generate overlapping same-clock siblings with causal_links (full chain or partial DAG)."""
    pids, participants = _build_demo_participants(draw(st.integers(min_value=2, max_value=4)))
    count = draw(st.integers(min_value=2, max_value=5))
    full_chain = draw(st.booleans())
    events: dict[str, TraceEvent] = {}
    for idx in range(count):
        eid = f"ov_{idx}"
        start = 100 + draw(st.integers(min_value=0, max_value=40))
        end = 500 + draw(st.integers(min_value=0, max_value=80))
        if idx == 0:
            links: tuple[str, ...] = ()
        elif full_chain:
            links = (f"ov_{idx - 1}",)
        else:
            candidates = [f"ov_{j}" for j in range(idx)]
            min_links = 1 if idx == count - 1 else 0
            links = tuple(
                draw(
                    st.lists(
                        st.sampled_from(candidates), min_size=min_links, max_size=idx, unique=True
                    )
                )
            )
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=pids[0],
            callee_id=pids[(idx % (len(pids) - 1)) + 1],
            signature=f"ovstep_{idx:02d}()",
            parent_event_id=None,
            causal_links=links,
            clock_domain_id="clock_0",
            start_ns=start,
            end_ns=end,
            sync_kind=SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=_STAMP,
        )
    return InteractionTraceIR(
        trace_id="overlap_causal", participants=participants, events=events
    ), full_chain


@st.composite
def loop_guard_trace_strategy(draw: st.DrawFn) -> tuple[InteractionTraceIR, str]:
    """Generate identical consecutive calls blocked from loop folding by clock or causal_links."""
    pids, participants = _build_demo_participants(2)
    count = draw(st.integers(min_value=2, max_value=5))
    guard_mode = draw(st.sampled_from(["diff_clock", "adjacent_causal", "back_link_causal"]))
    if guard_mode == "back_link_causal" and count < 3:
        count = 3
    events: dict[str, TraceEvent] = {}
    for idx in range(count):
        eid = f"lp_{idx}"
        start = 100 + idx * 100
        clock = f"clock_{idx % 2}" if guard_mode == "diff_clock" else "clock_0"
        links: tuple[str, ...] = ()
        if guard_mode == "adjacent_causal" and idx > 0:
            links = (f"lp_{idx - 1}",)
        elif guard_mode == "back_link_causal" and idx == count - 1:
            links = ("lp_0",)
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=pids[0],
            callee_id=pids[1],
            signature="repeat_op()",
            parent_event_id=None,
            causal_links=links,
            clock_domain_id=clock,
            start_ns=start,
            end_ns=start + 40,
            sync_kind=SyncKind.SYNC,
            status=EventStatus.OK,
            provenance=_STAMP,
        )
    return InteractionTraceIR(
        trace_id="loop_guard", participants=participants, events=events
    ), guard_mode


def _find_line_index(lines: list[str], needle: str) -> int:
    for idx, line in enumerate(lines):
        if needle in line:
            return idx
    msg = f"Missing expected marker {needle!r}"
    raise AssertionError(msg)


def _extract_par_groups(seq_src: str) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] | None = None
    for raw in seq_src.splitlines():
        line = raw.strip()
        if line.startswith("par "):
            current = []
        elif line == "end" and current is not None:
            groups.append(current)
            current = None
        elif current is not None and (match := re.search(r":\s*(ovstep_\d+\(\))", line)):
            current.append(match.group(1))
    return groups


def _extract_fork_groups(act_src: str) -> list[list[str]]:
    sig_by_node: dict[str, str] = {}
    fork_edges: dict[str, list[str]] = {}
    for raw in act_src.splitlines():
        line = raw.strip()
        if node_m := re.match(r'(a\d+)\(".*?: (ovstep_\d+\(\))"\)', line):
            sig_by_node[node_m.group(1)] = node_m.group(2)
        elif edge_m := re.match(r"(fk\d+)\s*-->\s*(a\d+)", line):
            fork_edges.setdefault(edge_m.group(1), []).append(edge_m.group(2))
    return [[sig_by_node[n] for n in nodes if n in sig_by_node] for nodes in fork_edges.values()]


@settings(max_examples=50)
@given(trace=skewed_causal_trace_strategy())
def test_causal_ordering_overrides_skewed_timestamps(trace: InteractionTraceIR) -> None:
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    emitter = MermaidEmitter()
    seq_src = emitter.emit(trace)
    act_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=trace.participants,
        events=trace.events,
        metadata={"diagram_variant": "activity"},
    )
    act_src = emitter.emit(act_trace)

    assert MermaidSyntaxValidator().validate(seq_src).valid
    assert MermaidSyntaxValidator().validate(act_src).valid

    seq_lines = seq_src.splitlines()
    act_lines = act_src.splitlines()
    for ev in trace.events.values():
        preds = list(ev.causal_links)
        if ev.parent_event_id is not None:
            preds.append(ev.parent_event_id)
        dep_sig = ev.signature
        for pred_id in preds:
            pred_sig = trace.events[pred_id].signature
            assert _find_line_index(seq_lines, f": {pred_sig}") < _find_line_index(
                seq_lines, f": {dep_sig}"
            )
            assert _find_line_index(act_lines, f': {pred_sig}")') < _find_line_index(
                act_lines, f': {dep_sig}")'
            )


@settings(max_examples=50)
@given(payload=causally_linked_overlap_strategy())
def test_causally_linked_overlapping_siblings_never_par_or_fork(
    payload: tuple[InteractionTraceIR, bool],
) -> None:
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    trace, full_chain = payload
    emitter = MermaidEmitter()
    seq_src = emitter.emit(trace)
    act_src = emitter.emit(
        InteractionTraceIR(
            trace_id=trace.trace_id,
            participants=trace.participants,
            events=trace.events,
            metadata={"diagram_variant": "activity"},
        )
    )

    assert MermaidSyntaxValidator().validate(seq_src).valid
    assert MermaidSyntaxValidator().validate(act_src).valid

    if full_chain:
        assert "\n  par " not in seq_src
        assert "\n  and " not in seq_src
        assert "=== fork ===" not in act_src
        assert "=== join ===" not in act_src

    ev_by_sig = {ev.signature: ev for ev in trace.events.values()}
    for group in _extract_par_groups(seq_src) + _extract_fork_groups(act_src):
        ids = {ev_by_sig[sig].event_id for sig in group}
        for sig in group:
            assert not (set(ev_by_sig[sig].causal_links) & ids)


@settings(max_examples=50)
@given(payload=loop_guard_trace_strategy())
def test_loop_folding_blocked_across_clock_domains_and_causal_links(
    payload: tuple[InteractionTraceIR, str],
) -> None:
    from archigram.features.lint_syntax import MermaidSyntaxValidator

    trace, guard_mode = payload
    emitter = MermaidEmitter()
    seq_src = emitter.emit(trace)
    assert MermaidSyntaxValidator().validate(seq_src).valid
    if guard_mode in {"diff_clock", "adjacent_causal"}:
        assert "\n  loop [" not in seq_src
    else:
        assert f"loop [{len(trace.events)} times]" not in seq_src
