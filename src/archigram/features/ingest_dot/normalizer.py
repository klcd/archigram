"""Graphviz .dot normalizer and visual-semantics extractor into Canonical StructuralGraphIR."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydot
import pyparsing

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
)
from archigram.ports.contracts import StructuralIngestorPort

pyparsing.ParserElement.enable_packrat()

_PSEUDO_NAMES = frozenset({"node", "edge", "graph", ""})
_SRC_LOC_RE = re.compile(r"^(?P<file>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
_EDGE_RELATIONS = {rel.value: rel for rel in EdgeRelation}
_SYNC_KINDS = {sk.value: sk for sk in SyncKind}
_NODE_KINDS = {nk.value: nk for nk in NodeKind}


def _unquote(value: Any) -> str:
    """Strip outer Graphviz double quotes or angle brackets and unescape characters."""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        return text[1:-1].strip()
    return text


def _clean_attrs(raw_attrs: dict[str, Any] | None) -> dict[str, str]:
    """Normalize pydot attribute dictionary into unquoted string key-value pairs."""
    if not raw_attrs:
        return {}
    return {_unquote(k): _unquote(v) for k, v in raw_attrs.items() if v is not None and _unquote(k)}


def _strip_endpoint_port(endpoint: str) -> str:
    """Extract raw node identifier from a Graphviz port-qualified endpoint."""
    raw = endpoint.strip()
    if raw.startswith('"'):
        end_quote = raw.find('"', 1)
        while end_quote != -1 and raw[end_quote - 1] == "\\":
            end_quote = raw.find('"', end_quote + 1)
        if end_quote != -1:
            return _unquote(raw[: end_quote + 1])
    if ":" in raw:
        return _unquote(raw.split(":", 1)[0])
    return _unquote(raw)


def _parse_src_loc(
    src_loc: str | None,
    fallback_file: str | None,
) -> tuple[str | None, tuple[int, int] | None]:
    """Parse `_src_loc` formatted like `file.py:10-25` into `(file_path, line_span)`."""
    if not src_loc:
        return fallback_file, None
    match = _SRC_LOC_RE.match(src_loc.strip())
    if not match:
        return src_loc.strip() or fallback_file, None
    file_path = match.group("file").strip() or fallback_file
    start = max(1, int(match.group("start")))
    end_str = match.group("end")
    end = max(start, int(end_str)) if end_str is not None else start
    return file_path, (start, end)


def _build_provenance(
    extractor: str,
    input_sha256: str,
    attrs: dict[str, str],
    default_file: str | None,
) -> ProvenanceStamp:
    """Create an OBSERVED ProvenanceStamp honoring `_src_loc` pass-through metadata."""
    file_path, line_span = _parse_src_loc(attrs.get("_src_loc"), default_file)
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor=extractor,
        input_sha256=input_sha256,
        file_path=file_path,
        line_span=line_span,
    )


def _infer_node_kind(attrs: dict[str, str], *, is_cluster: bool) -> NodeKind:
    """Infer canonical NodeKind from `_kind`, shape/label stereotypes, or cluster flag."""
    explicit = attrs.get("_kind", "").lower()
    if explicit in _NODE_KINDS:
        return _NODE_KINDS[explicit]
    if is_cluster:
        return NodeKind.CONTAINER
    label = attrs.get("label", "").lower()
    shape = attrs.get("shape", "").lower()
    if "interface" in label:
        return NodeKind.INTERFACE
    if shape in {"record", "mrecord"} or "{|" in label:
        return NodeKind.CLASS
    return NodeKind.PACKAGE if shape in {"cylinder", "folder", "tab"} else NodeKind.COMPONENT


def _is_async_edge(arrowhead: str, sync_override: str, label: str) -> bool:
    """Detect go-callvis goroutine spawns or explicit async concurrency markers."""
    if arrowhead == "normalnoneodot":
        return True
    if sync_override in {"async", SyncKind.ASYNC_FIRE_FORGET.value}:
        return True
    return "go " in label.lower()


def _infer_uml_relation(arrowhead: str, style: str) -> EdgeRelation | None:
    """Map Doxygen / UML arrowhead and style visual conventions to EdgeRelation."""
    if arrowhead in {"empty", "onormal"}:
        return EdgeRelation.IMPLEMENTS if "dashed" in style else EdgeRelation.INHERITS
    if arrowhead in {"diamond", "ediamond"}:
        return EdgeRelation.COMPOSES
    if arrowhead == "odiamond":
        return EdgeRelation.AGGREGATES
    if arrowhead in {"vee", "open"} and "dashed" in style:
        return EdgeRelation.IMPORTS
    return None


def _extract_edge_semantics(attrs: dict[str, str]) -> tuple[EdgeRelation, SyncKind]:
    """Translate pass-through attributes and Graphviz visual styling into IR semantics."""
    rel_override = attrs.get("_rel_kind", "").strip().lower()
    sync_override = attrs.get("_sync_kind", "").strip().lower()
    arrowhead = attrs.get("arrowhead", "").strip().lower()
    style = attrs.get("style", "").strip().lower()
    label = attrs.get("label", "")

    is_async = _is_async_edge(arrowhead, sync_override, label)
    sync_kind = (
        SyncKind.ASYNC_FIRE_FORGET if is_async else _SYNC_KINDS.get(sync_override, SyncKind.SYNC)
    )

    if rel_override in _EDGE_RELATIONS:
        return _EDGE_RELATIONS[rel_override], sync_kind
    if is_async:
        return EdgeRelation.ASYNC_SPAWN, sync_kind

    uml_rel = _infer_uml_relation(arrowhead, style)
    if uml_rel is not None:
        return uml_rel, sync_kind

    if "dotted" in style or "dashed" in style:
        return EdgeRelation.IMPORTS, sync_kind
    return EdgeRelation.CALLS, sync_kind


def _parse_edge_weight(attrs: dict[str, str]) -> float:
    """Extract positive float weight from DOT `weight` attribute, defaulting to 1.0."""
    raw = attrs.get("weight")
    if not raw:
        return 1.0
    try:
        val = float(raw)
    except ValueError:
        return 1.0
    return val if val > 0.0 else 1.0


@dataclass
class _RawEdgeRecord:
    """Intermediate edge collected during recursive DOT subgraph traversal."""

    raw_source: str
    raw_target: str
    attrs: dict[str, str]
    scope_cluster_id: str | None


@dataclass
class _ParseContext:
    """Mutable accumulator for recursive multi-graph and nested cluster traversal."""

    extractor: str
    input_sha256: str
    default_file: str | None
    graph_id: str = "default"
    metadata: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, IRNode] = field(default_factory=dict)
    raw_id_to_sym: dict[str, str] = field(default_factory=dict)
    cluster_to_sym: dict[str, str] = field(default_factory=dict)
    raw_edges: list[_RawEdgeRecord] = field(default_factory=list)

    def register_node(
        self,
        raw_id: str,
        attrs: dict[str, str],
        parent_sym_id: str | None,
        *,
        is_cluster: bool = False,
    ) -> str:
        """Create or merge an IRNode while preserving acyclic parent containment."""
        sym_id = attrs.get("_sym_id", "").strip() or raw_id
        self.raw_id_to_sym[raw_id] = sym_id
        if is_cluster:
            self.cluster_to_sym[raw_id] = sym_id

        existing = self.nodes.get(sym_id)
        merged_attrs = {**(existing.attributes if existing else {}), **attrs}
        label = (
            merged_attrs.get("label", "").strip() or (existing.label if existing else "") or sym_id
        )
        effective_parent = (
            existing.parent_id if existing and existing.parent_id is not None else parent_sym_id
        )
        if effective_parent == sym_id or self._would_cycle(sym_id, effective_parent):
            effective_parent = existing.parent_id if existing else None

        kind = _infer_node_kind(merged_attrs, is_cluster=is_cluster)
        if existing and existing.kind is NodeKind.CONTAINER:
            kind = NodeKind.CONTAINER

        self.nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=label,
            kind=kind,
            parent_id=effective_parent,
            attributes=merged_attrs,
            provenance=_build_provenance(
                self.extractor, self.input_sha256, merged_attrs, self.default_file
            ),
        )
        return sym_id

    def _would_cycle(self, sym_id: str, candidate_parent: str | None) -> bool:
        """Return True if setting `candidate_parent` on `sym_id` creates a parent cycle."""
        curr = candidate_parent
        visited: set[str] = {sym_id}
        while curr is not None:
            if curr in visited:
                return True
            visited.add(curr)
            parent_node = self.nodes.get(curr)
            curr = parent_node.parent_id if parent_node else None
        return False


def _collect_container_attrs(container: Any) -> dict[str, str]:
    """Gather graph/subgraph attributes including `graph [...]` pseudo-node declarations."""
    attrs = _clean_attrs(container.get_attributes())
    for node in container.get_nodes():
        name = _unquote(node.get_name())
        if name.lower() == "graph":
            attrs.update(_clean_attrs(node.get_attributes()))
    return attrs


def _walk_container(
    container: Any,
    ctx: _ParseContext,
    parent_cluster_sym: str | None,
) -> None:
    """Recursively walk a pydot Graph or Subgraph, preserving all nested nodes and edges."""
    raw_name = _unquote(container.get_name() or "")
    container_attrs = _collect_container_attrs(container)
    current_cluster_sym = parent_cluster_sym

    if raw_name.startswith("cluster"):
        current_cluster_sym = ctx.register_node(
            raw_name,
            container_attrs,
            parent_cluster_sym,
            is_cluster=True,
        )
    elif parent_cluster_sym is None:
        if raw_name and raw_name.lower() not in _PSEUDO_NAMES and ctx.graph_id == "default":
            ctx.graph_id = raw_name
        ctx.metadata.update(container_attrs)

    for node in container.get_nodes():
        node_name = _strip_endpoint_port(str(node.get_name() or ""))
        if not node_name or node_name.lower() in _PSEUDO_NAMES:
            continue
        node_attrs = _clean_attrs(node.get_attributes())
        ctx.register_node(node_name, node_attrs, current_cluster_sym, is_cluster=False)

    for edge in container.get_edges():
        src = _strip_endpoint_port(str(edge.get_source() or ""))
        dst = _strip_endpoint_port(str(edge.get_destination() or ""))
        if not src or not dst or src.lower() in _PSEUDO_NAMES or dst.lower() in _PSEUDO_NAMES:
            continue
        edge_attrs = _clean_attrs(edge.get_attributes())
        ctx.raw_edges.append(_RawEdgeRecord(src, dst, edge_attrs, current_cluster_sym))

    for subgraph in container.get_subgraphs():
        _walk_container(subgraph, ctx, current_cluster_sym)


def _resolve_endpoint(
    raw_endpoint: str,
    cluster_override: str | None,
    scope_cluster_id: str | None,
    ctx: _ParseContext,
) -> str:
    """Resolve an edge endpoint and optional `ltail`/`lhead` cluster boundary override."""
    if raw_endpoint not in ctx.raw_id_to_sym:
        ctx.register_node(raw_endpoint, {}, scope_cluster_id, is_cluster=False)

    if cluster_override:
        cluster_key = _unquote(cluster_override)
        if cluster_key in ctx.cluster_to_sym:
            return ctx.cluster_to_sym[cluster_key]
        if cluster_key in ctx.raw_id_to_sym:
            return ctx.raw_id_to_sym[cluster_key]
        if cluster_key in ctx.nodes:
            return cluster_key
        if cluster_key.startswith("cluster"):
            return ctx.register_node(cluster_key, {}, scope_cluster_id, is_cluster=True)

    return ctx.raw_id_to_sym.get(raw_endpoint, raw_endpoint)


def _build_ir_edges(ctx: _ParseContext) -> tuple[IREdge, ...]:
    """Convert raw edge records into typed IREdge objects with compound cluster resolution."""
    edges: list[IREdge] = []
    for idx, rec in enumerate(ctx.raw_edges):
        ltail = rec.attrs.get("ltail")
        lhead = rec.attrs.get("lhead")
        orig_src = ctx.raw_id_to_sym.get(rec.raw_source, rec.raw_source)
        orig_dst = ctx.raw_id_to_sym.get(rec.raw_target, rec.raw_target)

        source_sym = _resolve_endpoint(rec.raw_source, ltail, rec.scope_cluster_id, ctx)
        target_sym = _resolve_endpoint(rec.raw_target, lhead, rec.scope_cluster_id, ctx)

        edge_attrs = dict(rec.attrs)
        if ltail and source_sym != orig_src:
            edge_attrs.setdefault("_orig_source", orig_src)
        if lhead and target_sym != orig_dst:
            edge_attrs.setdefault("_orig_target", orig_dst)

        relation, sync_kind = _extract_edge_semantics(edge_attrs)
        weight = _parse_edge_weight(edge_attrs)
        edge_id = edge_attrs.get("_edge_id", "").strip() or f"e_{idx}"

        edges.append(
            IREdge(
                edge_id=edge_id,
                source=source_sym,
                target=target_sym,
                relation=relation,
                sync_kind=sync_kind,
                weight=weight,
                attributes=edge_attrs,
                provenance=_build_provenance(
                    ctx.extractor, ctx.input_sha256, edge_attrs, ctx.default_file
                ),
            )
        )
    return tuple(edges)


def _read_dot_source(source: Path | str) -> tuple[str, str, str | None]:
    """Resolve `Path` or raw DOT string into `(dot_text, sha256_hex, file_path)`."""
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return (
            raw_bytes.decode("utf-8"),
            hashlib.sha256(raw_bytes).hexdigest(),
            str(source),
        )

    if "{" not in source and "\n" not in source and len(source) <= 1024:
        candidate = Path(source)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
            raw_bytes = candidate.read_bytes()
            return (
                raw_bytes.decode("utf-8"),
                hashlib.sha256(raw_bytes).hexdigest(),
                str(candidate),
            )

    raw_bytes = source.encode("utf-8")
    return source, hashlib.sha256(raw_bytes).hexdigest(), None


class DotStructuralIngestor(StructuralIngestorPort):
    """Ingest Graphviz `.dot` / `.gv` topology and visual styling into StructuralGraphIR."""

    @property
    def extractor_name(self) -> str:
        """Canonical extractor identifier recorded in OBSERVED provenance stamps."""
        return "graphviz_dot"

    def ingest(self, source: Path | str) -> StructuralGraphIR:
        """Parse single or multi-graph DOT input with nested clusters into StructuralGraphIR."""
        dot_text, input_sha256, default_file = _read_dot_source(source)
        if not dot_text.strip():
            msg = "Cannot ingest empty Graphviz DOT input"
            raise ValueError(msg)

        parsed_graphs = pydot.graph_from_dot_data(dot_text)
        if not parsed_graphs:
            msg = "Failed to parse Graphviz DOT data"
            raise ValueError(msg)

        ctx = _ParseContext(
            extractor=self.extractor_name,
            input_sha256=input_sha256,
            default_file=default_file,
        )
        for root_graph in parsed_graphs:
            _walk_container(root_graph, ctx, parent_cluster_sym=None)

        edges = _build_ir_edges(ctx)
        return StructuralGraphIR(
            graph_id=ctx.graph_id,
            nodes=ctx.nodes,
            edges=edges,
            metadata=ctx.metadata,
        )
