"""Reproducible live runtime trace capture for ArchiGram self-hosted documentation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    InteractionTraceIR,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    TraceEvent,
    TraceParticipant,
)
from archigram.domain.spec import (
    BudgetRange,
    CollapsePartitionRule,
    CurationSpecV1,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    ViewInvariants,
    dump_curation_spec_yaml,
)
from archigram.entrypoints.cli.pipeline import compile_and_lint_pipeline
from archigram.entrypoints.mcp.propose_loop import (
    repair_curation_spec_from_diagnostics,
    run_propose_loop,
)
from archigram.features.ingest_python import capture_python_trace

if TYPE_CHECKING:
    from archigram.domain.diagnostics import LintDiagnostic

DEFAULT_TRACE_PATH = Path("docs/architecture/traces/pipeline-and-repair.ir.json")
_VIRTUAL_TICK_NS = 5_000_000
_SAMPLE_SHA = "0" * 64


def _make_sample_stamp() -> ProvenanceStamp:
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor="self_trace_workload_v1",
        input_sha256=_SAMPLE_SHA,
    )


def _build_sample_structural_ir() -> StructuralGraphIR:
    """Build a compact Hexagonal StructuralGraphIR exercising reduction, linting, and emission."""
    stamp = _make_sample_stamp()
    nodes = {
        "app.cli": IRNode(
            symbol_id="app.cli", label="CLI", kind=NodeKind.SERVICE, provenance=stamp
        ),
        "app.ingest": IRNode(
            symbol_id="app.ingest", label="Ingest", kind=NodeKind.MODULE, provenance=stamp
        ),
        "app.reduce": IRNode(
            symbol_id="app.reduce", label="Reduce", kind=NodeKind.MODULE, provenance=stamp
        ),
        "app.ports": IRNode(
            symbol_id="app.ports", label="Ports", kind=NodeKind.PACKAGE, provenance=stamp
        ),
        "app.domain": IRNode(
            symbol_id="app.domain", label="Domain", kind=NodeKind.PACKAGE, provenance=stamp
        ),
    }
    edges = (
        IREdge(
            edge_id="e_cli_ingest",
            source="app.cli",
            target="app.ingest",
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        ),
        IREdge(
            edge_id="e_cli_reduce",
            source="app.cli",
            target="app.reduce",
            relation=EdgeRelation.CALLS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        ),
        IREdge(
            edge_id="e_ingest_ports",
            source="app.ingest",
            target="app.ports",
            relation=EdgeRelation.IMPORTS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        ),
        IREdge(
            edge_id="e_reduce_ports",
            source="app.reduce",
            target="app.ports",
            relation=EdgeRelation.IMPORTS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        ),
        IREdge(
            edge_id="e_ports_domain",
            source="app.ports",
            target="app.domain",
            relation=EdgeRelation.IMPORTS,
            sync_kind=SyncKind.SYNC,
            provenance=stamp,
        ),
    )
    return StructuralGraphIR(graph_id="self_trace_sample", nodes=nodes, edges=edges)


def _build_multi_view_spec() -> CurationSpecV1:
    """Build a multi-view CurationSpecV1 exercising Mermaid, D2, and PlantUML pipelines."""
    collapse_features = CollapsePartitionRule(
        id="collapse_features",
        target_node_id="super.features",
        label="Feature Slices",
        member_globs=("app.ingest", "app.reduce"),
    )
    invariants = ViewInvariants(
        node_budget=BudgetRange(min_count=2, max_count=12),
        edge_budget_max=20,
    )
    return CurationSpecV1(
        global_abstractions=(collapse_features,),
        views=(
            DiagramViewSpec(
                id="sample_mermaid",
                title="Sample Mermaid",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.MERMAID,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="sample_d2",
                title="Sample D2",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.D2,
                invariants=invariants,
            ),
            DiagramViewSpec(
                id="sample_plantuml",
                title="Sample PlantUML",
                ir_mode=IRMode.STRUCTURAL_GRAPH,
                target_renderer=TargetRenderer.PLANTUML,
                invariants=invariants,
            ),
        ),
    )


_TURN_1_FLAWED_YAML = """\
schema_version: '1.0'
views:
- id: repaired_overview
  title: Self-Healed Overview
  ir_mode: structural_graph
  target_renderer: mermaid
  invariants:
    protected_nodes:
    - app.doman
    node_budget:
      min_count: 2
      max_count: 12
    edge_budget_max: 20
"""


class _SelfHealingWorkloadProposer:
    """Deterministic proposer that triggers a Gate 1 typo on turn 1 and repairs on turn 2."""

    def __init__(self, observed_ir: StructuralGraphIR) -> None:
        self._observed_ir = observed_ir

    def propose(
        self,
        digest_markdown: str,
        prior_spec_yaml: str | None,
        diagnostics: tuple[LintDiagnostic, ...],
        turn: int,
    ) -> str:
        """Emit an intentionally flawed spec on turn 1, then run deterministic repair on turn 2."""
        _ = digest_markdown
        if turn == 1 or prior_spec_yaml is None:
            return _TURN_1_FLAWED_YAML

        repaired = repair_curation_spec_from_diagnostics(
            prior_spec_yaml,
            diagnostics,
            self._observed_ir,
        )
        return dump_curation_spec_yaml(repaired)


def _normalize_repo_relative_path(raw_path: str | None) -> str | None:
    """Normalize provenance file paths to stable repository-relative POSIX paths."""
    if not raw_path:
        return raw_path
    marker = "src/archigram/"
    idx = raw_path.find(marker)
    if idx != -1:
        return raw_path[idx:]
    return raw_path


def _compute_normalized_sha256(
    participants: dict[str, TraceParticipant],
    events: dict[str, TraceEvent],
) -> str:
    payload = {
        "participants": [
            (pid, p.symbol_id, p.label, p.provenance.file_path)
            for pid, p in sorted(participants.items())
        ],
        "events": [
            (
                ev.event_id,
                ev.caller_id,
                ev.callee_id,
                ev.signature,
                ev.parent_event_id,
                ev.start_ns,
                ev.end_ns,
                ev.status.value,
                ev.provenance.file_path,
            )
            for ev in events.values()
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_trace_deterministically(raw_trace: InteractionTraceIR) -> InteractionTraceIR:
    """Assign deterministic virtual monotonic timestamps while preserving live call topology."""
    children_by_parent: dict[str | None, list[TraceEvent]] = {}
    for ev in sorted(raw_trace.events.values(), key=lambda e: (e.start_ns, e.end_ns, e.event_id)):
        children_by_parent.setdefault(ev.parent_event_id, []).append(ev)

    tick = 0
    normalized_events: dict[str, TraceEvent] = {}

    def _visit(ev: TraceEvent) -> None:
        nonlocal tick
        tick += 1
        start_ns = tick * _VIRTUAL_TICK_NS
        for child in children_by_parent.get(ev.event_id, ()):
            _visit(child)
        tick += 1
        end_ns = tick * _VIRTUAL_TICK_NS
        norm_prov = ev.provenance.model_copy(
            update={"file_path": _normalize_repo_relative_path(ev.provenance.file_path)}
        )
        normalized_events[ev.event_id] = ev.model_copy(
            update={
                "start_ns": start_ns,
                "end_ns": end_ns,
                "provenance": norm_prov,
            }
        )

    for root in children_by_parent.get(None, ()):
        _visit(root)

    ordered_events = {
        ev.event_id: ev
        for ev in sorted(normalized_events.values(), key=lambda e: (e.start_ns, e.event_id))
    }
    normalized_participants: dict[str, TraceParticipant] = {
        pid: part.model_copy(
            update={
                "provenance": part.provenance.model_copy(
                    update={"file_path": _normalize_repo_relative_path(part.provenance.file_path)}
                )
            }
        )
        for pid, part in sorted(raw_trace.participants.items())
    }
    norm_sha = _compute_normalized_sha256(normalized_participants, ordered_events)
    stamped_events = {
        eid: ev.model_copy(
            update={"provenance": ev.provenance.model_copy(update={"input_sha256": norm_sha})}
        )
        for eid, ev in ordered_events.items()
    }
    stamped_participants = {
        pid: part.model_copy(
            update={"provenance": part.provenance.model_copy(update={"input_sha256": norm_sha})}
        )
        for pid, part in normalized_participants.items()
    }
    metadata = dict(raw_trace.metadata)
    metadata["clock_mode"] = "virtual_monotonic_step_5ms"
    metadata["input_sha256"] = norm_sha
    return raw_trace.model_copy(
        update={
            "participants": stamped_participants,
            "events": stamped_events,
            "metadata": metadata,
        }
    )


def _execute_self_hosted_workload(
    sample_ir: StructuralGraphIR,
    multi_view_spec: CurationSpecV1,
) -> None:
    """Run real ArchiGram multi-view compilation followed by bounded 3-turn self-healing repair."""
    compilation = compile_and_lint_pipeline(sample_ir, multi_view_spec)
    if not compilation.lint_report.passed:
        msg = "Self-trace multi-view compilation unexpectedly failed linting"
        raise RuntimeError(msg)

    propose_res = run_propose_loop(
        sample_ir,
        proposer=_SelfHealingWorkloadProposer(sample_ir),
        max_turns=3,
    )
    if not propose_res.succeeded or propose_res.turns_used < 2:
        msg = (
            "Self-trace propose loop expected multi-turn self-healing repair "
            f"(succeeded={propose_res.succeeded}, turns_used={propose_res.turns_used})"
        )
        raise RuntimeError(msg)


_TRACED_MODULE_PREFIXES: tuple[str, ...] = (
    "archigram.domain",
    "archigram.ports",
    "archigram.features",
    "archigram.entrypoints.cli.pipeline",
    "archigram.entrypoints.mcp",
)


def capture_self_hosted_pipeline_trace() -> InteractionTraceIR:
    """Capture and normalize a live sys.monitoring execution trace of ArchiGram's pipeline."""
    sample_ir = _build_sample_structural_ir()
    multi_view_spec = _build_multi_view_spec()
    # Warm up lazy imports and module caches so cold CLI and warm pytest captures match 100%.
    _execute_self_hosted_workload(sample_ir, multi_view_spec)

    raw_trace = capture_python_trace(
        lambda: _execute_self_hosted_workload(sample_ir, multi_view_spec),
        trace_id="archigram_pipeline_and_repair_v1",
        module_prefixes=_TRACED_MODULE_PREFIXES,
        granularity="module",
        max_events=2000,
    )
    return _normalize_trace_deterministically(raw_trace)


def write_self_hosted_trace(out_path: Path = DEFAULT_TRACE_PATH) -> InteractionTraceIR:
    """Capture the self-hosted pipeline+repair trace and write deterministic JSON to disk."""
    trace_ir = capture_self_hosted_pipeline_trace()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(trace_ir.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return trace_ir


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `python -m archigram.entrypoints.cli.self_trace`."""
    parser = argparse.ArgumentParser(
        description="Capture deterministic ArchiGram runtime trace for L3 architecture docs."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_TRACE_PATH,
        help="Destination path for InteractionTraceIR JSON snapshot.",
    )
    args = parser.parse_args(argv)
    trace_ir = write_self_hosted_trace(args.out)
    n_parts = len(trace_ir.participants)
    n_events = len(trace_ir.events)
    print(f"Wrote {args.out} ({n_parts} participants, {n_events} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
