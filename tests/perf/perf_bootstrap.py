"""Performance benchmark baseline for rustworkx PyDiGraph topology operations."""

from __future__ import annotations

from typing import Any

import rustworkx as rx


def _build_and_condense_dag(size: int = 250) -> int:
    graph: rx.PyDiGraph[int, str] = rx.PyDiGraph()
    nodes = [graph.add_node(i) for i in range(size)]
    for i in range(size - 1):
        graph.add_edge(nodes[i], nodes[i + 1], "calls")
    sccs = rx.strongly_connected_components(graph)
    return len(sccs)


def test_rustworkx_scc_throughput(benchmark: Any) -> None:
    scc_count = benchmark(_build_and_condense_dag, 250)
    assert scc_count == 250
