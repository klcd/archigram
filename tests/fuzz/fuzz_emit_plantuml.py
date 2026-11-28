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
from archigram.features.emit_plantuml import PlantUmlEmitter
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


@st.composite
def overlapping_causal_activity_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate overlapping same-domain activity events with causal_links dependencies."""
    count = draw(st.integers(min_value=2, max_value=8))
    force_chain = draw(st.booleans())
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.node_{idx}",
            label=f"Node{idx}",
            kind=NodeKind.SERVICE,
            provenance=_STAMP,
        )
        for idx in range(count + 1)
    }

    events: dict[str, TraceEvent] = {}
    has_any_link = False
    for idx in range(count):
        eid = f"ev_{idx}"
        if force_chain:
            links: tuple[str, ...] = (f"ev_{idx - 1}",) if idx > 0 else ()
        else:
            links = tuple(f"ev_{pred}" for pred in range(idx) if draw(st.booleans()))
            if idx == count - 1 and not links and not has_any_link:
                links = ("ev_0",)
        has_any_link = has_any_link or bool(links)
        start_ns = 100 + draw(st.integers(min_value=0, max_value=40))
        end_ns = 500 + draw(st.integers(min_value=0, max_value=100))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{idx}",
            callee_id=f"p_{idx + 1}",
            signature=f"op_{idx:02d}()",
            parent_event_id=None,
            causal_links=links,
            clock_domain_id="shared_clock",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=EventStatus.OK,
            provenance=_STAMP,
        )

    return InteractionTraceIR(
        trace_id="fuzz_activity_causal",
        participants=participants,
        events=events,
        metadata={"diagram_variant": "activity"},
    )


@st.composite
def skewed_clock_causal_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate a causal DAG trace with backwards-skewed timestamps across clock domains."""
    count = draw(st.integers(min_value=3, max_value=7))
    use_parent_links = draw(st.booleans())
    shared_clock = draw(st.booleans())
    participants = {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.actor_{idx}",
            label=f"Actor{idx}",
            kind=NodeKind.SERVICE,
            provenance=_STAMP,
        )
        for idx in range(count + 1)
    }

    events: dict[str, TraceEvent] = {}
    for idx in range(count):
        eid = f"ev_{idx}"
        parent_id: str | None = None
        links: tuple[str, ...] = ()
        if idx > 0:
            if use_parent_links and idx % 2 == 1:
                parent_id = f"ev_{idx - 1}"
            elif use_parent_links:
                links = (f"ev_{idx - 1}",)
            else:
                drawn = tuple(f"ev_{p}" for p in range(idx) if draw(st.booleans()))
                links = drawn if drawn else (f"ev_{idx - 1}",)
        skewed_start = (count - idx) * 100_000 + draw(st.integers(min_value=0, max_value=5_000))
        domain = (
            "skewed_0" if shared_clock else f"skewed_{draw(st.integers(min_value=0, max_value=2))}"
        )
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=f"p_{idx}",
            callee_id=f"p_{idx + 1}",
            signature=f"step_{idx:02d}()",
            parent_event_id=parent_id,
            causal_links=links,
            clock_domain_id=domain,
            start_ns=skewed_start,
            end_ns=skewed_start + 50,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=EventStatus.OK,
            provenance=_STAMP,
        )

    return InteractionTraceIR(
        trace_id="fuzz_skewed_clocks",
        participants=participants,
        events=events,
    )


def _extract_fork_event_indices(puml: str) -> list[list[int]]:
    """Parse fork / fork again / end fork groups and extract event indices per fork block."""
    groups: list[list[int]] = []
    current: list[int] | None = None
    for raw_line in puml.splitlines():
        line = raw_line.strip()
        if line == "fork":
            assert current is None, "Nested fork is unexpected"
            current = []
        elif line == "fork again":
            assert current is not None, "'fork again' outside fork block"
        elif line == "end fork":
            assert current is not None, "'end fork' without matching fork"
            assert len(current) >= 2, "fork block must have at least 2 branches"
            groups.append(current)
            current = None
        elif current is not None and (match := re.search(r"op_(\d+)\(\)", line)) is not None:
            current.append(int(match.group(1)))
    assert current is None, "Unclosed fork block"
    return groups


def _transitive_ancestors(events: dict[str, TraceEvent]) -> dict[str, set[str]]:
    """Compute transitive causal ancestors (parent_event_id and causal_links) per event."""
    ancestors: dict[str, set[str]] = {}
    for eid in events:
        seen: set[str] = set()
        stack: list[str] = list(events[eid].causal_links)
        if (parent_id := events[eid].parent_event_id) is not None:
            stack.append(parent_id)
        while stack:
            pred = stack.pop()
            if pred in seen or pred not in events:
                continue
            seen.add(pred)
            stack.extend(events[pred].causal_links)
            if (pred_parent := events[pred].parent_event_id) is not None:
                stack.append(pred_parent)
        ancestors[eid] = seen
    return ancestors


@settings(max_examples=60)
@given(trace=overlapping_causal_activity_strategy())
def test_activity_overlapping_causal_links_never_fork(trace: InteractionTraceIR) -> None:
    """Overlapping activity events with causal_links are never grouped into fork/end fork."""
    emitter = PlantUmlEmitter()
    first = emitter.emit(trace)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    assert first == emitter.emit(permuted)
    _assert_plantuml_framing_and_safety(first)

    ancestors = _transitive_ancestors(trace.events)
    for group in _extract_fork_event_indices(first):
        for i, lhs in enumerate(group):
            for rhs in group[i + 1 :]:
                assert f"ev_{lhs}" not in ancestors[f"ev_{rhs}"]
                assert f"ev_{rhs}" not in ancestors[f"ev_{lhs}"]

    for eid, ev in trace.events.items():
        for pred_id in ev.causal_links:
            assert first.index(trace.events[pred_id].signature) < first.index(ev.signature)
        if eid != "ev_0" and all(
            f"ev_{k}" in trace.events[f"ev_{k + 1}"].causal_links
            for k in range(len(trace.events) - 1)
        ):
            assert "fork" not in first.splitlines()


@settings(max_examples=60)
@given(trace=skewed_clock_causal_trace_strategy())
def test_skewed_clock_causal_ordering_across_plantuml_views(
    trace: InteractionTraceIR,
) -> None:
    """Causal ordering holds across sequence, activity, state, and communication under skew."""
    emitter = PlantUmlEmitter()
    for variant in ("auto", "activity", "state", "communication"):
        variant_trace = InteractionTraceIR(
            trace_id=trace.trace_id,
            participants=trace.participants,
            events=trace.events,
            metadata={"diagram_variant": variant},
        )
        permuted = InteractionTraceIR(
            trace_id=trace.trace_id,
            participants=dict(reversed(list(trace.participants.items()))),
            events=dict(reversed(list(trace.events.items()))),
            metadata={"diagram_variant": variant},
        )
        rendered = emitter.emit(variant_trace)
        assert rendered == emitter.emit(permuted)
        _assert_plantuml_framing_and_safety(rendered)
        if variant == "auto":
            _assert_balanced_sequence_blocks(rendered)
        for ev in trace.events.values():
            preds = list(ev.causal_links)
            if ev.parent_event_id is not None:
                preds.append(ev.parent_event_id)
            for pred_id in preds:
                assert rendered.index(trace.events[pred_id].signature) < rendered.index(
                    ev.signature
                )


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
