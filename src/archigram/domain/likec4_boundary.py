"""Pure domain classifier for LikeC4 native predicate coverage vs ArchiGram reduction."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DiagramTargetKind(StrEnum):
    """Canonical diagram projection target categories."""

    C4_ARCHITECTURE = "c4_architecture"
    DEPENDENCY_GRAPH = "dependency_graph"
    OOP_CLASS = "oop_class"
    DYNAMIC_SEQUENCE = "dynamic_sequence"


class CurationStrategyMode(StrEnum):
    """Routing outcome deciding native LikeC4 delegation vs ArchiGram pre-reduction."""

    NATIVE_LIKEC4_ONLY = "native_likec4_only"
    HYBRID_PRE_REDUCE_THEN_LIKEC4 = "hybrid_pre_reduce_then_likec4"
    ARCHIGRAM_NATIVE_EMITTER_REQUIRED = "archigram_native_emitter_required"


class CurationOperatorKind(StrEnum):
    """Closed vocabulary of curation operators and topological/trace invariants."""

    # Native LikeC4 predicate-delegable capabilities
    HIERARCHICAL_ROLLUP = "hierarchical_rollup"
    TAG_METADATA_FILTER = "tag_metadata_filter"
    INTERACTIVE_DRILL_DOWN = "interactive_drill_down"
    EXPLICIT_DYNAMIC_FLOW = "explicit_dynamic_flow"

    # ArchiGram structural & trace pre-reduction operators
    PRUNE_LEAVES = "prune_leaves"
    BYPASS_VERTICES = "bypass_vertices"
    COLLAPSE_PARTITION = "collapse_partition"
    K_HOP_SLICE = "k_hop_slice"
    TRANSITIVE_REDUCTION = "transitive_reduction"
    TRACE_COMPACTION = "trace_compaction"

    # ArchiGram Gate 3 topological & partial-order lint invariants
    PRESERVE_REACHABILITY = "preserve_reachability"
    FORBID_NEW_CYCLES = "forbid_new_cycles"
    MAX_FANOUT_PRODUCT_GUARD = "max_fanout_product_guard"
    PRESERVE_PARTIAL_ORDER = "preserve_partial_order"

    # Capabilities outside LikeC4's model/codegen representation
    OOP_COMPARTMENTS = "oop_compartments"
    EXPORT_SEQUENCE_FRAGMENTS = "export_sequence_fragments"


_NATIVE_OPERATORS: frozenset[CurationOperatorKind] = frozenset(
    {
        CurationOperatorKind.HIERARCHICAL_ROLLUP,
        CurationOperatorKind.TAG_METADATA_FILTER,
        CurationOperatorKind.INTERACTIVE_DRILL_DOWN,
        CurationOperatorKind.EXPLICIT_DYNAMIC_FLOW,
    }
)

_REDUCER_OPERATORS: frozenset[CurationOperatorKind] = frozenset(
    {
        CurationOperatorKind.PRUNE_LEAVES,
        CurationOperatorKind.BYPASS_VERTICES,
        CurationOperatorKind.COLLAPSE_PARTITION,
        CurationOperatorKind.K_HOP_SLICE,
        CurationOperatorKind.TRANSITIVE_REDUCTION,
        CurationOperatorKind.TRACE_COMPACTION,
    }
)

_GATE3_LINT_OPERATORS: frozenset[CurationOperatorKind] = frozenset(
    {
        CurationOperatorKind.PRESERVE_REACHABILITY,
        CurationOperatorKind.FORBID_NEW_CYCLES,
        CurationOperatorKind.MAX_FANOUT_PRODUCT_GUARD,
        CurationOperatorKind.PRESERVE_PARTIAL_ORDER,
    }
)

_UNSUPPORTED_IN_LIKEC4_OPERATORS: frozenset[CurationOperatorKind] = frozenset(
    {
        CurationOperatorKind.OOP_COMPARTMENTS,
        CurationOperatorKind.EXPORT_SEQUENCE_FRAGMENTS,
    }
)

_FIELD_TO_OPERATOR: tuple[tuple[str, CurationOperatorKind], ...] = (
    ("hierarchical_rollup", CurationOperatorKind.HIERARCHICAL_ROLLUP),
    ("tag_metadata_filter", CurationOperatorKind.TAG_METADATA_FILTER),
    ("interactive_drill_down", CurationOperatorKind.INTERACTIVE_DRILL_DOWN),
    ("explicit_dynamic_flow", CurationOperatorKind.EXPLICIT_DYNAMIC_FLOW),
    ("prune_leaves", CurationOperatorKind.PRUNE_LEAVES),
    ("bypass_vertices", CurationOperatorKind.BYPASS_VERTICES),
    ("collapse_partition", CurationOperatorKind.COLLAPSE_PARTITION),
    ("k_hop_slice", CurationOperatorKind.K_HOP_SLICE),
    ("transitive_reduction", CurationOperatorKind.TRANSITIVE_REDUCTION),
    ("trace_compaction", CurationOperatorKind.TRACE_COMPACTION),
    ("preserve_reachability", CurationOperatorKind.PRESERVE_REACHABILITY),
    ("forbid_new_cycles", CurationOperatorKind.FORBID_NEW_CYCLES),
    ("max_fanout_product_guard", CurationOperatorKind.MAX_FANOUT_PRODUCT_GUARD),
    ("preserve_partial_order", CurationOperatorKind.PRESERVE_PARTIAL_ORDER),
    ("oop_compartments", CurationOperatorKind.OOP_COMPARTMENTS),
    ("export_sequence_fragments_to_text_dsl", CurationOperatorKind.EXPORT_SEQUENCE_FRAGMENTS),
)


class CurationCapabilityProfile(BaseModel):
    """Immutable specification of requested curation operators for a single view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: str = Field(min_length=1)
    target_kind: DiagramTargetKind = DiagramTargetKind.C4_ARCHITECTURE
    hierarchical_rollup: bool = False
    tag_metadata_filter: bool = False
    interactive_drill_down: bool = False
    explicit_dynamic_flow: bool = False
    prune_leaves: bool = False
    bypass_vertices: bool = False
    collapse_partition: bool = False
    k_hop_slice: bool = False
    transitive_reduction: bool = False
    preserve_reachability: bool = False
    forbid_new_cycles: bool = False
    max_fanout_product_guard: bool = False
    preserve_partial_order: bool = False
    oop_compartments: bool = False
    trace_compaction: bool = False
    export_sequence_fragments_to_text_dsl: bool = False
    require_likec4_output: bool = True


class CurationRoutingDecision(BaseModel):
    """Deterministic classification of native LikeC4 coverage vs ArchiGram reduction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: str = Field(min_length=1)
    strategy: CurationStrategyMode
    native_likec4_operators: tuple[CurationOperatorKind, ...] = Field(default_factory=tuple)
    archigram_pre_reduce_operators: tuple[CurationOperatorKind, ...] = Field(default_factory=tuple)
    unsupported_in_likec4_operators: tuple[CurationOperatorKind, ...] = Field(default_factory=tuple)
    requires_archigram_reducer: bool
    requires_gate3_topology_lint: bool
    likec4_can_render_final_view: bool
    rationale: tuple[str, ...] = Field(default_factory=tuple)


def _extract_active_operators(
    profile: CurationCapabilityProfile,
) -> tuple[CurationOperatorKind, ...]:
    return tuple(op for field_name, op in _FIELD_TO_OPERATOR if getattr(profile, field_name))


def _has_likec4_representation_gap(
    profile: CurationCapabilityProfile,
    unsupported_ops: tuple[CurationOperatorKind, ...],
) -> bool:
    if profile.target_kind == DiagramTargetKind.OOP_CLASS:
        return True
    if CurationOperatorKind.OOP_COMPARTMENTS in unsupported_ops:
        return True
    is_dynamic = (
        profile.target_kind == DiagramTargetKind.DYNAMIC_SEQUENCE
        or profile.explicit_dynamic_flow
        or profile.trace_compaction
    )
    return CurationOperatorKind.EXPORT_SEQUENCE_FRAGMENTS in unsupported_ops and is_dynamic


def _build_rationale(
    profile: CurationCapabilityProfile,
    strategy: CurationStrategyMode,
    pre_reduce_ops: tuple[CurationOperatorKind, ...],
    unsupported_ops: tuple[CurationOperatorKind, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if strategy == CurationStrategyMode.NATIVE_LIKEC4_ONLY:
        reasons.append("All requested curation predicates map natively to LikeC4 DSL.")
        return tuple(reasons)
    if unsupported_ops or profile.target_kind == DiagramTargetKind.OOP_CLASS:
        reasons.append(
            "Target requires OOP compartments or sequence fragment codegen unsupported by LikeC4."
        )
    if pre_reduce_ops:
        reasons.append(
            "Topological reduction, Gate 3 invariants, or R1-R6 trace compaction "
            "must run before emission."
        )
    if not profile.require_likec4_output:
        reasons.append("Caller requested non-LikeC4 output target via ArchiGram native emitters.")
    return tuple(reasons)


def route_curation_strategy(profile: CurationCapabilityProfile) -> CurationRoutingDecision:
    """Classify whether a view profile can use native LikeC4 predicates or needs ArchiGram."""
    active_ops = _extract_active_operators(profile)
    native_ops = tuple(op for op in active_ops if op in _NATIVE_OPERATORS)
    pre_reduce_ops = tuple(
        op for op in active_ops if op in _REDUCER_OPERATORS or op in _GATE3_LINT_OPERATORS
    )
    unsupported_ops = tuple(op for op in active_ops if op in _UNSUPPORTED_IN_LIKEC4_OPERATORS)

    has_gap = _has_likec4_representation_gap(profile, unsupported_ops)
    requires_reducer = any(op in _REDUCER_OPERATORS for op in pre_reduce_ops)
    requires_gate3 = any(op in _GATE3_LINT_OPERATORS for op in pre_reduce_ops)

    if has_gap or not profile.require_likec4_output:
        strategy = CurationStrategyMode.ARCHIGRAM_NATIVE_EMITTER_REQUIRED
    elif pre_reduce_ops:
        strategy = CurationStrategyMode.HYBRID_PRE_REDUCE_THEN_LIKEC4
    else:
        strategy = CurationStrategyMode.NATIVE_LIKEC4_ONLY

    return CurationRoutingDecision(
        view_id=profile.view_id,
        strategy=strategy,
        native_likec4_operators=native_ops,
        archigram_pre_reduce_operators=pre_reduce_ops,
        unsupported_in_likec4_operators=unsupported_ops,
        requires_archigram_reducer=requires_reducer,
        requires_gate3_topology_lint=requires_gate3,
        likec4_can_render_final_view=not has_gap,
        rationale=_build_rationale(profile, strategy, pre_reduce_ops, unsupported_ops),
    )
