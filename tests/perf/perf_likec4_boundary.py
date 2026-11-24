"""Performance benchmark for LikeC4 boundary strategy classification throughput."""

from __future__ import annotations

from typing import Any

from archigram.domain.likec4_boundary import (
    CurationCapabilityProfile,
    CurationRoutingDecision,
    DiagramTargetKind,
    route_curation_strategy,
)

_TARGETS: tuple[DiagramTargetKind, ...] = tuple(DiagramTargetKind)


def _build_profile_batch(batch_size: int = 500) -> list[CurationCapabilityProfile]:
    profiles: list[CurationCapabilityProfile] = []
    for idx in range(batch_size):
        profiles.append(
            CurationCapabilityProfile(
                view_id=f"view_{idx}",
                target_kind=_TARGETS[idx % len(_TARGETS)],
                hierarchical_rollup=(idx % 2 == 0),
                tag_metadata_filter=(idx % 3 == 0),
                interactive_drill_down=(idx % 5 == 0),
                explicit_dynamic_flow=(idx % 7 == 0),
                prune_leaves=(idx % 11 == 0),
                bypass_vertices=(idx % 4 == 0),
                collapse_partition=(idx % 6 == 0),
                k_hop_slice=(idx % 8 == 0),
                transitive_reduction=(idx % 9 == 0),
                preserve_reachability=(idx % 4 == 1),
                forbid_new_cycles=(idx % 6 == 1),
                max_fanout_product_guard=(idx % 10 == 0),
                preserve_partial_order=(idx % 12 == 0),
                oop_compartments=(idx % 13 == 0),
                trace_compaction=(idx % 15 == 0),
                export_sequence_fragments_to_text_dsl=(idx % 14 == 0),
                require_likec4_output=(idx % 17 != 0),
            )
        )
    return profiles


def _classify_batch(profiles: list[CurationCapabilityProfile]) -> list[CurationRoutingDecision]:
    return [route_curation_strategy(profile) for profile in profiles]


def test_likec4_boundary_routing_500_views_throughput(benchmark: Any) -> None:
    profiles = _build_profile_batch(500)
    decisions: list[CurationRoutingDecision] = benchmark(_classify_batch, profiles)
    assert len(decisions) == 500
    assert all(d.view_id == p.view_id for d, p in zip(decisions, profiles, strict=True))
