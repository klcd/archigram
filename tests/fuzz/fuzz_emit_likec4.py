"""Hypothesis property-based fuzz suite for deterministic LikeC4Emitter invariants."""

from __future__ import annotations

import os
import re
import shutil
import string
import subprocess
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
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
)
from archigram.features.emit_likec4 import LikeC4Emitter
from archigram.ports.contracts import DiagramEmitterPort

_NPX_BIN = shutil.which("npx")
_SHA256 = "d" * 64
_SAFE_ID = st.text(alphabet=string.ascii_lowercase + string.digits + "_", min_size=1, max_size=12)
_PUNCT_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + '_-.:/{}$"\\ \t',
    min_size=1,
    max_size=20,
).filter(lambda s: bool(s.strip()))
_LABEL_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + './:{}"$\\ \t-\n',
    min_size=1,
    max_size=30,
).filter(lambda s: bool(s.strip()))

_FQN_SEGMENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_FQN_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def _assert_balanced_c4_braces(c4_source: str) -> None:
    """Verify all quotes are closed and all `{}` outside single-quoted strings balance."""
    depth = 0
    in_single_quotes = False
    escaped = False
    for ch in c4_source:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_single_quotes:
            escaped = True
            continue
        if ch == "'":
            in_single_quotes = not in_single_quotes
            continue
        if in_single_quotes:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, "Unmatched closing brace '}' in emitted LikeC4"
    assert not in_single_quotes, "Unclosed single quote in emitted LikeC4"
    assert depth == 0, f"Unbalanced braces in emitted LikeC4 (remaining depth={depth})"


def _assert_likec4_cli_valid(c4_source: str) -> None:
    """Validate LikeC4 syntax via `npx likec4 validate` if npx is available."""
    if _NPX_BIN is None:
        return
    with tempfile.TemporaryDirectory() as td:
        c4_file = os.path.join(td, "test.c4")
        with open(c4_file, "w", encoding="utf-8") as f:
            f.write(c4_source)
        proc = subprocess.run(  # noqa: S603
            [_NPX_BIN, "likec4", "validate", td, "--json", "--no-layout"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"likec4 validate failed ({proc.returncode}): {proc.stdout}"


def _unique_ids(draw: st.DrawFn, count: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for idx in range(count):
        raw = draw(
            st.one_of(_PUNCT_TEXT, st.sampled_from(["model", "view", "specification", "123"]))
        )
        candidate = raw if raw not in seen else f"{raw}_{idx}"
        seen.add(candidate)
        ids.append(candidate)
    return ids


@st.composite
def _nested_structural_graph_strategy(
    draw: st.DrawFn,
    *,
    min_nodes: int = 1,
    max_nodes: int = 12,
) -> StructuralGraphIR:
    count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    node_ids = _unique_ids(draw, count)
    parents: list[str | None] = []
    for idx in range(count):
        pid = (
            node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
            if idx > 0 and draw(st.booleans())
            else None
        )
        parents.append(pid)

    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="fuzz", input_sha256=_SHA256)
    nodes: dict[str, IRNode] = {}
    for sid, pid in zip(node_ids, parents, strict=True):
        nodes[sid] = IRNode(
            symbol_id=sid,
            label=draw(_LABEL_TEXT),
            kind=draw(st.sampled_from(list(NodeKind))),
            parent_id=pid,
            provenance=stamp,
        )

    edge_count = draw(st.integers(min_value=0, max_value=10))
    edges: list[IREdge] = []
    for idx in range(edge_count):
        edges.append(
            IREdge(
                edge_id=f"e_{idx}",
                source=draw(st.sampled_from(node_ids)),
                target=draw(st.sampled_from(node_ids)),
                relation=draw(st.sampled_from(list(EdgeRelation))),
                sync_kind=draw(st.sampled_from(list(SyncKind))),
                provenance=stamp,
            )
        )
    return StructuralGraphIR(graph_id="fuzz_c4_graph", nodes=nodes, edges=tuple(edges))


@st.composite
def _interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    p_count = draw(st.integers(min_value=2, max_value=6))
    p_ids = _unique_ids(draw, p_count)
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="fuzz", input_sha256=_SHA256)
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{idx}",
            label=draw(_LABEL_TEXT),
            kind=draw(st.sampled_from(list(NodeKind))),
            provenance=stamp,
        )
        for idx, pid in enumerate(p_ids)
    }

    event_count = draw(st.integers(min_value=0, max_value=10))
    events: dict[str, TraceEvent] = {}
    event_ids = [f"evt_{i}" for i in range(event_count)]
    for idx, eid in enumerate(event_ids):
        caller = draw(st.sampled_from(p_ids))
        callee = draw(st.sampled_from(p_ids))
        parent = (
            event_ids[draw(st.integers(0, idx - 1))] if idx > 0 and draw(st.booleans()) else None
        )
        links = (
            tuple(
                draw(
                    st.lists(
                        st.sampled_from(event_ids[:idx]),
                        min_size=0,
                        max_size=min(2, idx),
                        unique=True,
                    )
                )
            )
            if idx > 0
            else ()
        )
        start = draw(st.integers(min_value=0, max_value=5_000))
        dur = draw(st.integers(min_value=1, max_value=1_000))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=caller,
            callee_id=callee,
            signature=draw(_LABEL_TEXT),
            parent_event_id=parent,
            causal_links=links,
            clock_domain_id=draw(st.sampled_from(["c0", "c1"])),
            start_ns=start,
            end_ns=start + dur,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=EventStatus.OK,
            provenance=stamp,
        )

    return InteractionTraceIR(
        trace_id=draw(st.sampled_from(["default", "trace_1", "adv-id:99"])),
        participants=participants,
        events=events,
    )


@settings(max_examples=10)
@given(dummy=st.booleans())
def test_likec4_emitter_target_format_and_port(dummy: bool) -> None:
    emitter = LikeC4Emitter()
    assert isinstance(emitter, DiagramEmitterPort)
    assert emitter.target_format == "likec4"


@settings(max_examples=40)
@given(graph=_nested_structural_graph_strategy())
def test_structural_graph_emission_determinism_and_syntax_invariants(
    graph: StructuralGraphIR,
) -> None:
    emitter = LikeC4Emitter()
    first = emitter.emit(graph)
    permuted = StructuralGraphIR(
        graph_id=graph.graph_id,
        nodes=dict(reversed(list(graph.nodes.items()))),
        edges=tuple(reversed(graph.edges)),
        metadata=graph.metadata,
    )
    second = emitter.emit(permuted)

    # Byte-identical determinism
    assert first == second

    # Syntax invariants
    assert first.startswith("specification {\n")
    assert "\nmodel {\n" in first
    assert "\nviews {\n  view index {\n    include *\n  }\n}\n" in first
    assert "<-" not in first
    _assert_balanced_c4_braces(first)

    # Validate FQNs, element styles, relationship declarations, and typed arrows
    assert "opacity 0%" in first
    for line in first.splitlines():
        stripped = line.strip()
        if not stripped or stripped in {
            "specification {",
            "model {",
            "views {",
            "view index {",
            "include *",
            "style {",
            "color slate",
            "border solid",
            "opacity 0%",
            "}",
        }:
            continue
        if stripped.startswith(("color ", "line ")):
            continue
        if stripped.startswith("element "):
            kind_token = stripped.removeprefix("element ").removesuffix("{").strip()
            assert _FQN_SEGMENT_RE.match(kind_token) is not None
        elif stripped.startswith("relationship "):
            rel_token = stripped.removeprefix("relationship ").removesuffix("{").strip()
            assert _FQN_SEGMENT_RE.match(rel_token) is not None
        elif " = " in stripped:
            ident = stripped.split(" = ")[0].strip()
            assert _FQN_SEGMENT_RE.match(ident) is not None
        elif "]-> " in stripped:
            edge_part = stripped.split(" '")[0].strip()
            src_fqn, rest = edge_part.split(" -[", 1)
            rel_token, dst_fqn = rest.split("]-> ", 1)
            assert _FQN_PATH_RE.match(src_fqn) is not None
            assert _FQN_SEGMENT_RE.match(rel_token) is not None
            assert _FQN_PATH_RE.match(dst_fqn) is not None


@settings(max_examples=40)
@given(trace=_interaction_trace_strategy())
def test_interaction_trace_emission_determinism_and_syntax_invariants(
    trace: InteractionTraceIR,
) -> None:
    emitter = LikeC4Emitter()
    first = emitter.emit(trace)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    second = emitter.emit(permuted)

    # Byte-identical determinism
    assert first == second

    # Syntax invariants
    assert first.startswith("specification {\n")
    assert "\nmodel {\n" in first
    assert "\nviews {\n  dynamic view trace_" in first
    assert "variant sequence\n" in first
    assert "<-" not in first
    _assert_balanced_c4_braces(first)


@settings(max_examples=30)
@given(
    start=st.integers(min_value=10, max_value=500),
    dur_a=st.integers(min_value=50, max_value=200),
    dur_b=st.integers(min_value=50, max_value=200),
    sig_a=_LABEL_TEXT,
    sig_b=_LABEL_TEXT,
)
def test_overlapping_concurrent_siblings_emit_parallel_block(
    start: int,
    dur_a: int,
    dur_b: int,
    sig_a: str,
    sig_b: str,
) -> None:
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    participants = {
        pid: TraceParticipant(
            participant_id=pid, symbol_id=pid, label=pid, kind=NodeKind.SERVICE, provenance=stamp
        )
        for pid in ("caller_svc", "worker_a", "worker_b")
    }
    events = {
        "root": TraceEvent(
            event_id="root",
            caller_id="caller_svc",
            callee_id="caller_svc",
            signature="dispatch()",
            clock_domain_id="clk0",
            start_ns=0,
            end_ns=2000,
            provenance=stamp,
        ),
        "branch_a": TraceEvent(
            event_id="branch_a",
            caller_id="caller_svc",
            callee_id="worker_a",
            signature=sig_a,
            parent_event_id="root",
            clock_domain_id="clk0",
            start_ns=start,
            end_ns=start + dur_a,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            provenance=stamp,
        ),
        "branch_b": TraceEvent(
            event_id="branch_b",
            caller_id="caller_svc",
            callee_id="worker_b",
            signature=sig_b,
            parent_event_id="root",
            clock_domain_id="clk0",
            start_ns=start + 5,
            end_ns=start + 5 + dur_b,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            provenance=stamp,
        ),
    }
    trace = InteractionTraceIR(trace_id="par_test", participants=participants, events=events)
    out = LikeC4Emitter().emit(trace)
    assert "parallel {\n" in out
    assert out.index("parallel {") < out.rindex("}")
    _assert_balanced_c4_braces(out)


@settings(max_examples=3, deadline=None)
@given(dummy=st.booleans())
def test_likec4_cli_validation_on_clean_hierarchical_and_dynamic_models(dummy: bool) -> None:
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="test", input_sha256=_SHA256)
    nodes = {
        "orders": IRNode(
            symbol_id="orders", label="Orders System", kind=NodeKind.CONTAINER, provenance=stamp
        ),
        "orders_api": IRNode(
            symbol_id="orders_api",
            label="Orders API",
            kind=NodeKind.COMPONENT,
            parent_id="orders",
            provenance=stamp,
        ),
        "db": IRNode(symbol_id="db", label="Database", kind=NodeKind.DATABASE, provenance=stamp),
    }
    edges = (
        IREdge(
            edge_id="e1",
            source="orders_api",
            target="db",
            relation=EdgeRelation.WRITES_DATA,
            provenance=stamp,
        ),
    )
    graph = StructuralGraphIR(graph_id="cli_check_graph", nodes=nodes, edges=edges)
    c4_graph = LikeC4Emitter().emit(graph)
    _assert_likec4_cli_valid(c4_graph)

    participants = {
        "p1": TraceParticipant(
            participant_id="p1",
            symbol_id="p1",
            label="Frontend",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        ),
        "p2": TraceParticipant(
            participant_id="p2",
            symbol_id="p2",
            label="Backend",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        ),
        "p3": TraceParticipant(
            participant_id="p3",
            symbol_id="p3",
            label="Worker",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        ),
    }
    events = {
        "e1": TraceEvent(
            event_id="e1",
            caller_id="p1",
            callee_id="p2",
            signature="POST /req",
            clock_domain_id="c0",
            start_ns=0,
            end_ns=500,
            provenance=stamp,
        ),
        "e2": TraceEvent(
            event_id="e2",
            caller_id="p2",
            callee_id="p3",
            signature="async work",
            parent_event_id="e1",
            clock_domain_id="c0",
            start_ns=100,
            end_ns=400,
            provenance=stamp,
        ),
    }
    trace = InteractionTraceIR(trace_id="cli_check_trace", participants=participants, events=events)
    c4_trace = LikeC4Emitter().emit(trace)
    _assert_likec4_cli_valid(c4_trace)


def _make_trace_participants(count: int) -> dict[str, TraceParticipant]:
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    return {
        f"p_{idx}": TraceParticipant(
            participant_id=f"p_{idx}",
            symbol_id=f"svc.node_{idx}",
            label=f"Svc{idx}",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        )
        for idx in range(count)
    }


@st.composite
def _skewed_clock_causal_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate a causal DAG trace with backwards-skewed timestamps across clock domains."""
    count = draw(st.integers(min_value=3, max_value=7))
    use_parent_links = draw(st.booleans())
    shared_clock = draw(st.booleans())
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    participants = _make_trace_participants(count + 1)

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
            provenance=stamp,
        )

    return InteractionTraceIR(
        trace_id="fuzz_skewed_clocks",
        participants=participants,
        events=events,
    )


@st.composite
def _overlapping_causal_siblings_strategy(
    draw: st.DrawFn,
) -> tuple[InteractionTraceIR, bool]:
    """Generate overlapping same-clock siblings with causal_links (full chain or partial DAG)."""
    count = draw(st.integers(min_value=2, max_value=6))
    full_chain = draw(st.booleans())
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    participants = _make_trace_participants(count + 1)

    events: dict[str, TraceEvent] = {
        "root": TraceEvent(
            event_id="root",
            caller_id="p_0",
            callee_id="p_0",
            signature="dispatch()",
            clock_domain_id="shared_clk",
            start_ns=0,
            end_ns=5_000,
            provenance=stamp,
        )
    }
    for idx in range(count):
        eid = f"ov_{idx}"
        if idx == 0:
            links: tuple[str, ...] = ()
        elif full_chain:
            links = (f"ov_{idx - 1}",)
        else:
            candidates = [f"ov_{p}" for p in range(idx)]
            min_links = 1 if idx == count - 1 else 0
            links = tuple(
                draw(
                    st.lists(
                        st.sampled_from(candidates),
                        min_size=min_links,
                        max_size=idx,
                        unique=True,
                    )
                )
            )
        start_ns = 100 + draw(st.integers(min_value=0, max_value=40))
        end_ns = 500 + draw(st.integers(min_value=0, max_value=100))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="p_0",
            callee_id=f"p_{idx + 1}",
            signature=f"ovstep_{idx:02d}()",
            parent_event_id="root",
            causal_links=links,
            clock_domain_id="shared_clk",
            start_ns=start_ns,
            end_ns=end_ns,
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            status=EventStatus.OK,
            provenance=stamp,
        )

    return (
        InteractionTraceIR(
            trace_id="fuzz_causal_overlap",
            participants=participants,
            events=events,
        ),
        full_chain,
    )


def _extract_parallel_groups(c4_source: str) -> list[list[int]]:
    """Extract ovstep event indices inside each `parallel { ... }` block."""
    groups: list[list[int]] = []
    current: list[int] | None = None
    for raw_line in c4_source.splitlines():
        line = raw_line.strip()
        if line == "parallel {":
            assert current is None, "Nested parallel block is unexpected"
            current = []
        elif current is not None and line == "}":
            assert len(current) >= 2, "parallel block must contain at least 2 steps"
            groups.append(current)
            current = None
        elif current is not None and (match := re.search(r"ovstep_(\d+)\(\)", line)) is not None:
            current.append(int(match.group(1)))
    assert current is None, "Unclosed parallel block"
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


@settings(max_examples=50)
@given(trace=_skewed_clock_causal_trace_strategy())
def test_skewed_clock_causal_ordering_and_dangling_safety(trace: InteractionTraceIR) -> None:
    """Causal ordering overrides skewed timestamps and handles dangling predecessors safely."""
    emitter = LikeC4Emitter()
    first = emitter.emit(trace)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    assert first == emitter.emit(permuted)
    _assert_balanced_c4_braces(first)

    for ev in trace.events.values():
        preds = list(ev.causal_links)
        if ev.parent_event_id is not None:
            preds.append(ev.parent_event_id)
        for pred_id in preds:
            assert first.index(trace.events[pred_id].signature) < first.index(ev.signature)

    dangling_trace = InteractionTraceIR.model_construct(
        schema_version=trace.schema_version,
        ir_mode=trace.ir_mode,
        trace_id=trace.trace_id,
        participants=trace.participants,
        events={k: v for k, v in trace.events.items() if k != "ev_0"},
        metadata=trace.metadata,
    )
    dangling_out = emitter.emit(dangling_trace)
    _assert_balanced_c4_braces(dangling_out)
    for eid, ev in dangling_trace.events.items():
        assert ev.signature in dangling_out, f"Missing {eid} after dangling predecessor removal"


@settings(max_examples=50)
@given(payload=_overlapping_causal_siblings_strategy())
def test_overlapping_causal_links_never_group_into_parallel(
    payload: tuple[InteractionTraceIR, bool],
) -> None:
    """Overlapping same-clock siblings linked via causal_links never share a parallel block."""
    trace, full_chain = payload
    emitter = LikeC4Emitter()
    first = emitter.emit(trace)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
        metadata=trace.metadata,
    )
    assert first == emitter.emit(permuted)
    _assert_balanced_c4_braces(first)

    if full_chain:
        assert "parallel {" not in first

    ancestors = _transitive_ancestors(trace.events)
    for group in _extract_parallel_groups(first):
        for i, lhs in enumerate(group):
            for rhs in group[i + 1 :]:
                assert f"ov_{lhs}" not in ancestors[f"ov_{rhs}"]
                assert f"ov_{rhs}" not in ancestors[f"ov_{lhs}"]

    for ev in trace.events.values():
        for pred_id in ev.causal_links:
            assert first.index(trace.events[pred_id].signature) < first.index(ev.signature)


@settings(max_examples=50)
@given(
    base_start=st.integers(min_value=1, max_value=400),
    offsets=st.lists(st.integers(min_value=0, max_value=25), min_size=2, max_size=7),
    durations=st.lists(st.integers(min_value=40, max_value=250), min_size=2, max_size=7),
)
def test_different_clock_domains_never_group_into_parallel(
    base_start: int,
    offsets: list[int],
    durations: list[int],
) -> None:
    """Overlapping siblings on distinct clock_domain_id values never emit a parallel block."""
    count = min(len(offsets), len(durations))
    stamp = ProvenanceStamp(grade=ProvenanceGrade.OBSERVED, extractor="otel", input_sha256=_SHA256)
    participants = _make_trace_participants(count + 1)
    events: dict[str, TraceEvent] = {
        "root": TraceEvent(
            event_id="root",
            caller_id="p_0",
            callee_id="p_0",
            signature="dispatch()",
            clock_domain_id="host_root",
            start_ns=0,
            end_ns=2_000,
            provenance=stamp,
        )
    }
    for idx in range(count):
        start_ns = base_start + offsets[idx]
        events[f"evt_{idx}"] = TraceEvent(
            event_id=f"evt_{idx}",
            caller_id="p_0",
            callee_id=f"p_{idx + 1}",
            signature=f"remote_call_{idx}()",
            parent_event_id="root",
            clock_domain_id=f"host_{idx}",
            start_ns=start_ns,
            end_ns=start_ns + durations[idx],
            sync_kind=SyncKind.FORK_JOIN_BRANCH,
            provenance=stamp,
        )

    trace = InteractionTraceIR(trace_id="clock_iso", participants=participants, events=events)
    permuted = InteractionTraceIR(
        trace_id=trace.trace_id,
        participants=dict(reversed(list(trace.participants.items()))),
        events=dict(reversed(list(trace.events.items()))),
    )
    emitter = LikeC4Emitter()
    out = emitter.emit(trace)
    assert out == emitter.emit(permuted)
    assert "parallel {" not in out
    _assert_balanced_c4_braces(out)
