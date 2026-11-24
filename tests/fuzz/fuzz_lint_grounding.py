"""Hypothesis property-based fuzz suite for Gates 1 & 2 (Grounding & Boundary/Leaf Safety)."""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.diagnostics import DiagnosticSeverity, GateId
from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    TargetRenderer,
    ViewInvariants,
)
from archigram.features.lint_grounding import (
    Gate1GroundingGate,
    Gate2BoundaryLeafGate,
    GroundingAndBoundaryGate,
    matches_glob,
)
from archigram.ports.contracts import LintGatePort

_OBS_PROV = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="fuzz-grounding",
    input_sha256="a" * 64,
)

_TOKEN_STRAT = st.text(
    alphabet=string.ascii_lowercase + string.digits,
    min_size=3,
    max_size=10,
).filter(lambda s: s[0].isalpha() and not s.isupper())


@dataclass(frozen=True, slots=True)
class LayeredGraphFixture:
    """Synthetic layered graph with guaranteed roots, internal bridges, and sinks."""

    ir: StructuralGraphIR
    roots: tuple[str, ...]
    bridges: tuple[str, ...]
    sinks: tuple[str, ...]


@st.composite
def layered_graph_strategy(draw: st.DrawFn) -> LayeredGraphFixture:
    """Generate a 3-layer StructuralGraphIR: roots -> bridges -> sinks."""
    root_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=3, unique=True))
    bridge_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=3, unique=True))
    sink_tokens = draw(st.lists(_TOKEN_STRAT, min_size=1, max_size=3, unique=True))

    roots = tuple(f"pkg.root.{tok}" for tok in root_tokens)
    bridges = tuple(f"pkg.bridge.{tok}" for tok in bridge_tokens)
    sinks = tuple(f"pkg.sink.{tok}" for tok in sink_tokens)

    nodes: dict[str, IRNode] = {}
    for sym in (*roots, *bridges, *sinks):
        nodes[sym] = IRNode(
            symbol_id=sym,
            label=sym.replace(".", "_"),
            kind=NodeKind.SERVICE,
            provenance=_OBS_PROV,
        )

    edges: list[IREdge] = []
    edge_idx = 0
    for idx, b_id in enumerate(bridges):
        r_id = roots[idx % len(roots)]
        s_id = sinks[idx % len(sinks)]
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=r_id,
                target=b_id,
                relation=EdgeRelation.CALLS,
                provenance=_OBS_PROV,
            )
        )
        edge_idx += 1
        edges.append(
            IREdge(
                edge_id=f"e_{edge_idx}",
                source=b_id,
                target=s_id,
                relation=EdgeRelation.CALLS,
                provenance=_OBS_PROV,
            )
        )
        edge_idx += 1

    for idx, r_id in enumerate(roots):
        if idx >= len(bridges):
            b_id = bridges[idx % len(bridges)]
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=r_id,
                    target=b_id,
                    relation=EdgeRelation.IMPORTS,
                    provenance=_OBS_PROV,
                )
            )
            edge_idx += 1

    for idx, s_id in enumerate(sinks):
        if idx >= len(bridges):
            b_id = bridges[idx % len(bridges)]
            edges.append(
                IREdge(
                    edge_id=f"e_{edge_idx}",
                    source=b_id,
                    target=s_id,
                    relation=EdgeRelation.WRITES_DATA,
                    provenance=_OBS_PROV,
                )
            )
            edge_idx += 1

    return LayeredGraphFixture(
        ir=StructuralGraphIR(graph_id="layered_fuzz", nodes=nodes, edges=tuple(edges)),
        roots=roots,
        bridges=bridges,
        sinks=sinks,
    )


def _mutate_symbol(symbol_id: str, mode: str) -> str:
    if mode == "case_flip":
        return symbol_id.upper()
    if mode == "suffix_typo":
        return f"{symbol_id}_typoz"
    return f"hallucinated.nonexistent.{symbol_id}"


@settings(max_examples=50, deadline=None)
@given(fixture=layered_graph_strategy())
def test_well_grounded_spec_passes_gates_1_and_2(fixture: LayeredGraphFixture) -> None:
    gate = GroundingAndBoundaryGate()
    g1 = Gate1GroundingGate()
    g2 = Gate2BoundaryLeafGate()
    assert isinstance(gate, LintGatePort)
    assert isinstance(g1, LintGatePort)
    assert isinstance(g2, LintGatePort)

    root0 = fixture.roots[0]
    bridge0 = fixture.bridges[0]
    sink0 = fixture.sinks[0]

    spec = CurationSpecV1(
        global_abstractions=(
            PruneLeavesRule(
                id="prune_sink_leaf",
                node_id_globs=(sink0,),
                direction="sink",
            ),
        ),
        views=(
            DiagramViewSpec(
                id="main_view",
                title="Main View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=(
                    CollapsePartitionRule(
                        id="collapse_bridges",
                        target_node_id="super.bridge_cluster",
                        label="Bridge Cluster",
                        member_globs=("pkg.bridge.**",),
                    ),
                ),
                scope=KHopNeighborhoodScope(
                    focal_nodes=(root0, "super.bridge_cluster"),
                    upstream_depth=2,
                    downstream_depth=2,
                ),
                invariants=ViewInvariants(
                    protected_nodes=(root0, bridge0, "super.bridge_cluster"),
                    preserve_reachability=(
                        ReachabilityAssertion(
                            from_node=root0,
                            to_node="super.bridge_cluster",
                            via_relations=("calls", "imports"),
                        ),
                    ),
                ),
            ),
        ),
    )

    report = gate.check(fixture.ir, spec)
    assert report.passed, f"Expected 0 errors, got: {report.diagnostics}"
    assert report.error_count == 0
    assert g1.check(fixture.ir, spec).passed
    assert g2.check(fixture.ir, spec).passed


@settings(max_examples=60, deadline=None)
@given(
    fixture=layered_graph_strategy(),
    mutation_mode=st.sampled_from(["case_flip", "suffix_typo", "hallucinated"]),
    target_slot=st.sampled_from(
        ["focal_nodes", "protected_nodes", "from_node", "to_node", "exclude_ids"]
    ),
)
def test_gate_1_rejects_mutated_symbol_references(
    fixture: LayeredGraphFixture,
    mutation_mode: str,
    target_slot: str,
) -> None:
    gate = GroundingAndBoundaryGate()
    base_sym = fixture.roots[0]
    bad_sym = _mutate_symbol(base_sym, mutation_mode)
    assert bad_sym not in fixture.ir.nodes

    focal = (bad_sym,) if target_slot == "focal_nodes" else (base_sym,)
    protected = (bad_sym,) if target_slot == "protected_nodes" else (base_sym,)
    from_n = bad_sym if target_slot == "from_node" else base_sym
    to_n = bad_sym if target_slot == "to_node" else fixture.bridges[0]
    excludes = (bad_sym,) if target_slot == "exclude_ids" else ()

    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="mutated_view",
                title="Mutated View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                rules=(
                    CollapsePartitionRule(
                        id="c_rule",
                        target_node_id="super.sink_group",
                        label="Sinks",
                        member_globs=("pkg.sink.*",),
                        exclude_ids=excludes,
                    ),
                ),
                scope=KHopNeighborhoodScope(focal_nodes=focal),
                invariants=ViewInvariants(
                    protected_nodes=protected,
                    preserve_reachability=(ReachabilityAssertion(from_node=from_n, to_node=to_n),),
                ),
            ),
        ),
    )

    report = gate.check(fixture.ir, spec)
    assert not report.passed
    g1_diags = report.by_gate(GateId.GATE_1_GROUNDING)
    assert len(g1_diags) >= 1
    matching = [d for d in g1_diags if bad_sym in d.offending_symbols]
    assert len(matching) == 1
    diag = matching[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.code == "UNKNOWN_SYMBOL_ID"
    if mutation_mode in {"case_flip", "suffix_typo"}:
        assert base_sym in diag.repair_hint


@settings(max_examples=50, deadline=None)
@given(
    fixture=layered_graph_strategy(),
    rule_kind=st.sampled_from(["prune_leaves", "bypass_vertices", "collapse_partition"]),
    is_global=st.booleans(),
    bad_glob=st.sampled_from(
        ["nonexistent.ns.*", "pkg.Ghost.*", "unmatched.**.leaf", "missing_prefix_*"]
    ),
)
def test_gate_1_rejects_zero_match_globs(
    fixture: LayeredGraphFixture,
    rule_kind: str,
    is_global: bool,
    bad_glob: str,
) -> None:
    gate = GroundingAndBoundaryGate()
    if rule_kind == "prune_leaves":
        rule = PruneLeavesRule(id="r_zero", node_id_globs=(bad_glob,))
    elif rule_kind == "bypass_vertices":
        rule = BypassVerticesRule(id="r_zero", node_id_globs=(bad_glob,))
    else:
        rule = CollapsePartitionRule(
            id="r_zero",
            target_node_id="super.zero",
            label="Zero",
            member_globs=(bad_glob,),
        )

    global_rules = (rule,) if is_global else ()
    view_rules = () if is_global else (rule,)

    spec = CurationSpecV1(
        global_abstractions=global_rules,
        views=(
            DiagramViewSpec(
                id="v_glob",
                title="Glob View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=view_rules,
            ),
        ),
    )

    report = gate.check(fixture.ir, spec)
    assert not report.passed
    g1_diags = report.by_gate(GateId.GATE_1_GROUNDING)
    zero_diags = [d for d in g1_diags if d.code == "ZERO_MATCH_GLOB"]
    assert len(zero_diags) == 1
    diag = zero_diags[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.rule_id == "r_zero"
    assert bad_glob in diag.offending_symbols
    assert bad_glob in diag.repair_hint


@settings(max_examples=40, deadline=None)
@given(
    fixture=layered_graph_strategy(),
    bad_rel=st.sampled_from(["teleports_to", "CALLS_SYNC", "depends_on", "unknown_rel"]),
    in_bypass=st.booleans(),
)
def test_gate_1_rejects_invalid_edge_relations(
    fixture: LayeredGraphFixture,
    bad_rel: str,
    in_bypass: bool,
) -> None:
    gate = GroundingAndBoundaryGate()
    rules = (
        (
            BypassVerticesRule(
                id="b_rel",
                node_id_globs=("pkg.bridge.*",),
                preserve_edge_relations=(bad_rel,),
            ),
        )
        if in_bypass
        else ()
    )
    reach = (
        ()
        if in_bypass
        else (
            ReachabilityAssertion(
                from_node=fixture.roots[0],
                to_node=fixture.sinks[0],
                via_relations=(bad_rel,),
            ),
        )
    )
    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="v_rel",
                title="Relation View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=rules,
                invariants=ViewInvariants(preserve_reachability=reach),
            ),
        ),
    )

    report = gate.check(fixture.ir, spec)
    assert not report.passed
    rel_diags = [
        d for d in report.by_gate(GateId.GATE_1_GROUNDING) if d.code == "INVALID_EDGE_RELATION"
    ]
    assert len(rel_diags) == 1
    assert bad_rel in rel_diags[0].offending_symbols


@settings(max_examples=60, deadline=None)
@given(
    fixture=layered_graph_strategy(),
    direction=st.sampled_from(["sink", "root", "both"]),
    is_global=st.booleans(),
)
def test_gate_2_rejects_pruning_internal_bridge_node(
    fixture: LayeredGraphFixture,
    direction: Literal["sink", "root", "both"],
    is_global: bool,
) -> None:
    gate = GroundingAndBoundaryGate()
    bridge_node = fixture.bridges[0]
    prune_rule = PruneLeavesRule(
        id="prune_bridge_err",
        node_id_globs=(bridge_node,),
        direction=direction,
    )
    spec = CurationSpecV1(
        global_abstractions=(prune_rule,) if is_global else (),
        views=(
            DiagramViewSpec(
                id="v_bridge",
                title="Bridge View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=() if is_global else (prune_rule,),
            ),
        ),
    )

    report = gate.check(fixture.ir, spec)
    assert not report.passed
    g2_diags = [
        d for d in report.by_gate(GateId.GATE_2_BOUNDARY_LEAF) if d.code == "PRUNE_INTERNAL_BRIDGE"
    ]
    assert len(g2_diags) == 1
    diag = g2_diags[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.rule_id == "prune_bridge_err"
    assert bridge_node in diag.offending_symbols
    assert "bypass_vertices" in diag.repair_hint


@settings(max_examples=60, deadline=None)
@given(
    fixture=layered_graph_strategy(),
    drop_mechanism=st.sampled_from(
        ["prune_sink", "bypass_bridge", "outside_khop", "missing_in_curated_ir"]
    ),
)
def test_gate_2_rejects_dropping_protected_node(
    fixture: LayeredGraphFixture,
    drop_mechanism: str,
) -> None:
    gate = GroundingAndBoundaryGate()
    root0 = fixture.roots[0]
    bridge0 = fixture.bridges[0]
    sink0 = fixture.sinks[0]

    if drop_mechanism == "prune_sink":
        protected = sink0
        rules = (PruneLeavesRule(id="r_drop", node_id_globs=(sink0,), direction="sink"),)
        scope = FullScope()
        curated = None
    elif drop_mechanism == "bypass_bridge":
        protected = bridge0
        rules = (BypassVerticesRule(id="r_drop", node_id_globs=(bridge0,)),)
        scope = FullScope()
        curated = None
    elif drop_mechanism == "outside_khop":
        protected = sink0
        rules = ()
        scope = KHopNeighborhoodScope(
            focal_nodes=(root0,),
            upstream_depth=0,
            downstream_depth=0,
        )
        curated = None
    else:
        protected = root0
        rules = ()
        scope = FullScope()
        remaining_nodes = {k: v for k, v in fixture.ir.nodes.items() if k != root0}
        remaining_edges = tuple(e for e in fixture.ir.edges if root0 not in {e.source, e.target})
        curated = {
            "v_prot": StructuralGraphIR(
                graph_id="curated_missing",
                nodes=remaining_nodes,
                edges=remaining_edges,
            )
        }

    spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="v_prot",
                title="Protected View",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                rules=rules,
                scope=scope,
                invariants=ViewInvariants(protected_nodes=(protected,)),
            ),
        ),
    )

    report = gate.check(fixture.ir, spec, curated_views=curated)
    assert not report.passed
    g2_diags = [
        d for d in report.by_gate(GateId.GATE_2_BOUNDARY_LEAF) if d.code == "PROTECTED_NODE_DROPPED"
    ]
    assert len(g2_diags) == 1
    diag = g2_diags[0]
    assert diag.severity is DiagnosticSeverity.ERROR
    assert protected in diag.offending_symbols
    assert diag.rule_id is not None


@settings(max_examples=35, deadline=None)
@given(p1=_TOKEN_STRAT, p2=_TOKEN_STRAT.filter(lambda s: s != "gateway"))
def test_interaction_trace_ir_grounding_and_protected_participant(
    p1: str,
    p2: str,
) -> None:
    pid_a = f"svc.{p1}"
    pid_b = f"svc.gateway.{p2}"
    trace_ir = InteractionTraceIR(
        trace_id="trace_fuzz",
        participants={
            pid_a: TraceParticipant(
                participant_id=pid_a,
                symbol_id=f"sym.{p1}",
                label=p1,
                provenance=_OBS_PROV,
            ),
            pid_b: TraceParticipant(
                participant_id=pid_b,
                symbol_id=f"sym.gateway.{p2}",
                label=p2,
                provenance=_OBS_PROV,
            ),
        },
        events={
            "ev1": TraceEvent(
                event_id="ev1",
                caller_id=pid_a,
                callee_id=pid_b,
                signature="POST /rpc",
                clock_domain_id="clk1",
                start_ns=10,
                end_ns=20,
                provenance=_OBS_PROV,
            )
        },
    )

    assert matches_glob(pid_b, "svc.**")
    gate = GroundingAndBoundaryGate()

    valid_spec = CurationSpecV1(
        views=(
            DiagramViewSpec(
                id="trace_view",
                title="Trace View",
                ir_mode=IRMode.INTERACTION_TRACE,
                target_renderer=TargetRenderer.MERMAID,
                invariants=ViewInvariants(protected_nodes=(pid_a, f"sym.gateway.{p2}")),
            ),
        ),
    )
    assert gate.check(trace_ir, valid_spec).passed
