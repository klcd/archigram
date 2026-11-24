"""Performance benchmark suite for Graphviz .dot ingestion into StructuralGraphIR."""

from __future__ import annotations

from typing import Any

from archigram.features.ingest_dot import DotStructuralIngestor


def _build_300_node_clustered_dot(
    cluster_count: int = 15,
    nodes_per_cluster: int = 20,
) -> str:
    """Build a 300-node clustered Graphviz .dot fixture with nested subgraphs and styles."""
    lines: list[str] = [
        "digraph Bench300 {",
        '    compound="true";',
        "    node [shape=box];",
    ]
    for c_idx in range(cluster_count):
        outer_id = f"cluster_svc_{c_idx}"
        inner_id = f"cluster_mod_{c_idx}"
        lines.append(f"    subgraph {outer_id} {{")
        lines.append(f'        label="Service {c_idx}";')
        lines.append(f'        _sym_id="svc_{c_idx}";')
        lines.append(f"        subgraph {inner_id} {{")
        lines.append(f'            label="Module {c_idx}";')
        lines.append(f'            _sym_id="svc_{c_idx}.mod";')
        for n_idx in range(nodes_per_cluster):
            global_idx = c_idx * nodes_per_cluster + n_idx
            node_id = f"n_{global_idx}"
            sym_id = f"svc_{c_idx}.mod.fn_{n_idx}"
            src_loc = f"src/svc_{c_idx}/mod.py:{n_idx * 10 + 1}-{n_idx * 10 + 9}"
            lines.append(
                f'            {node_id} [label="Fn {global_idx}", '
                f'_sym_id="{sym_id}", _src_loc="{src_loc}"];'
            )
            if n_idx > 0:
                prev_id = f"n_{global_idx - 1}"
                style = "dotted" if n_idx % 4 == 0 else "solid"
                lines.append(f'            {prev_id} -> {node_id} [style="{style}"];')
        lines.append("        }")
        lines.append("    }")

    # Cross-cluster compound edges and UML/async visual edges
    total_nodes = cluster_count * nodes_per_cluster
    for c_idx in range(cluster_count):
        src_idx = c_idx * nodes_per_cluster
        dst_cluster = (c_idx + 1) % cluster_count
        dst_idx = dst_cluster * nodes_per_cluster
        lines.append(
            f"    n_{src_idx} -> n_{dst_idx} "
            f'[ltail="cluster_mod_{c_idx}", lhead="cluster_mod_{dst_cluster}", '
            f'arrowhead="normalnoneodot"];'
        )
        lines.append(
            f"    n_{src_idx + 1} -> n_{(dst_idx + 2) % total_nodes} "
            f'[arrowhead="empty", style="dashed"];'
        )

    lines.append("}")
    return "\n".join(lines)


_DOT_300_FIXTURE = _build_300_node_clustered_dot(15, 20)
_INGESTOR = DotStructuralIngestor()


def test_ingest_300_node_clustered_dot_benchmark(benchmark: Any) -> None:
    ir = benchmark(_INGESTOR.ingest, _DOT_300_FIXTURE)
    # 300 leaf nodes + 15 outer clusters + 15 inner clusters = 330 nodes
    assert len(ir.nodes) == 330
    assert len(ir.edges) == 15 * 19 + 30
