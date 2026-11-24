"""Hypothesis property-based fuzz suite for Curation Spec v1 and 5-Gate Diagnostics."""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.ir import InteractionTraceIR, StructuralGraphIR
from archigram.domain.spec import (
    BudgetRange,
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    FullScope,
    GlobalAbstractionRule,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    ReachabilityAssertion,
    SimplifyConfig,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
    ViewScope,
    dump_curation_spec_yaml,
    export_curation_spec_json_schema,
    parse_curation_spec_yaml,
)
from archigram.ports.contracts import (
    DigestGeneratorPort,
    LintGatePort,
    StructuralReducerPort,
    TraceCompactorPort,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from archigram.domain.ir import CanonicalIR

_ID_ALPHABET = string.ascii_lowercase + string.digits + "_-:"
_GLOB_ALPHABET = _ID_ALPHABET + ".*?"
_SAFE_ID = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=16)
_SAFE_GLOB = st.text(alphabet=_GLOB_ALPHABET, min_size=1, max_size=24)
_SAFE_LABEL = st.text(
    alphabet=string.ascii_letters + string.digits + " _-:/()[]",
    min_size=1,
    max_size=32,
).filter(lambda s: bool(s.strip()))

_KNOWN_KEYS = frozenset(
    {
        "schema_version",
        "global_abstractions",
        "views",
        "metadata",
        "op",
        "id",
        "node_id_globs",
        "direction",
        "preserve_edge_relations",
        "preserve_concurrency_kind",
        "max_fanout_product",
        "target_node_id",
        "label",
        "summary",
        "member_globs",
        "exclude_ids",
        "mode",
        "focal_nodes",
        "upstream_depth",
        "downstream_depth",
        "condense_sccs",
        "transitive_reduction",
        "fold_loops",
        "fold_loop_with_opt",
        "cut_recursion",
        "fold_parallel_siblings",
        "parallel_epsilon_ns",
        "fold_parallel_map",
        "max_par_branches",
        "decompose_series_parallel",
        "max_call_depth",
        "min_count",
        "max_count",
        "from_node",
        "to_node",
        "via_relations",
        "protected_nodes",
        "preserve_reachability",
        "preserve_partial_order",
        "forbid_new_cycles",
        "node_budget",
        "edge_budget_max",
        "overflow_strategy",
        "title",
        "ir_mode",
        "target_renderer",
        "rules",
        "scope",
        "simplify",
        "trace_compaction",
        "invariants",
    }
)

_UNKNOWN_KEY_STRAT = st.one_of(
    st.sampled_from(
        [
            "add_edge",
            "new_edges",
            "add_node",
            "invented_nodes",
            "synthetic_edges",
            "custom_edges",
            "arbitrary_relation",
            "extra_hallucinated_field",
        ]
    ),
    _SAFE_ID.filter(lambda k: k not in _KNOWN_KEYS),
)


@st.composite
def structural_rule_strategy(draw: st.DrawFn, rule_id: str) -> GlobalAbstractionRule:
    """Generate a valid closed-world structural operator rule with a fixed rule_id."""
    kind = draw(st.integers(min_value=0, max_value=2))
    if kind == 0:
        return PruneLeavesRule(
            id=rule_id,
            node_id_globs=tuple(draw(st.lists(_SAFE_GLOB, min_size=1, max_size=3))),
            direction=draw(st.sampled_from(["sink", "root", "both"])),
        )
    if kind == 1:
        return BypassVerticesRule(
            id=rule_id,
            node_id_globs=tuple(draw(st.lists(_SAFE_GLOB, min_size=1, max_size=3))),
            preserve_edge_relations=tuple(draw(st.lists(_SAFE_ID, max_size=3))),
            preserve_concurrency_kind=draw(st.booleans()),
            max_fanout_product=draw(st.integers(min_value=1, max_value=100)),
        )
    return CollapsePartitionRule(
        id=rule_id,
        target_node_id=draw(_SAFE_ID),
        label=draw(_SAFE_LABEL),
        summary=draw(st.text(alphabet=string.ascii_letters + " ", max_size=24)),
        member_globs=tuple(draw(st.lists(_SAFE_GLOB, min_size=1, max_size=3))),
        exclude_ids=tuple(draw(st.lists(_SAFE_ID, max_size=2))),
    )


@st.composite
def view_scope_strategy(draw: st.DrawFn) -> ViewScope:
    """Generate either a FullScope or a KHopNeighborhoodScope."""
    if draw(st.booleans()):
        return FullScope()
    return KHopNeighborhoodScope(
        focal_nodes=tuple(draw(st.lists(_SAFE_ID, min_size=1, max_size=3))),
        upstream_depth=draw(st.integers(min_value=0, max_value=5)),
        downstream_depth=draw(st.integers(min_value=0, max_value=5)),
    )


@st.composite
def trace_compaction_strategy(draw: st.DrawFn) -> TraceCompactionConfig:
    """Generate a valid TraceCompactionConfig covering R1-R6 and SP-Poset controls."""
    max_depth = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=32)))
    return TraceCompactionConfig(
        fold_loops=draw(st.booleans()),
        fold_loop_with_opt=draw(st.booleans()),
        cut_recursion=draw(st.booleans()),
        fold_parallel_siblings=draw(st.booleans()),
        parallel_epsilon_ns=draw(st.integers(min_value=0, max_value=10_000_000)),
        fold_parallel_map=draw(st.booleans()),
        max_par_branches=draw(st.integers(min_value=2, max_value=16)),
        decompose_series_parallel=draw(st.booleans()),
        max_call_depth=max_depth,
    )


@st.composite
def view_invariants_strategy(draw: st.DrawFn) -> ViewInvariants:
    """Generate valid ViewInvariants with consistent BudgetRange and reachability assertions."""
    min_c = draw(st.integers(min_value=0, max_value=20))
    max_c = draw(st.integers(min_value=max(1, min_c), max_value=60))
    reach_count = draw(st.integers(min_value=0, max_value=2))
    reachability = tuple(
        ReachabilityAssertion(
            from_node=draw(_SAFE_ID),
            to_node=draw(_SAFE_ID),
            via_relations=tuple(draw(st.lists(_SAFE_ID, max_size=2))),
        )
        for _ in range(reach_count)
    )
    return ViewInvariants(
        protected_nodes=tuple(draw(st.lists(_SAFE_ID, max_size=3))),
        preserve_reachability=reachability,
        preserve_partial_order=draw(st.booleans()),
        forbid_new_cycles=draw(st.booleans()),
        node_budget=BudgetRange(min_count=min_c, max_count=max_c),
        edge_budget_max=draw(st.integers(min_value=0, max_value=120)),
        overflow_strategy=draw(st.sampled_from(["fail", "spanning_tree_table"])),
    )


@st.composite
def valid_curation_spec_strategy(draw: st.DrawFn) -> CurationSpecV1:
    """Generate a valid multi-view CurationSpecV1 with unique view and rule IDs."""
    global_count = draw(st.integers(min_value=0, max_value=3))
    global_rules = tuple(
        draw(structural_rule_strategy(f"global_rule_{idx}")) for idx in range(global_count)
    )

    view_count = draw(st.integers(min_value=1, max_value=3))
    views: list[DiagramViewSpec] = []
    for v_idx in range(view_count):
        v_rule_count = draw(st.integers(min_value=0, max_value=2))
        v_rules = tuple(
            draw(structural_rule_strategy(f"v{v_idx}_rule_{r_idx}"))
            for r_idx in range(v_rule_count)
        )
        views.append(
            DiagramViewSpec(
                id=f"view_{v_idx}",
                title=draw(_SAFE_LABEL),
                ir_mode=draw(st.sampled_from(list(IRMode))),
                target_renderer=draw(st.sampled_from(list(TargetRenderer))),
                rules=v_rules,
                scope=draw(view_scope_strategy()),
                simplify=SimplifyConfig(
                    condense_sccs=draw(st.booleans()),
                    transitive_reduction=draw(st.booleans()),
                ),
                trace_compaction=draw(trace_compaction_strategy()),
                invariants=draw(view_invariants_strategy()),
            )
        )

    metadata = draw(st.dictionaries(_SAFE_ID, _SAFE_LABEL, max_size=3))
    return CurationSpecV1(
        global_abstractions=global_rules,
        views=tuple(views),
        metadata=metadata,
    )


@st.composite
def lint_diagnostic_strategy(draw: st.DrawFn) -> LintDiagnostic:
    """Generate a valid immutable LintDiagnostic across all 5 gates and both severities."""
    return LintDiagnostic(
        gate=draw(st.sampled_from(list(GateId))),
        severity=draw(st.sampled_from(list(DiagnosticSeverity))),
        code=draw(_SAFE_ID),
        message=draw(_SAFE_LABEL),
        rule_id=draw(st.one_of(st.none(), _SAFE_ID)),
        view_id=draw(st.one_of(st.none(), _SAFE_ID)),
        offending_symbols=tuple(draw(st.lists(_SAFE_ID, max_size=3))),
        witness_path=tuple(draw(st.lists(_SAFE_ID, max_size=4))),
        repair_hint=draw(_SAFE_LABEL),
    )


@settings(max_examples=40)
@given(spec=valid_curation_spec_strategy())
def test_curation_spec_yaml_and_json_round_trip(spec: CurationSpecV1) -> None:
    dumped_yaml = dump_curation_spec_yaml(spec)
    assert "!!python" not in dumped_yaml

    restored_from_yaml = parse_curation_spec_yaml(dumped_yaml)
    assert restored_from_yaml == spec

    dumped_json = spec.model_dump_json()
    restored_from_json = CurationSpecV1.model_validate_json(dumped_json)
    assert restored_from_json == spec
    assert restored_from_yaml == restored_from_json


@settings(max_examples=40)
@given(
    spec=valid_curation_spec_strategy(),
    extra_key=_UNKNOWN_KEY_STRAT,
    target_section=st.sampled_from(
        ["root", "view", "scope", "simplify", "trace_compaction", "invariants", "rule"]
    ),
)
def test_extra_forbid_rejects_hallucinated_keys(
    spec: CurationSpecV1,
    extra_key: str,
    target_section: str,
) -> None:
    payload: dict[str, Any] = spec.model_dump(mode="json")
    if target_section == "root":
        payload[extra_key] = "hallucinated"
    elif target_section == "view":
        payload["views"][0][extra_key] = "hallucinated"
    elif target_section == "rule":
        payload["global_abstractions"] = [
            {
                "op": "prune_leaves",
                "id": "inject_rule",
                "node_id_globs": ["svc.*"],
                extra_key: "hallucinated",
            }
        ]
    else:
        payload["views"][0][target_section][extra_key] = "hallucinated"

    raw_yaml = yaml.safe_dump(payload, sort_keys=False)
    with pytest.raises(ValidationError):
        parse_curation_spec_yaml(raw_yaml)


@settings(max_examples=30)
@given(
    bad_op=st.one_of(
        st.sampled_from(["add_edge", "create_node", "inject_link", "synthesize_service"]),
        _SAFE_ID.filter(
            lambda op: op not in {"prune_leaves", "bypass_vertices", "collapse_partition"}
        ),
    ),
    bad_scope=st.one_of(
        st.sampled_from(["arbitrary_subgraph", "custom_nodes"]),
        _SAFE_ID.filter(lambda m: m not in {"full", "k_hop_neighborhood"}),
    ),
)
def test_unknown_operator_and_scope_discriminators_rejected(
    bad_op: str,
    bad_scope: str,
) -> None:
    bad_rule_payload = {
        "schema_version": "1.0",
        "global_abstractions": [{"op": bad_op, "id": "r1", "node_id_globs": ["a.*"]}],
        "views": [],
    }
    with pytest.raises(ValidationError):
        CurationSpecV1.model_validate(bad_rule_payload)

    bad_scope_payload = {
        "schema_version": "1.0",
        "views": [
            {
                "id": "v1",
                "title": "View 1",
                "ir_mode": "structural_graph",
                "target_renderer": "mermaid",
                "scope": {"mode": bad_scope},
            }
        ],
    }
    with pytest.raises(ValidationError):
        CurationSpecV1.model_validate(bad_scope_payload)


@settings(max_examples=50)
@given(
    min_count=st.integers(min_value=-10, max_value=60),
    max_count=st.integers(min_value=-10, max_value=60),
)
def test_budget_range_validates_bounds(min_count: int, max_count: int) -> None:
    is_valid = min_count >= 0 and max_count >= 1 and min_count <= max_count
    if is_valid:
        budget = BudgetRange(min_count=min_count, max_count=max_count)
        assert budget.min_count == min_count
        assert budget.max_count == max_count
    else:
        with pytest.raises(ValidationError):
            BudgetRange(min_count=min_count, max_count=max_count)


@settings(max_examples=30)
@given(
    bad_eps=st.integers(min_value=-1_000_000, max_value=-1),
    bad_branches=st.integers(min_value=-5, max_value=1),
    bad_depth=st.integers(min_value=-10, max_value=0),
)
def test_trace_compaction_rejects_invalid_bounds(
    bad_eps: int,
    bad_branches: int,
    bad_depth: int,
) -> None:
    with pytest.raises(ValidationError):
        TraceCompactionConfig(parallel_epsilon_ns=bad_eps)
    with pytest.raises(ValidationError):
        TraceCompactionConfig(max_par_branches=bad_branches)
    with pytest.raises(ValidationError):
        TraceCompactionConfig(max_call_depth=bad_depth)


@settings(max_examples=25)
@given(
    dup_id=_SAFE_ID,
    collision_mode=st.sampled_from(["global_dup", "view_dup", "view_rule_dup", "global_shadow"]),
)
def test_duplicate_rule_and_view_ids_rejected(dup_id: str, collision_mode: str) -> None:
    rule = PruneLeavesRule(id=dup_id, node_id_globs=("svc.*",))
    if collision_mode == "global_dup":
        with pytest.raises(ValidationError):
            CurationSpecV1(global_abstractions=(rule, rule))
        return
    if collision_mode == "view_rule_dup":
        with pytest.raises(ValidationError):
            DiagramViewSpec(
                id="v0",
                title="View 0",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                rules=(rule, rule),
            )
        return
    if collision_mode == "view_dup":
        view = DiagramViewSpec(
            id=dup_id,
            title="Duplicate View",
            ir_mode=IRMode.STRUCTURAL_GRAPH,
            target_renderer=TargetRenderer.MERMAID,
        )
        with pytest.raises(ValidationError):
            CurationSpecV1(views=(view, view))
        return

    shadow_view = DiagramViewSpec(
        id="v_shadow",
        title="Shadow View",
        ir_mode=IRMode.STRUCTURAL_GRAPH,
        target_renderer=TargetRenderer.LIKEC4,
        rules=(rule,),
    )
    with pytest.raises(ValidationError):
        CurationSpecV1(global_abstractions=(rule,), views=(shadow_view,))


@settings(max_examples=15)
@given(
    forbidden_field=st.sampled_from(
        ["add_edge", "new_edges", "add_node", "new_nodes", "invented_edges"]
    )
)
def test_json_schema_enforces_closed_world_contract(forbidden_field: str) -> None:
    schema = export_curation_spec_json_schema()
    assert schema.get("additionalProperties") is False
    defs = schema.get("$defs")
    assert isinstance(defs, dict)
    for model_schema in defs.values():
        if isinstance(model_schema, dict) and model_schema.get("type") == "object":
            assert model_schema.get("additionalProperties") is False
            props = model_schema.get("properties", {})
            assert isinstance(props, dict)
            assert forbidden_field not in props


@settings(max_examples=40)
@given(diagnostics=st.lists(lint_diagnostic_strategy(), max_size=12))
def test_lint_report_passed_invariant_and_round_trip(
    diagnostics: list[LintDiagnostic],
) -> None:
    report = LintReport.from_diagnostics(diagnostics)
    has_errors = any(d.severity is DiagnosticSeverity.ERROR for d in diagnostics)

    assert report.passed is (not has_errors)
    assert report.error_count + report.warning_count == len(diagnostics)
    assert sum(len(report.by_gate(gate)) for gate in GateId) == len(diagnostics)

    restored = LintReport.model_validate_json(report.model_dump_json())
    assert restored == report

    with pytest.raises(ValidationError):
        LintReport(passed=has_errors, diagnostics=tuple(diagnostics))


@settings(max_examples=30)
@given(
    left_diags=st.lists(lint_diagnostic_strategy(), max_size=6),
    right_diags=st.lists(lint_diagnostic_strategy(), max_size=6),
)
def test_lint_report_merge_preserves_conjunction_and_counts(
    left_diags: list[LintDiagnostic],
    right_diags: list[LintDiagnostic],
) -> None:
    left = LintReport.from_diagnostics(left_diags)
    right = LintReport.from_diagnostics(right_diags)
    merged = left.merge(right)

    assert merged.passed is (left.passed and right.passed)
    assert merged.error_count == left.error_count + right.error_count
    assert merged.warning_count == left.warning_count + right.warning_count
    assert merged.diagnostics == (*left.diagnostics, *right.diagnostics)


@settings(max_examples=15)
@given(
    spec=valid_curation_spec_strategy(),
    gate=st.sampled_from(list(GateId)),
    diagnostics=st.lists(lint_diagnostic_strategy(), max_size=4),
)
def test_extended_port_protocols_runtime_checkable(
    spec: CurationSpecV1,
    gate: GateId,
    diagnostics: list[LintDiagnostic],
) -> None:
    report = LintReport.from_diagnostics(diagnostics)

    class _StubStructuralReducer:
        def reduce(
            self,
            ir: StructuralGraphIR,
            view: DiagramViewSpec,
            global_abstractions: tuple[GlobalAbstractionRule, ...] = (),
        ) -> StructuralGraphIR:
            return ir

    class _StubTraceCompactor:
        def compact(
            self,
            ir: InteractionTraceIR,
            config: TraceCompactionConfig,
        ) -> InteractionTraceIR:
            return ir

    class _StubLintGate:
        gate_id = gate

        def check(
            self,
            observed_ir: CanonicalIR,
            spec: CurationSpecV1,
            curated_views: Mapping[str, CanonicalIR] | None = None,
        ) -> LintReport:
            return report

    class _StubDigestGenerator:
        def digest(self, ir: CanonicalIR) -> object:
            return ir

        def to_compact_markdown(
            self,
            ir: CanonicalIR,
            max_items_per_section: int = 15,
        ) -> str:
            return f"# Digest ({ir.ir_mode})"

    reducer = _StubStructuralReducer()
    compactor = _StubTraceCompactor()
    linter = _StubLintGate()
    digester = _StubDigestGenerator()

    assert isinstance(reducer, StructuralReducerPort)
    assert isinstance(compactor, TraceCompactorPort)
    assert isinstance(linter, LintGatePort)
    assert isinstance(digester, DigestGeneratorPort)

    empty_graph = StructuralGraphIR()
    empty_trace = InteractionTraceIR()
    assert reducer.reduce(empty_graph, spec.views[0], spec.global_abstractions) == empty_graph
    assert compactor.compact(empty_trace, spec.views[0].trace_compaction) == empty_trace
    assert linter.check(empty_graph, spec) == report
    assert digester.to_compact_markdown(empty_graph).startswith("# Digest")
