"""Declarative Curation Spec v1 (diagram-views.yaml) closed-world schema and YAML/JSON helpers."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        msg = "Identifier or text field must not be blank"
        raise ValueError(msg)
    return value


NonBlankStr = Annotated[str, AfterValidator(_validate_non_blank)]


class IRMode(StrEnum):
    """Canonical IR profile targeted by a curated diagram view."""

    STRUCTURAL_GRAPH = "structural_graph"
    INTERACTION_TRACE = "interaction_trace"


class TargetRenderer(StrEnum):
    """Supported deterministic diagram rendering targets."""

    MERMAID = "mermaid"
    D2 = "d2"
    PLANTUML = "plantuml"
    LIKEC4 = "likec4"
    DOT = "dot"


class DiagramVariant(StrEnum):
    """Structural or behavioral UML 2.5 projection sub-variant."""

    AUTO = "auto"
    ARCHITECTURE = "architecture"
    CLASS = "class"
    PACKAGE = "package"
    DEPLOYMENT = "deployment"
    USE_CASE = "use_case"
    STEPS = "steps"
    COMMUNICATION = "communication"
    ACTIVITY = "activity"
    STATE = "state"


class PruneLeavesRule(BaseModel):
    """Closed-world structural operator removing matched degree-0 leaf sinks and/or roots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["prune_leaves"] = "prune_leaves"
    id: NonBlankStr
    node_id_globs: tuple[NonBlankStr, ...] = Field(min_length=1)
    direction: Literal["sink", "root", "both"] = "sink"


class BypassVerticesRule(BaseModel):
    """Closed-world structural operator splicing through matched intermediate vertices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["bypass_vertices"] = "bypass_vertices"
    id: NonBlankStr
    node_id_globs: tuple[NonBlankStr, ...] = Field(min_length=1)
    preserve_edge_relations: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    preserve_concurrency_kind: bool = True
    max_fanout_product: int = Field(default=25, ge=1)


class CollapsePartitionRule(BaseModel):
    """Closed-world structural operator contracting matched existing symbols into one supernode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["collapse_partition"] = "collapse_partition"
    id: NonBlankStr
    target_node_id: NonBlankStr
    label: NonBlankStr
    summary: str = ""
    member_globs: tuple[NonBlankStr, ...] = Field(min_length=1)
    exclude_ids: tuple[NonBlankStr, ...] = Field(default_factory=tuple)


GlobalAbstractionRule = Annotated[
    PruneLeavesRule | BypassVerticesRule | CollapsePartitionRule,
    Field(discriminator="op"),
]

ViewRule = GlobalAbstractionRule


class FullScope(BaseModel):
    """Retain the entire post-abstraction graph or trace for a diagram view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["full"] = "full"


class KHopNeighborhoodScope(BaseModel):
    """Slice a view to the k-hop upstream/downstream neighborhood around focal Canonical IR IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["k_hop_neighborhood"] = "k_hop_neighborhood"
    focal_nodes: tuple[NonBlankStr, ...] = Field(min_length=1)
    upstream_depth: int = Field(default=1, ge=0)
    downstream_depth: int = Field(default=1, ge=0)


ViewScope = Annotated[
    FullScope | KHopNeighborhoodScope,
    Field(discriminator="mode"),
]


class SimplifyConfig(BaseModel):
    """Deterministic DAG simplification options applied after scoping and partitioning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condense_sccs: bool = True
    transitive_reduction: bool = True


class TraceCompactionConfig(BaseModel):
    """Deterministic trace & concurrency compaction configuration (Rules R1-R6 + SP-Poset)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fold_loops: bool = True
    fold_loop_with_opt: bool = True
    cut_recursion: bool = True
    fold_parallel_siblings: bool = True
    parallel_epsilon_ns: int = Field(default=1_000_000, ge=0)
    fold_parallel_map: bool = True
    max_par_branches: int = Field(default=6, ge=2)
    decompose_series_parallel: bool = True
    max_call_depth: int | None = Field(default=8, ge=1)


class BudgetRange(BaseModel):
    """Inclusive cognitive node budget bounds [min_count, max_count] for Gate 4 validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_count: int = Field(default=1, ge=0)
    max_count: int = Field(default=25, ge=1)

    @model_validator(mode="after")
    def _validate_bounds(self) -> BudgetRange:
        if self.min_count > self.max_count:
            msg = (
                f"BudgetRange min_count ({self.min_count}) cannot exceed "
                f"max_count ({self.max_count})"
            )
            raise ValueError(msg)
        return self


class ReachabilityAssertion(BaseModel):
    """Protected directed reachability invariant verified by Gate 3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_node: NonBlankStr
    to_node: NonBlankStr
    via_relations: tuple[NonBlankStr, ...] = Field(default_factory=tuple)


class ViewInvariants(BaseModel):
    """Semantic, topological, partial-order, and cognitive budget invariants for a view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_nodes: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    preserve_reachability: tuple[ReachabilityAssertion, ...] = Field(default_factory=tuple)
    preserve_partial_order: bool = True
    forbid_new_cycles: bool = True
    node_budget: BudgetRange = Field(default_factory=BudgetRange)
    edge_budget_max: int = Field(default=40, ge=0)
    overflow_strategy: Literal["fail", "spanning_tree_table"] = "fail"


def _ensure_unique_rule_ids(rules: tuple[GlobalAbstractionRule, ...], context: str) -> set[str]:
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            msg = f"Duplicate rule id '{rule.id}' in {context}"
            raise ValueError(msg)
        seen.add(rule.id)
    return seen


class DiagramViewSpec(BaseModel):
    """Declarative specification for one curated diagram projection view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonBlankStr
    title: NonBlankStr
    ir_mode: IRMode
    target_renderer: TargetRenderer
    diagram_variant: DiagramVariant = DiagramVariant.AUTO
    render_svg: bool = False
    animate_edges: bool | None = None
    rules: tuple[ViewRule, ...] = Field(default_factory=tuple)
    scope: ViewScope = Field(default_factory=FullScope)
    simplify: SimplifyConfig = Field(default_factory=SimplifyConfig)
    trace_compaction: TraceCompactionConfig = Field(default_factory=TraceCompactionConfig)
    invariants: ViewInvariants = Field(default_factory=ViewInvariants)

    @model_validator(mode="after")
    def _validate_unique_view_rules(self) -> DiagramViewSpec:
        _ensure_unique_rule_ids(self.rules, f"view '{self.id}'")
        return self


class CurationSpecV1(BaseModel):
    """Root closed-world schema for diagram-views.yaml (Curation Spec v1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    global_abstractions: tuple[GlobalAbstractionRule, ...] = Field(default_factory=tuple)
    views: tuple[DiagramViewSpec, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_spec_uniqueness(self) -> CurationSpecV1:
        global_rule_ids = _ensure_unique_rule_ids(self.global_abstractions, "global_abstractions")

        seen_views: set[str] = set()
        for view in self.views:
            if view.id in seen_views:
                msg = f"Duplicate view id '{view.id}' in CurationSpecV1"
                raise ValueError(msg)
            seen_views.add(view.id)
            for rule in view.rules:
                if rule.id in global_rule_ids:
                    msg = (
                        f"Rule id '{rule.id}' in view '{view.id}' collides with "
                        "a global_abstractions rule id"
                    )
                    raise ValueError(msg)
        return self


def parse_curation_spec_yaml(raw_yaml: str) -> CurationSpecV1:
    """Safely parse and strictly validate a YAML string into a CurationSpecV1."""
    loaded: object = yaml.safe_load(raw_yaml)
    return CurationSpecV1.model_validate(loaded)


def dump_curation_spec_yaml(spec: CurationSpecV1) -> str:
    """Deterministically serialize a CurationSpecV1 to clean YAML without custom tags."""
    payload = spec.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def export_curation_spec_json_schema() -> dict[str, object]:
    """Return the strict Pydantic v2 JSON Schema for CurationSpecV1 (diagram-views.yaml)."""
    return CurationSpecV1.model_json_schema()
