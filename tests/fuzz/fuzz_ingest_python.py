"""Hypothesis property-based fuzz suite for native Python AST & runtime trace ingestion."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import (
    EdgeRelation,
    EventStatus,
    InteractionTraceIR,
    NodeKind,
    StructuralGraphIR,
    Visibility,
)
from archigram.features.ingest_python import (
    PythonAstStructuralIngestor,
    PythonTraceIngestor,
    capture_python_trace,
)
from archigram.ports.contracts import StructuralIngestorPort, TraceIngestorPort

_LOWER_IDENT = st.from_regex(r"^[a-z][a-z0-9]{2,8}$", fullmatch=True)
_UPPER_IDENT = st.from_regex(r"^[A-Z][a-zA-Z0-9]{2,8}$", fullmatch=True)


@dataclass(frozen=True, slots=True)
class _GeneratedPkgSpec:
    """Generated multi-module Python package tree with ground-truth structural expectations."""

    pkg_name: str
    subpkg_name: str
    proto_name: str
    enum_name: str
    base_cls_name: str
    impl_cls_name: str
    field_name: str
    priv_field_name: str
    pub_fn_name: str
    priv_fn_name: str
    wrap_in_src: bool
    files: tuple[tuple[str, str], ...]


@st.composite
def _python_package_tree_strategy(draw: st.DrawFn) -> _GeneratedPkgSpec:
    """Generate a valid multi-module Python package with protocols, enums, inheritance & calls."""
    pkg_name = draw(_LOWER_IDENT)
    subpkg_name = draw(_LOWER_IDENT.filter(lambda s: s != pkg_name))
    names = draw(
        st.lists(
            _UPPER_IDENT,
            min_size=4,
            max_size=4,
            unique=True,
        )
    )
    proto_name = f"{names[0]}Port"
    enum_name = f"{names[1]}Kind"
    base_cls_name = f"{names[2]}Base"
    impl_cls_name = f"{names[3]}Service"

    fn_names = draw(st.lists(_LOWER_IDENT, min_size=4, max_size=4, unique=True))
    field_name, priv_field_name, pub_fn_name, priv_fn_name = fn_names
    wrap_in_src = draw(st.booleans())
    explicit_protocol_base = draw(st.booleans())
    impl_bases = f"{base_cls_name}, {proto_name}" if explicit_protocol_base else base_cls_name

    root_init = (
        "from __future__ import annotations\n"
        f"from .{subpkg_name}.models import {enum_name}, {proto_name}\n"
        f"__all__ = [{enum_name!r}, {proto_name!r}]\n"
    )
    sub_init = (
        "from __future__ import annotations\n"
        f"from .models import {base_cls_name}\n"
        f"__all__ = [{base_cls_name!r}]\n"
    )
    models_py = (
        "from __future__ import annotations\n"
        "from enum import StrEnum\n"
        "from typing import Annotated, Protocol\n\n"
        f"class {enum_name}(StrEnum):\n"
        '    ALPHA = "alpha"\n'
        '    BETA = "beta"\n\n'
        f"class {proto_name}(Protocol):\n"
        "    def execute(self, value: int) -> int: ...\n\n"
        f"class {base_cls_name}:\n"
        f"    mode: Annotated[{enum_name}, 'meta']\n"
        f"    _{priv_field_name}: int = 0\n\n"
        "    def _internal(self) -> int:\n"
        "        return 1\n\n"
        f"def helper_fn(x: int) -> int:\n"
        "    return x + 1\n"
    )
    service_py = (
        "from __future__ import annotations\n"
        f"from .{subpkg_name} import {base_cls_name}\n"
        f"from .{subpkg_name}.models import {enum_name}, {proto_name}, helper_fn\n\n"
        f"class {impl_cls_name}({impl_bases}):\n"
        f"    {field_name}: tuple[{enum_name}, ...] | None = None\n\n"
        "    def __init__(self) -> None:\n"
        f"        self._{priv_field_name}: int = 1\n\n"
        "    def execute(self, value: int) -> int:\n"
        "        return helper_fn(value)\n\n"
        f"def {pub_fn_name}(n: int) -> int:\n"
        f"    svc = {impl_cls_name}()\n"
        "    return svc.execute(n)\n\n"
        f"def _{priv_fn_name}() -> int:\n"
        "    return 0\n"
    )

    files = (
        (f"{pkg_name}/__init__.py", root_init),
        (f"{pkg_name}/{subpkg_name}/__init__.py", sub_init),
        (f"{pkg_name}/{subpkg_name}/models.py", models_py),
        (f"{pkg_name}/service.py", service_py),
    )
    return _GeneratedPkgSpec(
        pkg_name=pkg_name,
        subpkg_name=subpkg_name,
        proto_name=proto_name,
        enum_name=enum_name,
        base_cls_name=base_cls_name,
        impl_cls_name=impl_cls_name,
        field_name=field_name,
        priv_field_name=priv_field_name,
        pub_fn_name=pub_fn_name,
        priv_fn_name=priv_fn_name,
        wrap_in_src=wrap_in_src,
        files=files,
    )


def _write_tree(
    base_dir: Path,
    files: list[tuple[str, str]],
    *,
    add_ignored_noise: bool,
) -> None:
    """Write generated Python files and optional ignored cache/hidden directories."""
    for rel_path, content in files:
        dest = base_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    if add_ignored_noise:
        for noise in (
            "__pycache__/stale.py",
            ".git/hooks_sample.py",
            ".venv/lib/external.py",
            ".hidden_dir/secret.py",
        ):
            noise_path = base_dir / noise
            noise_path.parent.mkdir(parents=True, exist_ok=True)
            noise_path.write_text("class ShouldBeIgnored:\n    pass\n", encoding="utf-8")


def _assert_structural_invariants(ir: StructuralGraphIR) -> None:
    """Assert universal referential integrity and OBSERVED provenance invariants."""
    for sym_id, node in ir.nodes.items():
        assert sym_id == node.symbol_id
        if node.parent_id is not None:
            assert node.parent_id in ir.nodes
        assert node.provenance.grade is ProvenanceGrade.OBSERVED
        assert node.provenance.extractor == "python_ast_v1"
        assert node.provenance.input_sha256 is not None
        assert node.provenance.file_path is not None
        assert "\\" not in node.provenance.file_path
        assert node.provenance.line_span is not None
        assert 1 <= node.provenance.line_span[0] <= node.provenance.line_span[1]

    seen_edge_ids: set[str] = set()
    for edge in ir.edges:
        assert edge.edge_id not in seen_edge_ids
        seen_edge_ids.add(edge.edge_id)
        assert edge.source in ir.nodes
        assert edge.target in ir.nodes
        assert edge.source != edge.target
        assert edge.provenance.grade is ProvenanceGrade.OBSERVED
        assert edge.provenance.extractor == "python_ast_v1"
        assert edge.provenance.file_path is not None
        assert edge.provenance.line_span is not None


def _assert_trace_invariants(ir: InteractionTraceIR, expected_extractor: str) -> None:
    """Assert universal monotonic timestamp, causal tree, and provenance invariants."""
    for pid, participant in ir.participants.items():
        assert pid == participant.participant_id
        assert participant.provenance.grade is ProvenanceGrade.OBSERVED
        assert participant.provenance.extractor == expected_extractor
        assert participant.provenance.input_sha256 is not None

    for eid, ev in ir.events.items():
        assert eid == ev.event_id
        assert ev.caller_id in ir.participants
        assert ev.callee_id in ir.participants
        assert 0 <= ev.start_ns <= ev.end_ns
        assert ev.clock_domain_id == "local_process"
        assert ev.provenance.grade is ProvenanceGrade.OBSERVED
        assert ev.provenance.extractor == expected_extractor
        if ev.parent_event_id is not None:
            assert ev.parent_event_id in ir.events
            parent = ir.events[ev.parent_event_id]
            assert parent.start_ns <= ev.start_ns
            assert ev.end_ns <= parent.end_ns


def _assert_generated_pkg_semantics(
    ir: StructuralGraphIR,
    spec: _GeneratedPkgSpec,
    *,
    include_private: bool,
) -> None:
    """Assert expected packages, modules, classes, visibility, and semantic edges."""
    pkg = spec.pkg_name
    sub = f"{pkg}.{spec.subpkg_name}"
    models_mod = f"{sub}.models"
    service_mod = f"{pkg}.service"
    proto_id = f"{models_mod}.{spec.proto_name}"
    enum_id = f"{models_mod}.{spec.enum_name}"
    base_id = f"{models_mod}.{spec.base_cls_name}"
    impl_id = f"{service_mod}.{spec.impl_cls_name}"
    pub_fn_id = f"{service_mod}.{spec.pub_fn_name}"
    priv_fn_id = f"{service_mod}._{spec.priv_fn_name}"
    helper_id = f"{models_mod}.helper_fn"

    assert ir.nodes[pkg].kind is NodeKind.PACKAGE
    assert ir.nodes[sub].kind is NodeKind.PACKAGE
    assert ir.nodes[sub].parent_id == pkg
    assert ir.nodes[models_mod].kind is NodeKind.MODULE
    assert ir.nodes[models_mod].parent_id == sub
    assert ir.nodes[proto_id].kind is NodeKind.INTERFACE
    assert ir.nodes[enum_id].kind is NodeKind.ENUM
    assert ir.nodes[base_id].kind is NodeKind.CLASS
    assert ir.nodes[impl_id].kind is NodeKind.CLASS
    assert pub_fn_id in ir.nodes
    assert (priv_fn_id in ir.nodes) is include_private
    assert not any("ShouldBeIgnored" in sym for sym in ir.nodes)

    base_vis = {m.name: m.visibility for m in ir.nodes[base_id].members}
    assert base_vis["mode"] is Visibility.PUBLIC
    assert base_vis[f"_{spec.priv_field_name}"] is Visibility.PRIVATE
    assert base_vis["_internal"] is Visibility.PRIVATE

    edge_triples = {(e.source, e.target, e.relation) for e in ir.edges}
    expected_edges = {
        (service_mod, models_mod, EdgeRelation.IMPORTS),
        (service_mod, sub, EdgeRelation.IMPORTS),
        (impl_id, base_id, EdgeRelation.INHERITS),
        (impl_id, proto_id, EdgeRelation.IMPLEMENTS),
        (base_id, enum_id, EdgeRelation.COMPOSES),
        (impl_id, enum_id, EdgeRelation.COMPOSES),
        (impl_id, helper_id, EdgeRelation.CALLS),
        (pub_fn_id, impl_id, EdgeRelation.CALLS),
    }
    assert expected_edges <= edge_triples


@settings(max_examples=25, deadline=None)
@given(
    spec=_python_package_tree_strategy(),
    reverse_write_order=st.booleans(),
    include_private=st.booleans(),
)
def test_fuzz_ast_ingestor_package_tree_invariants_and_determinism(
    spec: _GeneratedPkgSpec,
    reverse_write_order: bool,
    include_private: bool,
) -> None:
    """Generated Python packages yield deterministic, referentially complete StructuralGraphIR."""
    ingestor = PythonAstStructuralIngestor(include_private=include_private)
    assert isinstance(ingestor, StructuralIngestorPort)
    assert ingestor.extractor_name == "python_ast_v1"

    files_order_a = list(spec.files)
    files_order_b = list(reversed(spec.files)) if reverse_write_order else list(spec.files)

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        root_a = Path(tmp_a) / ("src" if spec.wrap_in_src else "workspace")
        root_b = Path(tmp_b) / ("src" if spec.wrap_in_src else "workspace")
        _write_tree(root_a, files_order_a, add_ignored_noise=True)
        _write_tree(root_b, files_order_b, add_ignored_noise=False)

        scan_a = root_a / spec.pkg_name if spec.wrap_in_src else root_a
        scan_b = root_b / spec.pkg_name if spec.wrap_in_src else root_b

        ir_a = ingestor.ingest(scan_a)
        ir_b = ingestor.ingest(scan_b)
        ir_a_repeat = ingestor.ingest(str(scan_a))

    _assert_structural_invariants(ir_a)
    assert ir_a.nodes == ir_b.nodes
    assert ir_a.edges == ir_b.edges
    assert ir_a.metadata["input_sha256"] == ir_b.metadata["input_sha256"]
    assert ir_a == ir_a_repeat
    _assert_generated_pkg_semantics(ir_a, spec, include_private=include_private)


@settings(max_examples=25, deadline=None)
@given(
    cls_name=_UPPER_IDENT,
    fn_name=_LOWER_IDENT,
    attr_name=_LOWER_IDENT,
)
def test_fuzz_ast_ingestor_single_file_and_inline_source(
    cls_name: str,
    fn_name: str,
    attr_name: str,
) -> None:
    """Ingesting inline source strings and single .py files preserves AST structure & provenance."""
    source_code = (
        "from __future__ import annotations\n"
        "from enum import Enum\n\n"
        "class Mode(Enum):\n"
        "    FAST = 1\n\n"
        f"class {cls_name}:\n"
        f"    {attr_name}: Mode\n"
        "    def run(self) -> int:\n"
        "        return 7\n\n"
        f"def {fn_name}() -> {cls_name}:\n"
        f"    return {cls_name}()\n"
    )
    ingestor = PythonAstStructuralIngestor()
    inline_ir = ingestor.ingest(source_code)
    _assert_structural_invariants(inline_ir)
    assert f"inline.{cls_name}" in inline_ir.nodes
    assert f"inline.{fn_name}" in inline_ir.nodes
    assert (
        f"inline.{cls_name}",
        "inline.Mode",
        EdgeRelation.COMPOSES,
    ) in {(e.source, e.target, e.relation) for e in inline_ir.edges}

    with tempfile.TemporaryDirectory() as tmp_dir:
        py_file = Path(tmp_dir) / "standalone.py"
        py_file.write_text(source_code, encoding="utf-8")
        file_ir = ingestor.ingest(py_file)
    _assert_structural_invariants(file_ir)
    assert "standalone" in file_ir.nodes
    assert f"standalone.{cls_name}" in file_ir.nodes


def _build_runtime_workload_sources(
    pkg: str, loop_count: int, trigger_error: bool
) -> dict[str, str]:
    """Generate a live multi-module Python workload with loops, branches, and optional unwind."""
    return {
        f"{pkg}/__init__.py": "",
        f"{pkg}/leaf.py": (
            "def compute(i: int, fail: bool) -> int:\n"
            "    if fail and i == 0:\n"
            "        raise RuntimeError('expected_unwind')\n"
            "    return i * 2 + 1\n"
        ),
        f"{pkg}/worker.py": (
            f"from {pkg}.leaf import compute\n\n"
            "class Runner:\n"
            "    def step(self, i: int, fail: bool) -> int:\n"
            "        try:\n"
            "            return compute(i, fail)\n"
            "        except RuntimeError:\n"
            "            return -1\n"
        ),
        f"{pkg}/entry.py": (
            f"from {pkg}.worker import Runner\n\n"
            f"def run_workload() -> int:\n"
            "    runner = Runner()\n"
            "    total = 0\n"
            f"    for idx in range({loop_count}):\n"
            f"        total += runner.step(idx, {trigger_error!r})\n"
            "    return total\n"
        ),
    }


@settings(max_examples=20, deadline=None)
@given(
    loop_count=st.integers(min_value=1, max_value=12),
    trigger_error=st.booleans(),
    granularity=st.sampled_from(["module", "callable"]),
    max_events=st.integers(min_value=2, max_value=64),
    suffix=st.from_regex(r"^[a-z0-9]{4,8}$", fullmatch=True),
)
def test_fuzz_capture_python_trace_live_execution_and_roundtrip(
    loop_count: int,
    trigger_error: bool,
    granularity: Literal["module", "callable"],
    max_events: int,
    suffix: str,
) -> None:
    """Live runtime tracing captures valid monotonic causal trees and round-trips through JSON."""
    pkg = f"fuzzpkg_{suffix}"
    sources = _build_runtime_workload_sources(pkg, loop_count, trigger_error)
    trace_ingestor = PythonTraceIngestor()
    assert isinstance(trace_ingestor, TraceIngestorPort)
    assert trace_ingestor.extractor_name == "python_trace_v1"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for rel, body in sources.items():
            full = tmp_path / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body, encoding="utf-8")

        sys.path.insert(0, str(tmp_path))
        try:
            importlib.invalidate_caches()
            entry_mod = importlib.import_module(f"{pkg}.entry")
            run_fn: Any = entry_mod.run_workload
            trace_ir = capture_python_trace(
                run_fn,
                trace_id=f"trace_{suffix}",
                module_prefixes=(pkg,),
                granularity=granularity,
                max_events=max_events,
            )
        finally:
            if str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))
            for mod_name in list(sys.modules):
                if mod_name == pkg or mod_name.startswith(f"{pkg}."):
                    del sys.modules[mod_name]

    _assert_trace_invariants(trace_ir, "python_sys_monitoring_v1")
    assert 1 <= len(trace_ir.events) <= max_events
    assert trace_ir.trace_id == f"trace_{suffix}"

    if trigger_error and max_events >= 10:
        assert any(ev.status is EventStatus.ERROR for ev in trace_ir.events.values())

    json_payload = trace_ir.model_dump_json()
    roundtripped = trace_ingestor.ingest(json_payload)
    assert roundtripped == trace_ir

    with tempfile.TemporaryDirectory() as out_dir:
        trace_file = Path(out_dir) / "trace.json"
        trace_file.write_text(json_payload, encoding="utf-8")
        assert trace_ingestor.ingest(trace_file) == trace_ir


@settings(max_examples=25, deadline=None)
@given(
    trace_id=_LOWER_IDENT,
    call_count=st.integers(min_value=1, max_value=15),
    has_error=st.booleans(),
)
def test_fuzz_python_trace_ingestor_declarative_spec_json(
    trace_id: str,
    call_count: int,
    has_error: bool,
) -> None:
    """PythonTraceIngestor deterministically parses declarative trace specification JSON."""
    events_spec: list[dict[str, Any]] = []
    for idx in range(call_count):
        start_ns = idx * 100
        end_ns = (2 * call_count - idx) * 100
        events_spec.append(
            {
                "event_id": f"ev_{idx:03d}",
                "caller": "app.entry" if idx == 0 else f"app.mod_{idx - 1}",
                "callee": f"app.mod_{idx}",
                "signature": f"step_{idx}()",
                "parent_event_id": None if idx == 0 else f"ev_{idx - 1:03d}",
                "start_ns": start_ns,
                "end_ns": end_ns,
                "status": "error" if (has_error and idx == call_count - 1) else "ok",
                "file_path": f"app/mod_{idx}.py",
                "line_span": [idx + 1, idx + 5],
            }
        )
    spec_json = json.dumps({"trace_id": trace_id, "events": events_spec})
    ingestor = PythonTraceIngestor()
    ir_a = ingestor.ingest(spec_json)
    ir_b = ingestor.ingest(spec_json)

    assert ir_a == ir_b
    assert ir_a.trace_id == trace_id
    assert len(ir_a.events) == call_count
    _assert_trace_invariants(ir_a, "python_trace_v1")
    if has_error:
        assert ir_a.events[f"ev_{call_count - 1:03d}"].status is EventStatus.ERROR
