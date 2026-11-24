"""Two-Profile Canonical IR v1 models, provenance invariants, and JSON Schema export."""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from archigram.domain import ProvenanceGrade, ProvenanceStamp


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        msg = "Identifier or text field must not be blank"
        raise ValueError(msg)
    return value


def _is_non_blank(value: str | None) -> bool:
    return value is not None and bool(value.strip())


NonBlankStr = Annotated[str, AfterValidator(_validate_non_blank)]


def _validate_line_span(line_span: tuple[int, int] | None) -> None:
    if line_span is None:
        return
    start_line, end_line = line_span
    if start_line < 1 or end_line < start_line:
        msg = f"Invalid line_span {line_span!r}: expected 1 <= start <= end"
        raise ValueError(msg)


def validate_provenance_stamp(stamp: ProvenanceStamp) -> ProvenanceStamp:
    """Enforce epistemic grade-specific audit requirements on a ProvenanceStamp."""
    _validate_line_span(stamp.line_span)

    if stamp.grade is ProvenanceGrade.OBSERVED:
        if not (_is_non_blank(stamp.extractor) and _is_non_blank(stamp.input_sha256)):
            msg = "OBSERVED provenance requires non-blank 'extractor' and 'input_sha256'"
            raise ValueError(msg)
    elif stamp.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
        has_valid_backing = bool(stamp.backing_ids) and all(
            _is_non_blank(item) for item in stamp.backing_ids
        )
        if not (_is_non_blank(stamp.rule_id) and has_valid_backing):
            msg = (
                "DERIVED_DETERMINISTIC provenance requires non-blank 'rule_id' "
                "and non-empty 'backing_ids'"
            )
            raise ValueError(msg)
    elif stamp.grade is ProvenanceGrade.DECLARED:
        if not (_is_non_blank(stamp.file_path) or _is_non_blank(stamp.extractor)):
            msg = "DECLARED provenance requires non-blank 'file_path' or 'extractor'"
            raise ValueError(msg)
    elif stamp.grade is ProvenanceGrade.INFERRED and not (
        _is_non_blank(stamp.rule_id) or _is_non_blank(stamp.extractor)
    ):
        msg = "INFERRED provenance requires non-blank 'rule_id' or 'extractor'"
        raise ValueError(msg)

    return stamp


ValidatedProvenance = Annotated[ProvenanceStamp, AfterValidator(validate_provenance_stamp)]


class NodeKind(StrEnum):
    """Canonical node classification shared by structural graphs and trace participants."""

    SERVICE = "service"
    MODULE = "module"
    PACKAGE = "package"
    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    FUNCTION = "function"
    TABLE = "table"
    DATABASE = "database"
    QUEUE = "queue"
    CONTAINER = "container"
    COMPONENT = "component"
    EXTERNAL = "external"
    SUPERNODE = "supernode"


class Visibility(StrEnum):
    """UML member visibility markers for OOP class compartments."""

    PUBLIC = "+"
    PRIVATE = "-"
    PROTECTED = "#"
    PACKAGE = "~"


class EdgeRelation(StrEnum):
    """Typed semantic edge relations for structural and OOP class graphs."""

    IMPORTS = "imports"
    CALLS = "calls"
    WRITES_DATA = "writes_data"
    READS_DATA = "reads_data"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    COMPOSES = "composes"
    AGGREGATES = "aggregates"
    ASSOCIATES = "associates"
    ASYNC_SPAWN = "async_spawn"
    FORK_JOIN = "fork_join"


class SyncKind(StrEnum):
    """Execution synchrony semantics for edges and trace events."""

    SYNC = "sync"
    ASYNC_FIRE_FORGET = "async_fire_forget"
    FORK_JOIN_BRANCH = "fork_join_branch"


class EventStatus(StrEnum):
    """Completion status for interaction trace events/spans."""

    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


class ClassMember(BaseModel):
    """Optional OOP class compartment attribute or method entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonBlankStr
    visibility: Visibility = Visibility.PUBLIC
    signature: str = ""
    is_method: bool = False
    modifiers: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    generic_params: tuple[NonBlankStr, ...] = Field(default_factory=tuple)


class IRNode(BaseModel):
    """Immutable structural/class node keyed by symbol_id with ELK containment support."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol_id: NonBlankStr
    label: NonBlankStr
    kind: NodeKind
    parent_id: NonBlankStr | None = None
    members: tuple[ClassMember, ...] = Field(default_factory=tuple)
    attributes: dict[str, str] = Field(default_factory=dict)
    provenance: ValidatedProvenance

    @model_validator(mode="after")
    def _reject_self_parent(self) -> IRNode:
        if self.parent_id == self.symbol_id:
            msg = f"Node '{self.symbol_id}' cannot reference itself as parent_id"
            raise ValueError(msg)
        return self


class IREdge(BaseModel):
    """Typed weighted structural/concurrency edge with mandatory provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: NonBlankStr
    source: NonBlankStr
    target: NonBlankStr
    relation: EdgeRelation
    sync_kind: SyncKind = SyncKind.SYNC
    weight: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)
    parallel_region_id: NonBlankStr | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    provenance: ValidatedProvenance


def _validate_containment_forest(nodes: dict[str, IRNode]) -> None:
    """Verify every parent_id exists in nodes and forms an acyclic containment forest."""
    state: dict[str, int] = {}
    for node_id in nodes:
        if state.get(node_id, 0) != 0:
            continue
        chain: list[str] = []
        current: str | None = node_id
        while current is not None and state.get(current, 0) == 0:
            state[current] = 1
            chain.append(current)
            parent = nodes[current].parent_id
            if parent is not None and parent not in nodes:
                msg = f"Node '{current}' references unknown parent_id '{parent}'"
                raise ValueError(msg)
            current = parent
        if current is not None and state.get(current) == 1:
            msg = f"Hierarchical containment cycle detected involving node '{current}'"
            raise ValueError(msg)
        for visited in chain:
            state[visited] = 2


class StructuralGraphIR(BaseModel):
    """Profile 1 Canonical IR: hierarchical JGF v2 structural & OOP class graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    ir_mode: Literal["structural_graph"] = "structural_graph"
    graph_id: NonBlankStr = "default"
    nodes: dict[str, IRNode] = Field(default_factory=dict)
    edges: tuple[IREdge, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_referential_integrity(self) -> StructuralGraphIR:
        for key, node in self.nodes.items():
            if key != node.symbol_id:
                msg = f"Node map key '{key}' does not match symbol_id '{node.symbol_id}'"
                raise ValueError(msg)

        _validate_containment_forest(self.nodes)

        seen_edges: set[str] = set()
        for edge in self.edges:
            if edge.edge_id in seen_edges:
                msg = f"Duplicate edge_id '{edge.edge_id}'"
                raise ValueError(msg)
            seen_edges.add(edge.edge_id)
            if edge.source not in self.nodes:
                msg = f"Edge '{edge.edge_id}' references unknown source '{edge.source}'"
                raise ValueError(msg)
            if edge.target not in self.nodes:
                msg = f"Edge '{edge.edge_id}' references unknown target '{edge.target}'"
                raise ValueError(msg)

        return self


class TraceParticipant(BaseModel):
    """Actor/lifeline participating in an interaction trace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    participant_id: NonBlankStr
    symbol_id: NonBlankStr
    label: NonBlankStr
    kind: NodeKind = NodeKind.SERVICE
    provenance: ValidatedProvenance


class TraceEvent(BaseModel):
    """Causal DAG event/span with isolated clock domain and mandatory provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: NonBlankStr
    caller_id: NonBlankStr
    callee_id: NonBlankStr
    signature: NonBlankStr
    parent_event_id: NonBlankStr | None = None
    causal_links: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    clock_domain_id: NonBlankStr
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    sync_kind: SyncKind = SyncKind.SYNC
    status: EventStatus = EventStatus.UNSET
    attributes: dict[str, str] = Field(default_factory=dict)
    provenance: ValidatedProvenance

    @model_validator(mode="after")
    def _validate_event_invariants(self) -> TraceEvent:
        if self.end_ns < self.start_ns:
            msg = (
                f"Event '{self.event_id}' has end_ns ({self.end_ns}) "
                f"earlier than start_ns ({self.start_ns})"
            )
            raise ValueError(msg)
        if self.parent_event_id == self.event_id:
            msg = f"Event '{self.event_id}' cannot reference itself as parent_event_id"
            raise ValueError(msg)
        if self.event_id in self.causal_links:
            msg = f"Event '{self.event_id}' cannot reference itself in causal_links"
            raise ValueError(msg)
        return self


def _collect_event_predecessors(
    event: TraceEvent,
    events: dict[str, TraceEvent],
) -> set[str]:
    predecessors: set[str] = set()
    if event.parent_event_id is not None:
        if event.parent_event_id not in events:
            msg = f"Event '{event.event_id}' references unknown parent '{event.parent_event_id}'"
            raise ValueError(msg)
        predecessors.add(event.parent_event_id)

    for link_id in event.causal_links:
        if link_id not in events:
            msg = f"Event '{event.event_id}' references unknown causal_link '{link_id}'"
            raise ValueError(msg)
        predecessors.add(link_id)

    return predecessors


def _validate_causal_dag(events: dict[str, TraceEvent]) -> None:
    """Verify causal references exist and form a strict DAG via Kahn's algorithm."""
    dependents: dict[str, list[str]] = {event_id: [] for event_id in events}
    indegree: dict[str, int] = dict.fromkeys(events, 0)

    for event_id, event in events.items():
        predecessors = _collect_event_predecessors(event, events)
        indegree[event_id] = len(predecessors)
        for pred_id in predecessors:
            dependents[pred_id].append(event_id)

    ready: deque[str] = deque(eid for eid, deg in indegree.items() if deg == 0)
    visited = 0
    while ready:
        current = ready.popleft()
        visited += 1
        for child in dependents[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if visited != len(events):
        msg = "Causal cycle detected in InteractionTraceIR events"
        raise ValueError(msg)


class InteractionTraceIR(BaseModel):
    """Profile 2 Canonical IR: causal DAG of concurrent events/spans across clock domains."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    ir_mode: Literal["interaction_trace"] = "interaction_trace"
    trace_id: NonBlankStr = "default"
    participants: dict[str, TraceParticipant] = Field(default_factory=dict)
    events: dict[str, TraceEvent] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_trace_integrity(self) -> InteractionTraceIR:
        for key, participant in self.participants.items():
            if key != participant.participant_id:
                msg = (
                    f"Participant map key '{key}' does not match "
                    f"participant_id '{participant.participant_id}'"
                )
                raise ValueError(msg)

        for key, event in self.events.items():
            if key != event.event_id:
                msg = f"Event map key '{key}' does not match event_id '{event.event_id}'"
                raise ValueError(msg)
            if event.caller_id not in self.participants:
                msg = f"Event '{key}' references unknown caller_id '{event.caller_id}'"
                raise ValueError(msg)
            if event.callee_id not in self.participants:
                msg = f"Event '{key}' references unknown callee_id '{event.callee_id}'"
                raise ValueError(msg)

        _validate_causal_dag(self.events)
        return self


CanonicalIR = Annotated[
    StructuralGraphIR | InteractionTraceIR,
    Field(discriminator="ir_mode"),
]


def export_canonical_ir_json_schemas() -> dict[str, dict[str, object]]:
    """Return strict Pydantic v2 JSON Schemas for both Canonical IR v1 profiles."""
    return {
        "structural_graph": StructuralGraphIR.model_json_schema(),
        "interaction_trace": InteractionTraceIR.model_json_schema(),
    }
