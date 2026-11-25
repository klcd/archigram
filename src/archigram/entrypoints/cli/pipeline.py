"""Zero-LLM CLI Data Plane compilation, ingestion, reduction, emission, and 5-Gate verification."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, TypeAdapter

from archigram.domain.diagnostics import (
    DiagnosticSeverity,
    GateId,
    LintDiagnostic,
    LintReport,
)
from archigram.domain.ir import (
    CanonicalIR,
    EdgeRelation,
    InteractionTraceIR,
    StructuralGraphIR,
)
from archigram.domain.spec import (
    CurationSpecV1,
    DiagramVariant,
    DiagramViewSpec,
    IRMode,
    TargetRenderer,
    parse_curation_spec_yaml,
)
from archigram.domain.style import (
    DEFAULT_MERMAID_THEME,
    NEUTRAL_BORDER_HEX,
    TRANSPARENT_FILL,
    LinePattern,
    MermaidThemeConfig,
    parse_mermaid_theme_yaml,
    resolve_edge_visual_style,
)
from archigram.features.emit_d2 import D2Emitter
from archigram.features.emit_likec4 import LikeC4Emitter
from archigram.features.emit_mermaid import MermaidEmitter
from archigram.features.emit_plantuml import PlantUmlEmitter
from archigram.features.ingest_class import ClassHierarchyIngestor
from archigram.features.ingest_dot import DotStructuralIngestor
from archigram.features.ingest_otel import OtelTraceIngestor
from archigram.features.ingest_python import (
    PythonAstStructuralIngestor,
    PythonTraceIngestor,
)
from archigram.features.ingest_scip import ScipStructuralIngestor
from archigram.features.lint_budget import (
    CognitiveBudgetAndOrphanGate,
    apply_spanning_tree_edge_fallback,
)
from archigram.features.lint_grounding import GroundingAndBoundaryGate
from archigram.features.lint_syntax import Gate5SyntaxHarness, build_default_validators
from archigram.features.lint_topology import TopologicalInvariantGate
from archigram.features.reduce_structural import (
    CycleInducedError,
    StructuralTopologicalReducer,
)
from archigram.features.reduce_trace import TraceCompactionEngine

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from archigram.ports.contracts import DiagramEmitterPort

_CANONICAL_IR_ADAPTER: TypeAdapter[CanonicalIR] = TypeAdapter(CanonicalIR)


class InputFormat(StrEnum):
    """Supported ingestion formats for the CLI data plane."""

    AUTO = "auto"
    IR_JSON = "ir_json"
    DOT = "dot"
    SCIP = "scip"
    OTLP_JSON = "otlp_json"
    CLASS_JSON = "class_json"
    PYTHON_AST = "python_ast"
    PYTHON_TRACE = "python_trace"


_RENDERER_EXTENSIONS: dict[TargetRenderer, str] = {
    TargetRenderer.MERMAID: ".mmd",
    TargetRenderer.D2: ".d2",
    TargetRenderer.PLANTUML: ".puml",
    TargetRenderer.LIKEC4: ".c4",
    TargetRenderer.DOT: ".dot",
}


@dataclass(frozen=True, slots=True)
class CompiledViewArtifact:
    """Deterministic compilation result for a single DiagramViewSpec."""

    view: DiagramViewSpec
    curated_ir: CanonicalIR
    emitted_ir: CanonicalIR
    diagram_source: str
    file_extension: str
    overflow_markdown: str = ""

    @property
    def output_filename(self) -> str:
        """Return canonical filename `<view.id><ext>`."""
        return f"{self.view.id}{self.file_extension}"

    @property
    def overflow_filename(self) -> str:
        """Return companion overflow table filename `<view.id>.overflow.md`."""
        return f"{self.view.id}.overflow.md"


@dataclass(frozen=True, slots=True)
class PipelineCompilationResult:
    """Aggregated outcome of compiling and linting a CurationSpecV1 against an observed IR."""

    observed_ir: CanonicalIR
    spec: CurationSpecV1
    views: dict[str, CompiledViewArtifact]
    lint_report: LintReport
    extra_diagnostics: tuple[LintDiagnostic, ...] = field(default_factory=tuple)


def renderer_file_extension(renderer: TargetRenderer) -> str:
    """Return the canonical file extension (including leading dot) for a TargetRenderer."""
    return _RENDERER_EXTENSIONS[renderer]


def _resolve_existing_dir(source: Path | str) -> Path | None:
    if isinstance(source, Path):
        if source.is_dir():
            return source
        return None
    if not source or any(ch in source for ch in ("\n", "{", "}", ";", ":")) or len(source) > 1024:
        return None
    try:
        candidate = Path(source)
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def _ingest_python_ast_source(source: Path | str) -> StructuralGraphIR:
    ir = PythonAstStructuralIngestor().ingest(source)
    if ir.metadata.get("file_count") == "0" or not ir.nodes:
        msg = f"Python source directory contains zero .py files: {source}"
        raise FileNotFoundError(msg)
    return ir


def _read_source_text(source: Path | str) -> tuple[str, Path | None]:
    if isinstance(source, Path):
        if not source.is_file():
            msg = f"Input file does not exist: {source}"
            raise FileNotFoundError(msg)
        return source.read_text(encoding="utf-8"), source

    try:
        candidate = Path(source)
        is_existing_file = candidate.is_file()
    except OSError:
        candidate = None
        is_existing_file = False

    if is_existing_file and candidate is not None:
        return candidate.read_text(encoding="utf-8"), candidate
    is_inline_payload = any(ch in source for ch in ("\n", "{", "}", ";", ":"))
    if not is_inline_payload and len(source) < 256 and Path(source).suffix:
        msg = f"Input file does not exist: {source}"
        raise FileNotFoundError(msg)
    return source, None


def load_curation_spec(spec_source: Path | str) -> CurationSpecV1:
    """Load and strictly validate a CurationSpecV1 from a YAML file path or YAML string."""
    raw_yaml, _ = _read_source_text(spec_source)
    if not raw_yaml.strip():
        msg = "Curation spec YAML must not be empty"
        raise ValueError(msg)
    return parse_curation_spec_yaml(raw_yaml)


MERMAID_THEME_FILENAME = "mermaid-theme.yaml"


def load_mermaid_theme(
    spec_source: Path | str | None = None,
    theme_path: Path | None = None,
) -> MermaidThemeConfig:
    """Load central MermaidThemeConfig from `theme_path` or sibling `mermaid-theme.yaml`."""
    if theme_path is not None:
        if not theme_path.is_file():
            msg = f"Mermaid theme file does not exist: {theme_path}"
            raise FileNotFoundError(msg)
        return parse_mermaid_theme_yaml(theme_path.read_text(encoding="utf-8"))
    if isinstance(spec_source, Path) and spec_source.is_file():
        sibling = spec_source.parent / MERMAID_THEME_FILENAME
        if sibling.is_file():
            return parse_mermaid_theme_yaml(sibling.read_text(encoding="utf-8"))
    return DEFAULT_MERMAID_THEME


def _detect_json_format(payload: object) -> InputFormat:
    if not isinstance(payload, dict):
        msg = "JSON input root must be an object"
        raise ValueError(msg)
    detected = InputFormat.IR_JSON
    if payload.get("ir_mode") in {"structural_graph", "interaction_trace"}:
        detected = InputFormat.IR_JSON
    elif any(k in payload for k in ("resourceSpans", "resource_spans", "spans")):
        detected = InputFormat.OTLP_JSON
    elif isinstance(payload.get("events"), list):
        detected = InputFormat.PYTHON_TRACE
    elif any(k in payload for k in ("documents", "external_symbols", "symbols")):
        detected = InputFormat.SCIP
    elif any(k in payload for k in ("classes", "entities", "types", "relationships")):
        detected = InputFormat.CLASS_JSON
    return detected


_SUFFIX_FORMATS: dict[str, InputFormat] = {
    ".dot": InputFormat.DOT,
    ".gv": InputFormat.DOT,
    ".puml": InputFormat.CLASS_JSON,
    ".scip": InputFormat.SCIP,
    ".py": InputFormat.PYTHON_AST,
}


def detect_input_format(
    raw_text: str,
    source_path: Path | None = None,
    requested: InputFormat | str = InputFormat.AUTO,
) -> InputFormat:
    """Resolve explicit or auto-detected InputFormat from file suffix and payload content."""
    fmt = (
        requested if isinstance(requested, InputFormat) else InputFormat(requested.strip().lower())
    )
    if fmt is not InputFormat.AUTO:
        return fmt

    if source_path is not None and source_path.is_dir():
        return InputFormat.PYTHON_AST

    suffix_fmt = (
        _SUFFIX_FORMATS.get(source_path.suffix.lower()) if source_path is not None else None
    )
    if suffix_fmt is not None:
        return suffix_fmt

    stripped = raw_text.lstrip()
    if not stripped:
        msg = "Cannot detect format of empty input"
        raise ValueError(msg)

    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            msg = f"Invalid JSON input: {exc}"
            raise ValueError(msg) from exc
        return _detect_json_format(parsed)

    return InputFormat.CLASS_JSON if stripped.lower().startswith("@startuml") else InputFormat.DOT


def _dispatch_format_ingestor(
    resolved_fmt: InputFormat,
    raw_text: str,
    source_path: Path | None,
) -> CanonicalIR:
    effective_arg: Path | str = source_path if source_path is not None else raw_text
    source_name = str(source_path) if source_path is not None else "<memory>"
    result: CanonicalIR
    if resolved_fmt is InputFormat.IR_JSON:
        try:
            data: object = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            msg = f"Invalid Canonical IR JSON: {exc}"
            raise ValueError(msg) from exc
        result = _CANONICAL_IR_ADAPTER.validate_python(data)
    elif resolved_fmt is InputFormat.PYTHON_AST:
        result = _ingest_python_ast_source(effective_arg)
    elif resolved_fmt is InputFormat.PYTHON_TRACE:
        result = PythonTraceIngestor().ingest(effective_arg)
    elif resolved_fmt is InputFormat.DOT:
        result = DotStructuralIngestor().ingest(effective_arg)
    elif resolved_fmt is InputFormat.SCIP:
        result = ScipStructuralIngestor().ingest(effective_arg, source_name=source_name)
    elif resolved_fmt is InputFormat.OTLP_JSON:
        result = OtelTraceIngestor().ingest(effective_arg, source_name=source_name)
    else:
        result = ClassHierarchyIngestor().ingest(effective_arg)
    return result


def load_canonical_ir(
    source: Path | str,
    input_format: InputFormat | str = InputFormat.AUTO,
) -> CanonicalIR:
    """Ingest or deserialize an input path/payload into a validated CanonicalIR."""
    req_fmt = (
        input_format
        if isinstance(input_format, InputFormat)
        else InputFormat(input_format.strip().lower())
    )
    dir_path = _resolve_existing_dir(source)
    if dir_path is not None:
        if req_fmt not in {InputFormat.AUTO, InputFormat.PYTHON_AST}:
            msg = f"Directory input '{dir_path}' is only supported for python_ast format"
            raise ValueError(msg)
        return _ingest_python_ast_source(dir_path)

    if isinstance(source, Path) and source.suffix.lower() == ".py":
        if not source.is_file():
            msg = f"Input file does not exist: {source}"
            raise FileNotFoundError(msg)
        if req_fmt in {InputFormat.AUTO, InputFormat.PYTHON_AST}:
            return _ingest_python_ast_source(source)

    raw_text, source_path = _read_source_text(source)
    resolved_fmt = detect_input_format(raw_text, source_path, req_fmt)
    return _dispatch_format_ingestor(resolved_fmt, raw_text, source_path)


def _escape_dot_str(value: str) -> str:
    cleaned = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", "\\n")
    )
    return f'"{cleaned}"'


def _dot_preamble_lines(graph_title: str) -> list[str]:
    return [
        f"digraph {_escape_dot_str(graph_title)} {{",
        '  rankdir="LR";',
        f"  bgcolor={_escape_dot_str(TRANSPARENT_FILL)};",
        '  splines="ortho";',
        (
            f'  node [shape="box", style="rounded", '
            f"color={_escape_dot_str(NEUTRAL_BORDER_HEX)}, "
            f"fontcolor={_escape_dot_str(NEUTRAL_BORDER_HEX)}];"
        ),
        f'  edge [penwidth="1.5", fontcolor={_escape_dot_str(NEUTRAL_BORDER_HEX)}];',
    ]


def _dot_line_style(pattern: LinePattern) -> str:
    if pattern is LinePattern.DASHED:
        return "dashed"
    if pattern is LinePattern.BOLD:
        return "bold"
    return "solid"


def _emit_structural_dot(ir: StructuralGraphIR) -> str:
    lines = _dot_preamble_lines(ir.graph_id)
    children_by_parent: dict[str | None, list[str]] = {}
    for sym_id in sorted(ir.nodes):
        parent_id = ir.nodes[sym_id].parent_id
        parent_key = parent_id if (parent_id is not None and parent_id in ir.nodes) else None
        children_by_parent.setdefault(parent_key, []).append(sym_id)

    def _emit_level(parent_key: str | None, indent: str) -> None:
        for sym_id in children_by_parent.get(parent_key, ()):
            node = ir.nodes[sym_id]
            child_ids = children_by_parent.get(sym_id, [])
            kind_val = _escape_dot_str(node.kind.value)
            node_attrs = f"[label={_escape_dot_str(node.label)}, kind={kind_val}, _kind={kind_val}]"
            if child_ids:
                cluster_id = f"cluster_{sym_id}"
                lines.append(f"{indent}subgraph {_escape_dot_str(cluster_id)} {{")
                lines.append(f"{indent}  label={_escape_dot_str(node.label)};")
                lines.append(f"{indent}  bgcolor={_escape_dot_str(TRANSPARENT_FILL)};")
                lines.append(f'{indent}  style="rounded,dashed";')
                lines.append(f"{indent}  color={_escape_dot_str(NEUTRAL_BORDER_HEX)};")
                lines.append(f"{indent}  fontcolor={_escape_dot_str(NEUTRAL_BORDER_HEX)};")
                lines.append(f"{indent}  {_escape_dot_str(sym_id)} {node_attrs};")
                _emit_level(sym_id, indent + "  ")
                lines.append(f"{indent}}}")
            else:
                lines.append(f"{indent}{_escape_dot_str(sym_id)} {node_attrs};")

    _emit_level(None, "  ")
    for edge in sorted(ir.edges, key=lambda e: (e.source, e.target, e.relation.value, e.edge_id)):
        visual = resolve_edge_visual_style(edge.relation, edge.sync_kind)
        style = _dot_line_style(visual.line_pattern)
        rel_str = _escape_dot_str(edge.relation.value)
        lines.append(
            f"  {_escape_dot_str(edge.source)} -> {_escape_dot_str(edge.target)} "
            f"[xlabel={rel_str}, _rel_kind={rel_str}, "
            f"_sync_kind={_escape_dot_str(edge.sync_kind.value)}, "
            f"style={_escape_dot_str(style)}, "
            f"color={_escape_dot_str(visual.stroke_hex)}, "
            f"arrowhead={_escape_dot_str(visual.dot_arrowhead)}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _emit_trace_dot(ir: InteractionTraceIR) -> str:
    lines = _dot_preamble_lines(ir.trace_id)
    for pid in sorted(ir.participants):
        participant = ir.participants[pid]
        kind_val = _escape_dot_str(participant.kind.value)
        lines.append(
            f"  {_escape_dot_str(pid)} "
            f"[label={_escape_dot_str(participant.label)}, kind={kind_val}, _kind={kind_val}];"
        )
    ordered_events = sorted(
        ir.events.values(),
        key=lambda ev: (ev.start_ns, ev.end_ns, ev.event_id),
    )
    for event in ordered_events:
        visual = resolve_edge_visual_style(EdgeRelation.CALLS, event.sync_kind)
        style = _dot_line_style(visual.line_pattern)
        lines.append(
            f"  {_escape_dot_str(event.caller_id)} -> {_escape_dot_str(event.callee_id)} "
            f"[xlabel={_escape_dot_str(event.signature)}, "
            f"style={_escape_dot_str(style)}, "
            f"color={_escape_dot_str(visual.stroke_hex)}, "
            f"arrowhead={_escape_dot_str(visual.dot_arrowhead)}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def emit_deterministic_dot(ir: CanonicalIR) -> str:
    """Emit deterministic Graphviz `.dot` source for StructuralGraphIR or InteractionTraceIR."""
    if isinstance(ir, StructuralGraphIR):
        return _emit_structural_dot(ir)
    return _emit_trace_dot(ir)


_EMITTERS: dict[TargetRenderer, DiagramEmitterPort] = {
    TargetRenderer.MERMAID: MermaidEmitter(),
    TargetRenderer.D2: D2Emitter(),
    TargetRenderer.PLANTUML: PlantUmlEmitter(),
    TargetRenderer.LIKEC4: LikeC4Emitter(),
}


def _append_overflow_comment(
    diagram_source: str,
    renderer: TargetRenderer,
    overflow_filename: str,
    overflow_markdown: str,
) -> str:
    if not overflow_markdown:
        return diagram_source
    prefix_map: dict[TargetRenderer, str] = {
        TargetRenderer.MERMAID: "%%",
        TargetRenderer.D2: "#",
        TargetRenderer.PLANTUML: "'",
        TargetRenderer.LIKEC4: "//",
        TargetRenderer.DOT: "//",
    }
    prefix = prefix_map[renderer]
    row_count = max(0, len(overflow_markdown.splitlines()) - 2)
    comment_lines = [
        f"{prefix} Companion overflow table ({row_count} omitted edges): {overflow_filename}",
        *(f"{prefix} {line}" for line in overflow_markdown.splitlines()),
    ]
    if renderer is TargetRenderer.PLANTUML:
        stripped = diagram_source.rstrip("\n")
        if stripped.endswith("@enduml"):
            body = stripped[: -len("@enduml")].rstrip("\n")
            return "\n".join([body, *comment_lines, "@enduml"]) + "\n"
    return diagram_source.rstrip("\n") + "\n" + "\n".join(comment_lines) + "\n"


def emit_view_source(
    ir: CanonicalIR,
    view: DiagramViewSpec,
    overflow_markdown: str = "",
    mermaid_theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> str:
    """Emit deterministic target diagram source for a view, appending overflow comment if any."""
    meta_updates: dict[str, str] = {}
    if view.diagram_variant is not DiagramVariant.AUTO:
        meta_updates["diagram_variant"] = view.diagram_variant.value
    if view.animate_edges is not None:
        meta_updates["animate_edges"] = "true" if view.animate_edges else "false"
    effective_ir = (
        ir.model_copy(update={"metadata": {**ir.metadata, **meta_updates}}) if meta_updates else ir
    )
    if view.target_renderer is TargetRenderer.DOT:
        raw_source = emit_deterministic_dot(effective_ir)
    elif view.target_renderer is TargetRenderer.MERMAID:
        raw_source = MermaidEmitter(mermaid_theme).emit(effective_ir)
    else:
        emitter = _EMITTERS[view.target_renderer]
        raw_source = emitter.emit(effective_ir)
    overflow_file = f"{view.id}.overflow.md"
    return _append_overflow_comment(
        raw_source, view.target_renderer, overflow_file, overflow_markdown
    )


def _reduce_single_view(
    observed_ir: CanonicalIR,
    view: DiagramViewSpec,
    spec: CurationSpecV1,
    structural_reducer: StructuralTopologicalReducer,
    trace_compactor: TraceCompactionEngine,
) -> tuple[CanonicalIR, CanonicalIR, str, list[LintDiagnostic]]:
    if view.ir_mode.value != observed_ir.ir_mode:
        diag = LintDiagnostic(
            gate=GateId.GATE_1_GROUNDING,
            severity=DiagnosticSeverity.ERROR,
            code="VIEW_IR_MODE_MISMATCH",
            message=(
                f"View '{view.id}' expects ir_mode='{view.ir_mode.value}', "
                f"but input Canonical IR has ir_mode='{observed_ir.ir_mode}'."
            ),
            view_id=view.id,
            offending_symbols=(view.id,),
            repair_hint=(
                f"Align view '{view.id}' ir_mode with the input IR ('{observed_ir.ir_mode}') "
                "or provide a matching input payload."
            ),
        )
        return observed_ir, observed_ir, "", [diag]

    if isinstance(observed_ir, StructuralGraphIR) and view.ir_mode is IRMode.STRUCTURAL_GRAPH:
        try:
            curated = structural_reducer.reduce(
                observed_ir,
                view,
                spec.global_abstractions,
            )
        except CycleInducedError as exc:
            curated = structural_reducer.reduce(
                observed_ir,
                view.model_copy(
                    update={
                        "invariants": view.invariants.model_copy(
                            update={"forbid_new_cycles": False}
                        )
                    }
                ),
                spec.global_abstractions,
            )
            _ = exc
        overflow_md = ""
        emitted: CanonicalIR = curated
        if (
            view.invariants.overflow_strategy == "spanning_tree_table"
            and len(curated.edges) > view.invariants.edge_budget_max
        ):
            emitted, overflow_md = apply_spanning_tree_edge_fallback(
                curated,
                view.invariants.edge_budget_max,
            )
        return curated, emitted, overflow_md, []

    if isinstance(observed_ir, InteractionTraceIR) and view.ir_mode is IRMode.INTERACTION_TRACE:
        curated_trace = trace_compactor.compact(observed_ir, view.trace_compaction)
        return curated_trace, curated_trace, "", []

    return observed_ir, observed_ir, "", []


def _run_gate5_on_views(
    compiled_views: Mapping[str, CompiledViewArtifact],
    syntax_harness: Gate5SyntaxHarness,
) -> list[LintDiagnostic]:
    diagnostics: list[LintDiagnostic] = []
    for view_id, artifact in compiled_views.items():
        target_fmt = artifact.view.target_renderer.value
        result = syntax_harness.validate(target_fmt, artifact.diagram_source)
        if not result.valid:
            err_summary = "; ".join(result.errors) if result.errors else "Invalid diagram syntax"
            diagnostics.append(
                LintDiagnostic(
                    gate=GateId.GATE_5_SYNTAX,
                    severity=DiagnosticSeverity.ERROR,
                    code="SYNTAX_VALIDATION_FAILED",
                    message=(
                        f"View '{view_id}' ({target_fmt}) failed Gate 5 syntax validation: "
                        f"{err_summary}"
                    ),
                    view_id=view_id,
                    offending_symbols=(view_id,),
                    repair_hint=(
                        f"Inspect emitted '{target_fmt}' syntax for view '{view_id}' and fix "
                        "malformed labels or unsupported constructs."
                    ),
                )
            )
    return diagnostics


def compile_and_lint_pipeline(
    observed_ir: CanonicalIR,
    spec: CurationSpecV1,
    *,
    use_external_syntax_cli: bool = False,
    mermaid_theme: MermaidThemeConfig = DEFAULT_MERMAID_THEME,
) -> PipelineCompilationResult:
    """Compile all views in a CurationSpecV1 and execute Gates 1-5 deterministically."""
    structural_reducer = StructuralTopologicalReducer()
    trace_compactor = TraceCompactionEngine()

    compiled_views: dict[str, CompiledViewArtifact] = {}
    curated_by_id: dict[str, CanonicalIR] = {}
    pre_diagnostics: list[LintDiagnostic] = []

    for view in spec.views:
        curated_ir, emitted_ir, overflow_md, view_diags = _reduce_single_view(
            observed_ir,
            view,
            spec,
            structural_reducer,
            trace_compactor,
        )
        pre_diagnostics.extend(view_diags)
        curated_by_id[view.id] = curated_ir
        diagram_source = emit_view_source(emitted_ir, view, overflow_md, mermaid_theme)
        compiled_views[view.id] = CompiledViewArtifact(
            view=view,
            curated_ir=curated_ir,
            emitted_ir=emitted_ir,
            diagram_source=diagram_source,
            file_extension=renderer_file_extension(view.target_renderer),
            overflow_markdown=overflow_md,
        )

    gate_1_2 = GroundingAndBoundaryGate().check(observed_ir, spec, curated_by_id)
    gate_3 = TopologicalInvariantGate().check(observed_ir, spec, curated_by_id)
    gate_4 = CognitiveBudgetAndOrphanGate().check(observed_ir, spec, curated_by_id)

    syntax_harness = Gate5SyntaxHarness(
        validators=build_default_validators(use_cli=use_external_syntax_cli)
    )
    gate_5_diags = _run_gate5_on_views(compiled_views, syntax_harness)

    all_diagnostics: tuple[LintDiagnostic, ...] = (
        *pre_diagnostics,
        *gate_1_2.diagnostics,
        *gate_3.diagnostics,
        *gate_4.diagnostics,
        *gate_5_diags,
    )
    lint_report = LintReport.from_diagnostics(all_diagnostics)
    return PipelineCompilationResult(
        observed_ir=observed_ir,
        spec=spec,
        views=compiled_views,
        lint_report=lint_report,
        extra_diagnostics=tuple(pre_diagnostics),
    )


@dataclass(frozen=True, slots=True)
class ApplySyncOutcome:
    """Outcome of writing or checking compiled diagram artifacts on disk."""

    compilation: PipelineCompilationResult
    written_files: tuple[Path, ...] = ()
    drifted_files: tuple[Path, ...] = ()
    missing_files: tuple[Path, ...] = ()
    synced_md_files: tuple[Path, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Return True when Gates 1-5 passed and zero file drift/missing files occurred."""
        return (
            self.compilation.lint_report.passed
            and not self.drifted_files
            and not self.missing_files
        )


def _check_or_write_single_file(
    target_path: Path,
    expected_content: str,
    *,
    check_only: bool,
    written: list[Path],
    drifted: list[Path],
    missing: list[Path],
) -> None:
    expected_bytes = expected_content.encode("utf-8")
    if check_only:
        if not target_path.is_file():
            missing.append(target_path)
            return
        actual_bytes = target_path.read_bytes()
        if actual_bytes != expected_bytes:
            drifted.append(target_path)
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(expected_bytes)
    written.append(target_path)


def _stamp_svg_with_source_hash(raw_svg: str, source_sha256: str) -> str:
    stamp_line = f"<!-- archigram-source-sha256: {source_sha256} -->"
    stripped = raw_svg.lstrip()
    if stripped.startswith("<?xml"):
        newline_idx = stripped.find("\n")
        if newline_idx != -1:
            return f"{stripped[: newline_idx + 1]}{stamp_line}\n{stripped[newline_idx + 1 :]}"
    return f"{stamp_line}\n{stripped}"


_D2_DUAL_MODE_SVG_CSS = (
    '.appendix-icon path[fill="white"]{fill:#f8fafc;stroke:#94a3b8;}\n'
    "@media screen and (prefers-color-scheme:dark){\n"
    '.appendix-icon path[fill="white"]{fill:#1e293b !important;stroke:#64748b !important;}\n'
    '.appendix-icon path[stroke="#2E3346"]{stroke:#cbd5e1 !important;}\n'
    "}"
)

_DOT_DUAL_MODE_SVG_CSS = (
    "text{fill:#1e293b;}\n@media (prefers-color-scheme:dark){text{fill:#e2e8f0;}}"
)


def inject_svg_dual_mode_style(raw_svg: str, css_rules: str) -> str:
    """Inject a dual-mode `<style>` block immediately before the closing `</svg>` tag."""
    close_idx = raw_svg.rfind("</svg>")
    if close_idx == -1:
        return raw_svg
    style_tag = f'<style type="text/css"><![CDATA[\n{css_rules}\n]]></style>'
    return f"{raw_svg[:close_idx]}{style_tag}{raw_svg[close_idx:]}"


def _render_d2_svg(binary: str, diagram_source: str) -> str:
    has_steps = any(line.strip() == "steps: {" for line in diagram_source.splitlines())
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        in_path = tmp_dir / "diagram.d2"
        out_path = tmp_dir / "diagram.svg"
        in_path.write_text(diagram_source, encoding="utf-8")
        cli_args = (
            [binary, "--animate-interval", "1200", "--pad", "24", str(in_path), str(out_path)]
            if has_steps
            else [binary, "--pad", "24", str(in_path), str(out_path)]
        )
        proc = subprocess.run(  # noqa: S603
            cli_args,
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
        svg_text = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
    if proc.returncode != 0 or "<svg" not in svg_text:
        msg = f"Failed to render SVG via 'd2': {(proc.stderr or proc.stdout).strip()}"
        raise ValueError(msg)
    return inject_svg_dual_mode_style(svg_text, _D2_DUAL_MODE_SVG_CSS)


def render_companion_svg(renderer: TargetRenderer, diagram_source: str) -> str | None:
    """Compile a D2 or DOT source string to SVG stamped with its source SHA-256 when CLI exists."""
    source_sha = hashlib.sha256(diagram_source.encode("utf-8")).hexdigest()
    if renderer is TargetRenderer.D2:
        d2_bin = shutil.which("d2")
        if d2_bin is None:
            return None
        return _stamp_svg_with_source_hash(_render_d2_svg(d2_bin, diagram_source), source_sha)
    if renderer is TargetRenderer.DOT:
        dot_bin = shutil.which("dot")
        if dot_bin is None:
            return None
        proc = subprocess.run(  # noqa: S603
            [dot_bin, "-Tsvg"],
            input=diagram_source,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if proc.returncode != 0 or "<svg" not in proc.stdout:
            msg = f"Failed to render SVG via 'dot': {(proc.stderr or proc.stdout).strip()}"
            raise ValueError(msg)
        styled_dot = inject_svg_dual_mode_style(proc.stdout, _DOT_DUAL_MODE_SVG_CSS)
        return _stamp_svg_with_source_hash(styled_dot, source_sha)
    return None


def _sync_companion_svg(
    artifact: CompiledViewArtifact,
    out_dir: Path,
    *,
    check_only: bool,
    written: list[Path],
    drifted: list[Path],
    missing: list[Path],
) -> None:
    svg_path = out_dir / f"{artifact.output_filename}.svg"
    source_sha = hashlib.sha256(artifact.diagram_source.encode("utf-8")).hexdigest()
    expected_token = f"archigram-source-sha256: {source_sha}"
    if check_only:
        if not svg_path.is_file():
            missing.append(svg_path)
            return
        actual_svg = svg_path.read_text(encoding="utf-8")
        if "<svg" not in actual_svg or expected_token not in actual_svg:
            drifted.append(svg_path)
        return

    rendered = render_companion_svg(artifact.view.target_renderer, artifact.diagram_source)
    if rendered is not None:
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_bytes(rendered.encode("utf-8"))
        written.append(svg_path)


def sync_compiled_artifacts(
    compilation: PipelineCompilationResult,
    out_dir: Path,
    *,
    check_only: bool = False,
    sync_md_target: Path | None = None,
) -> ApplySyncOutcome:
    """Write compiled views to `out_dir` or verify byte-for-byte parity when `check_only=True`."""
    from archigram.entrypoints.cli.markdown_sync import sync_markdown_views

    written: list[Path] = []
    drifted: list[Path] = []
    missing: list[Path] = []
    synced_md: list[Path] = []

    if not check_only and not compilation.lint_report.passed:
        return ApplySyncOutcome(compilation=compilation)

    for artifact in compilation.views.values():
        diagram_path = out_dir / artifact.output_filename
        _check_or_write_single_file(
            diagram_path,
            artifact.diagram_source,
            check_only=check_only,
            written=written,
            drifted=drifted,
            missing=missing,
        )
        if artifact.overflow_markdown:
            overflow_path = out_dir / artifact.overflow_filename
            _check_or_write_single_file(
                overflow_path,
                artifact.overflow_markdown + "\n",
                check_only=check_only,
                written=written,
                drifted=drifted,
                missing=missing,
            )
        if artifact.view.render_svg:
            _sync_companion_svg(
                artifact,
                out_dir,
                check_only=check_only,
                written=written,
                drifted=drifted,
                missing=missing,
            )

    if sync_md_target is not None:
        md_outcome = sync_markdown_views(
            compilation.views,
            sync_md_target,
            check_only=check_only,
        )
        written.extend(md_outcome.synced_files)
        synced_md.extend(md_outcome.synced_files)
        drifted.extend(md_outcome.drifted_files)

    return ApplySyncOutcome(
        compilation=compilation,
        written_files=tuple(written),
        drifted_files=tuple(drifted),
        missing_files=tuple(missing),
        synced_md_files=tuple(synced_md),
    )


def _diagnostic_detail_lines(diag: LintDiagnostic) -> list[str]:
    """Render endpoint / symbol / witness / hint lines for one diagnostic (no fabricated paths)."""
    lines: list[str] = []
    symbols_label: str | None = "Symbols"
    if diag.code == "REACHABILITY_SEVERED" and diag.witness_path:
        lines.append(f"    Endpoints: {diag.witness_path[0]} -> {diag.witness_path[-1]}")
        symbols_label = "Bottlenecks"
    elif diag.code == "REACHABILITY_NOT_OBSERVED" and len(diag.offending_symbols) == 2:
        lines.append(f"    Endpoints: {diag.offending_symbols[0]} -> {diag.offending_symbols[1]}")
        symbols_label = None
    if diag.witness_path:
        lines.append(f"    Witness: {' -> '.join(diag.witness_path)}")
    if diag.offending_symbols and symbols_label is not None:
        lines.append(f"    {symbols_label}: {', '.join(diag.offending_symbols)}")
    lines.append(f"    Hint: {diag.repair_hint}")
    return lines


def format_lint_report_text(report: LintReport) -> str:
    """Format a LintReport into a deterministic human-readable CLI summary."""
    status = "PASSED" if report.passed else "FAILED"
    lines: list[str] = [
        f"ArchiGram 5-Gate Linter: {status} "
        f"({report.error_count} error(s), {report.warning_count} warning(s))"
    ]
    for diag in report.diagnostics:
        scope_parts: list[str] = [diag.gate.value]
        if diag.view_id is not None:
            scope_parts.append(f"view={diag.view_id}")
        if diag.rule_id is not None:
            scope_parts.append(f"rule={diag.rule_id}")
        scope_str = ", ".join(scope_parts)
        lines.append(f"  [{diag.severity.value.upper()}] {diag.code} ({scope_str}): {diag.message}")
        lines.extend(_diagnostic_detail_lines(diag))
    return "\n".join(lines)


def format_regeneration_command(
    subcommand: str,
    input_path: Path,
    flags: Sequence[tuple[str, str]],
) -> str:
    """Return the exact shell command that regenerates stale committed artifacts."""
    parts: list[str] = ["archigram", subcommand, shlex.quote(str(input_path))]
    for flag, value in flags:
        parts.extend((flag, shlex.quote(value)))
    return " ".join(parts)


class ApplyCheckReport(BaseModel):
    """Structured `apply` outcome for CI consumers (lint diagnostics + stale artifacts)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    is_clean: bool
    lint_report: LintReport
    missing_files: tuple[str, ...] = ()
    drifted_files: tuple[str, ...] = ()
    written_files: tuple[str, ...] = ()
    regeneration_command: str


def build_apply_report(outcome: ApplySyncOutcome, *, regeneration_command: str) -> ApplyCheckReport:
    """Project an ApplySyncOutcome into its structured, JSON-serializable CI report."""
    return ApplyCheckReport(
        passed=outcome.compilation.lint_report.passed,
        is_clean=outcome.is_clean,
        lint_report=outcome.compilation.lint_report,
        missing_files=tuple(str(p) for p in outcome.missing_files),
        drifted_files=tuple(str(p) for p in outcome.drifted_files),
        written_files=tuple(str(p) for p in outcome.written_files),
        regeneration_command=regeneration_command,
    )
