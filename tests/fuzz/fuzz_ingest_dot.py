"""Hypothesis property-based fuzz suite for Graphviz .dot ingestion & visual semantics."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import EdgeRelation, NodeKind, SyncKind
from archigram.features.ingest_dot import DotStructuralIngestor
from archigram.ports.contracts import StructuralIngestorPort


@dataclass(frozen=True)
class _LeafNodeSpec:
    raw_node: str
    node_sym: str
    inner_raw: str
    inner_sym: str
    attrs: dict[str, str]


@dataclass(frozen=True)
class _InnerClusterSpec:
    inner_raw: str
    inner_sym: str
    attrs: dict[str, str]
    leaves: tuple[_LeafNodeSpec, ...]


@dataclass(frozen=True)
class _OuterClusterSpec:
    outer_raw: str
    outer_sym: str
    attrs: dict[str, str]
    inners: tuple[_InnerClusterSpec, ...]


@dataclass
class _GeneratedDotFixture:
    dot_source: str
    expected_node_syms: set[str] = field(default_factory=set)
    expected_parent_map: dict[str, str | None] = field(default_factory=dict)
    expected_edges: list[tuple[str, str, EdgeRelation, SyncKind]] = field(default_factory=list)
    expected_loc_spans: dict[str, tuple[str, tuple[int, int]]] = field(default_factory=dict)


_VISUAL_STYLE_CASES: list[tuple[str, dict[str, str], EdgeRelation, SyncKind]] = [
    (
        "doxygen_inherits",
        {"arrowhead": "empty", "style": "solid"},
        EdgeRelation.INHERITS,
        SyncKind.SYNC,
    ),
    (
        "doxygen_onormal_inherits",
        {"arrowhead": "onormal"},
        EdgeRelation.INHERITS,
        SyncKind.SYNC,
    ),
    (
        "doxygen_implements",
        {"arrowhead": "empty", "style": "dashed"},
        EdgeRelation.IMPLEMENTS,
        SyncKind.SYNC,
    ),
    (
        "uml_composes",
        {"arrowhead": "diamond"},
        EdgeRelation.COMPOSES,
        SyncKind.SYNC,
    ),
    (
        "uml_aggregates",
        {"arrowhead": "odiamond"},
        EdgeRelation.AGGREGATES,
        SyncKind.SYNC,
    ),
    (
        "pyan3_imports",
        {"style": "dotted"},
        EdgeRelation.IMPORTS,
        SyncKind.SYNC,
    ),
    (
        "pyan3_calls",
        {"style": "solid"},
        EdgeRelation.CALLS,
        SyncKind.SYNC,
    ),
    (
        "gocallvis_arrowhead",
        {"arrowhead": "normalnoneodot"},
        EdgeRelation.ASYNC_SPAWN,
        SyncKind.ASYNC_FIRE_FORGET,
    ),
    (
        "gocallvis_sync_kind",
        {"_sync_kind": "async"},
        EdgeRelation.ASYNC_SPAWN,
        SyncKind.ASYNC_FIRE_FORGET,
    ),
    (
        "gocallvis_go_label",
        {"label": "go worker.Run()"},
        EdgeRelation.ASYNC_SPAWN,
        SyncKind.ASYNC_FIRE_FORGET,
    ),
    (
        "rel_kind_override_writes",
        {"_rel_kind": "writes_data", "style": "dotted"},
        EdgeRelation.WRITES_DATA,
        SyncKind.SYNC,
    ),
    (
        "rel_kind_override_reads",
        {"_rel_kind": "reads_data", "arrowhead": "diamond"},
        EdgeRelation.READS_DATA,
        SyncKind.SYNC,
    ),
]


def _format_attrs(attrs: dict[str, str]) -> str:
    if not attrs:
        return ""
    parts = [f'{k}="{v}"' for k, v in attrs.items()]
    return " [" + ", ".join(parts) + "]"


def _draw_leaf_node(
    draw: st.DrawFn,
    o_idx: int,
    i_idx: int,
    n_idx: int,
    inner_raw: str,
    inner_sym: str,
) -> tuple[_LeafNodeSpec, tuple[str, tuple[int, int]] | None]:
    raw_node = f"n_{o_idx}_{i_idx}_{n_idx}"
    use_custom = draw(st.booleans())
    node_sym = f"sym.{o_idx}.{i_idx}.{n_idx}" if use_custom else raw_node
    attrs: dict[str, str] = {"label": f"Node {raw_node}"}
    if use_custom:
        attrs["_sym_id"] = node_sym
    loc_info: tuple[str, tuple[int, int]] | None = None
    if draw(st.booleans()):
        start = draw(st.integers(min_value=1, max_value=500))
        end = start + draw(st.integers(min_value=0, max_value=50))
        file_name = f"src/mod_{raw_node}.py"
        attrs["_src_loc"] = f"{file_name}:{start}-{end}"
        loc_info = (file_name, (start, end))
    return _LeafNodeSpec(raw_node, node_sym, inner_raw, inner_sym, attrs), loc_info


def _draw_inner_cluster(
    draw: st.DrawFn,
    o_idx: int,
    i_idx: int,
    nodes_per_inner: int,
    loc_spans: dict[str, tuple[str, tuple[int, int]]],
) -> _InnerClusterSpec:
    inner_raw = f"cluster_inner_{o_idx}_{i_idx}"
    use_custom = draw(st.booleans())
    inner_sym = f"pkg.outer_{o_idx}.inner_{i_idx}" if use_custom else inner_raw
    attrs: dict[str, str] = {"label": f"Inner {o_idx}.{i_idx}"}
    if use_custom:
        attrs["_sym_id"] = inner_sym

    leaves: list[_LeafNodeSpec] = []
    for n_idx in range(nodes_per_inner):
        leaf, loc_info = _draw_leaf_node(draw, o_idx, i_idx, n_idx, inner_raw, inner_sym)
        leaves.append(leaf)
        if loc_info is not None:
            loc_spans[leaf.node_sym] = loc_info
    return _InnerClusterSpec(inner_raw, inner_sym, attrs, tuple(leaves))


def _draw_clusters(
    draw: st.DrawFn,
    loc_spans: dict[str, tuple[str, tuple[int, int]]],
) -> tuple[list[_OuterClusterSpec], list[_LeafNodeSpec]]:
    outer_count = draw(st.integers(min_value=1, max_value=3))
    inner_per_outer = draw(st.integers(min_value=1, max_value=2))
    nodes_per_inner = draw(st.integers(min_value=1, max_value=3))

    outers: list[_OuterClusterSpec] = []
    all_leaves: list[_LeafNodeSpec] = []
    for o_idx in range(outer_count):
        outer_raw = f"cluster_outer_{o_idx}"
        use_custom = draw(st.booleans())
        outer_sym = f"pkg.outer_{o_idx}" if use_custom else outer_raw
        o_attrs: dict[str, str] = {"label": f"Outer {o_idx}"}
        if use_custom:
            o_attrs["_sym_id"] = outer_sym
        inners = tuple(
            _draw_inner_cluster(draw, o_idx, i_idx, nodes_per_inner, loc_spans)
            for i_idx in range(inner_per_outer)
        )
        for inner in inners:
            all_leaves.extend(inner.leaves)
        outers.append(_OuterClusterSpec(outer_raw, outer_sym, o_attrs, inners))
    return outers, all_leaves


def _draw_edges(
    draw: st.DrawFn,
    all_leaves: list[_LeafNodeSpec],
) -> tuple[
    dict[str, list[str]],
    list[str],
    list[tuple[str, str, EdgeRelation, SyncKind]],
]:
    edge_count = draw(st.integers(min_value=1, max_value=12))
    intra_edges: dict[str, list[str]] = {}
    root_edges: list[str] = []
    expected_edges: list[tuple[str, str, EdgeRelation, SyncKind]] = []

    for _ in range(edge_count):
        src = draw(st.sampled_from(all_leaves))
        dst = draw(st.sampled_from(all_leaves))
        _, style_attrs, exp_rel, exp_sync = draw(st.sampled_from(_VISUAL_STYLE_CASES))
        e_attrs = dict(style_attrs)
        eff_src = src.node_sym
        eff_dst = dst.node_sym

        if draw(st.booleans()):
            e_attrs["ltail"] = src.inner_raw
            eff_src = src.inner_sym
        if draw(st.booleans()):
            e_attrs["lhead"] = dst.inner_raw
            eff_dst = dst.inner_sym

        stmt = f"{src.raw_node} -> {dst.raw_node}{_format_attrs(e_attrs)};"
        if draw(st.booleans()):
            intra_edges.setdefault(src.inner_raw, []).append(f"            {stmt}")
        else:
            root_edges.append(f"    {stmt}")
        expected_edges.append((eff_src, eff_dst, exp_rel, exp_sync))

    return intra_edges, root_edges, expected_edges


def _render_dot_document(
    outers: list[_OuterClusterSpec],
    intra_edges: dict[str, list[str]],
    root_edges: list[str],
) -> str:
    lines: list[str] = [
        "digraph G {",
        '    compound="true";',
        "    node [shape=box];",
        "    edge [color=black];",
    ]
    for outer in outers:
        lines.append(f"    subgraph {outer.outer_raw} {{")
        for k, v in outer.attrs.items():
            lines.append(f'        {k}="{v}";')
        for inner in outer.inners:
            lines.append(f"        subgraph {inner.inner_raw} {{")
            for k, v in inner.attrs.items():
                lines.append(f'            {k}="{v}";')
            for leaf in inner.leaves:
                lines.append(f"            {leaf.raw_node}{_format_attrs(leaf.attrs)};")
            lines.extend(intra_edges.get(inner.inner_raw, []))
            lines.append("        }")
        lines.append("    }")
    lines.extend(root_edges)
    lines.append("}")
    return "\n".join(lines)


@st.composite
def clustered_dot_fixture_strategy(draw: st.DrawFn) -> _GeneratedDotFixture:
    """Generate multi-cluster nested DOT graphs with compound edges and provenance."""
    loc_spans: dict[str, tuple[str, tuple[int, int]]] = {}
    outers, all_leaves = _draw_clusters(draw, loc_spans)
    intra_edges, root_edges, expected_edges = _draw_edges(draw, all_leaves)
    dot_source = _render_dot_document(outers, intra_edges, root_edges)

    expected_node_syms: set[str] = set()
    expected_parent_map: dict[str, str | None] = {}
    for outer in outers:
        expected_node_syms.add(outer.outer_sym)
        expected_parent_map[outer.outer_sym] = None
        for inner in outer.inners:
            expected_node_syms.add(inner.inner_sym)
            expected_parent_map[inner.inner_sym] = outer.outer_sym
            for leaf in inner.leaves:
                expected_node_syms.add(leaf.node_sym)
                expected_parent_map[leaf.node_sym] = inner.inner_sym

    return _GeneratedDotFixture(
        dot_source=dot_source,
        expected_node_syms=expected_node_syms,
        expected_parent_map=expected_parent_map,
        expected_edges=expected_edges,
        expected_loc_spans=loc_spans,
    )


@settings(max_examples=30, deadline=None)
@given(fixture=clustered_dot_fixture_strategy())
def test_ingest_dot_preserves_nested_clusters_compound_edges_and_semantics(
    fixture: _GeneratedDotFixture,
) -> None:
    ingestor = DotStructuralIngestor()
    assert isinstance(ingestor, StructuralIngestorPort)
    assert ingestor.extractor_name == "graphviz_dot"

    ir = ingestor.ingest(fixture.dot_source)
    expected_sha = hashlib.sha256(fixture.dot_source.encode("utf-8")).hexdigest()

    assert set(ir.nodes.keys()) == fixture.expected_node_syms
    for pseudo in ("node", "edge", "graph"):
        assert pseudo not in ir.nodes

    for sym_id, expected_parent in fixture.expected_parent_map.items():
        node = ir.nodes[sym_id]
        assert node.parent_id == expected_parent
        assert node.provenance.grade is ProvenanceGrade.OBSERVED
        assert node.provenance.extractor == "graphviz_dot"
        assert node.provenance.input_sha256 == expected_sha
        if sym_id in fixture.expected_loc_spans:
            exp_file, exp_span = fixture.expected_loc_spans[sym_id]
            assert node.provenance.file_path == exp_file
            assert node.provenance.line_span == exp_span

    assert len(ir.edges) == len(fixture.expected_edges)
    actual_edges = [(edge.source, edge.target, edge.relation, edge.sync_kind) for edge in ir.edges]
    assert sorted(actual_edges) == sorted(fixture.expected_edges)
    for edge in ir.edges:
        assert edge.provenance.grade is ProvenanceGrade.OBSERVED
        assert edge.provenance.extractor == "graphviz_dot"
        assert edge.provenance.input_sha256 == expected_sha

    ir_second = ingestor.ingest(fixture.dot_source)
    assert ir.model_dump_json() == ir_second.model_dump_json()


@settings(max_examples=20, deadline=None)
@given(
    fixture=clustered_dot_fixture_strategy(),
    pass_as_str_path=st.booleans(),
)
def test_ingest_dot_path_and_str_file_equivalence(
    fixture: _GeneratedDotFixture,
    pass_as_str_path: bool,
) -> None:
    ingestor = DotStructuralIngestor()
    with tempfile.TemporaryDirectory() as tmp_dir:
        dot_file = Path(tmp_dir) / "sample_graph.dot"
        dot_file.write_text(fixture.dot_source, encoding="utf-8")

        ir_from_file = ingestor.ingest(str(dot_file) if pass_as_str_path else dot_file)
        ir_from_raw = ingestor.ingest(fixture.dot_source)

        assert set(ir_from_file.nodes.keys()) == set(ir_from_raw.nodes.keys())
        assert len(ir_from_file.edges) == len(ir_from_raw.edges)
        for sym_id, node in ir_from_file.nodes.items():
            if sym_id not in fixture.expected_loc_spans:
                assert node.provenance.file_path == str(dot_file)


@settings(max_examples=25, deadline=None)
@given(
    g1_node=st.from_regex(r"^g1_[a-z]{2,6}$", fullmatch=True),
    g2_node=st.from_regex(r"^g2_[a-z]{2,6}$", fullmatch=True),
    port_a=st.from_regex(r"^p_[a-z]{1,4}$", fullmatch=True),
    port_b=st.from_regex(r"^p_[a-z]{1,4}$", fullmatch=True),
)
def test_multi_graph_and_port_qualified_endpoints(
    g1_node: str,
    g2_node: str,
    port_a: str,
    port_b: str,
) -> None:
    multi_dot = (
        f"digraph First {{\n"
        f"    subgraph cluster_c1 {{\n"
        f'        graph [label="Cluster 1", _sym_id="c1.sym"];\n'
        f"        {g1_node}:{port_a} -> {g2_node}:{port_b}:ne [style=dotted];\n"
        f"    }}\n"
        f"}}\n"
        f"digraph Second {{\n"
        f'    {g2_node} -> {g1_node} [arrowhead="normalnoneodot", weight="2.5"];\n'
        f"}}\n"
    )
    ingestor = DotStructuralIngestor()
    ir = ingestor.ingest(multi_dot)

    assert "c1.sym" in ir.nodes
    assert ir.nodes["c1.sym"].kind is NodeKind.CONTAINER
    assert g1_node in ir.nodes
    assert g2_node in ir.nodes
    assert ir.nodes[g1_node].parent_id == "c1.sym"
    assert ir.nodes[g2_node].parent_id == "c1.sym"
    assert len(ir.edges) == 2
    assert ir.edges[0].relation is EdgeRelation.IMPORTS
    assert ir.edges[1].relation is EdgeRelation.ASYNC_SPAWN
    assert ir.edges[1].sync_kind is SyncKind.ASYNC_FIRE_FORGET
    assert ir.edges[1].weight == 2.5
