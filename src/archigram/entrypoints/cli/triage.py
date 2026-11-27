"""3-Stage Program-to-Curated Triage Generator (`1_original` -> `2_triage` -> `3_curated`)."""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import (
    CanonicalIR,
    InteractionTraceIR,
    IREdge,
    IRNode,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    BypassVerticesRule,
    CurationSpecV1,
    DiagramViewSpec,
    GlobalAbstractionRule,
    TargetRenderer,
    validate_safe_view_id,
)
from archigram.domain.style import (
    D2_CANONICAL_HEADER,
    DEFAULT_MERMAID_THEME,
    MERMAID_GITEA_MAX_SOURCE_CHARS,
    NEUTRAL_BORDER_HEX,
    TRANSPARENT_FILL,
    TRIAGE_BYPASS_HEX,
    TRIAGE_COLLAPSE_HEX,
    TRIAGE_KEEP_HEX,
    TRIAGE_REMOVE_HEX,
    MermaidThemeConfig,
)
from archigram.entrypoints.cli.pipeline import (
    InputFormat,
    PipelineCompilationResult,
    compile_and_lint_pipeline,
    emit_view_source,
    load_canonical_ir,
    load_curation_spec,
    load_mermaid_theme,
    render_companion_svg,
    resolve_contained_output_path,
)
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_d2.emitter import (
    build_provenance_tooltip as _build_provenance_tooltip,
)
from archigram.features.emit_d2.emitter import (
    build_qualified_paths as _build_qualified_paths,
)
from archigram.features.emit_d2.emitter import (
    build_safe_key_map as _build_safe_key_map,
)
from archigram.features.emit_d2.emitter import (
    class_shaped_node_ids,
)
from archigram.features.emit_d2.emitter import (
    format_d2_key as _format_d2_key,
)
from archigram.features.emit_d2.emitter import (
    quote_d2_string as _quote_d2_string,
)
from archigram.features.emit_mermaid import MermaidEmitter

if TYPE_CHECKING:
    from pathlib import Path


class NodeDisposition(StrEnum):
    """Compiler-computed triage disposition for each raw observed element."""

    KEEP = "keep"
    REMOVE = "remove"
    BYPASS = "bypass"
    COLLAPSE = "collapse"


_DISPOSITION_STYLES: dict[NodeDisposition, tuple[str, str, bool]] = {
    NodeDisposition.KEEP: (TRIAGE_KEEP_HEX, "[KEEP]", False),
    NodeDisposition.REMOVE: (TRIAGE_REMOVE_HEX, "[REMOVE]", True),
    NodeDisposition.BYPASS: (TRIAGE_BYPASS_HEX, "[BYPASS]", True),
    NodeDisposition.COLLAPSE: (TRIAGE_COLLAPSE_HEX, "[COLLAPSE]", True),
}


@dataclass(frozen=True, slots=True)
class TriageOptions:
    """Configuration options for `archigram triage`."""

    input_path: Path
    spec_path: Path
    view_id: str
    out_dir: Path
    input_format: InputFormat = InputFormat.AUTO
    check_only: bool = False


@dataclass(frozen=True, slots=True)
class TriageStats:
    """Quantitative summary of raw vs triaged vs curated diagram size and dispositions."""

    view_id: str
    raw_elements: int
    raw_links: int
    raw_mermaid_chars: int
    curated_elements: int
    curated_links: int
    curated_mermaid_chars: int
    kept_count: int
    removed_count: int
    bypassed_count: int
    collapsed_count: int

    @property
    def exceeds_gitea_mermaid_limit(self) -> bool:
        """Return True when raw Mermaid output exceeds Gitea's 5,000-character limit."""
        return self.raw_mermaid_chars > MERMAID_GITEA_MAX_SOURCE_CHARS


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """Outcome of executing (or checking) the 3-stage triage pipeline."""

    compilation: PipelineCompilationResult
    stats: TriageStats
    stage1_d2: str
    stage2_d2: str
    stage3_d2: str
    stage3_mermaid: str
    written_files: tuple[Path, ...] = ()
    drifted_files: tuple[Path, ...] = ()
    missing_files: tuple[Path, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Return True when Gates 1-5 passed and zero file drift/missing files occurred."""
        return (
            self.compilation.lint_report.passed
            and not self.drifted_files
            and not self.missing_files
        )


def _classify_structural_nodes(
    observed: StructuralGraphIR,
    emitted: StructuralGraphIR,
    rules: tuple[GlobalAbstractionRule, ...],
) -> tuple[dict[str, NodeDisposition], dict[str, str]]:
    """Classify every raw node in `observed` as KEEP, COLLAPSE, BYPASS, or REMOVE."""
    dispositions: dict[str, NodeDisposition] = {}
    collapse_targets: dict[str, str] = {}
    for cur_node in emitted.nodes.values():
        if cur_node.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC:
            for bid in cur_node.provenance.backing_ids:
                if bid in observed.nodes and bid not in emitted.nodes:
                    collapse_targets[bid] = cur_node.label

    bypass_globs = [
        glob
        for rule in rules
        if isinstance(rule, BypassVerticesRule)
        for glob in rule.node_id_globs
    ]
    for sid in sorted(observed.nodes):
        if sid in emitted.nodes:
            dispositions[sid] = NodeDisposition.KEEP
        elif sid in collapse_targets:
            dispositions[sid] = NodeDisposition.COLLAPSE
        elif any(fnmatch.fnmatchcase(sid, pat) for pat in bypass_globs):
            dispositions[sid] = NodeDisposition.BYPASS
        else:
            dispositions[sid] = NodeDisposition.REMOVE
    return dispositions, collapse_targets


def _append_triage_legend(lines: list[str]) -> None:
    entries = (
        ("keep_key", "[KEEP] Retained in final view", TRIAGE_KEEP_HEX, False),
        ("remove_key", "[REMOVE] Pruned / out-of-scope", TRIAGE_REMOVE_HEX, True),
        ("bypass_key", "[BYPASS] Spliced intermediate", TRIAGE_BYPASS_HEX, True),
        ("collapse_key", "[COLLAPSE] Merged into supernode", TRIAGE_COLLAPSE_HEX, True),
    )
    lines.extend(
        (
            'triage_legend: "Triage Disposition Legend" {',
            f"  style.fill: {TRANSPARENT_FILL}",
            f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
            "  style.stroke-dash: 3",
        )
    )
    for key, text, stroke, dashed in entries:
        lines.append(f'  {key}: "{text}" {{')
        lines.append(f"    style.fill: {TRANSPARENT_FILL}")
        lines.append(f'    style.stroke: "{stroke}"')
        if dashed:
            lines.append("    style.stroke-dash: 5")
        lines.append("  }")
    lines.append("}")


_PASTEL_WHITE_MIX = 0.82


def _pastel(hex_color: str) -> str:
    """Blend `#rrggbb` toward white: class compartments take their color from `fill`."""
    channels = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    mixed = (round(c * (1 - _PASTEL_WHITE_MIX) + 255 * _PASTEL_WHITE_MIX) for c in channels)
    return "#" + "".join(f"{c:02x}" for c in mixed)


def _triage_node_lines(
    node: IRNode,
    path: str,
    disposition: NodeDisposition,
    collapse_target: str | None,
    is_class: bool,
) -> list[str]:
    """Style overrides that recolor one Stage 1 node and tag it with a hover tooltip."""
    stroke_hex, badge, dashed = _DISPOSITION_STYLES[disposition]
    note = badge if collapse_target is None else f"{badge} -> {collapse_target}"
    provenance = _build_provenance_tooltip(node)
    tooltip = note if provenance is None else f"{note} - {provenance}"
    if is_class:
        lines = [f'{path}.style.fill: "{_pastel(stroke_hex)}"']
    else:
        lines = [
            f'{path}.style.stroke: "{stroke_hex}"',
            f"{path}.style.stroke-dash: {5 if dashed else 0}",
        ]
    lines.append(f"{path}.tooltip: {_quote_d2_string(tooltip)}")
    return lines


def _triage_edge_style(
    edge: IREdge,
    surviving_pairs: set[tuple[str, str]],
    dispositions: dict[str, NodeDisposition],
) -> tuple[str, bool]:
    """Return the (stroke color, dashed) a raw edge takes in the triage overlay."""
    if (edge.source, edge.target) in surviving_pairs:
        return TRIAGE_KEEP_HEX, False
    ends = {dispositions[edge.source], dispositions[edge.target]}
    if NodeDisposition.REMOVE in ends:
        return TRIAGE_REMOVE_HEX, True
    if NodeDisposition.BYPASS in ends:
        return TRIAGE_BYPASS_HEX, True
    return TRIAGE_COLLAPSE_HEX, True


def _triage_edge_lines(
    observed: StructuralGraphIR,
    emitted: StructuralGraphIR,
    paths: dict[str, str],
    dispositions: dict[str, NodeDisposition],
) -> list[str]:
    """Recolor each Stage 1 edge, addressing parallel edges by the emitter's own ordering."""
    surviving_pairs = {(e.source, e.target) for e in emitted.edges}
    seen: dict[tuple[str, str], int] = {}
    lines: list[str] = []
    ordered = sorted(
        observed.edges,
        key=lambda e: (e.source, e.target, e.relation.value, e.sync_kind.value, e.edge_id),
    )
    for edge in ordered:
        pair = (edge.source, edge.target)
        index = seen.get(pair, 0)
        seen[pair] = index + 1
        stroke_hex, dashed = _triage_edge_style(edge, surviving_pairs, dispositions)
        ref = f"({paths[edge.source]} -> {paths[edge.target]})[{index}]"
        lines.append(f'{ref}.style.stroke: "{stroke_hex}"')
        lines.append(f"{ref}.style.stroke-dash: {5 if dashed else 0}")
    return lines


def _emit_structural_triage_d2(
    base_d2: str,
    observed: StructuralGraphIR,
    emitted: StructuralGraphIR,
    dispositions: dict[str, NodeDisposition],
    collapse_targets: dict[str, str],
) -> str:
    """Stage 1's D2 plus style overrides, so Stage 2 keeps Stage 1's layout.

    Overrides only change colors and dashes, never labels or shapes, so the layout engine
    places every node exactly where Stage 1 does and the two diagrams stay comparable.
    """
    key_map, _ = _build_safe_key_map(observed.nodes.keys())
    paths = _build_qualified_paths(observed.nodes, key_map)
    class_ids = class_shaped_node_ids(observed)
    lines = [base_d2.rstrip("\n"), ""]
    for sid in sorted(observed.nodes):
        lines.extend(
            _triage_node_lines(
                observed.nodes[sid],
                paths[sid],
                dispositions[sid],
                collapse_targets.get(sid),
                sid in class_ids,
            )
        )
    lines.append("")
    lines.extend(_triage_edge_lines(observed, emitted, paths, dispositions))
    lines.append("")
    _append_triage_legend(lines)
    return "\n".join(lines) + "\n"


def _emit_trace_raw_call_d2(trace: InteractionTraceIR) -> str:
    """Emit raw program-observed participant & call-channel topology in D2."""
    lines = [D2_CANONICAL_HEADER, "", "direction: right"]
    sorted_pids = sorted(trace.participants)
    key_map, _ = _build_safe_key_map(sorted_pids)
    for pid in sorted_pids:
        k = _format_d2_key(key_map[pid])
        lines.extend(
            (
                f"{k}: {_quote_d2_string(trace.participants[pid].label)} {{",
                f"  style.fill: {TRANSPARENT_FILL}",
                f'  style.stroke: "{NEUTRAL_BORDER_HEX}"',
                "}",
            )
        )
    counts: dict[tuple[str, str], int] = {}
    sample_sig: dict[tuple[str, str], str] = {}
    for ev in sorted(trace.events.values(), key=lambda item: (item.start_ns, item.event_id)):
        pair = (ev.caller_id, ev.callee_id)
        counts[pair] = counts.get(pair, 0) + 1
        sample_sig.setdefault(pair, ev.signature.split("(", 1)[0].strip()[:28])
    for (src, dst), cnt in sorted(counts.items()):
        sk, dk = _format_d2_key(key_map[src]), _format_d2_key(key_map[dst])
        lbl = f"{sample_sig[(src, dst)]} (x{cnt} raw)"
        lines.extend(
            (f"{sk} -> {dk}: {_quote_d2_string(lbl)} {{", '  style.stroke: "#3b82f6"', "}")
        )
    return "\n".join(lines) + "\n"


def _emit_trace_triage_d2(observed: InteractionTraceIR, curated: InteractionTraceIR) -> str:
    """Emit Stage 2 trace triage D2 highlighting pruned vs compacted vs kept spans."""
    lines = [D2_CANONICAL_HEADER, "", "direction: right"]
    _append_triage_legend(lines)
    active_curated = {pid for ev in curated.events.values() for pid in (ev.caller_id, ev.callee_id)}
    sorted_pids = sorted(observed.participants)
    key_map, _ = _build_safe_key_map([*sorted_pids, "triage_legend"])
    for pid in sorted_pids:
        kept = pid in active_curated or not curated.events
        stroke = TRIAGE_KEEP_HEX if kept else TRIAGE_REMOVE_HEX
        badge = "[KEEP]" if kept else "[REMOVE]"
        k = _format_d2_key(key_map[pid])
        lines.append(f"{k}: {_quote_d2_string(f'{badge} {observed.participants[pid].label}')} {{")
        lines.append(f"  style.fill: {TRANSPARENT_FILL}")
        lines.append(f'  style.stroke: "{stroke}"')
        if not kept:
            lines.append("  style.stroke-dash: 5")
        lines.append("}")

    raw_counts: dict[tuple[str, str], int] = {}
    cur_counts: dict[tuple[str, str], int] = {}
    for ev in observed.events.values():
        raw_counts[(ev.caller_id, ev.callee_id)] = (
            raw_counts.get((ev.caller_id, ev.callee_id), 0) + 1
        )
    for ev in curated.events.values():
        cur_counts[(ev.caller_id, ev.callee_id)] = (
            cur_counts.get((ev.caller_id, ev.callee_id), 0) + 1
        )

    for pair, raw_n in sorted(raw_counts.items()):
        cur_n = cur_counts.get(pair, 0)
        sk, dk = _format_d2_key(key_map[pair[0]]), _format_d2_key(key_map[pair[1]])
        if cur_n == 0:
            e_hex, lbl, dashed = TRIAGE_REMOVE_HEX, f"[REMOVE {raw_n} spans]", True
        elif cur_n < raw_n:
            e_hex, lbl, dashed = TRIAGE_COLLAPSE_HEX, f"[FOLD {raw_n} -> {cur_n} spans]", True
        else:
            e_hex, lbl, dashed = TRIAGE_KEEP_HEX, f"[KEEP {cur_n} spans]", False
        lines.append(f"{sk} -> {dk}: {_quote_d2_string(lbl)} {{")
        lines.append(f'  style.stroke: "{e_hex}"')
        if dashed:
            lines.append("  style.stroke-dash: 5")
        lines.append("}")
    return "\n".join(lines) + "\n"


def _sync_text_file(
    path: Path,
    content: str,
    *,
    check_only: bool,
    buckets: tuple[list[Path], list[Path], list[Path]],
) -> None:
    written, drifted, missing = buckets
    expected = content.encode("utf-8")
    if check_only:
        if not path.is_file():
            missing.append(path)
        elif path.read_bytes() != expected:
            drifted.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)
    written.append(path)


def _sync_d2_and_svg(
    stem_path: Path,
    d2_source: str,
    *,
    check_only: bool,
    buckets: tuple[list[Path], list[Path], list[Path]],
) -> None:
    _sync_text_file(stem_path, d2_source, check_only=check_only, buckets=buckets)
    svg_path = resolve_contained_output_path(stem_path.parent, f"{stem_path.name}.svg")
    sha = hashlib.sha256(d2_source.encode("utf-8")).hexdigest()
    token = f"archigram-source-sha256: {sha}"
    written, drifted, missing = buckets
    if check_only:
        if not svg_path.is_file():
            missing.append(svg_path)
        else:
            txt = svg_path.read_text(encoding="utf-8")
            if "<svg" not in txt or token not in txt:
                drifted.append(svg_path)
        return
    rendered = render_companion_svg(TargetRenderer.D2, d2_source)
    if rendered is not None:
        svg_path.write_bytes(rendered.encode("utf-8"))
        written.append(svg_path)


def _build_structural_stages(
    observed: StructuralGraphIR,
    emitted: StructuralGraphIR,
    view: DiagramViewSpec,
    spec: CurationSpecV1,
    overflow_md: str,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> tuple[str, str, str, str, TriageStats]:
    mermaid = MermaidEmitter(theme)
    d2 = D2Emitter()
    raw_mmd = mermaid.emit(observed)
    stage1_d2 = d2.emit(observed)
    rules = (*spec.global_abstractions, *view.rules)
    disps, collapse_targets = _classify_structural_nodes(observed, emitted, rules)
    stage2_d2 = _emit_structural_triage_d2(stage1_d2, observed, emitted, disps, collapse_targets)
    d2_view = view.model_copy(update={"target_renderer": TargetRenderer.D2})
    mmd_view = view.model_copy(update={"target_renderer": TargetRenderer.MERMAID})
    stage3_d2 = emit_view_source(emitted, d2_view, overflow_md, theme)
    stage3_mmd = emit_view_source(emitted, mmd_view, overflow_md, theme)
    stats = TriageStats(
        view_id=view.id,
        raw_elements=len(observed.nodes),
        raw_links=len(observed.edges),
        raw_mermaid_chars=len(raw_mmd),
        curated_elements=len(emitted.nodes),
        curated_links=len(emitted.edges),
        curated_mermaid_chars=len(stage3_mmd),
        kept_count=sum(1 for d in disps.values() if d is NodeDisposition.KEEP),
        removed_count=sum(1 for d in disps.values() if d is NodeDisposition.REMOVE),
        bypassed_count=sum(1 for d in disps.values() if d is NodeDisposition.BYPASS),
        collapsed_count=sum(1 for d in disps.values() if d is NodeDisposition.COLLAPSE),
    )
    return stage1_d2, stage2_d2, stage3_d2, stage3_mmd, stats


def _build_trace_stages(
    observed: InteractionTraceIR,
    curated: InteractionTraceIR,
    view: DiagramViewSpec,
    theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> tuple[str, str, str, str, TriageStats]:
    raw_mmd = MermaidEmitter(theme).emit(observed)
    stage1_d2 = _emit_trace_raw_call_d2(observed)
    stage2_d2 = _emit_trace_triage_d2(observed, curated)
    d2_view = view.model_copy(update={"target_renderer": TargetRenderer.D2})
    mmd_view = view.model_copy(update={"target_renderer": TargetRenderer.MERMAID})
    stage3_d2 = emit_view_source(curated, d2_view, "", theme)
    stage3_mmd = emit_view_source(curated, mmd_view, "", theme)
    cur_ids = set(curated.events)
    folded_backing = {
        bid
        for ev in curated.events.values()
        if ev.provenance.grade is ProvenanceGrade.DERIVED_DETERMINISTIC
        for bid in ev.provenance.backing_ids
    }
    kept = sum(1 for eid in observed.events if eid in cur_ids)
    collapsed = sum(1 for eid in observed.events if eid not in cur_ids and eid in folded_backing)
    removed = max(0, len(observed.events) - kept - collapsed)
    stats = TriageStats(
        view_id=view.id,
        raw_elements=len(observed.participants),
        raw_links=len(observed.events),
        raw_mermaid_chars=len(raw_mmd),
        curated_elements=len(curated.participants),
        curated_links=len(curated.events),
        curated_mermaid_chars=len(stage3_mmd),
        kept_count=kept,
        removed_count=removed,
        bypassed_count=0,
        collapsed_count=collapsed,
    )
    return stage1_d2, stage2_d2, stage3_d2, stage3_mmd, stats


def compute_triage_stages(
    observed_ir: CanonicalIR,
    spec: CurationSpecV1,
    view_id: str,
    mermaid_theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> TriageOutcome:
    """Compile a view and generate Stage 1 (Original), Stage 2 (Triage), and Stage 3 (Curated)."""
    safe_view_id = validate_safe_view_id(view_id)
    matching = [v for v in spec.views if v.id == safe_view_id]
    if not matching:
        msg = f"View '{safe_view_id}' not found in spec (available: {[v.id for v in spec.views]})"
        raise ValueError(msg)
    single_spec = spec.model_copy(update={"views": (matching[0],)})
    compilation = compile_and_lint_pipeline(observed_ir, single_spec, mermaid_theme=mermaid_theme)
    artifact = compilation.views[safe_view_id]
    if isinstance(observed_ir, StructuralGraphIR) and isinstance(
        artifact.emitted_ir, StructuralGraphIR
    ):
        s1, s2, s3_d2, s3_mmd, stats = _build_structural_stages(
            observed_ir,
            artifact.emitted_ir,
            artifact.view,
            single_spec,
            artifact.overflow_markdown,
            mermaid_theme,
        )
    elif isinstance(observed_ir, InteractionTraceIR) and isinstance(
        artifact.emitted_ir, InteractionTraceIR
    ):
        s1, s2, s3_d2, s3_mmd, stats = _build_trace_stages(
            observed_ir, artifact.emitted_ir, artifact.view, mermaid_theme
        )
    else:
        msg = f"IR mode mismatch between input '{observed_ir.ir_mode}' and view '{safe_view_id}'"
        raise ValueError(msg)
    return TriageOutcome(
        compilation=compilation,
        stats=stats,
        stage1_d2=s1,
        stage2_d2=s2,
        stage3_d2=s3_d2,
        stage3_mermaid=s3_mmd,
    )


def execute_triage(options: TriageOptions) -> TriageOutcome:
    """Run 3-stage triage generation (or `--check` verification) on disk."""
    vid = validate_safe_view_id(options.view_id)
    observed_ir = load_canonical_ir(options.input_path, options.input_format)
    spec = load_curation_spec(options.spec_path)
    theme = load_mermaid_theme(options.spec_path)
    base = compute_triage_stages(observed_ir, spec, vid, theme)
    if not options.check_only and not base.compilation.lint_report.passed:
        return base

    written: list[Path] = []
    drifted: list[Path] = []
    missing: list[Path] = []
    buckets = (written, drifted, missing)
    _sync_d2_and_svg(
        resolve_contained_output_path(options.out_dir, f"{vid}.1_original.d2"),
        base.stage1_d2,
        check_only=options.check_only,
        buckets=buckets,
    )
    _sync_d2_and_svg(
        resolve_contained_output_path(options.out_dir, f"{vid}.2_triage.d2"),
        base.stage2_d2,
        check_only=options.check_only,
        buckets=buckets,
    )
    _sync_d2_and_svg(
        resolve_contained_output_path(options.out_dir, f"{vid}.3_curated.d2"),
        base.stage3_d2,
        check_only=options.check_only,
        buckets=buckets,
    )
    _sync_text_file(
        resolve_contained_output_path(options.out_dir, f"{vid}.3_curated.mmd"),
        base.stage3_mermaid,
        check_only=options.check_only,
        buckets=buckets,
    )
    return TriageOutcome(
        compilation=base.compilation,
        stats=base.stats,
        stage1_d2=base.stage1_d2,
        stage2_d2=base.stage2_d2,
        stage3_d2=base.stage3_d2,
        stage3_mermaid=base.stage3_mermaid,
        written_files=tuple(written),
        drifted_files=tuple(drifted),
        missing_files=tuple(missing),
    )
