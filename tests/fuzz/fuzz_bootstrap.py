"""Hypothesis property-based fuzz suite for pure domain provenance invariants."""

from __future__ import annotations

import rustworkx as rx
from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade, ProvenanceStamp


@settings(max_examples=50)
@given(
    grade=st.sampled_from(list(ProvenanceGrade)),
    backing=st.lists(st.text(min_size=1, max_size=24), max_size=8),
    start_line=st.integers(min_value=1, max_value=10_000),
    span_len=st.integers(min_value=0, max_value=500),
)
def test_provenance_stamp_roundtrip_immutability(
    grade: ProvenanceGrade,
    backing: list[str],
    start_line: int,
    span_len: int,
) -> None:
    stamp = ProvenanceStamp(
        grade=grade,
        line_span=(start_line, start_line + span_len),
        backing_ids=tuple(backing),
    )
    restored = ProvenanceStamp.model_validate_json(stamp.model_dump_json())
    assert restored == stamp
    assert restored.backing_ids == tuple(backing)


@settings(max_examples=40)
@given(
    node_count=st.integers(min_value=3, max_value=20),
)
def test_rustworkx_dag_contraction_detects_induced_cycle(node_count: int) -> None:
    graph: rx.PyDiGraph[str, str] = rx.PyDiGraph()
    indices = [graph.add_node(f"n_{i}") for i in range(node_count)]
    for idx in range(node_count - 1):
        graph.add_edge(indices[idx], indices[idx + 1], "calls")

    # Merging first and last node of a >=3 chain must raise DAGWouldCycle
    try:
        graph.contract_nodes([indices[0], indices[-1]], "merged", check_cycle=True)
        raised = False
    except rx.DAGWouldCycle:
        raised = True
    assert raised
