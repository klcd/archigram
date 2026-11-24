"""Hypothesis property-based fuzz suite for deterministic PlantUmlEmitter invariants."""

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
from archigram.features.emit_plantuml import PlantUmlEmitter, emit_class_filter_overlay
from archigram.ports.contracts import DiagramEmitterPort

_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz_emit_plantuml",
    input_sha256="a" * 64,
)

_ADVERSARIAL_TOKENS: tuple[str, ...] = (
    "end",
    "END",
    "par",
    "else",
    "loop",
    "class",
    "package",
    "participant",
    "database",
    "queue",
    "skinparam",
    "hide",
    "remove",
    'svc"inject"; end; package hack',
    "foo(); bar();",
    "path\\to\\module:fn(x, y)",
    "trailing\\slash\\",
    'quote"semi;bracket[x]{y}',
    "line1\nline2\r\nline3\ttab",
    "Generic<T, K>",
)

_ADVERSARIAL_TEXT = st.one_of(
    st.sampled_from(_ADVERSARIAL_TOKENS),
    st.text(
        alphabet=(
            'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_;:"()/[]{}<>-\\\n\t '
        ),
        min_size=1,
        max_size=28,
    ).filter(lambda s: bool(s.strip())),
)

_COMPONENT_RELATIONS: tuple[EdgeRelation, ...] = (
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
    """Generate ClassMember instances with adversarial names, signatures, and modifiers."""
    return ClassMember(
        name=draw(_ADVERSARIAL_TEXT),
        visibility=draw(st.sampled_from(list(Visibility))),
        signature=draw(
            st.sampled_from(
                [
                    "",
                    "(x: int, y: str) -> bool",
                    "dict[str, list[int]]",
                    'bad";inject(){hack} -> Result[T]',
                    "trail\\",
                ]
            )
        ),
        is_method=draw(st.booleans()),
        modifiers=tuple(
            draw(st.lists(st.sampled_from(["static", "abstract", "readonly", "async"]), max_size=2))
        ),
        generic_params=tuple(draw(st.lists(_ADVERSARIAL_TEXT, max_size=2))),
    )


@st.composite
def component_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate hierarchical StructuralGraphIR triggering Mode C (ELK component view)."""
    count = draw(st.integers(min_value=1, max_value=14))
    raw_ids = draw(st.lists(_ADVERSARIAL_TEXT, min_size=count, max_size=count, unique=True))
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
                        NodeKind.COMPONENT,
                        NodeKind.DATABASE,
                        NodeKind.QUEUE,
                    ]
                )
            ),
            parent_id=parent_id,
            members=(),
            provenance=_STAMP,
        )

    edge_count = draw(st.integers(min_value=0, max_value=18))
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}_{draw(_ADVERSARIAL_TEXT)}",
            source=draw(st.sampled_from(raw_ids)),
            target=draw(st.sampled_from(raw_ids)),
            relation=draw(st.sampled_from(_COMPONENT_RELATIONS)),
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            provenance=_STAMP,
        )
        for idx in range(edge_count)
    )
    return StructuralGraphIR(
        graph_id=draw(_ADVERSARIAL_TEXT),
        nodes=nodes,
        edges=edges,
        metadata={"diagram_variant": "component"},
    )


@st.composite
def class_diagram_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate OOP StructuralGraphIR triggering Mode B (class diagram)."""
    count = draw(st.integers(min_value=1, max_value=10))
    raw_ids = draw(st.lists(_ADVERSARIAL_TEXT, min_size=count, max_size=count, unique=True))
    nodes: dict[str, IRNode] = {}
    for sym_id in raw_ids:
        attrs: dict[str, str] = {}
        if draw(st.booleans()):
            attrs["generic_params"] = "T, K"
        if draw(st.booleans()):
            attrs["tags"] = "internal, core"
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
    edges = tuple(
        IREdge(
            edge_id=f"ce_{e_idx}",
            source=draw(st.sampled_from(raw_ids)),
            target=draw(st.sampled_from(raw_ids)),
            relation=draw(st.sampled_from(_OOP_RELATIONS)),
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            provenance=_STAMP,
        )
        for e_idx in range(edge_count)
    )
    meta = {"diagram_variant": "class"} if draw(st.booleans()) else {}
    return StructuralGraphIR(
        graph_id=draw(_ADVERSARIAL_TEXT),
        nodes=nodes,
        edges=edges,
        metadata=meta,
    )


@st.composite
def interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate InteractionTraceIR with par, loop, async, and cross-domain teoz & anchors."""
    p_count = draw(st.integers(min_value=3, max_value=6))
    raw_pids = draw(st.lists(_ADVERSARIAL_TEXT, min_size=p_count, max_size=p_count, unique=True))
    kinds = [NodeKind.SERVICE, NodeKind.QUEUE, NodeKind.DATABASE, NodeKind.TABLE]
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{idx}",
            label=draw(_ADVERSARIAL_TEXT),
            kind=kinds[idx % len(kinds)],
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

    for a_idx in range(2):
        eid = f"anchor_evt_{a_idx}"
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=raw_pids[0],
            callee_id=raw_pids[1 + a_idx],
            signature=draw(_ADVERSARIAL_TEXT),
            parent_event_id=None,
            clock_domain_id=f"cross_clock_{a_idx}",
            start_ns=5000,
            end_ns=5080 + a_idx * 20,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
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


def _assert_plantuml_framing_and_safety(puml_src: str) -> None:
    """Assert @startuml/@enduml framing, balanced quotes, no skinparam trap, no trailing slash."""
    lines = puml_src.splitlines()
    assert lines[0] == "@startuml"
    assert lines[-1] == "@enduml"
    assert puml_src.endswith("\n")
    assert re.search(r"skinparam\s+\S+\s+<<", puml_src) is None
    for line in lines:
        assert line.count('"') % 2 == 0, f"Unclosed quote in line: {line!r}"
        assert not line.endswith("\\"), f"Trailing backslash line continuation: {line!r}"


def _assert_balanced_braces(puml_src: str) -> None:
    """Verify curly braces outside double-quoted strings are strictly balanced."""
    depth = 0
    in_quotes = False
    for ch in puml_src:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                assert depth >= 0, "Unmatched closing brace '}'"
    assert not in_quotes
    assert depth == 0, f"Unbalanced braces (remaining depth={depth})"


def _assert_balanced_sequence_blocks(puml_src: str) -> None:
    """Verify par/else/end and loop/end sequence fragments are balanced and valid for teoz."""
    stack: list[tuple[str, int]] = []
    prev_was_message = False
    for raw_line in puml_src.splitlines():
        line = raw_line.strip()
        if line.startswith("par "):
            stack.append(("par", 0))
            prev_was_message = False
        elif line.startswith("else "):
            assert stack and stack[-1][0] == "par"
            kind, branches = stack[-1]
            stack[-1] = (kind, branches + 1)
            prev_was_message = False
        elif line.startswith("loop "):
            stack.append(("loop", 0))
            prev_was_message = False
        elif line == "end":
            assert stack, "Unexpected 'end' without matching par/loop"
            kind, branches = stack.pop()
            if kind == "par":
                assert branches >= 1, "par block must contain at least one else branch"
            prev_was_message = False
        elif line.startswith("& "):
            assert prev_was_message, f"teoz '&' anchor must follow a message, got after: {line!r}"
        elif " -> " in line or " --> " in line or " ->> " in line:
            prev_was_message = True
    assert not stack, f"Unclosed sequence fragments: {stack}"


@settings(max_examples=50)
@given(graph=component_graph_strategy())
def test_component_graph_plantuml_determinism_and_elk_invariants(
    graph: StructuralGraphIR,
) -> None:
    emitter = PlantUmlEmitter()
    assert isinstance(emitter, DiagramEmitterPort)
    assert emitter.target_format == "plantuml"

    first = emitter.emit(graph)
    permuted = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(reversed(list(graph.nodes.items()))),
        edges=tuple(reversed(graph.edges)),
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted)

    assert first == second
    _assert_plantuml_framing_and_safety(first)
    _assert_balanced_braces(first)
    assert first.startswith("@startuml\n!pragma layout elk\n")
    assert re.search(r"\bas\s+(?:end|package|component|database|queue)\b", first) is None


@settings(max_examples=50)
@given(graph=class_diagram_graph_strategy())
def test_class_diagram_plantuml_determinism_and_uml_invariants(
    graph: StructuralGraphIR,
) -> None:
    emitter = PlantUmlEmitter()
    first = emitter.emit(graph)
    permuted = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(reversed(list(graph.nodes.items()))),
        edges=tuple(reversed(graph.edges)),
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted)

    assert first == second
    _assert_plantuml_framing_and_safety(first)
    _assert_balanced_braces(first)
    assert re.search(r"\bas\s+(?:end|class|interface|enum)\b", first) is None
    assert any(kw in first for kw in ("class ", "interface ", "enum "))


@settings(max_examples=50)
@given(
    base_path=_ADVERSARIAL_TEXT,
    hide_private=st.booleans(),
    remove_unlinked=st.booleans(),
    hide_empty=st.booleans(),
    tags=st.lists(_ADVERSARIAL_TEXT, max_size=5),
)
def test_class_filter_overlay_determinism_and_directives(
    base_path: str,
    hide_private: bool,
    remove_unlinked: bool,
    hide_empty: bool,
    tags: list[str],
) -> None:
    first = emit_class_filter_overlay(
        base_path,
        hide_private=hide_private,
        remove_unlinked=remove_unlinked,
        remove_tags=tags,
        hide_empty=hide_empty,
    )
    second = emit_class_filter_overlay(
        base_path,
        hide_private=hide_private,
        remove_unlinked=remove_unlinked,
        remove_tags=list(reversed(tags)),
        hide_empty=hide_empty,
    )

    assert first == second
    _assert_plantuml_framing_and_safety(first)
    assert "\n!include " in first
    assert ("\nhide empty members\n" in first) is hide_empty
    assert ("\nhide private members\n" in first) is hide_private
    assert ("\nremove @unlinked\n" in first) is remove_unlinked


@settings(max_examples=50)
@given(trace=interaction_trace_strategy())
def test_sequence_teoz_par_loop_async_and_anchor_invariants(
    trace: InteractionTraceIR,
) -> None:
    emitter = PlantUmlEmitter()
    first = emitter.emit(trace)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    second = emitter.emit(permuted)

    assert first == second
    _assert_plantuml_framing_and_safety(first)
    _assert_balanced_sequence_blocks(first)
    assert first.startswith("@startuml\n!pragma teoz true\n")
    assert "\npar " in first
    assert "\nelse " in first
    assert "\nloop " in first
    assert " ->> " in first
    assert "\n& " in first
    assert re.search(r"\bas\s+(?:end|par|else|loop|participant|queue|database)\b", first) is None
