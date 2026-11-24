"""SCIP symbol & occurrence JSON ingestor with static fork-join concurrency annotations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

_SCIP_ROLE_DEFINITION = 1
_SCIP_ROLE_IMPORT = 2

_NODE_KIND_BY_NAME: dict[str, NodeKind] = {
    "package": NodeKind.PACKAGE,
    "namespace": NodeKind.PACKAGE,
    "module": NodeKind.MODULE,
    "file": NodeKind.MODULE,
    "class": NodeKind.CLASS,
    "struct": NodeKind.CLASS,
    "object": NodeKind.CLASS,
    "singleton": NodeKind.CLASS,
    "type": NodeKind.CLASS,
    "typealias": NodeKind.CLASS,
    "interface": NodeKind.INTERFACE,
    "trait": NodeKind.INTERFACE,
    "protocol": NodeKind.INTERFACE,
    "contract": NodeKind.INTERFACE,
    "enum": NodeKind.ENUM,
    "enummember": NodeKind.ENUM,
    "function": NodeKind.FUNCTION,
    "method": NodeKind.FUNCTION,
    "constructor": NodeKind.FUNCTION,
    "macro": NodeKind.FUNCTION,
    "getter": NodeKind.FUNCTION,
    "setter": NodeKind.FUNCTION,
    "property": NodeKind.COMPONENT,
    "field": NodeKind.COMPONENT,
    "variable": NodeKind.COMPONENT,
    "constant": NodeKind.COMPONENT,
    "service": NodeKind.SERVICE,
    "component": NodeKind.COMPONENT,
    "container": NodeKind.CONTAINER,
    "external": NodeKind.EXTERNAL,
    "database": NodeKind.DATABASE,
    "table": NodeKind.TABLE,
    "queue": NodeKind.QUEUE,
    "supernode": NodeKind.SUPERNODE,
}

# Standard SCIP SymbolInformation.Kind integer enum values
_SCIP_INT_KIND_MAP: dict[int, NodeKind] = {
    1: NodeKind.COMPONENT,
    6: NodeKind.FUNCTION,  # Method
    7: NodeKind.PACKAGE,  # Package
    8: NodeKind.COMPONENT,  # Field
    9: NodeKind.FUNCTION,  # Constructor
    10: NodeKind.ENUM,  # Enum
    11: NodeKind.INTERFACE,  # Interface
    12: NodeKind.FUNCTION,  # Function
    13: NodeKind.COMPONENT,  # Variable
    14: NodeKind.COMPONENT,  # Constant
    17: NodeKind.CLASS,  # Class
    21: NodeKind.ENUM,  # EnumMember
    22: NodeKind.CLASS,  # Struct
    26: NodeKind.FUNCTION,  # Macro
    29: NodeKind.MODULE,  # Module
    30: NodeKind.PACKAGE,  # Namespace
    35: NodeKind.PACKAGE,  # PackageObject
    41: NodeKind.COMPONENT,  # Property
    53: NodeKind.INTERFACE,  # Trait
    54: NodeKind.CLASS,  # Type
    55: NodeKind.CLASS,  # TypeAlias
    58: NodeKind.INTERFACE,  # Contract
    62: NodeKind.INTERFACE,  # Protocol
}

_FORK_JOIN_PATTERNS = (
    "promise.all",
    "promise.allsettled",
    "promise.race",
    "promise.any",
    "asyncio.gather",
    "taskgroup",
    "errgroup",
    "waitgroup",
    "fork_join",
    "forkjoin",
)

_ASYNC_SPAWN_PATTERNS = (
    "async_fire_forget",
    "async_spawn",
    "fire_and_forget",
    "fire_forget",
    "create_task",
    "ensure_future",
    "goroutine",
    "go_stmt",
    "go ",
    "unawaited",
    "void ",
)


@dataclass(frozen=True, slots=True)
class _ParsedDescriptor:
    package_id: str | None
    descriptor_chain: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class _RawNodeRecord:
    symbol_id: str
    display_name: str | None = None
    explicit_kind: NodeKind | None = None
    enclosing_symbol: str | None = None
    file_path: str | None = None
    line_span: tuple[int, int] | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RawEdgeRecord:
    source: str
    target: str
    relation: EdgeRelation
    sync_kind: SyncKind
    parallel_region_id: str | None
    file_path: str | None
    line_span: tuple[int, int] | None
    attributes: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ConcurrencyAnnotation:
    caller: str | None
    callee: str | None
    file_path: str | None
    line_span: tuple[int, int] | None
    relation: EdgeRelation
    sync_kind: SyncKind
    parallel_region_id: str | None
    idiom: str | None


@dataclass(frozen=True, slots=True)
class _DefSpan:
    symbol_id: str
    start_pos: tuple[int, int]
    end_pos: tuple[int, int]
    span_size: tuple[int, int]


def _first_str(
    item: dict[str, Any], keys: tuple[str, ...], fallback: str | None = None
) -> str | None:
    for key in keys:
        val = item.get(key)
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    if fallback is not None and fallback.strip():
        return fallback.strip()
    return None


def _read_payload(
    raw_payload: Path | str | bytes,
    source_name: str,
) -> tuple[str, str, str | None]:
    """Resolve raw payload or file path into (json_text, sha256_hex, default_file_path)."""
    if isinstance(raw_payload, bytes):
        text = raw_payload.decode("utf-8")
        sha = hashlib.sha256(raw_payload).hexdigest()
        default_file = None if source_name == "<memory>" else source_name
        return text, sha, default_file

    if isinstance(raw_payload, Path):
        raw_bytes = raw_payload.read_bytes()
        return (
            raw_bytes.decode("utf-8"),
            hashlib.sha256(raw_bytes).hexdigest(),
            str(raw_payload),
        )

    stripped = raw_payload.lstrip()
    if not stripped.startswith("{") and "\n" not in raw_payload and len(raw_payload) <= 1024:
        candidate = Path(raw_payload)
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

    raw_bytes = raw_payload.encode("utf-8")
    default_file = None if source_name == "<memory>" else source_name
    return raw_payload, hashlib.sha256(raw_bytes).hexdigest(), default_file


def _parse_scip_range(
    raw_range: Any,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    """Convert 0-indexed SCIP range [startLine, startChar, (endLine,) endChar] to 1-based span."""
    if not isinstance(raw_range, list) or len(raw_range) not in {3, 4}:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in raw_range):
        return None
    start_line_0 = max(0, int(raw_range[0]))
    start_char_0 = max(0, int(raw_range[1]))
    if len(raw_range) == 3:
        end_line_0 = start_line_0
        end_char_0 = max(start_char_0, int(raw_range[2]))
    else:
        end_line_0 = max(start_line_0, int(raw_range[2]))
        raw_end_char = int(raw_range[3])
        end_char_0 = raw_end_char if end_line_0 > start_line_0 else max(start_char_0, raw_end_char)
    line_span = (start_line_0 + 1, end_line_0 + 1)
    return line_span, (start_line_0, start_char_0), (end_line_0, end_char_0)


def _consume_call_descriptor(descriptor_str: str, idx: int) -> int:
    close_paren = descriptor_str.find(")", idx)
    if (
        close_paren != -1
        and close_paren + 1 < len(descriptor_str)
        and descriptor_str[close_paren + 1] == "."
    ):
        return close_paren + 2
    return -1


def _split_descriptor_tokens(descriptor_str: str) -> tuple[tuple[str, str], ...]:
    """Tokenize a SCIP descriptor path into (name, suffix) hierarchy steps."""
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    idx = 0
    length = len(descriptor_str)
    while idx < length:
        ch = descriptor_str[idx]
        if ch == "`" and idx + 1 < length:
            end_tick = descriptor_str.find("`", idx + 1)
            if end_tick != -1:
                buf.append(descriptor_str[idx + 1 : end_tick])
                idx = end_tick + 1
                continue
        if ch in {"/", "#", "."}:
            name = "".join(buf).strip()
            buf.clear()
            if name:
                tokens.append((name, ch))
            idx += 1
            continue
        if ch == "(":
            next_idx = _consume_call_descriptor(descriptor_str, idx)
            if next_idx != -1:
                raw_name = "".join(buf).strip()
                buf.clear()
                name = raw_name.split("(", 1)[0].strip() or raw_name
                if name:
                    tokens.append((name, "()."))
                idx = next_idx
                continue
        buf.append(ch)
        idx += 1

    trailing = "".join(buf).strip()
    if trailing:
        tokens.append((trailing, ""))
    return tuple(tokens)


def _parse_scip_symbol(symbol_id: str) -> _ParsedDescriptor:
    """Parse a SCIP symbol string into package prefix and descriptor chain."""
    trimmed = symbol_id.strip()
    if trimmed.startswith("local "):
        return _ParsedDescriptor(package_id=None, descriptor_chain=((trimmed, ""),))

    parts = trimmed.split(" ", 4)
    if len(parts) == 5 and parts[2] != "":
        pkg_name = parts[2]
        pkg_id = (
            f"{parts[0]} {parts[1]} {parts[2]} {parts[3]}" if pkg_name not in {".", "-"} else None
        )
        chain = _split_descriptor_tokens(parts[4])
        return _ParsedDescriptor(package_id=pkg_id, descriptor_chain=chain)

    chain = _split_descriptor_tokens(trimmed)
    return _ParsedDescriptor(package_id=None, descriptor_chain=chain)


def _infer_kind_from_descriptor(symbol_id: str, parsed: _ParsedDescriptor) -> NodeKind:
    """Infer canonical NodeKind from SCIP descriptor suffix or symbol structure."""
    trimmed = symbol_id.strip()
    if not parsed.descriptor_chain:
        return NodeKind.PACKAGE if len(trimmed.split(" ")) == 4 else NodeKind.COMPONENT

    last_name, last_suffix = parsed.descriptor_chain[-1]
    if last_suffix == "()." or (last_suffix == "." and trimmed.endswith("().")):
        return NodeKind.FUNCTION
    if last_suffix == "#":
        is_iface = last_name.startswith("I") and len(last_name) > 1 and last_name[1].isupper()
        return NodeKind.INTERFACE if is_iface else NodeKind.CLASS
    if last_suffix == "/":
        return NodeKind.MODULE
    return NodeKind.COMPONENT


def _resolve_node_kind(raw_kind: Any, symbol_id: str, parsed: _ParsedDescriptor) -> NodeKind:
    """Map explicit SCIP kind (string or integer enum) or fallback to descriptor suffix."""
    if isinstance(raw_kind, str):
        cleaned = raw_kind.strip().lower().replace("_", "").replace("-", "")
        if cleaned in _NODE_KIND_BY_NAME:
            return _NODE_KIND_BY_NAME[cleaned]
    elif (
        isinstance(raw_kind, int)
        and not isinstance(raw_kind, bool)
        and raw_kind in _SCIP_INT_KIND_MAP
    ):
        return _SCIP_INT_KIND_MAP[raw_kind]
    return _infer_kind_from_descriptor(symbol_id, parsed)


def _derive_label(symbol_id: str, display_name: str | None, parsed: _ParsedDescriptor) -> str:
    """Derive human-readable label from display_name or final SCIP descriptor segment."""
    if display_name and display_name.strip():
        return display_name.strip()
    if parsed.descriptor_chain:
        last_name = parsed.descriptor_chain[-1][0].strip()
        if last_name:
            return last_name
    parts = symbol_id.strip().split(" ")
    if len(parts) >= 4 and parts[2] not in {".", "-"}:
        return parts[2]
    return symbol_id.strip()


def _derive_descriptor_parent_id(symbol_id: str) -> str | None:
    """Derive immediate lexical parent SCIP symbol string from descriptor hierarchy."""
    trimmed = symbol_id.strip()
    if trimmed.startswith("local "):
        return None
    parts = trimmed.split(" ", 4)
    if len(parts) < 5:
        return None
    prefix = f"{parts[0]} {parts[1]} {parts[2]} {parts[3]}"
    chain = _split_descriptor_tokens(parts[4])
    if len(chain) <= 1:
        return None
    parent_desc = "".join(f"{name}{suffix}" for name, suffix in chain[:-1])
    if not parent_desc:
        return None
    candidate = f"{prefix} {parent_desc}"
    return candidate if candidate != trimmed else None


def _classify_concurrency_mode(
    raw_mode: str,
    raw_idiom: str,
    *,
    has_parallel_region: bool,
) -> tuple[EdgeRelation, SyncKind]:
    """Classify concurrency annotation into (FORK_JOIN | ASYNC_SPAWN, SyncKind)."""
    combined = f"{raw_mode} {raw_idiom}".lower()
    if any(pat in combined for pat in _FORK_JOIN_PATTERNS):
        return EdgeRelation.FORK_JOIN, SyncKind.FORK_JOIN_BRANCH
    if any(pat in combined for pat in _ASYNC_SPAWN_PATTERNS):
        return EdgeRelation.ASYNC_SPAWN, SyncKind.ASYNC_FIRE_FORGET
    if has_parallel_region:
        return EdgeRelation.FORK_JOIN, SyncKind.FORK_JOIN_BRANCH
    return EdgeRelation.ASYNC_SPAWN, SyncKind.ASYNC_FIRE_FORGET


def _extract_annotation_span(item: dict[str, Any]) -> tuple[int, int] | None:
    """Extract 1-based line_span from concurrency annotation range or line fields."""
    raw_range = item.get("range")
    if raw_range is not None:
        parsed_range = _parse_scip_range(raw_range)
        if parsed_range is not None:
            return parsed_range[0]
    raw_span = item.get("line_span")
    if (
        isinstance(raw_span, list)
        and len(raw_span) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in raw_span)
    ):
        start_1 = max(1, int(raw_span[0]))
        end_1 = max(start_1, int(raw_span[1]))
        return (start_1, end_1)
    raw_line = item.get("line") if item.get("line") is not None else item.get("start_line")
    if isinstance(raw_line, int) and not isinstance(raw_line, bool):
        line_1 = max(1, raw_line if item.get("one_based", True) else raw_line + 1)
        return (line_1, line_1)
    return None


def _parse_concurrency_annotation(
    item: dict[str, Any],
    idx: int,
    default_file: str | None,
) -> _ConcurrencyAnnotation | None:
    """Parse a single static concurrency annotation entry."""
    caller = _first_str(item, ("caller", "source", "enclosing_symbol"))
    callee = _first_str(item, ("callee", "target", "symbol"))
    file_path = _first_str(item, ("file_path", "relative_path"), fallback=default_file)
    line_span = _extract_annotation_span(item)
    raw_mode = _first_str(item, ("mode", "kind", "relation", "sync_kind")) or ""
    raw_idiom = _first_str(item, ("idiom", "construct", "call_site")) or ""
    raw_region = _first_str(item, ("parallel_region_id", "region_id"))

    relation, sync_kind = _classify_concurrency_mode(
        raw_mode,
        raw_idiom,
        has_parallel_region=raw_region is not None,
    )
    if relation is EdgeRelation.FORK_JOIN and raw_region is None:
        loc_tag = f"{file_path or 'scip'}:{line_span[0] if line_span else idx}"
        raw_region = f"par_region:{caller or 'anon'}:{loc_tag}"
    elif relation is EdgeRelation.ASYNC_SPAWN:
        raw_region = None

    if caller is None and callee is None and line_span is None:
        return None
    return _ConcurrencyAnnotation(
        caller=caller,
        callee=callee,
        file_path=file_path,
        line_span=line_span,
        relation=relation,
        sync_kind=sync_kind,
        parallel_region_id=raw_region,
        idiom=raw_idiom or None,
    )


def _collect_annotations(
    payload: dict[str, Any],
    default_file: str | None,
) -> list[_ConcurrencyAnnotation]:
    """Collect top-level and document-level concurrency annotations."""
    annotations: list[_ConcurrencyAnnotation] = []
    raw_top = payload.get("concurrency_annotations")
    if not isinstance(raw_top, list):
        return annotations

    for idx, entry in enumerate(raw_top):
        if not isinstance(entry, dict):
            continue
        targets = entry.get("targets") or entry.get("callees") or entry.get("branches")
        if isinstance(targets, list) and targets:
            for t_idx, target_sym in enumerate(targets):
                if isinstance(target_sym, str) and target_sym.strip():
                    cloned = dict(entry)
                    cloned["callee"] = target_sym.strip()
                    sub_ann = _parse_concurrency_annotation(
                        cloned, idx * 1000 + t_idx, default_file
                    )
                    if sub_ann is not None:
                        annotations.append(sub_ann)
        else:
            ann = _parse_concurrency_annotation(entry, idx, default_file)
            if ann is not None:
                annotations.append(ann)
    return annotations


def _update_node_location(
    existing: _RawNodeRecord,
    file_path: str | None,
    line_span: tuple[int, int] | None,
) -> None:
    if file_path is not None and line_span is not None:
        cand_loc = (file_path, line_span)
        curr_loc = (
            (existing.file_path, existing.line_span)
            if existing.file_path is not None and existing.line_span is not None
            else None
        )
        if curr_loc is None or cand_loc < curr_loc:
            existing.file_path = file_path
            existing.line_span = line_span
    elif file_path is not None and existing.file_path is None:
        existing.file_path = file_path


def _merge_node_record(
    registry: dict[str, _RawNodeRecord],
    update: _RawNodeRecord,
) -> None:
    """Deterministically merge symbol metadata across definitions, symbols, and occurrences."""
    sym = update.symbol_id.strip()
    if not sym:
        return
    existing = registry.get(sym)
    if existing is None:
        registry[sym] = _RawNodeRecord(
            symbol_id=sym,
            display_name=update.display_name,
            explicit_kind=update.explicit_kind,
            enclosing_symbol=update.enclosing_symbol if update.enclosing_symbol != sym else None,
            file_path=update.file_path,
            line_span=update.line_span,
            attributes=dict(update.attributes),
        )
        return

    if update.display_name and (
        existing.display_name is None or update.display_name < existing.display_name
    ):
        existing.display_name = update.display_name
    if update.explicit_kind is not None and existing.explicit_kind is None:
        existing.explicit_kind = update.explicit_kind
    if (
        update.enclosing_symbol
        and existing.enclosing_symbol is None
        and update.enclosing_symbol != sym
    ):
        existing.enclosing_symbol = update.enclosing_symbol
    _update_node_location(existing, update.file_path, update.line_span)
    for k, v in sorted(update.attributes.items()):
        existing.attributes.setdefault(k, v)


def _find_enclosing_def(
    def_spans: list[_DefSpan],
    start_pos: tuple[int, int],
    end_pos: tuple[int, int],
    occ_symbol: str,
) -> str | None:
    """Find innermost definition occurrence enclosing the given range in the same document."""
    best: _DefSpan | None = None
    for dspan in def_spans:
        if dspan.symbol_id == occ_symbol:
            continue
        is_enclosed = dspan.start_pos <= start_pos and end_pos <= dspan.end_pos
        if is_enclosed and (
            best is None or (dspan.span_size, dspan.symbol_id) < (best.span_size, best.symbol_id)
        ):
            best = dspan
    return best.symbol_id if best is not None else None


def _annotation_matches(
    ann: _ConcurrencyAnnotation,
    caller: str,
    callee: str,
    file_path: str | None,
    line_span: tuple[int, int] | None,
) -> int:
    if ann.caller is not None and ann.caller != caller:
        return -1
    if ann.callee is not None and ann.callee != callee:
        return -1
    if ann.file_path is not None and file_path is not None and ann.file_path != file_path:
        return -1
    if (
        ann.line_span is not None
        and line_span is not None
        and not (ann.line_span[0] <= line_span[0] and line_span[1] <= ann.line_span[1])
    ):
        return -1
    return (
        (4 if ann.caller == caller else 0)
        + (4 if ann.callee == callee else 0)
        + (2 if ann.line_span is not None else 0)
        + (1 if ann.file_path is not None else 0)
    )


def _match_annotation(
    caller: str,
    callee: str,
    file_path: str | None,
    line_span: tuple[int, int] | None,
    annotations: list[_ConcurrencyAnnotation],
) -> _ConcurrencyAnnotation | None:
    """Match an occurrence call against static concurrency sidecar annotations."""
    best: _ConcurrencyAnnotation | None = None
    best_score = -1
    for ann in annotations:
        score = _annotation_matches(ann, caller, callee, file_path, line_span)
        if score > best_score:
            best_score = score
            best = ann
    return best


def _extract_doc_def_spans(
    occurrences: list[Any],
    rel_path: str | None,
    registry: dict[str, _RawNodeRecord],
) -> list[_DefSpan]:
    """First pass over document occurrences: index all definition occurrences and spans."""
    def_spans: list[_DefSpan] = []
    for occ in occurrences:
        if not isinstance(occ, dict):
            continue
        sym = str(occ.get("symbol") or "").strip()
        if not sym:
            continue
        roles = occ.get("symbol_roles", 0)
        is_def = (
            isinstance(roles, int)
            and not isinstance(roles, bool)
            and bool(roles & _SCIP_ROLE_DEFINITION)
        ) or bool(occ.get("is_definition"))
        if not is_def:
            continue
        parsed_range = _parse_scip_range(occ.get("range"))
        line_span = parsed_range[0] if parsed_range is not None else None
        enclosing_sym = _first_str(occ, ("enclosing_symbol",))
        _merge_node_record(
            registry,
            _RawNodeRecord(
                symbol_id=sym,
                enclosing_symbol=enclosing_sym,
                file_path=rel_path,
                line_span=line_span,
            ),
        )
        if parsed_range is not None:
            _, start_pos, end_pos = parsed_range
            size = (end_pos[0] - start_pos[0], end_pos[1] - start_pos[1])
            def_spans.append(_DefSpan(sym, start_pos, end_pos, size))
    return def_spans


def _process_doc_symbols(
    symbols: list[Any],
    rel_path: str | None,
    registry: dict[str, _RawNodeRecord],
    raw_edges: list[_RawEdgeRecord],
) -> None:
    """Process a document's symbols array (SymbolInformation and relationships)."""
    for sym_info in symbols:
        if not isinstance(sym_info, dict):
            continue
        sym = str(sym_info.get("symbol") or "").strip()
        if not sym:
            continue
        parsed_desc = _parse_scip_symbol(sym)
        raw_kind = sym_info.get("kind")
        explicit_kind = (
            _resolve_node_kind(raw_kind, sym, parsed_desc) if raw_kind is not None else None
        )
        display_name = _first_str(sym_info, ("display_name",))
        enclosing = _first_str(sym_info, ("enclosing_symbol",))
        parsed_range = _parse_scip_range(sym_info.get("range"))
        line_span = parsed_range[0] if parsed_range is not None else None
        _merge_node_record(
            registry,
            _RawNodeRecord(
                symbol_id=sym,
                display_name=display_name,
                explicit_kind=explicit_kind,
                enclosing_symbol=enclosing,
                file_path=rel_path,
                line_span=line_span,
            ),
        )
        _extract_symbol_relationships(sym, sym_info, rel_path, line_span, registry, raw_edges)


def _resolve_relationship_edge_kind(
    explicit_rel: str,
    src_kind: NodeKind,
    target_kind: NodeKind | None,
) -> EdgeRelation:
    if explicit_rel == "inherits":
        return EdgeRelation.INHERITS
    if explicit_rel == "implements":
        return EdgeRelation.IMPLEMENTS
    if src_kind is NodeKind.CLASS and target_kind is NodeKind.CLASS:
        return EdgeRelation.INHERITS
    return EdgeRelation.IMPLEMENTS


def _extract_symbol_relationships(
    sym: str,
    sym_info: dict[str, Any],
    rel_path: str | None,
    line_span: tuple[int, int] | None,
    registry: dict[str, _RawNodeRecord],
    raw_edges: list[_RawEdgeRecord],
) -> None:
    """Extract IMPLEMENTS / INHERITS edges from SCIP symbol relationships."""
    rels = sym_info.get("relationships")
    if not isinstance(rels, list):
        return
    src_rec = registry.get(sym)
    src_kind = (
        src_rec.explicit_kind
        if src_rec and src_rec.explicit_kind is not None
        else _infer_kind_from_descriptor(sym, _parse_scip_symbol(sym))
    )
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        target_sym = str(rel.get("symbol") or "").strip()
        if not target_sym or target_sym == sym:
            continue
        _merge_node_record(registry, _RawNodeRecord(symbol_id=target_sym, file_path=rel_path))
        if not (
            rel.get("is_implementation", True)
            or rel.get("is_reference")
            or rel.get("is_type_definition")
        ):
            continue
        explicit_rel = (_first_str(rel, ("kind", "relation")) or "").lower()
        target_rec = registry.get(target_sym)
        target_kind = target_rec.explicit_kind if target_rec else None
        edge_rel = _resolve_relationship_edge_kind(explicit_rel, src_kind, target_kind)
        raw_edges.append(
            _RawEdgeRecord(
                source=sym,
                target=target_sym,
                relation=edge_rel,
                sync_kind=SyncKind.SYNC,
                parallel_region_id=None,
                file_path=rel_path,
                line_span=line_span,
                attributes={},
            )
        )


def _build_occurrence_edge(
    enclosing: str,
    target_sym: str,
    rel_path: str | None,
    line_span: tuple[int, int] | None,
    *,
    is_import: bool,
    annotations: list[_ConcurrencyAnnotation],
) -> _RawEdgeRecord:
    ann = _match_annotation(enclosing, target_sym, rel_path, line_span, annotations)
    if ann is not None:
        attrs = {"concurrency_idiom": ann.idiom} if ann.idiom else {}
        return _RawEdgeRecord(
            source=enclosing,
            target=target_sym,
            relation=ann.relation,
            sync_kind=ann.sync_kind,
            parallel_region_id=ann.parallel_region_id,
            file_path=rel_path,
            line_span=line_span,
            attributes=attrs,
        )
    relation = EdgeRelation.IMPORTS if is_import else EdgeRelation.CALLS
    return _RawEdgeRecord(
        source=enclosing,
        target=target_sym,
        relation=relation,
        sync_kind=SyncKind.SYNC,
        parallel_region_id=None,
        file_path=rel_path,
        line_span=line_span,
        attributes={},
    )


def _process_doc_occurrences(
    occurrences: list[Any],
    rel_path: str | None,
    def_spans: list[_DefSpan],
    annotations: list[_ConcurrencyAnnotation],
    registry: dict[str, _RawNodeRecord],
    raw_edges: list[_RawEdgeRecord],
) -> None:
    """Second pass over occurrences: extract CALLS, IMPORTS, FORK_JOIN, and ASYNC_SPAWN edges."""
    for occ in occurrences:
        if not isinstance(occ, dict):
            continue
        target_sym = str(occ.get("symbol") or "").strip()
        if not target_sym:
            continue
        roles = occ.get("symbol_roles", 0)
        is_def = (
            isinstance(roles, int)
            and not isinstance(roles, bool)
            and bool(roles & _SCIP_ROLE_DEFINITION)
        ) or bool(occ.get("is_definition"))
        if is_def:
            continue
        parsed_range = _parse_scip_range(occ.get("range"))
        line_span = parsed_range[0] if parsed_range is not None else None
        enclosing = _first_str(occ, ("enclosing_symbol",))
        if enclosing is None and parsed_range is not None:
            enclosing = _find_enclosing_def(def_spans, parsed_range[1], parsed_range[2], target_sym)
        if enclosing is None or enclosing == target_sym:
            _merge_node_record(
                registry,
                _RawNodeRecord(symbol_id=target_sym, file_path=rel_path, line_span=line_span),
            )
            continue

        _merge_node_record(registry, _RawNodeRecord(symbol_id=enclosing, file_path=rel_path))
        _merge_node_record(registry, _RawNodeRecord(symbol_id=target_sym, file_path=rel_path))
        is_import = (
            isinstance(roles, int)
            and not isinstance(roles, bool)
            and bool(roles & _SCIP_ROLE_IMPORT)
        ) or bool(occ.get("is_import"))
        raw_edges.append(
            _build_occurrence_edge(
                enclosing,
                target_sym,
                rel_path,
                line_span,
                is_import=is_import,
                annotations=annotations,
            )
        )


def _apply_standalone_annotations(
    annotations: list[_ConcurrencyAnnotation],
    registry: dict[str, _RawNodeRecord],
    raw_edges: list[_RawEdgeRecord],
) -> None:
    """Emit explicit edges for concurrency annotations that specify both caller and callee."""
    existing_pairs = {(e.source, e.target, e.relation, e.parallel_region_id) for e in raw_edges}
    for ann in annotations:
        if ann.caller is None or ann.callee is None or ann.caller == ann.callee:
            continue
        key = (ann.caller, ann.callee, ann.relation, ann.parallel_region_id)
        if key in existing_pairs:
            continue
        existing_pairs.add(key)
        _merge_node_record(registry, _RawNodeRecord(symbol_id=ann.caller, file_path=ann.file_path))
        _merge_node_record(registry, _RawNodeRecord(symbol_id=ann.callee, file_path=ann.file_path))
        attrs = {"concurrency_idiom": ann.idiom} if ann.idiom else {}
        raw_edges.append(
            _RawEdgeRecord(
                source=ann.caller,
                target=ann.callee,
                relation=ann.relation,
                sync_kind=ann.sync_kind,
                parallel_region_id=ann.parallel_region_id,
                file_path=ann.file_path,
                line_span=ann.line_span,
                attributes=attrs,
            )
        )


def _resolve_acyclic_parents(registry: dict[str, _RawNodeRecord]) -> dict[str, str | None]:
    """Resolve lexical containment parent_ids and break any cycles deterministically."""
    parent_map: dict[str, str | None] = {}
    for sym in sorted(registry):
        rec = registry[sym]
        candidate = rec.enclosing_symbol
        if candidate is None or candidate not in registry or candidate == sym:
            derived = _derive_descriptor_parent_id(sym)
            candidate = derived if derived in registry and derived != sym else None
        parent_map[sym] = candidate

    for sym in sorted(parent_map):
        visited: set[str] = {sym}
        curr = parent_map[sym]
        while curr is not None:
            if curr in visited:
                parent_map[sym] = None
                break
            visited.add(curr)
            curr = parent_map.get(curr)
    return parent_map


def _build_ir_nodes(
    registry: dict[str, _RawNodeRecord],
    extractor: str,
    input_sha256: str,
    default_file: str | None,
) -> dict[str, IRNode]:
    """Construct canonically ordered IRNode mapping from merged SCIP symbol records."""
    parent_map = _resolve_acyclic_parents(registry)
    nodes: dict[str, IRNode] = {}
    for sym in sorted(registry):
        rec = registry[sym]
        parsed = _parse_scip_symbol(sym)
        kind = (
            rec.explicit_kind
            if rec.explicit_kind is not None
            else _infer_kind_from_descriptor(sym, parsed)
        )
        label = _derive_label(sym, rec.display_name, parsed)
        provenance = ProvenanceStamp(
            grade=ProvenanceGrade.OBSERVED,
            extractor=extractor,
            input_sha256=input_sha256,
            file_path=rec.file_path or default_file,
            line_span=rec.line_span,
        )
        nodes[sym] = IRNode(
            symbol_id=sym,
            label=label,
            kind=kind,
            parent_id=parent_map.get(sym),
            attributes=dict(sorted(rec.attributes.items())),
            provenance=provenance,
        )
    return nodes


def _edge_sort_key(edge: _RawEdgeRecord) -> tuple[str, str, str, str, str, str, int, int]:
    span = edge.line_span or (0, 0)
    return (
        edge.source,
        edge.target,
        edge.relation.value,
        edge.sync_kind.value,
        edge.parallel_region_id or "",
        edge.file_path or "",
        span[0],
        span[1],
    )


def _build_ir_edges(
    raw_edges: list[_RawEdgeRecord],
    extractor: str,
    input_sha256: str,
    default_file: str | None,
) -> tuple[IREdge, ...]:
    """Deduplicate, sort, and build deterministic IREdge tuple."""
    deduped: dict[tuple[str, str, str, str, str], _RawEdgeRecord] = {}
    for edge in sorted(raw_edges, key=_edge_sort_key):
        concurrency_key = (
            edge.source,
            edge.target,
            edge.relation.value,
            edge.sync_kind.value,
            edge.parallel_region_id or "",
        )
        if concurrency_key not in deduped:
            deduped[concurrency_key] = edge

    concurrent_pairs = {
        (e.source, e.target)
        for e in deduped.values()
        if e.relation in {EdgeRelation.FORK_JOIN, EdgeRelation.ASYNC_SPAWN}
    }

    final_records = [
        e
        for e in deduped.values()
        if not (e.relation is EdgeRelation.CALLS and (e.source, e.target) in concurrent_pairs)
    ]
    final_records.sort(key=_edge_sort_key)

    ir_edges: list[IREdge] = []
    for idx, rec in enumerate(final_records):
        digest_input = (
            f"{rec.source}->{rec.target}:{rec.relation.value}:"
            f"{rec.sync_kind.value}:{rec.parallel_region_id or ''}:{idx}"
        )
        short_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:12]
        edge_id = f"scip_edge_{idx}_{short_hash}"
        provenance = ProvenanceStamp(
            grade=ProvenanceGrade.OBSERVED,
            extractor=extractor,
            input_sha256=input_sha256,
            file_path=rec.file_path or default_file,
            line_span=rec.line_span,
        )
        ir_edges.append(
            IREdge(
                edge_id=edge_id,
                source=rec.source,
                target=rec.target,
                relation=rec.relation,
                sync_kind=rec.sync_kind,
                parallel_region_id=rec.parallel_region_id,
                attributes=dict(sorted(rec.attributes.items())),
                provenance=provenance,
            )
        )
    return tuple(ir_edges)


def _ingest_document(
    doc: dict[str, Any],
    default_file: str | None,
    top_annotations: list[_ConcurrencyAnnotation],
    registry: dict[str, _RawNodeRecord],
    raw_edges: list[_RawEdgeRecord],
) -> list[_ConcurrencyAnnotation]:
    """Ingest a single SCIP document dictionary into symbol registry and raw edge list."""
    rel_path = _first_str(doc, ("relative_path", "file_path"), fallback=default_file)
    doc_annotations = list(top_annotations)
    doc_level_anns = _collect_annotations(doc, rel_path)
    doc_annotations.extend(doc_level_anns)

    occurrences = doc.get("occurrences")
    occ_list = occurrences if isinstance(occurrences, list) else []
    def_spans = _extract_doc_def_spans(occ_list, rel_path, registry)

    symbols = doc.get("symbols")
    if isinstance(symbols, list):
        _process_doc_symbols(symbols, rel_path, registry, raw_edges)

    _process_doc_occurrences(occ_list, rel_path, def_spans, doc_annotations, registry, raw_edges)
    return doc_level_anns


class ScipStructuralIngestor(StructuralIngestorPort):
    """Ingest SCIP JSON exports (`scip.graph-export.v1`) and static fork-join annotations."""

    source_format: str = "scip"

    @property
    def extractor_name(self) -> str:
        """Canonical extractor identifier recorded in OBSERVED provenance stamps."""
        return "scip-json"

    def ingest(
        self,
        source: Path | str | bytes | None = None,
        *,
        raw_payload: Path | str | bytes | None = None,
        source_name: str = "<memory>",
    ) -> StructuralGraphIR:
        """Parse SCIP JSON document export and optional static concurrency annotations."""
        effective_payload = raw_payload if raw_payload is not None else source
        if effective_payload is None:
            msg = "Cannot ingest missing SCIP JSON payload"
            raise ValueError(msg)
        text, input_sha256, default_file = _read_payload(effective_payload, source_name)
        if not text.strip():
            msg = "Cannot ingest empty SCIP JSON payload"
            raise ValueError(msg)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"Invalid SCIP JSON payload: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(payload, dict):
            msg = "SCIP JSON payload root must be a JSON object"
            raise ValueError(msg)

        registry: dict[str, _RawNodeRecord] = {}
        raw_edges: list[_RawEdgeRecord] = []
        all_annotations = _collect_annotations(payload, default_file)

        documents = payload.get("documents")
        if isinstance(documents, list):
            for doc in documents:
                if isinstance(doc, dict):
                    doc_anns = _ingest_document(
                        doc, default_file, all_annotations, registry, raw_edges
                    )
                    all_annotations.extend(doc_anns)

        ext_symbols = payload.get("external_symbols") or payload.get("symbols")
        if isinstance(ext_symbols, list):
            _process_doc_symbols(ext_symbols, default_file, registry, raw_edges)

        _apply_standalone_annotations(all_annotations, registry, raw_edges)

        graph_id = (
            _first_str(payload, ("graph_id", "project_name"), fallback="scip_graph") or "scip_graph"
        )
        nodes = _build_ir_nodes(registry, self.extractor_name, input_sha256, default_file)
        edges = _build_ir_edges(raw_edges, self.extractor_name, input_sha256, default_file)
        return StructuralGraphIR(
            graph_id=graph_id,
            nodes=nodes,
            edges=edges,
            metadata={"source_format": self.source_format},
        )
