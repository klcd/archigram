"""Compact Token-Efficient IR Digest Generator (Louvain/Modularity, PageRank, Fanout)."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Literal

import rustworkx as rx
from pydantic import BaseModel, ConfigDict, Field

from archigram.domain.ir import (
    InteractionTraceIR,
    NodeKind,
    StructuralGraphIR,
    TraceEvent,
)

if TYPE_CHECKING:
    from archigram.domain.ir import CanonicalIR, IREdge

_MAX_MARKDOWN_CHARS: int = 12_000
_MAX_WRAPPER_FANOUT: int = 16


class ContainmentClusterSummary(BaseModel):
    """Summary of a parent package/container or top-level module prefix in StructuralGraphIR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    container_id: str
    member_count: int = Field(ge=1)
    sample_symbols: tuple[str, ...] = Field(default_factory=tuple)


class CentralityHub(BaseModel):
    """Top PageRank & Betweenness centrality hub in a StructuralGraphIR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol_id: str
    label: str
    kind: NodeKind
    pagerank: float = Field(ge=0.0)
    betweenness: float = Field(ge=0.0)
    in_degree: int = Field(ge=0)
    out_degree: int = Field(ge=0)


class CommunityCluster(BaseModel):
    """Cohesive topological community with intra- vs inter-cluster coupling strengths (SemRef)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    community_id: str
    node_count: int = Field(ge=1)
    member_symbols: tuple[str, ...] = Field(default_factory=tuple)
    intra_edge_weight: float = Field(ge=0.0)
    inter_edge_weight: float = Field(ge=0.0)
    cohesion_ratio: float = Field(ge=0.0, le=1.0)


class CommunityCoupling(BaseModel):
    """Directed inter-community edge weight summarizing coupling between two clusters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_community_id: str
    target_community_id: str
    edge_count: int = Field(ge=1)
    total_weight: float = Field(gt=0.0)


class CandidateLeafSink(BaseModel):
    """Zero-out-degree node sorted by (-in_degree, symbol_id) for prune_leaves curation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol_id: str
    label: str
    kind: NodeKind
    in_degree: int = Field(ge=0)
    out_degree: Literal[0] = 0


class CandidateWrapperNode(BaseModel):
    """Intermediate plumbing/bridge node (1 <= in_deg * out_deg <= 16) for bypass_vertices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol_id: str
    label: str
    kind: NodeKind
    in_degree: int = Field(ge=1)
    out_degree: int = Field(ge=1)
    fanout_product: int = Field(ge=1, le=_MAX_WRAPPER_FANOUT)


def _format_structural_overview(digest: StructuralDigest) -> list[str]:
    return [
        f"# Structural Graph Digest: `{digest.graph_id}`",
        "",
        "## 1. Topology Overview",
        f"- **Nodes**: {digest.node_count} | **Edges**: {digest.edge_count}",
        (
            f"- **Weakly Connected Components**: {digest.weakly_connected_components} | "
            f"**Strongly Connected Components**: {digest.strongly_connected_components}"
        ),
    ]


def _format_containment_section(
    clusters: tuple[ContainmentClusterSummary, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 2. Package & Containment Hierarchy"]
    if not clusters:
        lines.append("- None")
        return lines
    for item in clusters[:limit]:
        samples = ", ".join(f"`{s}`" for s in item.sample_symbols[:4])
        lines.append(f"- `{item.container_id}` ({item.member_count} nodes): {samples}")
    return lines


def _format_hubs_section(hubs: tuple[CentralityHub, ...], limit: int) -> list[str]:
    lines = ["", "## 3. Top PageRank & Centrality Hubs"]
    if not hubs:
        lines.append("- None")
        return lines
    for hub in hubs[:limit]:
        lines.append(
            f"- `{hub.symbol_id}` ({hub.kind.value}, PR={hub.pagerank:.4f}, "
            f"BW={hub.betweenness:.4f}, in={hub.in_degree}, out={hub.out_degree})"
        )
    return lines


def _format_communities_section(
    communities: tuple[CommunityCluster, ...],
    couplings: tuple[CommunityCoupling, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 4. Cohesive Communities & SemRef Coupling"]
    if not communities:
        lines.append("- None")
        return lines
    for comm in communities[:limit]:
        members = ", ".join(f"`{m}`" for m in comm.member_symbols[:5])
        lines.append(
            f"- `{comm.community_id}` (nodes={comm.node_count}, "
            f"intra_w={comm.intra_edge_weight:.2f}, inter_w={comm.inter_edge_weight:.2f}, "
            f"cohesion={comm.cohesion_ratio:.2f}): {members}"
        )
    if couplings:
        lines.append("")
        lines.append("### Inter-Community Coupling")
        for cp in couplings[:limit]:
            lines.append(
                f"- `{cp.source_community_id}` -> `{cp.target_community_id}`: "
                f"edges={cp.edge_count}, weight={cp.total_weight:.2f}"
            )
    return lines


def _format_reduction_candidates_section(
    leaves: tuple[CandidateLeafSink, ...],
    wrappers: tuple[CandidateWrapperNode, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 5. Candidate Leaf Sinks (`prune_leaves`)"]
    if not leaves:
        lines.append("- None")
    for leaf in leaves[:limit]:
        lines.append(
            f"- `{leaf.symbol_id}` ({leaf.kind.value}, in={leaf.in_degree}, out={leaf.out_degree})"
        )
    lines.extend(["", "## 6. Candidate Wrapper / Plumbing Chains (`bypass_vertices`)"])
    if not wrappers:
        lines.append("- None")
    for wrap in wrappers[:limit]:
        lines.append(
            f"- `{wrap.symbol_id}` ({wrap.kind.value}, in={wrap.in_degree}, "
            f"out={wrap.out_degree}, product={wrap.fanout_product})"
        )
    return lines


def _clamp_markdown(text: str) -> str:
    if len(text) <= _MAX_MARKDOWN_CHARS:
        return text
    suffix = "\n... [digest truncated to stay under 4,000-token budget]"
    return text[: _MAX_MARKDOWN_CHARS - len(suffix)] + suffix


class StructuralDigest(BaseModel):
    """Immutable compact structural graph digest for the MCP Control Plane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ir_mode: Literal["structural_graph"] = "structural_graph"
    graph_id: str
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    weakly_connected_components: int = Field(ge=0)
    strongly_connected_components: int = Field(ge=0)
    containment_summary: tuple[ContainmentClusterSummary, ...] = Field(default_factory=tuple)
    top_hubs: tuple[CentralityHub, ...] = Field(default_factory=tuple)
    communities: tuple[CommunityCluster, ...] = Field(default_factory=tuple)
    community_couplings: tuple[CommunityCoupling, ...] = Field(default_factory=tuple)
    candidate_leaf_sinks: tuple[CandidateLeafSink, ...] = Field(default_factory=tuple)
    candidate_wrappers: tuple[CandidateWrapperNode, ...] = Field(default_factory=tuple)

    def to_compact_markdown(self, max_items_per_section: int = 15) -> str:
        """Render a bounded (<4,000 tokens) structured Markdown digest."""
        limit = max(1, min(max_items_per_section, 50))
        lines: list[str] = []
        lines.extend(_format_structural_overview(self))
        lines.extend(_format_containment_section(self.containment_summary, limit))
        lines.extend(_format_hubs_section(self.top_hubs, limit))
        lines.extend(_format_communities_section(self.communities, self.community_couplings, limit))
        lines.extend(
            _format_reduction_candidates_section(
                self.candidate_leaf_sinks,
                self.candidate_wrappers,
                limit,
            )
        )
        return _clamp_markdown("\n".join(lines))


class ParticipantCallSummary(BaseModel):
    """Call volume and error summary for a single interaction trace participant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    participant_id: str
    symbol_id: str
    label: str
    kind: NodeKind
    calls_initiated: int = Field(ge=0)
    calls_received: int = Field(ge=0)
    error_count: int = Field(ge=0)


class ConcurrentFanoutHotspot(BaseModel):
    """Parent event spawning overlapping same-clock or fork-join sibling spans (R5/R6 candidate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_event_id: str
    clock_domain_id: str
    caller_id: str
    concurrent_degree: int = Field(ge=2)
    overlapping_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    target_callees: tuple[str, ...] = Field(default_factory=tuple)


class RepeatingLoopHotspot(BaseModel):
    """Contiguous repeating call pattern under a shared parent event (R1-R3 loop candidate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_event_id: str
    caller_id: str
    callee_id: str
    signature: str
    repeat_count: int = Field(ge=2)
    event_ids: tuple[str, ...] = Field(default_factory=tuple)


class RecursiveCallHotspot(BaseModel):
    """Direct or mutual recursive call cycle along an ancestor chain (R4 recursion candidate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    participant_id: str
    signature: str
    max_recursion_depth: int = Field(ge=1)
    occurrence_count: int = Field(ge=1)
    sample_event_ids: tuple[str, ...] = Field(default_factory=tuple)


class TraceDigest(BaseModel):
    """Immutable compact concurrent interaction trace digest for the MCP Control Plane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ir_mode: Literal["interaction_trace"] = "interaction_trace"
    trace_id: str
    participant_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    clock_domain_count: int = Field(ge=0)
    max_call_depth: int = Field(ge=0)
    participants: tuple[ParticipantCallSummary, ...] = Field(default_factory=tuple)
    concurrent_fanout_hotspots: tuple[ConcurrentFanoutHotspot, ...] = Field(default_factory=tuple)
    repeating_loop_hotspots: tuple[RepeatingLoopHotspot, ...] = Field(default_factory=tuple)
    recursive_call_hotspots: tuple[RecursiveCallHotspot, ...] = Field(default_factory=tuple)

    def to_compact_markdown(self, max_items_per_section: int = 15) -> str:
        """Render a bounded (<4,000 tokens) structured Markdown trace digest."""
        limit = max(1, min(max_items_per_section, 50))
        lines = [
            f"# Interaction Trace Digest: `{self.trace_id}`",
            "",
            "## 1. Trace Overview",
            (
                f"- **Participants**: {self.participant_count} | **Events**: {self.event_count} | "
                f"**Clock Domains**: {self.clock_domain_count} | "
                f"**Max Call Depth**: {self.max_call_depth}"
            ),
            "",
            "## 2. Participant Activity",
        ]
        lines.extend(_format_trace_participants(self.participants, limit))
        lines.extend(_format_trace_fanouts(self.concurrent_fanout_hotspots, limit))
        lines.extend(_format_trace_loops(self.repeating_loop_hotspots, limit))
        lines.extend(_format_trace_recursions(self.recursive_call_hotspots, limit))
        return _clamp_markdown("\n".join(lines))


def _format_trace_participants(
    participants: tuple[ParticipantCallSummary, ...],
    limit: int,
) -> list[str]:
    if not participants:
        return ["- None"]
    return [
        (
            f"- `{p.participant_id}` (`{p.symbol_id}`, {p.kind.value}): "
            f"out={p.calls_initiated}, in={p.calls_received}, errors={p.error_count}"
        )
        for p in participants[:limit]
    ]


def _format_trace_fanouts(
    fanouts: tuple[ConcurrentFanoutHotspot, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 3. Concurrent Fan-Out Hotspots (`R5`/`R6` Candidates)"]
    if not fanouts:
        lines.append("- None")
        return lines
    for item in fanouts[:limit]:
        callees = ", ".join(f"`{c}`" for c in item.target_callees[:5])
        lines.append(
            f"- parent=`{item.parent_event_id}` (clock=`{item.clock_domain_id}`, "
            f"caller=`{item.caller_id}`, degree={item.concurrent_degree}): callees=[{callees}]"
        )
    return lines


def _format_trace_loops(
    loops: tuple[RepeatingLoopHotspot, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 4. Repeating Loop Hotspots (`R1`-`R3` Candidates)"]
    if not loops:
        lines.append("- None")
        return lines
    for lp in loops[:limit]:
        lines.append(
            f"- parent=`{lp.parent_event_id}` `{lp.caller_id}` -> `{lp.callee_id}` "
            f"`{lp.signature}` (x{lp.repeat_count})"
        )
    return lines


def _format_trace_recursions(
    recursions: tuple[RecursiveCallHotspot, ...],
    limit: int,
) -> list[str]:
    lines = ["", "## 5. Recursive Call Hotspots (`R4` Candidates)"]
    if not recursions:
        lines.append("- None")
        return lines
    for rec in recursions[:limit]:
        lines.append(
            f"- participant=`{rec.participant_id}` `{rec.signature}` "
            f"(max_depth={rec.max_recursion_depth}, count={rec.occurrence_count})"
        )
    return lines


def _build_rx_digraph(
    ir: StructuralGraphIR,
) -> tuple[rx.PyDiGraph[str, float], list[str], dict[str, int]]:
    """Construct a deterministic rustworkx.PyDiGraph with sorted node indices."""
    graph: rx.PyDiGraph[str, float] = rx.PyDiGraph()
    sorted_syms = sorted(ir.nodes)
    sym_to_idx: dict[str, int] = {}
    for sym_id in sorted_syms:
        sym_to_idx[sym_id] = graph.add_node(sym_id)
    for edge in sorted(ir.edges, key=lambda e: e.edge_id):
        graph.add_edge(sym_to_idx[edge.source], sym_to_idx[edge.target], float(edge.weight))
    return graph, sorted_syms, sym_to_idx


def _compute_containment_summary(ir: StructuralGraphIR) -> tuple[ContainmentClusterSummary, ...]:
    groups: dict[str, list[str]] = defaultdict(list)
    for sym_id in sorted(ir.nodes):
        node = ir.nodes[sym_id]
        if node.parent_id is not None:
            container = node.parent_id
        elif "." in sym_id:
            container = sym_id.rsplit(".", 1)[0]
        elif "/" in sym_id:
            container = sym_id.rsplit("/", 1)[0]
        else:
            container = "<root>"
        groups[container].append(sym_id)

    summaries = [
        ContainmentClusterSummary(
            container_id=cid,
            member_count=len(members),
            sample_symbols=tuple(members[:8]),
        )
        for cid, members in groups.items()
    ]
    summaries.sort(key=lambda item: (-item.member_count, item.container_id))
    return tuple(summaries)


def _compute_pagerank_scores(
    graph: rx.PyDiGraph[str, float],
    node_count: int,
) -> dict[int, float]:
    if node_count == 0:
        return {}
    if graph.num_edges() == 0:
        uniform = 1.0 / float(node_count)
        return dict.fromkeys(range(node_count), uniform)
    raw_pr = rx.pagerank(graph, alpha=0.85, weight_fn=float, max_iter=100, tol=1e-6)
    return {int(idx): round(float(score), 8) for idx, score in raw_pr.items()}


def _compute_centrality_hubs(
    ir: StructuralGraphIR,
    graph: rx.PyDiGraph[str, float],
    sorted_syms: list[str],
    sym_to_idx: dict[str, int],
) -> tuple[CentralityHub, ...]:
    node_count = len(sorted_syms)
    if node_count == 0:
        return ()
    pr_scores = _compute_pagerank_scores(graph, node_count)
    bw_raw = dict(rx.digraph_betweenness_centrality(graph, normalized=True).items())
    hubs: list[CentralityHub] = []
    for sym_id in sorted_syms:
        idx = sym_to_idx[sym_id]
        node = ir.nodes[sym_id]
        hubs.append(
            CentralityHub(
                symbol_id=sym_id,
                label=node.label,
                kind=node.kind,
                pagerank=max(0.0, pr_scores.get(idx, 0.0)),
                betweenness=max(0.0, round(float(bw_raw.get(idx, 0.0)), 8)),
                in_degree=graph.in_degree(idx),
                out_degree=graph.out_degree(idx),
            )
        )
    hubs.sort(
        key=lambda h: (
            -round(h.pagerank, 8),
            -round(h.betweenness, 8),
            -(h.in_degree + h.out_degree),
            h.symbol_id,
        )
    )
    return tuple(hubs)


def _build_undirected_weights(
    sorted_syms: list[str],
    edges: tuple[IREdge, ...],
) -> tuple[dict[str, dict[str, float]], dict[str, float], float]:
    adj: dict[str, dict[str, float]] = {sym: defaultdict(float) for sym in sorted_syms}
    degree_w: dict[str, float] = dict.fromkeys(sorted_syms, 0.0)
    total_w = 0.0
    for edge in edges:
        w = float(edge.weight)
        u, v = edge.source, edge.target
        adj[u][v] += w
        adj[v][u] += w
        degree_w[u] += w
        degree_w[v] += w
        total_w += w
    return adj, degree_w, total_w


def _best_community_for_node(
    sym_id: str,
    node_to_comm: dict[str, str],
    adj: dict[str, dict[str, float]],
    comm_tot: dict[str, float],
    k_i: float,
    two_m: float,
) -> str:
    current_comm = node_to_comm[sym_id]
    comm_weights: dict[str, float] = defaultdict(float)
    for nbr, w in adj[sym_id].items():
        if nbr != sym_id:
            comm_weights[node_to_comm[nbr]] += w

    k_i_in_current = comm_weights.get(current_comm, 0.0)
    tot_current_without = comm_tot[current_comm] - k_i
    base_gain = k_i_in_current - (tot_current_without * k_i) / two_m

    best_comm = current_comm
    best_delta = 0.0
    candidate_comms = sorted(comm_weights.keys())
    for target_comm in candidate_comms:
        if target_comm == current_comm:
            continue
        gain = comm_weights[target_comm] - (comm_tot[target_comm] * k_i) / two_m
        delta = round(gain - base_gain, 12)
        if delta > best_delta or (delta == best_delta and delta > 0.0 and target_comm < best_comm):
            best_delta = delta
            best_comm = target_comm
    return best_comm


def _detect_louvain_communities(
    sorted_syms: list[str],
    edges: tuple[IREdge, ...],
) -> dict[str, str]:
    """Run deterministic Louvain modularity local-moving passes over sorted symbols."""
    node_to_comm = {sym: sym for sym in sorted_syms}
    if not sorted_syms or not edges:
        return node_to_comm

    adj, degree_w, total_w = _build_undirected_weights(sorted_syms, edges)
    two_m = 2.0 * total_w
    if two_m <= 0.0:
        return node_to_comm

    comm_tot: dict[str, float] = dict(degree_w)
    for _ in range(12):
        moved = False
        for sym_id in sorted_syms:
            cur = node_to_comm[sym_id]
            k_i = degree_w[sym_id]
            best = _best_community_for_node(sym_id, node_to_comm, adj, comm_tot, k_i, two_m)
            if best != cur:
                comm_tot[cur] -= k_i
                comm_tot[best] += k_i
                node_to_comm[sym_id] = best
                moved = True
        if not moved:
            break
    return node_to_comm


def _accumulate_cluster_weights(
    edges: tuple[IREdge, ...],
    sym_to_cid: dict[str, str],
) -> tuple[dict[str, float], dict[str, float], dict[tuple[str, str], list[float]]]:
    intra_w: dict[str, float] = defaultdict(float)
    inter_w: dict[str, float] = defaultdict(float)
    pair_couplings: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])

    for edge in edges:
        src_c = sym_to_cid[edge.source]
        dst_c = sym_to_cid[edge.target]
        w = float(edge.weight)
        if src_c == dst_c:
            intra_w[src_c] += w
        else:
            inter_w[src_c] += w
            inter_w[dst_c] += w
            pair = pair_couplings[(src_c, dst_c)]
            pair[0] += 1.0
            pair[1] += w
    return intra_w, inter_w, pair_couplings


def _compute_communities_and_couplings(
    sorted_syms: list[str],
    edges: tuple[IREdge, ...],
) -> tuple[tuple[CommunityCluster, ...], tuple[CommunityCoupling, ...]]:
    if not sorted_syms:
        return (), ()
    raw_assignment = _detect_louvain_communities(sorted_syms, edges)
    grouped: dict[str, list[str]] = defaultdict(list)
    for sym_id in sorted_syms:
        grouped[raw_assignment[sym_id]].append(sym_id)

    ordered_groups = sorted(grouped.values(), key=lambda members: (-len(members), members[0]))
    sym_to_cid: dict[str, str] = {}
    cid_to_members: dict[str, tuple[str, ...]] = {}
    for idx, members in enumerate(ordered_groups):
        cid = f"cluster_{idx}"
        cid_to_members[cid] = tuple(members)
        for m in members:
            sym_to_cid[m] = cid

    intra_w, inter_w, pair_couplings = _accumulate_cluster_weights(edges, sym_to_cid)
    clusters: list[CommunityCluster] = []
    for cid, members in cid_to_members.items():
        in_w = round(intra_w.get(cid, 0.0), 6)
        out_w = round(inter_w.get(cid, 0.0), 6)
        denom = in_w + out_w
        cohesion = round(in_w / denom, 6) if denom > 0.0 else 1.0
        clusters.append(
            CommunityCluster(
                community_id=cid,
                node_count=len(members),
                member_symbols=members,
                intra_edge_weight=in_w,
                inter_edge_weight=out_w,
                cohesion_ratio=min(1.0, max(0.0, cohesion)),
            )
        )

    couplings = [
        CommunityCoupling(
            source_community_id=src_c,
            target_community_id=dst_c,
            edge_count=int(stats[0]),
            total_weight=round(stats[1], 6),
        )
        for (src_c, dst_c), stats in pair_couplings.items()
    ]
    couplings.sort(
        key=lambda c: (-c.total_weight, -c.edge_count, c.source_community_id, c.target_community_id)
    )
    return tuple(clusters), tuple(couplings)


def _compute_leaf_sinks_and_wrappers(
    ir: StructuralGraphIR,
    graph: rx.PyDiGraph[str, float],
    sorted_syms: list[str],
    sym_to_idx: dict[str, int],
) -> tuple[tuple[CandidateLeafSink, ...], tuple[CandidateWrapperNode, ...]]:
    leaf_sinks: list[CandidateLeafSink] = []
    wrappers: list[CandidateWrapperNode] = []

    for sym_id in sorted_syms:
        idx = sym_to_idx[sym_id]
        in_deg = graph.in_degree(idx)
        out_deg = graph.out_degree(idx)
        node = ir.nodes[sym_id]
        if out_deg == 0:
            leaf_sinks.append(
                CandidateLeafSink(
                    symbol_id=sym_id,
                    label=node.label,
                    kind=node.kind,
                    in_degree=in_deg,
                )
            )
        elif in_deg >= 1 and in_deg * out_deg <= _MAX_WRAPPER_FANOUT:
            wrappers.append(
                CandidateWrapperNode(
                    symbol_id=sym_id,
                    label=node.label,
                    kind=node.kind,
                    in_degree=in_deg,
                    out_degree=out_deg,
                    fanout_product=in_deg * out_deg,
                )
            )

    leaf_sinks.sort(key=lambda leaf: (-leaf.in_degree, leaf.symbol_id))
    wrappers.sort(key=lambda w: (w.fanout_product, w.in_degree + w.out_degree, w.symbol_id))
    return tuple(leaf_sinks), tuple(wrappers)


def _compute_event_depths(events: dict[str, TraceEvent]) -> dict[str, int]:
    depths: dict[str, int] = {}
    for eid in sorted(events):
        if eid in depths:
            continue
        chain: list[str] = []
        curr: str | None = eid
        while curr is not None and curr not in depths:
            chain.append(curr)
            curr = events[curr].parent_event_id
        base = depths[curr] if curr is not None else 0
        for item in reversed(chain):
            base += 1
            depths[item] = base
    return depths


def _compute_participant_summaries(
    ir: InteractionTraceIR,
) -> tuple[ParticipantCallSummary, ...]:
    initiated: dict[str, int] = dict.fromkeys(ir.participants, 0)
    received: dict[str, int] = dict.fromkeys(ir.participants, 0)
    errors: dict[str, int] = dict.fromkeys(ir.participants, 0)

    for event in ir.events.values():
        initiated[event.caller_id] += 1
        received[event.callee_id] += 1
        if event.status.value == "error":
            errors[event.callee_id] += 1

    summaries = [
        ParticipantCallSummary(
            participant_id=pid,
            symbol_id=part.symbol_id,
            label=part.label,
            kind=part.kind,
            calls_initiated=initiated[pid],
            calls_received=received[pid],
            error_count=errors[pid],
        )
        for pid, part in ir.participants.items()
    ]
    summaries.sort(
        key=lambda p: (-(p.calls_initiated + p.calls_received), -p.error_count, p.participant_id)
    )
    return tuple(summaries)


def _find_overlapping_cluster(siblings: list[TraceEvent]) -> list[TraceEvent]:
    """Return the maximal overlapping subset of sibling spans within the same clock domain."""
    ordered = sorted(siblings, key=lambda e: (e.start_ns, e.end_ns, e.event_id))
    best_cluster: list[TraceEvent] = []
    active: list[TraceEvent] = []
    for ev in ordered:
        active = [a for a in active if a.end_ns >= ev.start_ns]
        active.append(ev)
        if len(active) > len(best_cluster):
            best_cluster = list(active)
    return best_cluster


def _compute_concurrent_fanouts(
    events: dict[str, TraceEvent],
) -> tuple[ConcurrentFanoutHotspot, ...]:
    by_parent_clock: dict[tuple[str, str], list[TraceEvent]] = defaultdict(list)
    for ev in events.values():
        parent_key = ev.parent_event_id if ev.parent_event_id is not None else "<root>"
        by_parent_clock[(parent_key, ev.clock_domain_id)].append(ev)

    hotspots: list[ConcurrentFanoutHotspot] = []
    for (parent_key, clock_id), siblings in sorted(by_parent_clock.items()):
        if len(siblings) < 2:
            continue
        cluster = _find_overlapping_cluster(siblings)
        if len(cluster) < 2:
            continue
        caller_id = events[parent_key].callee_id if parent_key in events else cluster[0].caller_id
        ev_ids = tuple(sorted(e.event_id for e in cluster))
        callees = tuple(sorted({e.callee_id for e in cluster}))
        hotspots.append(
            ConcurrentFanoutHotspot(
                parent_event_id=parent_key,
                clock_domain_id=clock_id,
                caller_id=caller_id,
                concurrent_degree=len(cluster),
                overlapping_event_ids=ev_ids,
                target_callees=callees,
            )
        )

    hotspots.sort(key=lambda h: (-h.concurrent_degree, h.parent_event_id, h.clock_domain_id))
    return tuple(hotspots)


def _compute_repeating_loops(
    events: dict[str, TraceEvent],
) -> tuple[RepeatingLoopHotspot, ...]:
    by_parent: dict[str, list[TraceEvent]] = defaultdict(list)
    for ev in events.values():
        parent_key = ev.parent_event_id if ev.parent_event_id is not None else "<root>"
        by_parent[parent_key].append(ev)

    loops: list[RepeatingLoopHotspot] = []
    for parent_key, children in sorted(by_parent.items()):
        ordered = sorted(children, key=lambda e: (e.start_ns, e.end_ns, e.event_id))
        grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for ev in ordered:
            grouped[(ev.caller_id, ev.callee_id, ev.signature)].append(ev.event_id)
        for (caller_id, callee_id, sig), eids in grouped.items():
            if len(eids) >= 2:
                loops.append(
                    RepeatingLoopHotspot(
                        parent_event_id=parent_key,
                        caller_id=caller_id,
                        callee_id=callee_id,
                        signature=sig,
                        repeat_count=len(eids),
                        event_ids=tuple(eids),
                    )
                )

    loops.sort(
        key=lambda lp: (
            -lp.repeat_count,
            lp.parent_event_id,
            lp.caller_id,
            lp.callee_id,
            lp.signature,
        )
    )
    return tuple(loops)


def _event_recursion_depth(event: TraceEvent, events: dict[str, TraceEvent]) -> int:
    depth = 1 if event.caller_id == event.callee_id else 0
    curr_id = event.parent_event_id
    while curr_id is not None:
        ancestor = events[curr_id]
        if event.callee_id in {ancestor.callee_id, ancestor.caller_id}:
            depth += 1
        curr_id = ancestor.parent_event_id
    return depth


def _compute_recursive_hotspots(
    events: dict[str, TraceEvent],
) -> tuple[RecursiveCallHotspot, ...]:
    stats: dict[tuple[str, str], tuple[int, int, list[str]]] = {}
    for eid in sorted(events):
        ev = events[eid]
        rec_depth = _event_recursion_depth(ev, events)
        if rec_depth < 1:
            continue
        key = (ev.callee_id, ev.signature)
        if key not in stats:
            stats[key] = (rec_depth, 1, [eid])
        else:
            prev_depth, prev_count, sample_list = stats[key]
            if len(sample_list) < 6:
                sample_list.append(eid)
            stats[key] = (max(prev_depth, rec_depth), prev_count + 1, sample_list)

    hotspots = [
        RecursiveCallHotspot(
            participant_id=pid,
            signature=sig,
            max_recursion_depth=max_d,
            occurrence_count=count,
            sample_event_ids=tuple(samples),
        )
        for (pid, sig), (max_d, count, samples) in stats.items()
    ]
    hotspots.sort(
        key=lambda r: (-r.max_recursion_depth, -r.occurrence_count, r.participant_id, r.signature)
    )
    return tuple(hotspots)


class GraphDigestGenerator:
    """Deterministic generator producing compact StructuralDigest and TraceDigest models."""

    def digest_structural(self, ir: StructuralGraphIR) -> StructuralDigest:
        """Compute Louvain communities, PageRank hubs, leaf sinks, and wrapper chains."""
        graph, sorted_syms, sym_to_idx = _build_rx_digraph(ir)
        node_count = len(sorted_syms)
        weak_cc = len(rx.weakly_connected_components(graph)) if node_count > 0 else 0
        strong_cc = len(rx.strongly_connected_components(graph)) if node_count > 0 else 0
        containment = _compute_containment_summary(ir)
        hubs = _compute_centrality_hubs(ir, graph, sorted_syms, sym_to_idx)
        communities, couplings = _compute_communities_and_couplings(sorted_syms, ir.edges)
        leaves, wrappers = _compute_leaf_sinks_and_wrappers(ir, graph, sorted_syms, sym_to_idx)

        return StructuralDigest(
            graph_id=ir.graph_id,
            node_count=node_count,
            edge_count=len(ir.edges),
            weakly_connected_components=weak_cc,
            strongly_connected_components=strong_cc,
            containment_summary=containment,
            top_hubs=hubs,
            communities=communities,
            community_couplings=couplings,
            candidate_leaf_sinks=leaves,
            candidate_wrappers=wrappers,
        )

    def digest_trace(self, ir: InteractionTraceIR) -> TraceDigest:
        """Compute concurrent fan-out hotspots (R5/R6), loops (R1-R3), and recursion (R4)."""
        depths = _compute_event_depths(ir.events)
        max_depth = max(depths.values(), default=0)
        clock_domains = {ev.clock_domain_id for ev in ir.events.values()}
        participants = _compute_participant_summaries(ir)
        fanouts = _compute_concurrent_fanouts(ir.events)
        loops = _compute_repeating_loops(ir.events)
        recursions = _compute_recursive_hotspots(ir.events)

        return TraceDigest(
            trace_id=ir.trace_id,
            participant_count=len(ir.participants),
            event_count=len(ir.events),
            clock_domain_count=len(clock_domains),
            max_call_depth=max_depth,
            participants=participants,
            concurrent_fanout_hotspots=fanouts,
            repeating_loop_hotspots=loops,
            recursive_call_hotspots=recursions,
        )

    def digest(self, ir: CanonicalIR) -> StructuralDigest | TraceDigest:
        """Dispatch CanonicalIR to digest_structural or digest_trace."""
        if isinstance(ir, StructuralGraphIR):
            return self.digest_structural(ir)
        return self.digest_trace(ir)

    def to_compact_markdown(
        self,
        ir: CanonicalIR | StructuralDigest | TraceDigest,
        max_items_per_section: int = 15,
    ) -> str:
        """Render a bounded (<4,000-token) structured Markdown digest for an IR or digest."""
        if isinstance(ir, (StructuralDigest, TraceDigest)):
            return ir.to_compact_markdown(max_items_per_section=max_items_per_section)
        return self.digest(ir).to_compact_markdown(max_items_per_section=max_items_per_section)
