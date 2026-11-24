"""Hypothesis property-based fuzz suite for SCIP JSON & static fork-join ingestion."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import EdgeRelation, NodeKind, StructuralGraphIR, SyncKind
from archigram.features.ingest_scip import ScipStructuralIngestor
from archigram.ports.contracts import StructuralIngestorPort

_IDENT = st.from_regex(r"^[A-Za-z][A-Za-z0-9_]{1,10}$", fullmatch=True)
_FORK_JOIN_IDIOMS = st.sampled_from(
    [
        "Promise.all",
        "asyncio.gather",
        "asyncio.TaskGroup",
        "errgroup.Group",
    ]
)
_ASYNC_SPAWN_IDIOMS = st.sampled_from(
    [
        "asyncio.create_task",
        "go_stmt",
        "async_fire_forget",
    ]
)


@dataclass(frozen=True)
class _MethodSpec:
    symbol_id: str
    display_name: str
    start_line_0: int
    end_line_0: int


@dataclass(frozen=True)
class _ClassSpec:
    symbol_id: str
    display_name: str
    kind: NodeKind
    start_line_0: int
    end_line_0: int
    methods: tuple[_MethodSpec, ...]
    implemented_iface: str | None


@dataclass(frozen=True)
class _DocSpec:
    relative_path: str
    module_sym: str
    module_name: str
    iface_sym: str
    iface_name: str
    classes: tuple[_ClassSpec, ...]


@dataclass(frozen=True)
class _CallSpec:
    caller_sym: str
    callee_sym: str
    doc_path: str
    line_0: int
    mode: str  # "sync" | "import" | "fork_join" | "async_spawn"
    idiom: str
    parallel_region_id: str | None


@dataclass(frozen=True)
class _ScipFixtureSpec:
    docs: tuple[_DocSpec, ...]
    calls: tuple[_CallSpec, ...]


def _build_scip_symbol(pkg: str, descriptor: str) -> str:
    return f"scip-python python {pkg} 0.1.0 {descriptor}"


def _draw_doc_spec(
    draw: st.DrawFn,
    d_idx: int,
    mod_name: str,
    all_methods: list[tuple[str, str, int, int]],
) -> _DocSpec:
    pkg = f"pkg_{d_idx}"
    rel_path = f"src/{pkg}/{mod_name}.py"
    mod_sym = _build_scip_symbol(pkg, f"{mod_name}/")
    iface_name = f"I{mod_name.capitalize()}Port"
    iface_sym = _build_scip_symbol(pkg, f"{mod_name}/{iface_name}#")

    cls_count = draw(st.integers(min_value=1, max_value=2))
    cls_names = draw(st.lists(_IDENT, min_size=cls_count, max_size=cls_count, unique=True))
    classes: list[_ClassSpec] = []
    cursor_line = 5

    for c_idx, raw_cls_name in enumerate(cls_names):
        cls_name = f"Cls_{c_idx}_{raw_cls_name}"
        cls_sym = _build_scip_symbol(pkg, f"{mod_name}/{cls_name}#")
        cls_kind = draw(st.sampled_from([NodeKind.CLASS, NodeKind.ENUM]))
        m_count = draw(st.integers(min_value=1, max_value=3))
        m_names = draw(st.lists(_IDENT, min_size=m_count, max_size=m_count, unique=True))
        methods: list[_MethodSpec] = []
        cls_start = cursor_line
        cursor_line += 2

        for m_idx, raw_m_name in enumerate(m_names):
            m_name = f"fn_{m_idx}_{raw_m_name}"
            m_sym = _build_scip_symbol(pkg, f"{mod_name}/{cls_name}#{m_name}().")
            m_start = cursor_line
            m_end = m_start + draw(st.integers(min_value=4, max_value=10))
            cursor_line = m_end + 2
            methods.append(_MethodSpec(m_sym, m_name, m_start, m_end))
            all_methods.append((m_sym, rel_path, m_start, m_end))

        cls_end = cursor_line
        cursor_line += 4
        implements_iface = (
            iface_sym if (cls_kind is NodeKind.CLASS and draw(st.booleans())) else None
        )
        classes.append(
            _ClassSpec(
                symbol_id=cls_sym,
                display_name=cls_name,
                kind=cls_kind,
                start_line_0=cls_start,
                end_line_0=cls_end,
                methods=tuple(methods),
                implemented_iface=implements_iface,
            )
        )

    return _DocSpec(rel_path, mod_sym, mod_name, iface_sym, iface_name, tuple(classes))


def _draw_calls(
    draw: st.DrawFn,
    all_methods: list[tuple[str, str, int, int]],
) -> tuple[_CallSpec, ...]:
    calls: list[_CallSpec] = []
    if len(all_methods) < 2:
        return ()
    seen_pairs: set[tuple[str, str]] = set()
    for idx, (caller_sym, doc_path, m_start, m_end) in enumerate(all_methods):
        callee_sym = all_methods[(idx + 1) % len(all_methods)][0]
        if caller_sym == callee_sym or (caller_sym, callee_sym) in seen_pairs:
            continue
        seen_pairs.add((caller_sym, callee_sym))
        mode = draw(st.sampled_from(["sync", "import", "fork_join", "async_spawn"]))
        call_line_0 = m_start + 1 if m_start + 1 <= m_end else m_start
        idiom = (
            draw(_FORK_JOIN_IDIOMS)
            if mode == "fork_join"
            else (draw(_ASYNC_SPAWN_IDIOMS) if mode == "async_spawn" else "")
        )
        region_id = f"fj_region_{idx}" if mode == "fork_join" else None
        calls.append(
            _CallSpec(caller_sym, callee_sym, doc_path, call_line_0, mode, idiom, region_id)
        )
    return tuple(calls)


@st.composite
def _scip_fixture_strategy(draw: st.DrawFn) -> _ScipFixtureSpec:
    doc_count = draw(st.integers(min_value=1, max_value=3))
    mod_names = draw(st.lists(_IDENT, min_size=doc_count, max_size=doc_count, unique=True))
    all_methods: list[tuple[str, str, int, int]] = []
    docs = tuple(
        _draw_doc_spec(draw, d_idx, mod_name, all_methods)
        for d_idx, mod_name in enumerate(mod_names)
    )
    calls = _draw_calls(draw, all_methods)
    return _ScipFixtureSpec(docs=docs, calls=calls)


def _serialize_doc_payload(
    doc: _DocSpec,
    doc_calls: list[_CallSpec],
    *,
    reverse_order: bool,
) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = [
        {"symbol": doc.module_sym, "display_name": doc.module_name, "kind": "module"},
        {
            "symbol": doc.iface_sym,
            "display_name": doc.iface_name,
            "kind": "interface",
            "enclosing_symbol": doc.module_sym,
        },
    ]
    occurrences: list[dict[str, Any]] = [
        {"range": [0, 0, 200, 0], "symbol": doc.module_sym, "symbol_roles": 1},
        {"range": [1, 0, 3, 10], "symbol": doc.iface_sym, "symbol_roles": 1},
    ]

    cls_iter = reversed(doc.classes) if reverse_order else doc.classes
    for cls in cls_iter:
        rels = (
            [{"symbol": cls.implemented_iface, "is_implementation": True}]
            if cls.implemented_iface is not None
            else []
        )
        symbols.append(
            {
                "symbol": cls.symbol_id,
                "display_name": cls.display_name,
                "kind": cls.kind.value,
                "enclosing_symbol": doc.module_sym,
                "relationships": rels,
            }
        )
        occurrences.append(
            {
                "range": [cls.start_line_0, 0, cls.end_line_0, 0],
                "symbol": cls.symbol_id,
                "symbol_roles": 1,
            }
        )
        m_iter = reversed(cls.methods) if reverse_order else cls.methods
        for method in m_iter:
            symbols.append(
                {"symbol": method.symbol_id, "display_name": method.display_name, "kind": "Method"}
            )
            occurrences.append(
                {
                    "range": [method.start_line_0, 2, method.end_line_0, 20],
                    "symbol": method.symbol_id,
                    "symbol_roles": 1,
                }
            )

    for call in doc_calls:
        role = 2 if call.mode == "import" else 8
        occurrences.append(
            {
                "range": [call.line_0, 4, call.line_0, 20],
                "symbol": call.callee_sym,
                "symbol_roles": role,
            }
        )

    if reverse_order:
        symbols.reverse()
        occurrences.reverse()

    return {
        "relative_path": doc.relative_path,
        "symbols": symbols,
        "occurrences": occurrences,
    }


def _serialize_scip_payload(
    spec: _ScipFixtureSpec,
    *,
    reverse_order: bool = False,
) -> dict[str, Any]:
    calls_by_doc: dict[str, list[_CallSpec]] = {}
    annotations: list[dict[str, Any]] = []

    call_iter = reversed(spec.calls) if reverse_order else spec.calls
    for call in call_iter:
        calls_by_doc.setdefault(call.doc_path, []).append(call)
        if call.mode in {"fork_join", "async_spawn"}:
            ann_entry: dict[str, Any] = {
                "caller": call.caller_sym,
                "callee": call.callee_sym,
                "file_path": call.doc_path,
                "range": [call.line_0, 4, call.line_0, 20],
                "mode": call.mode,
                "idiom": call.idiom,
            }
            if call.parallel_region_id is not None:
                ann_entry["parallel_region_id"] = call.parallel_region_id
            annotations.append(ann_entry)

    doc_iter = reversed(spec.docs) if reverse_order else spec.docs
    documents = [
        _serialize_doc_payload(
            doc, calls_by_doc.get(doc.relative_path, []), reverse_order=reverse_order
        )
        for doc in doc_iter
    ]

    return {
        "schema": "scip.graph-export.v1",
        "graph_id": "fuzz_scip_graph",
        "documents": documents,
        "concurrency_annotations": annotations,
    }


def _assert_doc_nodes_and_implements(
    ir: StructuralGraphIR,
    doc: _DocSpec,
    expected_sha: str,
) -> None:
    assert doc.module_sym in ir.nodes
    iface_node = ir.nodes[doc.iface_sym]
    assert iface_node.kind is NodeKind.INTERFACE
    assert iface_node.parent_id == doc.module_sym
    assert iface_node.provenance.grade is ProvenanceGrade.OBSERVED
    assert iface_node.provenance.extractor == "scip-json"
    assert iface_node.provenance.input_sha256 == expected_sha
    assert iface_node.provenance.file_path == doc.relative_path
    assert iface_node.provenance.line_span == (2, 4)

    for cls in doc.classes:
        cls_node = ir.nodes[cls.symbol_id]
        assert cls_node.kind is cls.kind
        assert cls_node.parent_id == doc.module_sym
        assert cls_node.provenance.line_span == (cls.start_line_0 + 1, cls.end_line_0 + 1)

        if cls.implemented_iface is not None:
            impl_edges = [
                e
                for e in ir.edges
                if e.source == cls.symbol_id
                and e.target == cls.implemented_iface
                and e.relation is EdgeRelation.IMPLEMENTS
            ]
            assert len(impl_edges) == 1
            assert impl_edges[0].sync_kind is SyncKind.SYNC

        for method in cls.methods:
            m_node = ir.nodes[method.symbol_id]
            assert m_node.kind is NodeKind.FUNCTION
            assert m_node.parent_id == cls.symbol_id
            assert m_node.provenance.line_span == (
                method.start_line_0 + 1,
                method.end_line_0 + 1,
            )


def _assert_call_edge(ir: StructuralGraphIR, call: _CallSpec) -> None:
    matching = [e for e in ir.edges if e.source == call.caller_sym and e.target == call.callee_sym]
    assert len(matching) == 1
    edge = matching[0]
    assert edge.provenance.file_path == call.doc_path
    assert edge.provenance.line_span == (call.line_0 + 1, call.line_0 + 1)
    if call.mode == "fork_join":
        assert edge.relation is EdgeRelation.FORK_JOIN
        assert edge.sync_kind is SyncKind.FORK_JOIN_BRANCH
        assert edge.parallel_region_id == call.parallel_region_id
        assert edge.attributes.get("concurrency_idiom") == call.idiom
    elif call.mode == "async_spawn":
        assert edge.relation is EdgeRelation.ASYNC_SPAWN
        assert edge.sync_kind is SyncKind.ASYNC_FIRE_FORGET
        assert edge.parallel_region_id is None
    elif call.mode == "import":
        assert edge.relation is EdgeRelation.IMPORTS
        assert edge.sync_kind is SyncKind.SYNC
    else:
        assert edge.relation is EdgeRelation.CALLS
        assert edge.sync_kind is SyncKind.SYNC


@settings(max_examples=35, deadline=None)
@given(spec=_scip_fixture_strategy())
def test_scip_ingestor_provenance_containment_and_concurrency_invariants(
    spec: _ScipFixtureSpec,
) -> None:
    ingestor = ScipStructuralIngestor()
    assert isinstance(ingestor, StructuralIngestorPort)
    assert ingestor.source_format == "scip"
    assert ingestor.extractor_name == "scip-json"

    raw_json = json.dumps(_serialize_scip_payload(spec, reverse_order=False))
    expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    ir = ingestor.ingest(raw_json)
    assert ir.graph_id == "fuzz_scip_graph"
    assert ir.metadata["source_format"] == "scip"

    for doc in spec.docs:
        _assert_doc_nodes_and_implements(ir, doc, expected_sha)

    for call in spec.calls:
        _assert_call_edge(ir, call)


@settings(max_examples=25, deadline=None)
@given(spec=_scip_fixture_strategy())
def test_scip_ingestor_order_invariance_and_bytes_path_equivalence(
    spec: _ScipFixtureSpec,
) -> None:
    ingestor = ScipStructuralIngestor()
    forward_json = json.dumps(_serialize_scip_payload(spec, reverse_order=False))
    reversed_json = json.dumps(_serialize_scip_payload(spec, reverse_order=True))

    ir_forward = ingestor.ingest(forward_json)
    ir_reversed = ingestor.ingest(reversed_json)

    assert list(ir_forward.nodes.keys()) == list(ir_reversed.nodes.keys())
    for sym, node_fwd in ir_forward.nodes.items():
        node_rev = ir_reversed.nodes[sym]
        assert node_fwd.label == node_rev.label
        assert node_fwd.kind == node_rev.kind
        assert node_fwd.parent_id == node_rev.parent_id
        assert node_fwd.provenance.file_path == node_rev.provenance.file_path
        assert node_fwd.provenance.line_span == node_rev.provenance.line_span

    assert len(ir_forward.edges) == len(ir_reversed.edges)
    for edge_fwd, edge_rev in zip(ir_forward.edges, ir_reversed.edges, strict=True):
        assert edge_fwd.edge_id == edge_rev.edge_id
        assert edge_fwd.source == edge_rev.source
        assert edge_fwd.target == edge_rev.target
        assert edge_fwd.relation == edge_rev.relation
        assert edge_fwd.sync_kind == edge_rev.sync_kind
        assert edge_fwd.parallel_region_id == edge_rev.parallel_region_id
        assert edge_fwd.provenance.line_span == edge_rev.provenance.line_span

    ir_from_bytes = ingestor.ingest(forward_json.encode("utf-8"), source_name="<memory>")
    assert ir_forward.model_dump_json() == ir_from_bytes.model_dump_json()

    with tempfile.TemporaryDirectory() as tmp_dir:
        scip_file = Path(tmp_dir) / "index.scip.json"
        scip_file.write_text(forward_json, encoding="utf-8")
        ir_from_path = ingestor.ingest(scip_file)
        assert list(ir_from_path.nodes.keys()) == list(ir_forward.nodes.keys())
