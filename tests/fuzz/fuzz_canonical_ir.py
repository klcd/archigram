"""Hypothesis property-based fuzz suite for Two-Profile Canonical IR v1 invariants."""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    CanonicalIR,
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
    export_canonical_ir_json_schemas,
    validate_provenance_stamp,
)
from archigram.ports.contracts import (
    DiagramEmitterPort,
    StructuralIngestorPort,
    SyntaxValidationResult,
    SyntaxValidatorPort,
    TraceIngestorPort,
)

_ID_ALPHABET = string.ascii_lowercase + string.digits + "_-:"
_SAFE_TEXT = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=16)
_SHA256_STRAT = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_CANONICAL_ADAPTER: TypeAdapter[CanonicalIR] = TypeAdapter(CanonicalIR)


@st.composite
def valid_provenance_strategy(draw: st.DrawFn) -> ProvenanceStamp:
    """Generate a ProvenanceStamp satisfying grade-specific epistemic invariants."""
    grade = draw(st.sampled_from(list(ProvenanceGrade)))
    use_span = draw(st.booleans())
    line_span: tuple[int, int] | None = None
    if use_span:
        start = draw(st.integers(min_value=1, max_value=5000))
        line_span = (start, start + draw(st.integers(min_value=0, max_value=200)))

    if grade is ProvenanceGrade.OBSERVED:
        return ProvenanceStamp(
            grade=grade,
            extractor=draw(_SAFE_TEXT),
            input_sha256=draw(_SHA256_STRAT),
            line_span=line_span,
        )
    if grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        backing = tuple(draw(st.lists(_SAFE_TEXT, min_size=1, max_size=4)))
        return ProvenanceStamp(
            grade=grade,
            rule_id=draw(_SAFE_TEXT),
            backing_ids=backing,
            line_span=line_span,
        )
    if grade is ProvenanceGrade.DECLARED:
        use_file = draw(st.booleans())
        return ProvenanceStamp(
            grade=grade,
            file_path=draw(_SAFE_TEXT) if use_file else None,
            extractor=None if use_file else draw(_SAFE_TEXT),
            line_span=line_span,
        )
    use_rule = draw(st.booleans())
    return ProvenanceStamp(
        grade=grade,
        rule_id=draw(_SAFE_TEXT) if use_rule else None,
        extractor=None if use_rule else draw(_SAFE_TEXT),
        line_span=line_span,
    )


@st.composite
def class_member_strategy(draw: st.DrawFn) -> ClassMember:
    """Generate an OOP class compartment attribute or method."""
    return ClassMember(
        name=draw(_SAFE_TEXT),
        visibility=draw(st.sampled_from(list(Visibility))),
        signature=draw(st.text(alphabet=_ID_ALPHABET + "()-> ,", max_size=24)),
        is_method=draw(st.booleans()),
        modifiers=tuple(draw(st.lists(_SAFE_TEXT, max_size=2))),
        generic_params=tuple(draw(st.lists(_SAFE_TEXT, max_size=2))),
    )


@st.composite
def valid_structural_graph_strategy(draw: st.DrawFn) -> StructuralGraphIR:
    """Generate a valid hierarchical StructuralGraphIR with acyclic containment and typed edges."""
    node_count = draw(st.integers(min_value=1, max_value=12))
    node_ids = [f"sym_{idx}" for idx in range(node_count)]
    nodes: dict[str, IRNode] = {}

    for idx, sym_id in enumerate(node_ids):
        parent_id: str | None = None
        if idx > 0 and draw(st.booleans()):
            parent_id = node_ids[draw(st.integers(min_value=0, max_value=idx - 1))]
        kind = draw(st.sampled_from(list(NodeKind)))
        members = (
            tuple(draw(st.lists(class_member_strategy(), max_size=3)))
            if kind in {NodeKind.CLASS, NodeKind.INTERFACE}
            else ()
        )
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=draw(_SAFE_TEXT),
            kind=kind,
            parent_id=parent_id,
            members=members,
            attributes=draw(st.dictionaries(_SAFE_TEXT, _SAFE_TEXT, max_size=3)),
            provenance=draw(valid_provenance_strategy()),
        )

    edge_count = draw(st.integers(min_value=0, max_value=16))
    edges: list[IREdge] = []
    for e_idx in range(edge_count):
        edges.append(
            IREdge(
                edge_id=f"edge_{e_idx}",
                source=draw(st.sampled_from(node_ids)),
                target=draw(st.sampled_from(node_ids)),
                relation=draw(st.sampled_from(list(EdgeRelation))),
                sync_kind=draw(st.sampled_from(list(SyncKind))),
                weight=draw(st.floats(min_value=0.01, max_value=1000.0, allow_nan=False)),
                parallel_region_id=draw(st.one_of(st.none(), _SAFE_TEXT)),
                provenance=draw(valid_provenance_strategy()),
            )
        )

    return StructuralGraphIR(
        graph_id=draw(_SAFE_TEXT),
        nodes=nodes,
        edges=tuple(edges),
        metadata=draw(st.dictionaries(_SAFE_TEXT, _SAFE_TEXT, max_size=3)),
    )


@st.composite
def valid_interaction_trace_strategy(draw: st.DrawFn) -> InteractionTraceIR:
    """Generate a valid causal DAG InteractionTraceIR across multiple clock domains."""
    p_count = draw(st.integers(min_value=1, max_value=8))
    p_ids = [f"p_{idx}" for idx in range(p_count)]
    participants = {
        pid: TraceParticipant(
            participant_id=pid,
            symbol_id=f"sym_{pid}",
            label=draw(_SAFE_TEXT),
            kind=draw(st.sampled_from(list(NodeKind))),
            provenance=draw(valid_provenance_strategy()),
        )
        for pid in p_ids
    }

    e_count = draw(st.integers(min_value=1, max_value=16))
    e_ids = [f"evt_{idx}" for idx in range(e_count)]
    events: dict[str, TraceEvent] = {}

    for idx, eid in enumerate(e_ids):
        earlier = e_ids[:idx]
        parent_id: str | None = None
        causal_links: tuple[str, ...] = ()
        if earlier:
            if draw(st.booleans()):
                parent_id = draw(st.sampled_from(earlier))
            causal_links = tuple(draw(st.lists(st.sampled_from(earlier), max_size=3, unique=True)))
        start_ns = draw(st.integers(min_value=0, max_value=10**12))
        duration_ns = draw(st.integers(min_value=0, max_value=10**8))
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id=draw(st.sampled_from(p_ids)),
            callee_id=draw(st.sampled_from(p_ids)),
            signature=draw(_SAFE_TEXT),
            parent_event_id=parent_id,
            causal_links=causal_links,
            clock_domain_id=f"host_{draw(st.integers(min_value=0, max_value=3))}",
            start_ns=start_ns,
            end_ns=start_ns + duration_ns,
            sync_kind=draw(st.sampled_from(list(SyncKind))),
            status=draw(st.sampled_from(list(EventStatus))),
            provenance=draw(valid_provenance_strategy()),
        )

    return InteractionTraceIR(
        trace_id=draw(_SAFE_TEXT),
        participants=participants,
        events=events,
        metadata=draw(st.dictionaries(_SAFE_TEXT, _SAFE_TEXT, max_size=3)),
    )


@settings(max_examples=40)
@given(graph=valid_structural_graph_strategy())
def test_structural_graph_json_roundtrip_determinism(graph: StructuralGraphIR) -> None:
    dumped_once = graph.model_dump_json()
    restored = StructuralGraphIR.model_validate_json(dumped_once)
    dumped_twice = restored.model_dump_json()

    assert restored == graph
    assert dumped_once == dumped_twice
    assert _CANONICAL_ADAPTER.validate_json(dumped_once) == graph


@settings(max_examples=40)
@given(trace=valid_interaction_trace_strategy())
def test_interaction_trace_json_roundtrip_determinism(trace: InteractionTraceIR) -> None:
    dumped_once = trace.model_dump_json()
    restored = InteractionTraceIR.model_validate_json(dumped_once)
    dumped_twice = restored.model_dump_json()

    assert restored == trace
    assert dumped_once == dumped_twice
    assert _CANONICAL_ADAPTER.validate_json(dumped_once) == trace


@settings(max_examples=50)
@given(
    grade=st.sampled_from(list(ProvenanceGrade)),
    blank_text=st.sampled_from(["", "   ", "\t\n"]),
)
def test_adversarial_provenance_grade_invariants_rejected(
    grade: ProvenanceGrade,
    blank_text: str,
) -> None:
    if grade is ProvenanceGrade.OBSERVED:
        bad_stamp = ProvenanceStamp(grade=grade, extractor=blank_text, input_sha256="abc123")
    elif grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        bad_stamp = ProvenanceStamp(grade=grade, rule_id="rule_1", backing_ids=())
    elif grade is ProvenanceGrade.DECLARED:
        bad_stamp = ProvenanceStamp(grade=grade, file_path=blank_text, extractor=None)
    else:
        bad_stamp = ProvenanceStamp(grade=grade, rule_id=None, extractor=blank_text)

    with pytest.raises(ValueError):
        validate_provenance_stamp(bad_stamp)

    with pytest.raises(ValidationError):
        IRNode(symbol_id="n0", label="Node", kind=NodeKind.SERVICE, provenance=bad_stamp)


@settings(max_examples=40)
@given(
    start_line=st.integers(min_value=2, max_value=10_000),
    delta=st.integers(min_value=1, max_value=500),
)
def test_invalid_provenance_line_span_rejected(start_line: int, delta: int) -> None:
    inverted_span = (start_line, start_line - delta)
    stamp = ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="scip",
        input_sha256="a" * 64,
        line_span=inverted_span,
    )
    with pytest.raises(ValueError, match="line_span"):
        validate_provenance_stamp(stamp)


@settings(max_examples=40)
@given(
    cycle_len=st.integers(min_value=2, max_value=10),
    stamp=valid_provenance_strategy(),
)
def test_containment_cycle_rejected(cycle_len: int, stamp: ProvenanceStamp) -> None:
    nodes: dict[str, IRNode] = {}
    for idx in range(cycle_len):
        sym_id = f"n_{idx}"
        parent_id = f"n_{(idx + 1) % cycle_len}"
        nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=sym_id,
            kind=NodeKind.PACKAGE,
            parent_id=parent_id,
            provenance=stamp,
        )

    with pytest.raises(ValidationError, match="containment cycle"):
        StructuralGraphIR(nodes=nodes)


@settings(max_examples=40)
@given(
    graph=valid_structural_graph_strategy(),
    missing_endpoint=st.sampled_from(["source", "target", "parent"]),
    ghost_id=st.text(alphabet=string.ascii_lowercase, min_size=4, max_size=12),
    stamp=valid_provenance_strategy(),
)
def test_dangling_references_in_structural_graph_rejected(
    graph: StructuralGraphIR,
    missing_endpoint: str,
    ghost_id: str,
    stamp: ProvenanceStamp,
) -> None:
    dangling = f"ghost_{ghost_id}"
    existing = next(iter(graph.nodes))

    if missing_endpoint == "parent":
        bad_nodes = dict(graph.nodes)
        bad_nodes["orphan_node"] = IRNode(
            symbol_id="orphan_node",
            label="Orphan",
            kind=NodeKind.MODULE,
            parent_id=dangling,
            provenance=stamp,
        )
        with pytest.raises(ValidationError, match="unknown parent_id"):
            StructuralGraphIR(nodes=bad_nodes, edges=graph.edges)
        return

    bad_edge = IREdge(
        edge_id="dangling_edge",
        source=dangling if missing_endpoint == "source" else existing,
        target=dangling if missing_endpoint == "target" else existing,
        relation=EdgeRelation.CALLS,
        provenance=stamp,
    )
    with pytest.raises(ValidationError, match="unknown"):
        StructuralGraphIR(nodes=graph.nodes, edges=(*graph.edges, bad_edge))


@settings(max_examples=40)
@given(
    cycle_len=st.integers(min_value=2, max_value=10),
    use_causal_link=st.booleans(),
    stamp=valid_provenance_strategy(),
)
def test_causal_cycle_in_interaction_trace_rejected(
    cycle_len: int,
    use_causal_link: bool,
    stamp: ProvenanceStamp,
) -> None:
    participants = {
        "p0": TraceParticipant(
            participant_id="p0",
            symbol_id="svc.a",
            label="Service A",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        ),
        "p1": TraceParticipant(
            participant_id="p1",
            symbol_id="svc.b",
            label="Service B",
            kind=NodeKind.SERVICE,
            provenance=stamp,
        ),
    }
    events: dict[str, TraceEvent] = {}
    for idx in range(cycle_len):
        eid = f"e_{idx}"
        prev_id = f"e_{(idx - 1) % cycle_len}"
        is_closing_edge = idx == 0 and use_causal_link
        events[eid] = TraceEvent(
            event_id=eid,
            caller_id="p0",
            callee_id="p1",
            signature="call()",
            parent_event_id=None if is_closing_edge else prev_id,
            causal_links=(prev_id,) if is_closing_edge else (),
            clock_domain_id="host_0",
            start_ns=100 * idx,
            end_ns=100 * idx + 50,
            provenance=stamp,
        )

    with pytest.raises(ValidationError, match="Causal cycle"):
        InteractionTraceIR(participants=participants, events=events)


@settings(max_examples=30)
@given(
    trace=valid_interaction_trace_strategy(),
    bad_ref_kind=st.sampled_from(["caller", "callee", "parent", "causal_link"]),
    stamp=valid_provenance_strategy(),
)
def test_dangling_references_in_interaction_trace_rejected(
    trace: InteractionTraceIR,
    bad_ref_kind: str,
    stamp: ProvenanceStamp,
) -> None:
    existing_p = next(iter(trace.participants))
    bad_event = TraceEvent(
        event_id="evt_dangling",
        caller_id="missing_p" if bad_ref_kind == "caller" else existing_p,
        callee_id="missing_p" if bad_ref_kind == "callee" else existing_p,
        signature="op()",
        parent_event_id="missing_evt" if bad_ref_kind == "parent" else None,
        causal_links=("missing_evt",) if bad_ref_kind == "causal_link" else (),
        clock_domain_id="host_0",
        start_ns=10,
        end_ns=20,
        provenance=stamp,
    )
    bad_events = {**trace.events, bad_event.event_id: bad_event}
    with pytest.raises(ValidationError, match="unknown"):
        InteractionTraceIR(participants=trace.participants, events=bad_events)


@settings(max_examples=20)
@given(profile_key=st.sampled_from(["structural_graph", "interaction_trace"]))
def test_exported_json_schemas_are_deterministic_and_strict(profile_key: str) -> None:
    schemas_a = export_canonical_ir_json_schemas()
    schemas_b = export_canonical_ir_json_schemas()

    assert json.dumps(schemas_a, sort_keys=True) == json.dumps(schemas_b, sort_keys=True)
    schema = schemas_a[profile_key]
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "schema_version" in properties
    assert "ir_mode" in properties


@settings(max_examples=40)
@given(
    non_positive_weight=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
    start_ns=st.integers(min_value=1, max_value=10**9),
    delta_ns=st.integers(min_value=1, max_value=10**6),
    stamp=valid_provenance_strategy(),
)
def test_adversarial_edge_and_event_field_invariants_rejected(
    non_positive_weight: float,
    start_ns: int,
    delta_ns: int,
    stamp: ProvenanceStamp,
) -> None:
    with pytest.raises(ValidationError):
        IREdge(
            edge_id="e0",
            source="n0",
            target="n1",
            relation=EdgeRelation.CALLS,
            weight=non_positive_weight,
            provenance=stamp,
        )

    with pytest.raises(ValidationError, match="parent_id"):
        IRNode(
            symbol_id="n0",
            label="Self",
            kind=NodeKind.MODULE,
            parent_id="n0",
            provenance=stamp,
        )

    with pytest.raises(ValidationError, match="earlier than start_ns"):
        TraceEvent(
            event_id="e0",
            caller_id="p0",
            callee_id="p1",
            signature="fn()",
            clock_domain_id="host_0",
            start_ns=start_ns,
            end_ns=max(0, start_ns - delta_ns) if start_ns >= delta_ns else 0,
            provenance=stamp,
        )

    with pytest.raises(ValidationError, match="causal_links"):
        TraceEvent(
            event_id="e0",
            caller_id="p0",
            callee_id="p1",
            signature="fn()",
            causal_links=("e0",),
            clock_domain_id="host_0",
            start_ns=start_ns,
            end_ns=start_ns + delta_ns,
            provenance=stamp,
        )


@settings(max_examples=15)
@given(
    graph=valid_structural_graph_strategy(),
    trace=valid_interaction_trace_strategy(),
    fmt=st.sampled_from(["mermaid", "d2", "plantuml", "likec4"]),
)
def test_port_protocols_runtime_checkable(
    graph: StructuralGraphIR,
    trace: InteractionTraceIR,
    fmt: str,
) -> None:
    class _StubStructuralIngestor:
        extractor_name = "stub_struct"

        def ingest(self, source: object) -> StructuralGraphIR:
            return graph

    class _StubTraceIngestor:
        extractor_name = "stub_trace"

        def ingest(self, source: object) -> InteractionTraceIR:
            return trace

    class _StubEmitter:
        target_format = fmt

        def emit(self, ir: CanonicalIR) -> str:
            return ir.model_dump_json()

    class _StubValidator:
        target_format = fmt

        def validate(self, diagram_source: str) -> SyntaxValidationResult:
            return SyntaxValidationResult(valid=bool(diagram_source), target_format=fmt)

    s_ingestor = _StubStructuralIngestor()
    t_ingestor = _StubTraceIngestor()
    emitter = _StubEmitter()
    validator = _StubValidator()

    assert isinstance(s_ingestor, StructuralIngestorPort)
    assert isinstance(t_ingestor, TraceIngestorPort)
    assert isinstance(emitter, DiagramEmitterPort)
    assert isinstance(validator, SyntaxValidatorPort)
    assert validator.validate(emitter.emit(s_ingestor.ingest("in"))).valid
    assert validator.validate(emitter.emit(t_ingestor.ingest("in"))).valid
