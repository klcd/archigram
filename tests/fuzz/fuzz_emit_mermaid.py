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
    emitter = MermaidEmitter()
    assert isinstance(emitter, DiagramEmitterPort)
    assert emitter.target_format == "mermaid"

    first = emitter.emit(graph)
    PermutedNodes = dict(reversed(list(graph.nodes.items())))
    PermutedEdges = tuple(reversed(graph.edges))
    permuted_graph = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=PermutedNodes,
        edges=PermutedEdges,
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted_graph)

    assert first == second
    assert first.startswith("---\nconfig:\n  layout: elk\n---\nflowchart TD\n")
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    _assert_balanced_mermaid_blocks(first)
    assert re.search(r"\bsubgraph\s+end\b", first, re.IGNORECASE) is None


@settings(max_examples=50)
@given(graph=class_diagram_graph_strategy())
def test_class_diagram_emission_determinism_and_syntax_invariants(
    graph: StructuralGraphIR,
) -> None:
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
    assert first.startswith("---\nconfig:\n  layout: elk\n---\nclassDiagram\n")
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    assert re.search(r"\bclass\s+end\b", first, re.IGNORECASE) is None


@settings(max_examples=50)
@given(trace=interaction_trace_strategy())
def test_sequence_diagram_par_loop_and_end_aliasing_invariants(
    trace: InteractionTraceIR,
) -> None:
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
    assert first.startswith("sequenceDiagram\n")
    _assert_no_unescaped_semicolons_in_quoted_labels(first)
    _assert_balanced_mermaid_blocks(first)
    assert re.search(r"\bparticipant\s+end\b", first, re.IGNORECASE) is None
    assert "\n  par " in first
    assert "\n  and " in first
    assert "\n  loop [" in first
    assert "-)" in first
