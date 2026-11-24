"""Performance benchmark for D2Emitter serialization of a 300-node nested container graph."""

from __future__ import annotations

from typing import Any

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    ClassMember,
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    Visibility,
)
from archigram.features.emit_d2 import D2Emitter

_OBSERVED_STAMP = ProvenanceStamp(
    grade=ProvenanceGrade.OBSERVED,
    extractor="perf_fixture",
    input_sha256="b" * 64,
    file_path="src/system.py",
    line_span=(1, 20),
)


def _node_kind_for_index(idx: int) -> NodeKind:
    rules = (
        (15, 0, NodeKind.SERVICE),
        (15, 1, NodeKind.MODULE),
        (7, 0, NodeKind.DATABASE),
        (11, 0, NodeKind.QUEUE),
        (5, 0, NodeKind.CLASS),
    )
    for mod, rem, kind in rules:
        if idx % mod == rem:
            return kind
    return NodeKind.COMPONENT


def _build_nested_300_node_graph(node_count: int = 300) -> StructuralGraphIR:
    nodes: dict[str, IRNode] = {}
    for idx in range(node_count):
        group_base = (idx // 15) * 15
        offset = idx % 15
        if offset == 0:
            parent_id: str | None = None
        elif offset in {1, 2}:
            parent_id = f"svc_{group_base}"
        elif offset < 9:
            parent_id = f"svc_{group_base + 1}"
        else:
            parent_id = f"svc_{group_base + 2}"

        kind = _node_kind_for_index(idx)
        is_leaf_class = kind is NodeKind.CLASS and offset not in {0, 1, 2}
        members = (
            (
                ClassMember(
                    name="handle",
                    visibility=Visibility.PUBLIC,
                    signature="(req: Request) -> Response",
                    is_method=True,
                ),
                ClassMember(
                    name="_repo",
                    visibility=Visibility.PRIVATE,
                    signature="Repository",
                    is_method=False,
                ),
            )
            if is_leaf_class
            else ()
        )
        provenance = (
            ProvenanceStamp(
                grade=ProvenanceGrade.DERIVED_DETERMINISTIC,
                rule_id="collapse_pkg",
                backing_ids=(f"raw_{idx}_a", f"raw_{idx}_b"),
            )
            if idx % 10 == 0
            else _OBSERVED_STAMP
        )
        sid = f"svc_{idx}"
        nodes[sid] = IRNode(
            symbol_id=sid,
            label=f"Node {idx} (tier:{idx % 3})",
            kind=kind,
            parent_id=parent_id,
            members=members,
            provenance=provenance,
        )

    sym_ids = list(nodes)
    edges = tuple(
        IREdge(
            edge_id=f"e_{idx}",
            source=sym_ids[idx],
            target=sym_ids[(idx + 17) % node_count],
            relation=EdgeRelation.CALLS if idx % 4 != 0 else EdgeRelation.ASYNC_SPAWN,
            sync_kind=SyncKind.SYNC if idx % 4 != 0 else SyncKind.ASYNC_FIRE_FORGET,
            provenance=_OBSERVED_STAMP,
        )
        for idx in range(node_count)
    )
    return StructuralGraphIR(graph_id="bench_d2_300", nodes=nodes, edges=edges)


_GRAPH_300 = _build_nested_300_node_graph(300)
_EMITTER = D2Emitter()


def test_d2_emitter_300_node_nested_graph_throughput(benchmark: Any) -> None:
    d2_output: str = benchmark(_EMITTER.emit, _GRAPH_300)
    assert d2_output.startswith("vars: {\n  d2-config: {\n    layout-engine: elk\n  }\n}")
    assert "shape: cylinder" in d2_output
    assert "shape: queue" in d2_output
    assert "shape: class" in d2_output
    assert "style.stroke-dash: 5" in d2_output
    assert "tooltip:" in d2_output
