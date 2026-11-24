"""CLI-only CurationSpecV1 authoring & rule manipulation (`archigram curate ...`)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

import typer
from pydantic import ValidationError

from archigram.domain.spec import (
    BudgetRange,
    BypassVerticesRule,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    FullScope,
    IRMode,
    KHopNeighborhoodScope,
    PruneLeavesRule,
    TargetRenderer,
    TraceCompactionConfig,
    ViewInvariants,
    ViewRule,
    ViewScope,
    dump_curation_spec_yaml,
    parse_curation_spec_yaml,
)

EXIT_USAGE_ERROR = 2
_TYPER_RUNTIME_TYPES = (Path,)

curate_app = typer.Typer(
    name="curate",
    help="Author and manipulate CurationSpecV1 YAML rules deterministically via CLI.",
    no_args_is_help=True,
)


def _load_or_empty_spec(spec_path: Path) -> CurationSpecV1:
    if not spec_path.is_file():
        return CurationSpecV1()
    raw = spec_path.read_text(encoding="utf-8")
    return parse_curation_spec_yaml(raw) if raw.strip() else CurationSpecV1()


def _save_spec(spec_path: Path, spec: CurationSpecV1) -> CurationSpecV1:
    validated = CurationSpecV1.model_validate(spec.model_dump(mode="python"))
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(dump_curation_spec_yaml(validated), encoding="utf-8")
    return validated


def _replace_view(spec: CurationSpecV1, updated_view: DiagramViewSpec) -> CurationSpecV1:
    found = False
    new_views: list[DiagramViewSpec] = []
    for existing in spec.views:
        if existing.id == updated_view.id:
            new_views.append(updated_view)
            found = True
        else:
            new_views.append(existing)
    if not found:
        new_views.append(updated_view)
    return spec.model_copy(update={"views": tuple(new_views)})


def _get_view_or_raise(spec: CurationSpecV1, view_id: str) -> DiagramViewSpec:
    for view in spec.views:
        if view.id == view_id:
            return view
    msg = f"View '{view_id}' does not exist in spec (available: {[v.id for v in spec.views]})"
    raise ValueError(msg)


def upsert_view(spec_path: Path, view: DiagramViewSpec) -> CurationSpecV1:
    """Insert or replace a DiagramViewSpec in `spec_path` and persist validated YAML."""
    spec = _load_or_empty_spec(spec_path)
    return _save_spec(spec_path, _replace_view(spec, view))


def upsert_view_rule(spec_path: Path, view_id: str, rule: ViewRule) -> CurationSpecV1:
    """Append or replace `rule` (by `rule.id`) on `view_id` in `spec_path`."""
    spec = _load_or_empty_spec(spec_path)
    view = _get_view_or_raise(spec, view_id)
    replaced = False
    rules: list[ViewRule] = []
    for existing in view.rules:
        if existing.id == rule.id:
            rules.append(rule)
            replaced = True
        else:
            rules.append(existing)
    if not replaced:
        rules.append(rule)
    return _save_spec(
        spec_path, _replace_view(spec, view.model_copy(update={"rules": tuple(rules)}))
    )


def delete_view_rule(spec_path: Path, view_id: str, rule_id: str) -> CurationSpecV1:
    """Delete `rule_id` from `view_id` in `spec_path`."""
    spec = _load_or_empty_spec(spec_path)
    view = _get_view_or_raise(spec, view_id)
    filtered = tuple(r for r in view.rules if r.id != rule_id)
    if len(filtered) == len(view.rules):
        msg = f"Rule '{rule_id}' not found in view '{view_id}'"
        raise ValueError(msg)
    return _save_spec(spec_path, _replace_view(spec, view.model_copy(update={"rules": filtered})))


def set_view_scope(spec_path: Path, view_id: str, scope: ViewScope) -> CurationSpecV1:
    """Update `scope` on `view_id` in `spec_path`."""
    spec = _load_or_empty_spec(spec_path)
    view = _get_view_or_raise(spec, view_id)
    return _save_spec(spec_path, _replace_view(spec, view.model_copy(update={"scope": scope})))


def set_view_budget(
    spec_path: Path,
    view_id: str,
    min_nodes: int,
    max_nodes: int,
    max_edges: int,
    overflow: Literal["fail", "spanning_tree_table"] = "fail",
) -> CurationSpecV1:
    """Update cognitive node/edge budget invariants on `view_id` in `spec_path`."""
    spec = _load_or_empty_spec(spec_path)
    view = _get_view_or_raise(spec, view_id)
    updated_inv = view.invariants.model_copy(
        update={
            "node_budget": BudgetRange(min_count=min_nodes, max_count=max_nodes),
            "edge_budget_max": max_edges,
            "overflow_strategy": overflow,
        }
    )
    return _save_spec(
        spec_path, _replace_view(spec, view.model_copy(update={"invariants": updated_inv}))
    )


def set_view_trace_compaction(
    spec_path: Path,
    view_id: str,
    config: TraceCompactionConfig,
) -> CurationSpecV1:
    """Update `trace_compaction` configuration on `view_id` in `spec_path`."""
    spec = _load_or_empty_spec(spec_path)
    view = _get_view_or_raise(spec, view_id)
    return _save_spec(
        spec_path, _replace_view(spec, view.model_copy(update={"trace_compaction": config}))
    )


def _run_curate_mutation(action_label: str, action: Callable[[], object]) -> None:
    try:
        action()
    except (OSError, ValueError, ValidationError) as exc:
        typer.echo(f"Curate Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc
    typer.echo(f"[CURATE] {action_label}")


@curate_app.command("init")
def init_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    title: Annotated[str, typer.Option("--title", "-t", help="Human-readable view title.")],
    ir_mode: Annotated[
        IRMode,
        typer.Option(
            "--ir-mode", help="Canonical IR profile (structural_graph, interaction_trace)."
        ),
    ] = IRMode.STRUCTURAL_GRAPH,
    renderer: Annotated[
        TargetRenderer,
        typer.Option("--renderer", "-r", help="Target diagram renderer."),
    ] = TargetRenderer.D2,
    variant: Annotated[
        DiagramVariant,
        typer.Option("--variant", help="UML 2.5 projection sub-variant."),
    ] = DiagramVariant.AUTO,
) -> None:
    """Create or reset a view in a CurationSpecV1 YAML file."""
    new_view = DiagramViewSpec(
        id=view,
        title=title,
        ir_mode=ir_mode,
        target_renderer=renderer,
        diagram_variant=variant,
        render_svg=renderer in {TargetRenderer.D2, TargetRenderer.DOT},
        scope=FullScope(),
        invariants=ViewInvariants(),
    )
    _run_curate_mutation(
        f"Initialized view '{view}' in {spec_path}",
        lambda: upsert_view(spec_path, new_view),
    )


@curate_app.command("budget")
def budget_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    min_nodes: Annotated[
        int, typer.Option("--min-nodes", help="Minimum cognitive node count.")
    ] = 2,
    max_nodes: Annotated[
        int, typer.Option("--max-nodes", help="Maximum cognitive node count.")
    ] = 20,
    max_edges: Annotated[
        int, typer.Option("--max-edges", help="Maximum cognitive edge count.")
    ] = 40,
    overflow: Annotated[
        str,
        typer.Option("--overflow", help="Overflow strategy ('fail' or 'spanning_tree_table')."),
    ] = "fail",
) -> None:
    """Set Gate 4 cognitive node/edge budgets on a view."""
    if overflow not in {"fail", "spanning_tree_table"}:
        typer.echo(f"Curate Error: invalid overflow '{overflow}'", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR)
    lit_overflow: Literal["fail", "spanning_tree_table"] = (
        "spanning_tree_table" if overflow == "spanning_tree_table" else "fail"
    )
    _run_curate_mutation(
        f"Updated budget [{min_nodes}..{max_nodes} nodes, <={max_edges} edges] on '{view}'",
        lambda: set_view_budget(spec_path, view, min_nodes, max_nodes, max_edges, lit_overflow),
    )


@curate_app.command("prune-leaves")
def prune_leaves_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    rule_id: Annotated[str, typer.Option("--id", help="Deterministic rule ID.")],
    glob: Annotated[list[str], typer.Option("--glob", "-g", help="Node ID glob(s) to prune.")],
    direction: Annotated[
        str,
        typer.Option("--direction", help="Leaf pruning direction ('sink', 'root', or 'both')."),
    ] = "both",
) -> None:
    """Delete unnecessary degree-0 leaf sinks/roots matching `--glob`."""
    if direction not in {"sink", "root", "both"}:
        typer.echo(f"Curate Error: invalid direction '{direction}'", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR)
    lit_dir: Literal["sink", "root", "both"] = (
        "sink" if direction == "sink" else ("root" if direction == "root" else "both")
    )
    rule = PruneLeavesRule(id=rule_id, node_id_globs=tuple(glob), direction=lit_dir)
    _run_curate_mutation(
        f"Added prune_leaves rule '{rule_id}' to '{view}'",
        lambda: upsert_view_rule(spec_path, view, rule),
    )


@curate_app.command("bypass")
def bypass_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    rule_id: Annotated[str, typer.Option("--id", help="Deterministic rule ID.")],
    glob: Annotated[list[str], typer.Option("--glob", "-g", help="Intermediate vertex glob(s).")],
    max_fanout: Annotated[int, typer.Option("--max-fanout", help="Max fanout product.")] = 25,
) -> None:
    """Delete pass-through intermediate vertices while splicing transitive paths."""
    rule = BypassVerticesRule(id=rule_id, node_id_globs=tuple(glob), max_fanout_product=max_fanout)
    _run_curate_mutation(
        f"Added bypass_vertices rule '{rule_id}' to '{view}'",
        lambda: upsert_view_rule(spec_path, view, rule),
    )


@curate_app.command("collapse")
def collapse_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    rule_id: Annotated[str, typer.Option("--id", help="Deterministic rule ID.")],
    target: Annotated[str, typer.Option("--target", help="Target supernode ID.")],
    label: Annotated[str, typer.Option("--label", help="Supernode display label.")],
    glob: Annotated[
        list[str],
        typer.Option("--glob", "-g", help="Member glob(s) (prefix with '!' to exclude an ID)."),
    ],
) -> None:
    """Collapse internal symbol partitions into a bounded supernode."""
    includes = tuple(g for g in glob if not g.startswith("!"))
    excludes = tuple(g[1:] for g in glob if g.startswith("!") and len(g) > 1)
    try:
        rule = CollapsePartitionRule(
            id=rule_id,
            target_node_id=target,
            label=label,
            member_globs=includes,
            exclude_ids=excludes,
        )
    except ValidationError as exc:
        typer.echo(f"Curate Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE_ERROR) from exc
    _run_curate_mutation(
        f"Added collapse_partition rule '{rule_id}' -> '{target}' on '{view}'",
        lambda: upsert_view_rule(spec_path, view, rule),
    )


@curate_app.command("scope-khop")
def scope_khop_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    focal: Annotated[list[str], typer.Option("--focal", "-f", help="Focal symbol ID(s).")],
    upstream: Annotated[int, typer.Option("--upstream", help="Upstream hop depth.")] = 1,
    downstream: Annotated[int, typer.Option("--downstream", help="Downstream hop depth.")] = 1,
) -> None:
    """Delete out-of-neighborhood branches by scoping a view to k-hop focal symbols."""
    scope = KHopNeighborhoodScope(
        focal_nodes=tuple(focal),
        upstream_depth=upstream,
        downstream_depth=downstream,
    )
    _run_curate_mutation(
        f"Scoped '{view}' to k-hop (up={upstream}, down={downstream}) around {focal}",
        lambda: set_view_scope(spec_path, view, scope),
    )


@curate_app.command("trace-config")
def trace_config_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    max_depth: Annotated[
        int, typer.Option("--max-depth", help="Maximum call depth to retain.")
    ] = 3,
    fold_loops: Annotated[bool, typer.Option("--fold-loops/--no-fold-loops")] = True,
    fold_parallel: Annotated[bool, typer.Option("--fold-parallel/--no-fold-parallel")] = True,
) -> None:
    """Configure R1-R6 trace compaction to prune deep internal frames and fold loops."""
    cfg = TraceCompactionConfig(
        max_call_depth=max_depth,
        fold_loops=fold_loops,
        fold_loop_with_opt=fold_loops,
        fold_parallel_siblings=fold_parallel,
        fold_parallel_map=fold_parallel,
    )
    _run_curate_mutation(
        f"Updated trace_compaction (max_call_depth={max_depth}) on '{view}'",
        lambda: set_view_trace_compaction(spec_path, view, cfg),
    )


@curate_app.command("delete-rule")
def delete_rule_cmd(
    spec_path: Annotated[Path, typer.Argument(help="Path to CurationSpecV1 YAML file.")],
    view: Annotated[str, typer.Option("--view", "-v", help="View identifier.")],
    rule_id: Annotated[str, typer.Option("--id", help="Rule ID to delete.")],
) -> None:
    """Remove a rule by ID from a view."""
    _run_curate_mutation(
        f"Deleted rule '{rule_id}' from '{view}'",
        lambda: delete_view_rule(spec_path, view, rule_id),
    )
