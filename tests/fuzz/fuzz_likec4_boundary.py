"""Hypothesis property-based fuzz suite for LikeC4 boundary routing invariants."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain.likec4_boundary import (
    CurationCapabilityProfile,
    CurationOperatorKind,
    CurationRoutingDecision,
    CurationStrategyMode,
    DiagramTargetKind,
    route_curation_strategy,
)

_STRATEGY_RANK: dict[CurationStrategyMode, int] = {
    CurationStrategyMode.NATIVE_LIKEC4_ONLY: 0,
    CurationStrategyMode.HYBRID_PRE_REDUCE_THEN_LIKEC4: 1,
    CurationStrategyMode.ARCHIGRAM_NATIVE_EMITTER_REQUIRED: 2,
}

_NON_NATIVE_FIELDS: tuple[str, ...] = (
    "prune_leaves",
    "bypass_vertices",
    "collapse_partition",
    "k_hop_slice",
    "transitive_reduction",
    "preserve_reachability",
    "forbid_new_cycles",
    "max_fanout_product_guard",
    "preserve_partial_order",
    "oop_compartments",
    "trace_compaction",
)

_ALL_BOOL_FIELDS: tuple[str, ...] = (
    "hierarchical_rollup",
    "tag_metadata_filter",
    "interactive_drill_down",
    "explicit_dynamic_flow",
    *_NON_NATIVE_FIELDS,
    "export_sequence_fragments_to_text_dsl",
)


@st.composite
def curation_profiles(draw: st.DrawFn) -> CurationCapabilityProfile:
    """Generate arbitrary CurationCapabilityProfile instances across all operator combinations."""
    view_id = draw(st.text(min_size=1, max_size=24))
    target_kind = draw(st.sampled_from(list(DiagramTargetKind)))
    flags = {field: draw(st.booleans()) for field in _ALL_BOOL_FIELDS}
    require_likec4_output = draw(st.booleans())
    return CurationCapabilityProfile(
        view_id=view_id,
        target_kind=target_kind,
        require_likec4_output=require_likec4_output,
        **flags,
    )


@settings(max_examples=120)
@given(profile=curation_profiles())
def test_non_native_operators_never_route_to_likec4_only(
    profile: CurationCapabilityProfile,
) -> None:
    decision = route_curation_strategy(profile)
    has_non_native = any(getattr(profile, field) for field in _NON_NATIVE_FIELDS)
    if has_non_native or profile.target_kind == DiagramTargetKind.OOP_CLASS:
        assert decision.strategy != CurationStrategyMode.NATIVE_LIKEC4_ONLY

    if profile.oop_compartments or profile.target_kind == DiagramTargetKind.OOP_CLASS:
        assert decision.strategy == CurationStrategyMode.ARCHIGRAM_NATIVE_EMITTER_REQUIRED
        assert not decision.likec4_can_render_final_view

    if (
        profile.bypass_vertices
        or profile.collapse_partition
        or profile.trace_compaction
        or profile.prune_leaves
        or profile.k_hop_slice
        or profile.transitive_reduction
    ):
        assert decision.requires_archigram_reducer

    if (
        profile.forbid_new_cycles
        or profile.preserve_reachability
        or profile.max_fanout_product_guard
        or profile.preserve_partial_order
    ):
        assert decision.requires_gate3_topology_lint


@settings(max_examples=80)
@given(
    view_id=st.text(min_size=1, max_size=20),
    target_kind=st.sampled_from(
        [
            DiagramTargetKind.C4_ARCHITECTURE,
            DiagramTargetKind.DEPENDENCY_GRAPH,
            DiagramTargetKind.DYNAMIC_SEQUENCE,
        ]
    ),
    hierarchical_rollup=st.booleans(),
    tag_metadata_filter=st.booleans(),
    interactive_drill_down=st.booleans(),
    explicit_dynamic_flow=st.booleans(),
)
def test_pure_native_profiles_route_to_native_likec4(
    view_id: str,
    target_kind: DiagramTargetKind,
    hierarchical_rollup: bool,
    tag_metadata_filter: bool,
    interactive_drill_down: bool,
    explicit_dynamic_flow: bool,
) -> None:
    profile = CurationCapabilityProfile(
        view_id=view_id,
        target_kind=target_kind,
        hierarchical_rollup=hierarchical_rollup,
        tag_metadata_filter=tag_metadata_filter,
        interactive_drill_down=interactive_drill_down,
        explicit_dynamic_flow=explicit_dynamic_flow,
        require_likec4_output=True,
    )
    decision = route_curation_strategy(profile)
    assert decision.strategy == CurationStrategyMode.NATIVE_LIKEC4_ONLY
    assert not decision.requires_archigram_reducer
    assert not decision.requires_gate3_topology_lint
    assert decision.likec4_can_render_final_view
    assert not decision.archigram_pre_reduce_operators
    assert not decision.unsupported_in_likec4_operators


@settings(max_examples=120)
@given(
    base=curation_profiles(),
    extra_flags=st.lists(st.sampled_from(_ALL_BOOL_FIELDS), max_size=8),
)
def test_routing_strategy_monotonicity_under_feature_addition(
    base: CurationCapabilityProfile,
    extra_flags: list[str],
) -> None:
    updates = dict.fromkeys(extra_flags, True)
    extended = base.model_copy(update=updates)
    base_decision = route_curation_strategy(base)
    ext_decision = route_curation_strategy(extended)

    assert _STRATEGY_RANK[ext_decision.strategy] >= _STRATEGY_RANK[base_decision.strategy]
    if base_decision.requires_archigram_reducer:
        assert ext_decision.requires_archigram_reducer
    if base_decision.requires_gate3_topology_lint:
        assert ext_decision.requires_gate3_topology_lint
    if not base_decision.likec4_can_render_final_view:
        assert not ext_decision.likec4_can_render_final_view


@settings(max_examples=100)
@given(profile=curation_profiles())
def test_operator_partition_is_disjoint_and_exhaustive(
    profile: CurationCapabilityProfile,
) -> None:
    decision = route_curation_strategy(profile)
    native_set = set(decision.native_likec4_operators)
    pre_set = set(decision.archigram_pre_reduce_operators)
    unsup_set = set(decision.unsupported_in_likec4_operators)

    assert len(native_set) == len(decision.native_likec4_operators)
    assert len(pre_set) == len(decision.archigram_pre_reduce_operators)
    assert len(unsup_set) == len(decision.unsupported_in_likec4_operators)

    assert native_set.isdisjoint(pre_set)
    assert native_set.isdisjoint(unsup_set)
    assert pre_set.isdisjoint(unsup_set)

    combined = native_set | pre_set | unsup_set
    assert combined <= set(CurationOperatorKind)
    assert decision.rationale


@settings(max_examples=80)
@given(profile=curation_profiles())
def test_routing_decision_deterministic_and_json_roundtrip(
    profile: CurationCapabilityProfile,
) -> None:
    first = route_curation_strategy(profile)
    second = route_curation_strategy(profile)
    assert first == second
    restored = CurationRoutingDecision.model_validate_json(first.model_dump_json())
    assert restored == first
