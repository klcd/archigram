"""Hypothesis fuzz suite: hierarchical glob parity across Gates 1-3, Reducer, and Triage."""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.glob import matches_any_glob, matches_glob
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    PruneLeavesRule,
    TargetRenderer,
    ViewInvariants,
)
from archigram.entrypoints.cli.triage import compute_triage_stages
from archigram.features.emit_d2.emitter import build_qualified_paths, build_safe_key_map
from archigram.features.lint_grounding import Gate1GroundingGate, GroundingAndBoundaryGate
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.reduce_structural import StructuralTopologicalReducer

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-curation-glob",
    input_sha256="b" * 64,
)

_SEG_STRAT = st.text(
    alphabet=string.ascii_lowercase,
    min_size=2,
    max_size=6,
)


def _node(symbol_id: str) -> IRNode:
    return IRNode(
        symbol_id=symbol_id,
        label=symbol_id,
        kind=NodeKind.SERVICE,
        provenance=_OBS_PROV,
    )


def _edge(edge_id: str, source: str, target: str) -> IREdge:
    return IREdge(
        edge_id=edge_id,
        source=source,
        target=target,
        relation=EdgeRelation.CALLS,
        provenance=_OBS_PROV,
    )


def _build_hierarchical_bridge_ir(
    style: str,
    prefix: str,
    mid_segments: list[str],
    target_suffix: str,
    decoy_suffix: str,
) -> tuple[StructuralGraphIR, str, tuple[str, ...], tuple[str, ...]]:
    sep = "." if style == "dot" else "/"
    root_a = f"{prefix}{sep}EntryA"
    root_b = f"{prefix}{sep}EntryB"
    sink = f"{prefix}{sep}Sink"

    # Depth 0, 1, and 2 matching symbols under hierarchical glob.
    matched: list[str] = [
        f"{prefix}{sep}{target_suffix}" if style == "dot" else target_suffix,
        f"{prefix}{sep}{mid_segments[0]}{sep}{target_suffix}",
        f"{prefix}{sep}{mid_segments[0]}{sep}{mid_segments[1]}{sep}{target_suffix}",
    ]

    decoys = (
        f"{prefix}{sep}{mid_segments[0]}{sep}{decoy_suffix}",
        f"other{sep}{mid_segments[1]}{sep}{target_suffix}"
        if style == "dot"
        else f"other{sep}{mid_segments[1]}{sep}{decoy_suffix}",
    )
    pattern = f"{prefix}.**.{target_suffix}" if style == "dot" else f"**/{target_suffix}"

    nodes = {sid: _node(sid) for sid in (root_a, root_b, sink, *matched, *decoys)}
    edges: list[IREdge] = []
    for idx, mid in enumerate((*matched, *decoys)):
        edges.append(_edge(f"in_a_{idx}", root_a, mid))
        edges.append(_edge(f"in_b_{idx}", root_b, mid))
        edges.append(_edge(f"out_{idx}", mid, sink))

    ir = StructuralGraphIR(graph_id="glob_parity", nodes=nodes, edges=tuple(edges))
    return ir, pattern, tuple(sorted(matched)), tuple(sorted(decoys))


@settings(max_examples=60, deadline=None)
@given(
    prefix=_SEG_STRAT,
    segments=st.lists(_SEG_STRAT, min_size=2, max_size=4),
    suffix=_SEG_STRAT,
    other_suffix=_SEG_STRAT,
)
def test_hierarchical_glob_semantics_and_any_helper(
    prefix: str,
    segments: list[str],
    suffix: str,
    other_suffix: str,
) -> None:
    """`matches_glob` and `matches_any_glob` honor `.**`, `**/`, and non-delimiter `*`."""
    if suffix == other_suffix or prefix == "other":
        return

    dot_zero = f"{prefix}.{suffix}"
    dot_deep = f"{prefix}.{'.'.join(segments)}.{suffix}"
    dot_other = f"{prefix}.{segments[0]}.{other_suffix}"
    dot_pat = f"{prefix}.**.{suffix}"

    assert matches_glob(dot_zero, dot_pat)
    assert matches_glob(dot_deep, dot_pat)
    assert not matches_glob(dot_other, dot_pat)
    assert not matches_glob(f"other.{segments[0]}.{suffix}", dot_pat)

    slash_zero = suffix
    slash_deep = f"{'/'.join(segments)}/{suffix}"
    slash_other = f"{segments[0]}/{other_suffix}"
    slash_pat = f"**/{suffix}"

    assert matches_glob(slash_zero, slash_pat)
    assert matches_glob(slash_deep, slash_pat)
    assert not matches_glob(slash_other, slash_pat)

    patterns = (dot_pat, slash_pat)
    for cand in (dot_zero, dot_deep, dot_other, slash_zero, slash_deep, slash_other):
        assert matches_any_glob(cand, patterns) == any(matches_glob(cand, p) for p in patterns)


@settings(max_examples=50, deadline=None)
@given(
    style=st.sampled_from(["dot", "slash"]),
    prefix=_SEG_STRAT.filter(lambda s: s != "other"),
    mids=st.lists(_SEG_STRAT, min_size=2, max_size=2, unique=True),
    target_suffix=_SEG_STRAT.filter(lambda s: s not in {"entry", "sink"}),
    decoy_suffix=_SEG_STRAT.filter(lambda s: s not in {"entry", "sink"}),
)
def test_bypass_glob_parity_across_gate1_reducer_gate3_and_triage(
    style: str,
    prefix: str,
    mids: list[str],
    target_suffix: str,
    decoy_suffix: str,
) -> None:
    """Gate 1, Reducer, Gate 3, and Triage agree identically on `pkg.**.X` / `**/X` bypass."""
    if target_suffix == decoy_suffix:
        return

    ir, pattern, expected_matched, decoys = _build_hierarchical_bridge_ir(
        style, prefix, mids, target_suffix, decoy_suffix
    )

    # 1. Gate 1 + Gate 2 accept the hierarchical glob.
    bypass_rule = BypassVerticesRule(id="bypass_targets", node_id_globs=(pattern,))
    view = DiagramViewSpec(
        id="v_bypass",
        title="Bypass View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.D2,
        rules=(bypass_rule,),
    )
    spec = CurationSpecV1(views=(view,))
    g12_report = GroundingAndBoundaryGate().check(ir, spec)
    assert g12_report.passed, g12_report.diagnostics

    # 2. StructuralTopologicalReducer removes/splices the exact matched set.
    curated = StructuralTopologicalReducer().reduce(ir, view)
    assert set(expected_matched).isdisjoint(curated.nodes)
    for decoy in decoys:
        assert decoy in curated.nodes

    # 3. Gate 3 biclique fanout check evaluates the exact same matched symbols (2*1 > 1).
    strict_bypass = BypassVerticesRule(
        id="bypass_targets",
        node_id_globs=(pattern,),
        max_fanout_product=1,
    )
    fanout_view = view.model_copy(update={"rules": (strict_bypass,)})
    fanout_report = TopologicalInvariantGate().check(ir, CurationSpecV1(views=(fanout_view,)))
    flagged = tuple(
        sorted(
            sym
            for diag in fanout_report.diagnostics
            if diag.code == "BICLIQUE_FANOUT_EXCEEDED"
            for sym in diag.offending_symbols
        )
    )
    assert flagged == expected_matched

    # 4. Triage classifies every matched symbol as BYPASS and decoys as KEEP.
    outcome = compute_triage_stages(ir, spec, "v_bypass")
    assert outcome.stats.bypassed_count == len(expected_matched)
    badges = _triage_badges(ir, outcome.stage2_d2)
    bypassed_in_triage = tuple(sorted(sid for sid, badge in badges.items() if badge == "BYPASS"))
    assert bypassed_in_triage == expected_matched
    for decoy in decoys:
        assert badges[decoy] == "KEEP"


def _triage_badges(ir: StructuralGraphIR, stage2_d2: str) -> dict[str, str]:
    key_map, _ = build_safe_key_map(ir.nodes.keys())
    paths = build_qualified_paths(ir.nodes, key_map)
    badges: dict[str, str] = {}
    for sid, path in paths.items():
        prefix = f'{path}.tooltip: "'
        for line in stage2_d2.splitlines():
            if line.startswith(prefix):
                badges[sid] = line[len(prefix) :].split("]", 1)[0].lstrip("[")
                break
    return badges


@settings(max_examples=50, deadline=None)
@given(
    style=st.sampled_from(["dot", "slash"]),
    prefix=_SEG_STRAT.filter(lambda s: s != "other"),
    mids=st.lists(_SEG_STRAT, min_size=2, max_size=2, unique=True),
    target_suffix=_SEG_STRAT,
    decoy_suffix=_SEG_STRAT,
)
def test_collapse_glob_parity_across_gate1_reducer_gate3_and_triage(
    style: str,
    prefix: str,
    mids: list[str],
    target_suffix: str,
    decoy_suffix: str,
) -> None:
    """Gate 1, Reducer, Gate 3 cohesion check, and Triage agree on hierarchical collapse globs."""
    if target_suffix == decoy_suffix:
        return

    sep = "." if style == "dot" else "/"
    matched = (
        f"{prefix}{sep}{target_suffix}" if style == "dot" else target_suffix,
        f"{prefix}{sep}{mids[0]}{sep}{target_suffix}",
        f"{prefix}{sep}{mids[0]}{sep}{mids[1]}{sep}{target_suffix}",
    )
    decoy = f"{prefix}{sep}{mids[0]}{sep}{decoy_suffix}"
    pattern = f"{prefix}.**.{target_suffix}" if style == "dot" else f"**/{target_suffix}"

    # Disconnected components so Gate 3 must detect DISCONNECTED_PARTITION_MERGE for matched nodes.
    nodes = {sid: _node(sid) for sid in (*matched, decoy)}
    ir = StructuralGraphIR(graph_id="collapse_parity", nodes=nodes, edges=())

    collapse_rule = CollapsePartitionRule(
        id="collapse_targets",
        target_node_id="super.cluster",
        label="Cluster",
        member_globs=(pattern,),
    )
    view = DiagramViewSpec(
        id="v_collapse",
        title="Collapse View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.D2,
        rules=(collapse_rule,),
        invariants=ViewInvariants(forbid_new_cycles=False),
    )
    spec = CurationSpecV1(views=(view,))

    # 1. Gate 1 grounds the hierarchical glob.
    g1_report = Gate1GroundingGate().check(ir, spec)
    assert g1_report.passed, g1_report.diagnostics

    # 2. Reducer collapses all matched symbols into super.cluster while keeping decoy.
    curated = StructuralTopologicalReducer().reduce(ir, view)
    assert "super.cluster" in curated.nodes
    assert decoy in curated.nodes
    assert set(matched).isdisjoint(curated.nodes)
    assert set(curated.nodes["super.cluster"].provenance.backing_ids) == set(matched)

    # 3. Gate 3 strict cohesion sees the exact same matched symbols across components.
    g3_report = TopologicalInvariantGate(strict_cohesion=True).check(
        ir, spec, curated_views={"v_collapse": curated}
    )
    merge_diags = [d for d in g3_report.diagnostics if d.code == "DISCONNECTED_PARTITION_MERGE"]
    assert len(merge_diags) == 1
    assert set(merge_diags[0].offending_symbols) == set(matched)

    # 4. Triage classifies all matched symbols as COLLAPSE and decoy as KEEP.
    outcome = compute_triage_stages(ir, spec, "v_collapse")
    assert outcome.stats.collapsed_count == len(matched)
    assert outcome.stats.kept_count == 1
    badges = _triage_badges(ir, outcome.stage2_d2)
    assert {sid for sid, badge in badges.items() if badge == "COLLAPSE"} == set(matched)
    assert badges[decoy] == "KEEP"


@settings(max_examples=40, deadline=None)
@given(
    style=st.sampled_from(["dot", "slash"]),
    prefix=_SEG_STRAT.filter(lambda s: s != "other"),
    mid=_SEG_STRAT,
    leaf_suffix=_SEG_STRAT.filter(lambda s: s != "root"),
)
def test_prune_leaves_glob_parity_across_gate1_gate2_reducer_and_triage(
    style: str,
    prefix: str,
    mid: str,
    leaf_suffix: str,
) -> None:
    """Gate 1, Gate 2, Reducer, and Triage agree on hierarchical `prune_leaves` globs."""
    sep = "." if style == "dot" else "/"
    root = f"{prefix}{sep}root"
    leaf_shallow = f"{prefix}{sep}{leaf_suffix}" if style == "dot" else leaf_suffix
    leaf_nested = f"{prefix}{sep}{mid}{sep}{leaf_suffix}"
    pattern = f"{prefix}.**.{leaf_suffix}" if style == "dot" else f"**/{leaf_suffix}"

    ir = StructuralGraphIR(
        graph_id="prune_parity",
        nodes={
            root: _node(root),
            leaf_shallow: _node(leaf_shallow),
            leaf_nested: _node(leaf_nested),
        },
        edges=(_edge("e1", root, leaf_shallow), _edge("e2", root, leaf_nested)),
    )
    prune_rule = PruneLeavesRule(id="prune_sinks", node_id_globs=(pattern,), direction="sink")
    view = DiagramViewSpec(
        id="v_prune",
        title="Prune View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.D2,
        rules=(prune_rule,),
    )
    spec = CurationSpecV1(views=(view,))

    assert GroundingAndBoundaryGate().check(ir, spec).passed
    curated = StructuralTopologicalReducer().reduce(ir, view)
    assert set(curated.nodes) == {root}

    outcome = compute_triage_stages(ir, spec, "v_prune")
    assert outcome.stats.removed_count == 2
    assert outcome.stats.kept_count == 1
    assert _triage_badges(ir, outcome.stage2_d2) == {
        root: "KEEP",
        leaf_shallow: "REMOVE",
        leaf_nested: "REMOVE",
    }
