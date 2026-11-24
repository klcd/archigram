"""Hypothesis property-based fuzz suite for deterministic D2Emitter invariants."""

from __future__ import annotations

import shutil
import string
import subprocess

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
from archigram.features.emit_d2 import D2Emitter
from archigram.ports.contracts import DiagramEmitterPort

_PUNCT_CHARS = './:{}"$\\ \t-'
_ID_ALPHABET = string.ascii_letters + string.digits + '_-.:/{}$"\\'
_LABEL_ALPHABET = string.ascii_letters + string.digits + _PUNCT_CHARS + "\n"
_SAFE_ID = st.text(alphabet=string.ascii_lowercase + string.digits + "_", min_size=1, max_size=12)
_PUNCT_TEXT = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=24).filter(
    lambda s: bool(s.strip())
)
_LABEL_TEXT = st.text(alphabet=_LABEL_ALPHABET, min_size=1, max_size=32).filter(
    lambda s: bool(s.strip())
)
_SHA256 = "a" * 64
_D2_BIN = shutil.which("d2")
_RESERVED_SAMPLE = ("shape", "style", "vars", "tooltip", "label", "d2-config")


def _assert_balanced_d2_braces(d2_source: str) -> None:
    """Verify all quotes are closed and all `{}` outside D2 double-quoted strings balance."""
    depth = 0
    in_quotes = False
    escaped = False
    for ch in d2_source:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, "Unmatched closing brace '}' in emitted D2"
    assert not in_quotes, "Unclosed double quote in emitted D2"
    assert depth == 0, f"Unbalanced braces in emitted D2 (remaining depth={depth})"


def _assert_d2_cli_valid(d2_source: str) -> None:
    """Validate D2 syntax via `d2 fmt -` when the `d2` binary is present on PATH."""
    if _D2_BIN is None:
        return
    proc = subprocess.run(  # noqa: S603
        [_D2_BIN, "fmt", "-"],
        input=d2_source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"d2 fmt failed ({proc.returncode}): {proc.stderr}"


@st.composite
def _provenance_strategy(draw: st.DrawFn, *, force_derived: bool = False) -> ProvenanceStamp:
    grade = (
        ProvenanceGrade.DERIVED_DETERMINISTIC
        if force_derived
        else draw(st.sampled_from(list(ProvenanceGrade)))
    )
    if grade is ProvenanceGrade.OBSERVED:
        return ProvenanceStamp(grade=grade, extractor="fuzz", input_sha256=_SHA256)
    if grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        backing = tuple(draw(st.lists(_PUNCT_TEXT, min_size=1, max_size=7)))
        return ProvenanceStamp(
            grade=grade,
            rule_id=draw(_PUNCT_TEXT),
            backing_ids=backing,
        )
    if grade is ProvenanceGrade.DECLARED:
        return ProvenanceStamp(grade=grade, file_path="src/app.py")
    return ProvenanceStamp(grade=grade, rule_id="inferred_rule")


@st.composite
def _member_strategy(draw: st.DrawFn) -> ClassMember:
    is_method = draw(st.booleans())
    sig = (
        draw(st.sampled_from(["(x: int) -> str", "(a: str, b: bool)", "int", ""]))
        if is_method
        else draw(st.sampled_from(["str", "dict[str, int]", "list[bytes]", ""]))
    )
    return ClassMember(
        name=draw(_SAFE_ID),
        visibility=draw(st.sampled_from(list(Visibility))),
        signature=sig,
        is_method=is_method,
    )


def _unique_ids(draw: st.DrawFn, count: int, *, include_punct: bool) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for idx in range(count):
        if include_punct and draw(st.booleans()):
            raw = draw(st.one_of(_PUNCT_TEXT, st.sampled_from(_RESERVED_SAMPLE)))
            candidate = raw if raw not in seen else f"{raw}_{idx}"
        else:
            candidate = f"node_{idx}"
        seen.add(candidate)
        ids.append(candidate)
    return ids


@st.composite
def _nested_structural_graph_strategy(
    draw: st.DrawFn,
    *,
    include_punct: bool = True,
    min_nodes: int = 1,
    max_nodes: int = 14,
) -> StructuralGraphIR:
    count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    node_ids = _unique_ids(draw, count, include_punct=include_punct)
    children_map: dict[str, list[str]] = {}
    parents: list[str | None] = []
    for idx in range(count):
        pid = (
            node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
            if idx > 0 and draw(st.booleans())
            else None
        )
        parents.append(pid)
        if pid is not None:
            children_map.setdefault(pid, []).append(node_ids[idx])

    nodes: dict[str, IRNode] = {}
    for sid, pid in zip(node_ids, parents, strict=True):
        has_children = sid in children_map
        kind = draw(st.sampled_from(list(NodeKind)))
        is_derived = kind is NodeKind.SUPERNODE or draw(st.booleans())
        members = (
            tuple(draw(st.lists(_member_strategy(), min_size=1, max_size=3)))
            if not has_children and kind in {NodeKind.CLASS, NodeKind.INTERFACE}
            else ()
        )
        label = draw(_LABEL_TEXT if include_punct else _SAFE_ID)
        nodes[sid] = IRNode(
            symbol_id=sid,
            label=label,
            kind=kind,
            parent_id=pid,
            members=members,
            provenance=draw(_provenance_strategy(force_derived=is_derived)),
        )

    edge_count = draw(st.integers(min_value=0, max_value=14))
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=draw(st.sampled_from(node_ids)),
            target=draw(st.sampled_from(node_ids)),
            relation=draw(st.sampled_from(list(EdgeRelation))),
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            provenance=draw(_provenance_strategy()),
        )
        for idx in range(edge_count)
    )
    return StructuralGraphIR(graph_id="fuzz_graph", nodes=nodes, edges=edges)


@st.composite
def _interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    p_count = draw(st.integers(min_value=2, max_value=7))
    p_ids = _unique_ids(draw, p_count, include_punct=True)
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{idx}",
            label=draw(_LABEL_TEXT),
            kind=draw(st.sampled_from(list(NodeKind))),
            provenance=draw(_provenance_strategy()),
        )
        for idx, pid in enumerate(p_ids)
    }

    e_count = draw(st.integers(min_value=0, max_value=14))
    e_ids = [f"evt_{idx}" for idx in range(e_count)]
    events: dict[str, TraceEvent] = {}
    for idx, eid in enumerate(e_ids):
        earlier = e_ids[:idx]
        parent_id = draw(st.sampled_from(earlier)) if earlier and draw(st.booleans()) else None
        links = (
            tuple(draw(st.lists(st.sampled_from(earlier), max_size=2, unique=True)))
            if earlier
            else ()
        )
        start_ns = draw(st.integers(min_value=0, max_value=1000))
        end_ns = start_ns + draw(st.integers(min_value=1, max_value=500))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=draw(st.sampled_from(p_ids)),
            callee_id=draw(st.sampled_from(p_ids)),
            signature=draw(_LABEL_TEXT),
            parent_event_id=parent_id,
            causal_links=links,
            clock_domain_id=f"host_{idx % 3}",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=draw(st.sampled_from(list(EventStatus))),
            provenance=draw(_provenance_strategy()),
        )
    return InteractionTraceIR(trace_id="fuzz_trace", participants=participants, events=events)


@settings(max_examples=80, deadline=None)
@given(graph=_nested_structural_graph_strategy())
def test_structural_graph_d2_determinism_and_syntax_invariants(graph: StructuralGraphIR) -> None:
    from archigram.domain.style import D2_CANONICAL_HEADER

    emitter = D2Emitter()
    assert isinstance(emitter, DiagramEmitterPort)
    assert emitter.target_format == "d2"

    out_first = emitter.emit(graph)
    out_second = emitter.emit(graph)
    assert out_first == out_second
    assert out_first.startswith(D2_CANONICAL_HEADER)
    assert "style.fill: transparent" in out_first
    _assert_balanced_d2_braces(out_first)
    _assert_d2_cli_valid(out_first)

    for node in graph.nodes.values():
        if node.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
            assert "tooltip:" in out_first
            count_token = f"({len(node.provenance.backing_ids)} backing node"
            assert count_token in out_first
        if node.kind in {NodeKind.DATABASE, NodeKind.TABLE}:
            assert "shape: cylinder" in out_first
        if node.kind is NodeKind.QUEUE:
            assert "shape: queue" in out_first
        if node.members and node.kind in {NodeKind.CLASS, NodeKind.INTERFACE}:
            has_child = any(n.parent_id == node.symbol_id for n in graph.nodes.values())
            if not has_child:
                assert "shape: class" in out_first

    if any(edge.sync_kind is SyncKind.ASYNC_FIRE_FORGET for edge in graph.edges):
        assert "style.stroke-dash: 5" in out_first


@settings(max_examples=60, deadline=None)
@given(graph=_nested_structural_graph_strategy(include_punct=False, min_nodes=4, max_nodes=10))
def test_cross_boundary_qualified_paths_and_order_invariance(graph: StructuralGraphIR) -> None:
    emitter = D2Emitter()
    reversed_graph = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(reversed(list(graph.nodes.items()))),
        edges=tuple(reversed(graph.edges)),
    )
    out_a = emitter.emit(graph)
    out_b = emitter.emit(reversed_graph)
    assert out_a == out_b

    for edge in graph.edges:
        src_node = graph.nodes[edge.source]
        dst_node = graph.nodes[edge.target]
        if src_node.parent_id is not None and dst_node.parent_id is not None:
            expected_arrow = f".{edge.source} -> "
            expected_target = f".{edge.target}:"
            assert expected_arrow in out_a
            assert expected_target in out_a


@settings(max_examples=80, deadline=None)
@given(trace=_interaction_trace_strategy())
def test_interaction_trace_d2_sequence_and_concurrency_grouping(
    trace: InteractionTraceIR,
) -> None:
    from archigram.domain.style import D2_CANONICAL_HEADER

    emitter = D2Emitter()
    reversed_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
    )
    out_first = emitter.emit(trace)
    out_second = emitter.emit(reversed_trace)

    assert out_first == out_second
    assert out_first.startswith(D2_CANONICAL_HEADER)
    assert "\nshape: sequence_diagram\n" in out_first
    _assert_balanced_d2_braces(out_first)
    _assert_d2_cli_valid(out_first)


@settings(max_examples=50, deadline=None)
@given(
    start_a=st.integers(min_value=0, max_value=500),
    dur_a=st.integers(min_value=50, max_value=300),
    offset_b=st.integers(min_value=0, max_value=49),
    dur_b=st.integers(min_value=10, max_value=200),
    sig_a=_LABEL_TEXT,
    sig_b=_LABEL_TEXT,
)
def test_overlapping_concurrent_siblings_emit_d2_message_group(
    start_a: int,
    dur_a: int,
    offset_b: int,
    dur_b: int,
    sig_a: str,
    sig_b: str,
) -> None:
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    participants = {
        pid: TraceParticipant(
            participant_id=pid, symbol_id=pid, label=pid, kind=NodeKind.SERVICE, provenance=stamp
        )
        for pid in ("api", "svc_a", "svc_b")
    }
    events = {
        "root": TraceEvent(
            event_id="root",
            caller_id="api",
            callee_id="api",
            signature="dispatch()",
            clock_domain_id="h0",
            start_ns=0,
            end_ns=2000,
            provenance=stamp,
        ),
        "child_a": TraceEvent(
            event_id="child_a",
            caller_id="api",
            callee_id="svc_a",
            signature=sig_a,
            parent_event_id="root",
            clock_domain_id="h0",
            start_ns=start_a + 1,
            end_ns=start_a + 1 + dur_a,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            provenance=stamp,
        ),
        "child_b": TraceEvent(
            event_id="child_b",
            caller_id="api",
            callee_id="svc_b",
            signature=sig_b,
            parent_event_id="root",
            clock_domain_id="h1",
            start_ns=start_a + 1 + offset_b,
            end_ns=start_a + 1 + offset_b + dur_b,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            provenance=stamp,
        ),
    }
    trace = InteractionTraceIR(trace_id="concurrent", participants=participants, events=events)
    out = D2Emitter().emit(trace)
    assert "concurrent_group_0:" in out
    assert out.index("dispatch") < out.index("concurrent_group_0:")
    _assert_balanced_d2_braces(out)
    _assert_d2_cli_valid(out)


@settings(max_examples=60, deadline=None)
@given(trace=_interaction_trace_strategy())
def test_interaction_trace_d2_steps_variant_determinism_and_cli_validity(
    trace: InteractionTraceIR,
) -> None:
    from archigram.domain.style import D2_CANONICAL_HEADER

    steps_trace = trace.model_copy(
        update={"metadata": {**trace.metadata, "diagram_variant": "steps"}}
    )
    reversed_steps_trace = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata={"diagram_variant": "steps"},
    )
    emitter = D2Emitter()
    out_first = emitter.emit(steps_trace)
    out_second = emitter.emit(reversed_steps_trace)

    assert out_first == out_second
    assert out_first.startswith(D2_CANONICAL_HEADER)
    assert "\ndirection: right\n" in out_first
    assert "shape: sequence_diagram" not in out_first
    if trace.events:
        assert "\nsteps: {\n" in out_first
        assert "  step_1: {" in out_first
        assert "  step_13: {" not in out_first
    _assert_balanced_d2_braces(out_first)
    _assert_d2_cli_valid(out_first)
