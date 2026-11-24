"""OOP Class Hierarchy Ingestor (JSON descriptors, pyreverse PlantUML, and Graphviz DOT)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydot
import pyparsing

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
from archigram.ports.contracts import StructuralIngestorPort

pyparsing.ParserElement.enable_packrat()

_SRC_LOC_RE = re.compile(r"^(?P<file>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
_STEREOTYPE_RE = re.compile(r"<<([^<>]+)>>")
_BRACE_MOD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ -]*)\}")
_CLASS_DECL_RE = re.compile(
    r"^(?:(?P<abstract>abstract)\s+)?(?P<kind>class|interface|enum|abstract)\s+(?P<body>.+)$",
    re.IGNORECASE,
)
_PKG_DECL_RE = re.compile(
    r"^(?:package|namespace)\s+(?P<body>.+?)\s*\{$",
    re.IGNORECASE,
)
_ARROW_RE = re.compile(
    r"(?P<arrow>"
    r"<\|[-.]+(?:\w+[-.]*)?"
    r"|(?:[-.]+\w+)?[-.]+\|>"
    r"|[*o][-.]+(?:\w+[-.]*)?"
    r"|(?:[-.]+\w+)?[-.]+[*o]"
    r"|<[-.]+(?:\w+[-.]*)?"
    r"|(?:[-.]+\w+)?[-.]+>"
    r"|--"
    r"|\.\."
    r")"
)
_WORD_MODIFIERS = frozenset({"static", "abstract", "final", "readonly", "async", "classmethod"})
_SKIP_PUML_PREFIXES = (
    "@startuml",
    "@enduml",
    "skinparam ",
    "set ",
    "hide ",
    "show ",
    "title ",
    "left to right",
    "top to bottom",
    "allow_mixing",
    "allowmixing",
)
_DOT_PSEUDO = frozenset({"node", "edge", "graph", ""})
_VISIBILITY_MAP: dict[str, Visibility] = {
    "+": Visibility.PUBLIC,
    "public": Visibility.PUBLIC,
    "-": Visibility.PRIVATE,
    "private": Visibility.PRIVATE,
    "#": Visibility.PROTECTED,
    "protected": Visibility.PROTECTED,
    "~": Visibility.PACKAGE,
    "package": Visibility.PACKAGE,
    "internal": Visibility.PACKAGE,
}
_RELATION_ALIASES: dict[str, EdgeRelation] = {
    "inherits": EdgeRelation.INHERITS,
    "extends": EdgeRelation.INHERITS,
    "bases": EdgeRelation.INHERITS,
    "implements": EdgeRelation.IMPLEMENTS,
    "composes": EdgeRelation.COMPOSES,
    "aggregates": EdgeRelation.AGGREGATES,
    "associates": EdgeRelation.ASSOCIATES,
    "uses": EdgeRelation.ASSOCIATES,
    "depends_on": EdgeRelation.ASSOCIATES,
    "imports": EdgeRelation.IMPORTS,
    "calls": EdgeRelation.CALLS,
}
_NODE_KIND_ALIASES: dict[str, NodeKind] = {
    "class": NodeKind.CLASS,
    "dataclass": NodeKind.CLASS,
    "abstract": NodeKind.CLASS,
    "interface": NodeKind.INTERFACE,
    "protocol": NodeKind.INTERFACE,
    "enum": NodeKind.ENUM,
    "enumeration": NodeKind.ENUM,
    "package": NodeKind.PACKAGE,
    "module": NodeKind.PACKAGE,
}
_BRACKET_PAIRS = {"[": "]", "<": ">", "(": ")", "{": "}"}
_CLOSE_TO_OPEN = {"]": "[", ">": "<", "~": "~"}


def _read_source(source: Path | str) -> tuple[str, str, str | None]:
    """Resolve Path or raw content string into (text, sha256_hex, file_path)."""
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return raw_bytes.decode("utf-8"), hashlib.sha256(raw_bytes).hexdigest(), str(source)

    stripped = source.lstrip()
    looks_like_path = (
        "\n" not in source
        and len(source) <= 1024
        and not stripped.startswith(("{", "[", "@startuml", "digraph", "strict", "graph "))
    )
    if looks_like_path:
        candidate = Path(source)
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
            raw_bytes = candidate.read_bytes()
            return raw_bytes.decode("utf-8"), hashlib.sha256(raw_bytes).hexdigest(), str(candidate)

    raw_bytes = source.encode("utf-8")
    return source, hashlib.sha256(raw_bytes).hexdigest(), None


def _detect_format(text: str, file_path: str | None) -> str:
    """Detect whether input text is 'json', 'dot', or 'puml'."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")) or (file_path and file_path.lower().endswith(".json")):
        return "json"
    lower = stripped.lower()
    if (file_path and file_path.lower().endswith((".dot", ".gv"))) or lower.startswith(
        ("digraph ", "digraph{", "strict digraph", "graph ", "graph{", "strict graph")
    ):
        return "dot"
    return "puml"


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split text on separator only at bracket depth zero."""
    parts: list[str] = []
    stack: list[str] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch == sep and not stack:
            parts.append(text[start:idx])
            start = idx + 1
    parts.append(text[start:])
    return parts


def _find_matching_opener(text: str, closer_char: str, opener_char: str) -> int:
    """Find matching opener index for trailing generic delimiter at len(text) - 1."""
    if opener_char == "~":
        return text.rfind("~", 0, len(text) - 1)
    depth = 0
    for idx in range(len(text) - 1, -1, -1):
        ch = text[idx]
        if ch == closer_char:
            depth += 1
        elif ch == opener_char:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _parse_generic_expr(raw: str) -> tuple[str, tuple[str, ...]]:
    """Split a generic type expression like 'Repository[Order]' into (base, params)."""
    cleaned = raw.strip()
    if not cleaned:
        return "", ()
    last = cleaned[-1]
    opener = _CLOSE_TO_OPEN.get(last)
    if opener is None:
        return cleaned, ()
    open_idx = _find_matching_opener(cleaned, last, opener)
    if open_idx <= 0:
        return cleaned, ()
    base = cleaned[:open_idx].strip()
    inner = cleaned[open_idx + 1 : -1].strip()
    if not base or not inner:
        return cleaned, ()
    params = tuple(p.strip() for p in _split_top_level(inner) if p.strip())
    return (base, params) if params else (cleaned, ())


def _canonical_symbol_name(base: str, generics: tuple[str, ...]) -> str:
    """Build collision-free canonical name preserving generic instantiation arguments."""
    if not generics:
        return base
    return f"{base}[{', '.join(generics)}]"


def _parse_src_loc(
    src_loc: str | None,
    fallback_file: str | None,
) -> tuple[str | None, tuple[int, int] | None]:
    """Parse 'file.py:10-25' into (file_path, line_span)."""
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


def _parse_visibility(raw: str | None) -> Visibility:
    """Normalize visibility marker or keyword into Visibility enum."""
    if not raw:
        return Visibility.PUBLIC
    return _VISIBILITY_MAP.get(raw.strip().lower(), Visibility.PUBLIC)


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Convert string, list, or tuple into a tuple of non-blank stripped strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(p.strip() for p in _split_top_level(value) if p.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _strip_member_modifiers(text: str) -> tuple[str, list[str], bool | None]:
    """Extract UML {static}/{abstract}/{field}/{method} and <<...>> modifiers."""
    mods: list[str] = []
    force_method: bool | None = None

    def _collect_brace(match: re.Match[str]) -> str:
        nonlocal force_method
        marker = match.group(1).strip().lower()
        if marker == "method":
            force_method = True
        elif marker == "field":
            force_method = False
        elif marker:
            mods.append(marker)
        return " "

    cleaned = _BRACE_MOD_RE.sub(_collect_brace, text)

    def _collect_stereo(match: re.Match[str]) -> str:
        marker = match.group(1).strip().lower()
        if marker and marker not in mods:
            mods.append(marker)
        return " "

    cleaned = _STEREOTYPE_RE.sub(_collect_stereo, cleaned)
    return cleaned.strip(), mods, force_method


def _extract_leading_visibility(text: str) -> tuple[Visibility, str]:
    """Consume optional UML visibility prefix (+, -, #, ~) from member text."""
    stripped = text.strip()
    if stripped and stripped[0] in _VISIBILITY_MAP:
        return _VISIBILITY_MAP[stripped[0]], stripped[1:].strip()
    return Visibility.PUBLIC, stripped


def _extract_word_modifiers(text: str, mods: list[str]) -> str:
    """Consume leading word modifiers such as 'static' or 'abstract'."""
    parts = text.split(None, 1)
    while len(parts) == 2 and parts[0].lower() in _WORD_MODIFIERS:
        mod = parts[0].lower()
        if mod not in mods:
            mods.append(mod)
        text = parts[1].strip()
        parts = text.split(None, 1)
    return text


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Find matching ')' index for '(' at open_idx."""
    depth = 0
    for idx in range(open_idx, len(text)):
        if text[idx] == "(":
            depth += 1
        elif text[idx] == ")":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _has_method_parens_before_colon(text: str) -> bool:
    """Return True if '(' appears at bracket depth 0 before any top-level ':'."""
    stack: list[str] = []
    for ch in text:
        if ch == "(" and not stack:
            return True
        if ch == ":" and not stack:
            return False
        if ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
    return False


def _parse_method_member(rest: str, vis: Visibility, mods: list[str]) -> ClassMember:
    """Parse a UML method signature into ClassMember."""
    open_idx = rest.find("(")
    if open_idx <= 0:
        name, generics = _parse_generic_expr(rest)
        return ClassMember(
            name=name or "method",
            visibility=vis,
            signature="()",
            is_method=True,
            modifiers=tuple(mods),
            generic_params=generics,
        )
    head = rest[:open_idx].strip()
    name, generics = _parse_generic_expr(head)
    close_idx = _find_matching_paren(rest, open_idx)
    if close_idx == -1:
        return ClassMember(
            name=name or head,
            visibility=vis,
            signature=rest[open_idx:].strip(),
            is_method=True,
            modifiers=tuple(mods),
            generic_params=generics,
        )
    params_inner = rest[open_idx + 1 : close_idx].strip()
    after = rest[close_idx + 1 :].strip()
    if after.startswith("->"):
        ret = after[2:].strip()
    elif after.startswith(":"):
        ret = after[1:].strip()
    else:
        ret = after
    sig = f"({params_inner}) -> {ret}" if ret else f"({params_inner})"
    return ClassMember(
        name=name or head,
        visibility=vis,
        signature=sig,
        is_method=True,
        modifiers=tuple(mods),
        generic_params=generics,
    )


def _parse_field_member(rest: str, vis: Visibility, mods: list[str]) -> ClassMember:
    """Parse a UML attribute/field declaration into ClassMember."""
    parts = _split_top_level(rest, ":")
    head = parts[0].strip()
    field_type = ":".join(parts[1:]).strip() if len(parts) > 1 else ""
    if not field_type and "=" in head:
        head_part, default_part = head.split("=", 1)
        head = head_part.strip()
        field_type = f"= {default_part.strip()}"
    name, generics = _parse_generic_expr(head)
    return ClassMember(
        name=name or rest,
        visibility=vis,
        signature=field_type,
        is_method=False,
        modifiers=tuple(mods),
        generic_params=generics,
    )


def _parse_uml_member(raw_line: str, force_method: bool | None = None) -> ClassMember | None:
    """Parse a single UML compartment member line into ClassMember or None for separators."""
    stripped = raw_line.strip()
    if not stripped or stripped.startswith(("'", "//", "--", "..", "==", "__")):
        return None
    cleaned, mods, explicit_method = _strip_member_modifiers(stripped)
    vis, rest = _extract_leading_visibility(cleaned)
    rest, post_mods, post_method = _strip_member_modifiers(rest)
    for mod in post_mods:
        if mod not in mods:
            mods.append(mod)
    rest = _extract_word_modifiers(rest, mods)
    if not rest:
        return None
    effective_force = post_method if post_method is not None else explicit_method
    if effective_force is None:
        effective_force = force_method
    is_method = (
        effective_force if effective_force is not None else _has_method_parens_before_colon(rest)
    )
    if is_method:
        return _parse_method_member(rest, vis, mods)
    return _parse_field_member(rest, vis, mods)


@dataclass
class _RawEdge:
    """Deferred class relationship record resolved once all nodes are registered."""

    source_ref: str
    target_ref: str
    relation: EdgeRelation
    scope_pkg: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    weight: float = 1.0
    line_span: tuple[int, int] | None = None
    edge_id: str | None = None


@dataclass
class _PackageRegistration:
    """Normalized package declaration payload ready for registration in _ClassGraphBuilder."""

    raw_name: str
    parent_pkg: str | None = None
    explicit_sym: str | None = None
    label: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    file_path: str | None = None
    line_span: tuple[int, int] | None = None


@dataclass
class _ClassRegistration:
    """Normalized class declaration payload ready for registration in _ClassGraphBuilder."""

    raw_name: str
    kind: NodeKind
    parent_pkg: str | None = None
    explicit_sym: str | None = None
    label: str | None = None
    explicit_generics: tuple[str, ...] = ()
    members: tuple[ClassMember, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    file_path: str | None = None
    line_span: tuple[int, int] | None = None


class _ClassGraphBuilder:
    """Accumulator guaranteeing collision-free symbol_ids and valid StructuralGraphIR."""

    def __init__(self, extractor: str, input_sha256: str, default_file: str | None) -> None:
        self.extractor = extractor
        self.input_sha256 = input_sha256
        self.default_file = default_file
        self.graph_id = "default"
        self.metadata: dict[str, str] = {"diagram_variant": "class"}
        self.nodes: dict[str, IRNode] = {}
        self.alias_to_sym: dict[str, str] = {}
        self.short_to_syms: dict[str, list[str]] = {}
        self.raw_edges: list[_RawEdge] = []

    def _stamp(
        self,
        file_path: str | None = None,
        line_span: tuple[int, int] | None = None,
    ) -> ProvenanceStamp:
        return ProvenanceStamp(
            grade=ProvenanceGrade.OBSERVED,
            extractor=self.extractor,
            input_sha256=self.input_sha256,
            file_path=file_path or self.default_file,
            line_span=line_span,
        )

    def _index_short_name(self, short_name: str, sym_id: str) -> None:
        bucket = self.short_to_syms.setdefault(short_name, [])
        if sym_id not in bucket:
            bucket.append(sym_id)

    def register_package(self, reg: _PackageRegistration) -> str:
        """Create or update a PACKAGE IRNode and return its symbol_id."""
        cleaned = reg.raw_name.strip()
        if reg.explicit_sym and reg.explicit_sym.strip():
            sym_id = reg.explicit_sym.strip()
        elif reg.parent_pkg and "." not in cleaned and not cleaned.startswith(f"{reg.parent_pkg}."):
            sym_id = f"{reg.parent_pkg}.{cleaned}"
        else:
            sym_id = cleaned

        existing = self.nodes.get(sym_id)
        merged_attrs = {**(existing.attributes if existing else {}), **reg.attributes}
        eff_parent = existing.parent_id if existing and existing.parent_id else reg.parent_pkg
        if eff_parent == sym_id:
            eff_parent = None
        self.nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=(reg.label or "").strip() or (existing.label if existing else cleaned),
            kind=NodeKind.PACKAGE,
            parent_id=eff_parent,
            members=(),
            attributes=merged_attrs,
            provenance=self._stamp(reg.file_path, reg.line_span),
        )
        self.alias_to_sym[cleaned] = sym_id
        self.alias_to_sym[sym_id] = sym_id
        return sym_id

    def _resolve_class_sym_id(
        self,
        reg: _ClassRegistration,
        base_name: str,
        generics: tuple[str, ...],
    ) -> str:
        """Compute collision-free symbol_id distinguishing generic instantiations."""
        if reg.explicit_sym and reg.explicit_sym.strip():
            candidate = reg.explicit_sym.strip()
            existing = self.nodes.get(candidate)
            if existing is not None:
                existing_gen = existing.attributes.get("generic_params", "")
                new_gen = ", ".join(generics)
                if new_gen and existing_gen != new_gen:
                    return _canonical_symbol_name(candidate, generics)
            return candidate

        qualified_base = (
            f"{reg.parent_pkg}.{base_name}"
            if reg.parent_pkg and "." not in base_name
            else base_name
        )
        return _canonical_symbol_name(qualified_base, generics)

    def register_class(self, reg: _ClassRegistration) -> str:
        """Register or merge a CLASS/INTERFACE/ENUM IRNode with disambiguated symbol_id."""
        parsed_base, parsed_generics = _parse_generic_expr(reg.raw_name)
        base_name = parsed_base or reg.raw_name.strip()
        generics = reg.explicit_generics or parsed_generics
        sym_id = self._resolve_class_sym_id(reg, base_name, generics)
        canon_short = _canonical_symbol_name(base_name.rsplit(".", 1)[-1], generics)

        existing = self.nodes.get(sym_id)
        merged_attrs = {**(existing.attributes if existing else {}), **reg.attributes}
        if generics:
            merged_attrs["generic_params"] = ", ".join(generics)

        merged_members = reg.members if reg.members else (existing.members if existing else ())
        eff_kind = reg.kind
        if existing and eff_kind is NodeKind.CLASS and existing.kind is not NodeKind.CLASS:
            eff_kind = existing.kind
        eff_parent = (
            reg.parent_pkg
            if reg.parent_pkg is not None
            else (existing.parent_id if existing else None)
        )
        if eff_parent == sym_id:
            eff_parent = None

        display_label = (
            (reg.label or "").strip()
            or (existing.label if existing else "")
            or _canonical_symbol_name(base_name, generics)
        )
        self.nodes[sym_id] = IRNode(
            symbol_id=sym_id,
            label=display_label,
            kind=eff_kind,
            parent_id=eff_parent,
            members=merged_members,
            attributes=merged_attrs,
            provenance=self._stamp(reg.file_path, reg.line_span),
        )
        self._register_aliases(reg, sym_id, base_name, generics, canon_short)
        return sym_id

    def _register_aliases(
        self,
        reg: _ClassRegistration,
        sym_id: str,
        base_name: str,
        generics: tuple[str, ...],
        canon_short: str,
    ) -> None:
        self.alias_to_sym[sym_id] = sym_id
        self.alias_to_sym[_canonical_symbol_name(base_name, generics)] = sym_id
        for alias in reg.aliases:
            cleaned_alias = alias.strip()
            if cleaned_alias and (not generics or cleaned_alias != base_name):
                self.alias_to_sym[cleaned_alias] = sym_id
        self._index_short_name(canon_short, sym_id)

    def resolve_endpoint(self, raw_ref: str, scope_pkg: str | None) -> str:
        """Resolve an edge endpoint to a registered symbol_id or auto-register external class."""
        cleaned = raw_ref.strip()
        base, generics = _parse_generic_expr(cleaned)
        canon_ref = _canonical_symbol_name(base, generics) if base else cleaned

        for key in (canon_ref, cleaned, f"{scope_pkg}.{canon_ref}" if scope_pkg else ""):
            if key and key in self.nodes:
                return key
        for key in (canon_ref, cleaned):
            if key in self.alias_to_sym:
                return self.alias_to_sym[key]
        candidates = self.short_to_syms.get(canon_ref, [])
        if len(candidates) == 1:
            return candidates[0]

        return self.register_class(
            _ClassRegistration(
                raw_name=canon_ref,
                kind=NodeKind.CLASS,
                parent_pkg=scope_pkg,
                explicit_generics=generics,
            )
        )

    def add_edge(self, edge: _RawEdge) -> None:
        """Queue a relationship edge for post-declaration resolution."""
        if edge.source_ref.strip() and edge.target_ref.strip():
            self.raw_edges.append(edge)

    def build(self) -> StructuralGraphIR:
        """Resolve all deferred edges and construct validated StructuralGraphIR."""
        edges: list[IREdge] = []
        used_edge_ids: set[str] = set()
        for idx, raw in enumerate(self.raw_edges):
            src = self.resolve_endpoint(raw.source_ref, raw.scope_pkg)
            dst = self.resolve_endpoint(raw.target_ref, raw.scope_pkg)
            eid = raw.edge_id or f"e_{idx}"
            while eid in used_edge_ids:
                eid = f"{eid}_{idx}"
            used_edge_ids.add(eid)
            edges.append(
                IREdge(
                    edge_id=eid,
                    source=src,
                    target=dst,
                    relation=raw.relation,
                    sync_kind=SyncKind.SYNC,
                    weight=raw.weight if raw.weight > 0.0 else 1.0,
                    attributes=raw.attributes,
                    provenance=self._stamp(line_span=raw.line_span),
                )
            )
        return StructuralGraphIR(
            graph_id=self.graph_id,
            nodes=self.nodes,
            edges=tuple(edges),
            metadata=self.metadata,
        )


def _parse_json_params(params_raw: Any) -> str:
    """Format JSON method parameters list or string into '(...)'."""
    if params_raw is None:
        return "()"
    if isinstance(params_raw, str):
        stripped = params_raw.strip()
        if not stripped:
            return "()"
        return stripped if stripped.startswith("(") else f"({stripped})"
    if isinstance(params_raw, list):
        formatted: list[str] = []
        for item in params_raw:
            if isinstance(item, dict):
                p_name = str(item.get("name", "")).strip()
                p_type = str(item.get("type", "")).strip()
                formatted.append(f"{p_name}: {p_type}" if p_type else p_name)
            else:
                formatted.append(str(item).strip())
        return f"({', '.join(p for p in formatted if p)})"
    return "()"


def _parse_json_member(item: Any, *, default_is_method: bool) -> ClassMember | None:
    """Parse either a UML string or structured JSON dict into ClassMember."""
    if isinstance(item, str):
        return _parse_uml_member(item, force_method=default_is_method)
    if not isinstance(item, dict):
        return None
    raw_name = str(item.get("name", "")).strip()
    if not raw_name:
        return None
    name, parsed_generics = _parse_generic_expr(raw_name)
    explicit_generics = _coerce_str_tuple(item.get("generic_params") or item.get("type_params"))
    generics = explicit_generics or parsed_generics
    is_method = bool(item.get("is_method", default_is_method))
    vis = _parse_visibility(str(item.get("visibility", "+")))
    modifiers = _coerce_str_tuple(item.get("modifiers"))

    raw_sig = item.get("signature")
    if isinstance(raw_sig, str) and raw_sig.strip():
        sig = raw_sig.strip()
    elif is_method:
        params_str = _parse_json_params(item.get("params") or item.get("parameters"))
        ret = str(item.get("return_type") or item.get("returns") or "").strip()
        sig = f"{params_str} -> {ret}" if ret else params_str
    else:
        sig = str(item.get("type") or item.get("field_type") or "").strip()

    return ClassMember(
        name=name or raw_name,
        visibility=vis,
        signature=sig,
        is_method=is_method,
        modifiers=modifiers,
        generic_params=generics,
    )


def _collect_json_class_relations(
    item: dict[str, Any],
    cls_sym: str,
    scope_pkg: str | None,
    builder: _ClassGraphBuilder,
) -> None:
    """Queue inheritance, implementation, composition, aggregation, and association edges."""
    field_map: tuple[tuple[tuple[str, ...], EdgeRelation], ...] = (
        (("bases", "inherits", "extends"), EdgeRelation.INHERITS),
        (("implements",), EdgeRelation.IMPLEMENTS),
        (("composes",), EdgeRelation.COMPOSES),
        (("aggregates",), EdgeRelation.AGGREGATES),
        (("associates", "uses", "depends_on"), EdgeRelation.ASSOCIATES),
    )
    for keys, relation in field_map:
        for key in keys:
            targets = item.get(key)
            if not targets:
                continue
            target_list = [targets] if isinstance(targets, (str, dict)) else list(targets)
            for tgt in target_list:
                if isinstance(tgt, dict):
                    tgt_ref = str(tgt.get("target") or tgt.get("name") or "").strip()
                    weight = float(tgt.get("weight", 1.0))
                    attrs = (
                        {"label": str(tgt["label"])} if isinstance(tgt.get("label"), str) else {}
                    )
                else:
                    tgt_ref = str(tgt).strip()
                    weight = 1.0
                    attrs = {}
                if tgt_ref:
                    builder.add_edge(_RawEdge(cls_sym, tgt_ref, relation, scope_pkg, attrs, weight))


def _resolve_json_package(
    pkg_name: str | None,
    parent_pkg: str | None,
    builder: _ClassGraphBuilder,
) -> str | None:
    """Ensure referenced package exists in builder and return its symbol_id."""
    if not pkg_name or not pkg_name.strip():
        return parent_pkg
    cleaned = pkg_name.strip()
    if cleaned in builder.nodes and builder.nodes[cleaned].kind is NodeKind.PACKAGE:
        return cleaned
    return builder.register_package(_PackageRegistration(raw_name=cleaned, parent_pkg=parent_pkg))


def _extract_json_members(item: dict[str, Any]) -> tuple[ClassMember, ...]:
    """Collect field and method ClassMember entries from a JSON class descriptor."""
    members: list[ClassMember] = []
    raw_fields = (
        item.get("fields")
        or item.get("attributes_list")
        or item.get("properties")
        or item.get("members")
        or []
    )
    for field_entry in raw_fields:
        parsed = _parse_json_member(field_entry, default_is_method=False)
        if parsed is not None:
            members.append(parsed)
    for method_entry in item.get("methods") or []:
        parsed = _parse_json_member(method_entry, default_is_method=True)
        if parsed is not None:
            members.append(parsed)
    return tuple(members)


def _ingest_json_class(
    item: dict[str, Any],
    default_pkg: str | None,
    builder: _ClassGraphBuilder,
) -> None:
    """Register one JSON class/interface/enum descriptor and its outgoing relations."""
    raw_name = str(item.get("name") or item.get("symbol_id") or item.get("id") or "").strip()
    if not raw_name:
        return
    pkg_ref = item.get("package") or item.get("parent_id") or item.get("namespace")
    pkg_sym = _resolve_json_package(str(pkg_ref) if pkg_ref else None, default_pkg, builder)

    raw_kind = str(item.get("kind", "class")).strip().lower()
    kind = _NODE_KIND_ALIASES.get(raw_kind, NodeKind.CLASS)
    attrs = (
        {str(k): str(v) for k, v in item["attributes"].items()}
        if isinstance(item.get("attributes"), dict)
        else {}
    )
    modifiers = _coerce_str_tuple(item.get("modifiers"))
    if raw_kind == "abstract" and "abstract" not in modifiers:
        modifiers = (*modifiers, "abstract")
    if modifiers:
        attrs["modifiers"] = ", ".join(modifiers)

    file_path, line_span = _parse_src_loc(
        str(item["src_loc"]) if isinstance(item.get("src_loc"), str) else None,
        str(item["file_path"]) if isinstance(item.get("file_path"), str) else None,
    )
    if isinstance(item.get("line_span"), (list, tuple)) and len(item["line_span"]) == 2:
        start, end = int(item["line_span"][0]), int(item["line_span"][1])
        line_span = (max(1, start), max(max(1, start), end))

    explicit_sym = item.get("symbol_id") or item.get("id")
    cls_sym = builder.register_class(
        _ClassRegistration(
            raw_name=raw_name,
            kind=kind,
            parent_pkg=pkg_sym,
            explicit_sym=str(explicit_sym).strip() if explicit_sym else None,
            label=str(item["label"]).strip() if item.get("label") else None,
            explicit_generics=_coerce_str_tuple(
                item.get("generic_params") or item.get("type_params")
            ),
            members=_extract_json_members(item),
            attributes=attrs,
            aliases=(raw_name,),
            file_path=file_path,
            line_span=line_span,
        )
    )
    _collect_json_class_relations(item, cls_sym, pkg_sym, builder)


def _iter_named_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize either a list of dicts or a {name: dict} map into a list of dicts."""
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    if isinstance(raw, dict):
        entries: list[dict[str, Any]] = []
        for name, val in raw.items():
            if isinstance(val, dict):
                merged = dict(val)
                merged.setdefault("name", str(name))
                entries.append(merged)
        return entries
    return []


def _ingest_json_package(
    item: dict[str, Any],
    parent_pkg: str | None,
    builder: _ClassGraphBuilder,
) -> None:
    """Recursively register a JSON package and its nested subpackages/classes."""
    raw_name = str(item.get("name") or item.get("symbol_id") or item.get("id") or "").strip()
    if not raw_name:
        return
    explicit_sym = item.get("symbol_id") or item.get("id")
    attrs = (
        {str(k): str(v) for k, v in item["attributes"].items()}
        if isinstance(item.get("attributes"), dict)
        else {}
    )
    file_path, line_span = _parse_src_loc(
        str(item["src_loc"]) if isinstance(item.get("src_loc"), str) else None,
        str(item["file_path"]) if isinstance(item.get("file_path"), str) else None,
    )
    pkg_sym = builder.register_package(
        _PackageRegistration(
            raw_name=raw_name,
            parent_pkg=parent_pkg,
            explicit_sym=str(explicit_sym).strip() if explicit_sym else None,
            label=str(item["label"]).strip() if item.get("label") else None,
            attributes=attrs,
            file_path=file_path,
            line_span=line_span,
        )
    )
    for subpkg in _iter_named_entries(item.get("packages") or item.get("subpackages")):
        _ingest_json_package(subpkg, pkg_sym, builder)
    for cls_item in _iter_named_entries(item.get("classes")):
        _ingest_json_class(cls_item, pkg_sym, builder)


def _apply_json_root_metadata(data: dict[str, Any], builder: _ClassGraphBuilder) -> None:
    """Apply graph_id, default file_path, and metadata dictionary from JSON root."""
    graph_id = data.get("graph_id")
    if isinstance(graph_id, str) and graph_id.strip():
        builder.graph_id = graph_id.strip()
    file_path = data.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        builder.default_file = file_path.strip()
    raw_meta = data.get("metadata")
    if isinstance(raw_meta, dict):
        for k, v in raw_meta.items():
            builder.metadata[str(k)] = str(v)
    builder.metadata["diagram_variant"] = "class"


def _ingest_json_edges(raw_edges: Any, builder: _ClassGraphBuilder) -> None:
    """Queue explicit top-level JSON relations/edges entries into the builder."""
    for edge_item in _iter_named_entries(raw_edges):
        src = str(edge_item.get("source") or edge_item.get("from") or "").strip()
        dst = str(edge_item.get("target") or edge_item.get("to") or "").strip()
        rel_key = str(edge_item.get("relation") or edge_item.get("type") or "associates").lower()
        relation = _RELATION_ALIASES.get(rel_key, EdgeRelation.ASSOCIATES)
        attrs = (
            {"label": str(edge_item["label"])} if isinstance(edge_item.get("label"), str) else {}
        )
        weight = float(edge_item.get("weight", 1.0))
        builder.add_edge(_RawEdge(src, dst, relation, None, attrs, weight))


def _ingest_json(text: str, builder: _ClassGraphBuilder) -> None:
    """Parse structured OOP class descriptor JSON into _ClassGraphBuilder."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Invalid OOP class JSON descriptor: {exc}"
        raise ValueError(msg) from exc

    if isinstance(payload, list):
        data: dict[str, Any] = {"classes": payload}
    elif isinstance(payload, dict):
        data = payload
    else:
        msg = "OOP class JSON root must be an object or array of classes"
        raise ValueError(msg)

    _apply_json_root_metadata(data, builder)
    for pkg_item in _iter_named_entries(data.get("packages")):
        _ingest_json_package(pkg_item, None, builder)
    for cls_item in _iter_named_entries(data.get("classes") or data.get("nodes")):
        _ingest_json_class(cls_item, None, builder)
    _ingest_json_edges(data.get("relations") or data.get("edges"), builder)


def _strip_puml_quotes(raw: str) -> str:
    """Strip surrounding double quotes and trailing PlantUML multiplicity tokens."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].strip()
    return text


def _extract_stereotypes(text: str) -> tuple[str, list[str]]:
    """Strip <<stereotype>> markers from header text and return them."""
    stereotypes: list[str] = []

    def _collect(match: re.Match[str]) -> str:
        val = match.group(1).strip()
        if val:
            stereotypes.append(val)
        return " "

    cleaned = _STEREOTYPE_RE.sub(_collect, text)
    return " ".join(cleaned.split()), stereotypes


def _split_as_clause(text: str) -> tuple[str, str | None]:
    """Split PlantUML 'DisplayName as Alias' outside quotes."""
    in_quotes = False
    lower = text.lower()
    for idx in range(len(text)):
        if text[idx] == '"':
            in_quotes = not in_quotes
        elif not in_quotes and lower[idx : idx + 4] == " as ":
            left = _strip_puml_quotes(text[:idx])
            right = _strip_puml_quotes(text[idx + 4 :])
            return left, right or None
    return _strip_puml_quotes(text), None


def _parse_puml_class_header(
    kind_kw: str,
    body_text: str,
    is_abstract_kw: bool,
) -> tuple[str, str | None, str | None, NodeKind, list[str]]:
    """Parse class header into (raw_name, explicit_sym, label, NodeKind, stereotypes)."""
    cleaned, stereotypes = _extract_stereotypes(body_text)
    display_name, alias = _split_as_clause(cleaned)

    kind = _NODE_KIND_ALIASES.get(kind_kw.lower(), NodeKind.CLASS)
    for stype in stereotypes:
        mapped = _NODE_KIND_ALIASES.get(stype.lower())
        if mapped is not None:
            kind = mapped

    if (is_abstract_kw or kind_kw.lower() == "abstract") and "abstract" not in [
        s.lower() for s in stereotypes
    ]:
        stereotypes.append("abstract")

    explicit_sym: str | None = None
    label: str | None = None
    raw_name = display_name
    if alias:
        base_disp, disp_generics = _parse_generic_expr(display_name)
        if "." in alias and alias.endswith(f".{base_disp}"):
            explicit_sym = _canonical_symbol_name(alias, disp_generics)
        label = display_name

    return raw_name, explicit_sym, label, kind, stereotypes


def _clean_puml_endpoint(raw: str) -> str:
    """Extract class endpoint identifier stripping trailing/leading multiplicity quotes."""
    text = raw.strip()
    if text.startswith('"'):
        end_q = text.find('"', 1)
        if end_q != -1 and end_q + 1 < len(text) and text[end_q + 1 :].strip():
            return _clean_puml_endpoint(text[end_q + 1 :])
        return _strip_puml_quotes(text)
    if text.endswith('"'):
        open_q = text.rfind('"', 0, len(text) - 1)
        if open_q > 0 and text[:open_q].strip():
            return _clean_puml_endpoint(text[:open_q])
        return _strip_puml_quotes(text)
    return text


def _classify_puml_arrow(arrow: str) -> tuple[EdgeRelation, bool]:
    """Return (EdgeRelation, reverse_left_right) for a PlantUML arrow token."""
    result: tuple[EdgeRelation, bool] = (EdgeRelation.ASSOCIATES, False)
    if arrow.startswith("<|"):
        result = (EdgeRelation.IMPLEMENTS if "." in arrow else EdgeRelation.INHERITS, True)
    elif arrow.endswith("|>"):
        result = (EdgeRelation.IMPLEMENTS if "." in arrow else EdgeRelation.INHERITS, False)
    elif arrow.startswith("*"):
        result = (EdgeRelation.COMPOSES, False)
    elif arrow.endswith("*"):
        result = (EdgeRelation.COMPOSES, True)
    elif arrow.startswith("o"):
        result = (EdgeRelation.AGGREGATES, False)
    elif arrow.endswith("o"):
        result = (EdgeRelation.AGGREGATES, True)
    elif arrow.startswith("<"):
        result = (EdgeRelation.ASSOCIATES, True)
    return result


def _strip_edge_label(line: str) -> tuple[str, str | None]:
    """Split trailing ': edge label' outside quotes and bracketed type expressions."""
    stack: list[str] = []
    in_quotes = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif in_quotes:
            continue
        elif ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch == ":" and not stack:
            return line[:idx].strip(), line[idx + 1 :].strip() or None
    return line.strip(), None


def _parse_puml_edge_line(line: str) -> tuple[str, str, EdgeRelation, str | None] | None:
    """Parse a PlantUML relationship line into (source, target, relation, label)."""
    edge_part, label = _strip_edge_label(line)
    match = _ARROW_RE.search(edge_part)
    if match is None:
        return None
    left_raw = _clean_puml_endpoint(edge_part[: match.start()])
    right_raw = _clean_puml_endpoint(edge_part[match.end() :])
    if not left_raw or not right_raw:
        return None
    relation, reverse = _classify_puml_arrow(match.group("arrow"))
    source, target = (right_raw, left_raw) if reverse else (left_raw, right_raw)
    return source, target, relation, label


@dataclass
class _ActiveClassBlock:
    """In-progress PlantUML class/interface/enum block being accumulated across lines."""

    raw_name: str
    explicit_sym: str | None
    label: str | None
    kind: NodeKind
    pkg_sym: str | None
    aliases: tuple[str, ...]
    attributes: dict[str, str]
    start_line: int
    members: list[ClassMember] = field(default_factory=list)


class _PumlParser:
    """Line-oriented PlantUML parser supporting packages, compartments, and UML arrows."""

    def __init__(self, builder: _ClassGraphBuilder) -> None:
        self.builder = builder
        self.pkg_stack: list[str] = []
        self.active_class: _ActiveClassBlock | None = None
        self.in_comment_block = False
        self.in_note_block = False

    def _current_pkg(self) -> str | None:
        return self.pkg_stack[-1] if self.pkg_stack else None

    def _skip_non_code_line(self, stripped: str) -> bool:
        skip = False
        if self.in_comment_block:
            if "'/" in stripped:
                self.in_comment_block = False
            skip = True
        elif self.in_note_block:
            if stripped.lower() == "end note":
                self.in_note_block = False
            skip = True
        elif stripped.startswith("/'"):
            self.in_comment_block = "'/" not in stripped[2:]
            skip = True
        else:
            lower = stripped.lower()
            if lower.startswith("note ") and ":" not in stripped:
                self.in_note_block = True
                skip = True
            elif (
                not stripped
                or stripped.startswith(("'", "//", "note "))
                or lower.startswith(_SKIP_PUML_PREFIXES)
            ):
                skip = True
        return skip

    def _finalize_active_class(self, end_line: int) -> None:
        block = self.active_class
        if block is None:
            return
        self.builder.register_class(
            _ClassRegistration(
                raw_name=block.raw_name,
                kind=block.kind,
                parent_pkg=block.pkg_sym,
                explicit_sym=block.explicit_sym,
                label=block.label,
                members=tuple(block.members),
                attributes=block.attributes,
                aliases=block.aliases,
                line_span=(block.start_line, end_line),
            )
        )
        self.active_class = None

    def _handle_active_class_line(
        self,
        block: _ActiveClassBlock,
        stripped: str,
        line_no: int,
    ) -> None:
        if stripped == "}":
            self._finalize_active_class(line_no)
            return
        if stripped.endswith("}"):
            prefix = stripped[:-1].strip()
            if prefix:
                member = _parse_uml_member(prefix)
                if member is not None:
                    block.members.append(member)
            self._finalize_active_class(line_no)
            return
        member = _parse_uml_member(stripped)
        if member is not None:
            block.members.append(member)

    def _handle_class_decl(self, match: re.Match[str], line_no: int) -> None:
        body = match.group("body").strip()
        inline_body = ""
        has_open_brace = False
        closes_inline = False

        if "{" in body:
            open_idx = body.find("{")
            header_part = body[:open_idx].strip()
            rest = body[open_idx + 1 :].strip()
            has_open_brace = True
            if rest.endswith("}"):
                inline_body = rest[:-1].strip()
                closes_inline = True
        else:
            header_part = body

        raw_name, explicit_sym, label, kind, stereotypes = _parse_puml_class_header(
            match.group("kind"), header_part, match.group("abstract") is not None
        )
        _, alias = _split_as_clause(_extract_stereotypes(header_part)[0])
        attrs = {"stereotypes": ", ".join(stereotypes)} if stereotypes else {}
        aliases = (alias,) if alias else ()
        block = _ActiveClassBlock(
            raw_name=raw_name,
            explicit_sym=explicit_sym,
            label=label,
            kind=kind,
            pkg_sym=self._current_pkg(),
            aliases=aliases,
            attributes=attrs,
            start_line=line_no,
        )
        if inline_body:
            for item in inline_body.split(";"):
                member = _parse_uml_member(item)
                if member is not None:
                    block.members.append(member)
        self.active_class = block
        if not has_open_brace or closes_inline:
            self._finalize_active_class(line_no)

    def parse(self, text: str) -> None:
        """Parse entire PlantUML source into the builder."""
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if self._skip_non_code_line(stripped):
                continue
            if self.active_class is not None:
                self._handle_active_class_line(self.active_class, stripped, line_no)
                continue
            if stripped == "}":
                if self.pkg_stack:
                    self.pkg_stack.pop()
                continue
            pkg_match = _PKG_DECL_RE.match(stripped)
            if pkg_match is not None:
                disp, alias = _split_as_clause(pkg_match.group("body"))
                pkg_sym = self.builder.register_package(
                    _PackageRegistration(
                        raw_name=alias or disp,
                        parent_pkg=self._current_pkg(),
                        label=disp,
                        line_span=(line_no, line_no),
                    )
                )
                self.pkg_stack.append(pkg_sym)
                continue
            cls_match = _CLASS_DECL_RE.match(stripped)
            if cls_match is not None:
                self._handle_class_decl(cls_match, line_no)
                continue
            edge_parsed = _parse_puml_edge_line(stripped)
            if edge_parsed is not None:
                src, dst, rel, label = edge_parsed
                attrs = {"label": label} if label else {}
                self.builder.add_edge(
                    _RawEdge(
                        src, dst, rel, self._current_pkg(), attrs, line_span=(line_no, line_no)
                    )
                )


def _unquote_dot(value: Any) -> str:
    """Strip Graphviz double quotes or angle brackets."""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        return text[1:-1].strip()
    return text


def _clean_dot_attrs(raw_attrs: dict[str, Any] | None) -> dict[str, str]:
    """Normalize pydot attribute dict into plain string mapping."""
    if not raw_attrs:
        return {}
    return {
        _unquote_dot(k): _unquote_dot(v)
        for k, v in raw_attrs.items()
        if v is not None and _unquote_dot(k)
    }


def _split_record_compartments(label: str) -> list[str]:
    """Split a Graphviz record label '{Header|Fields|Methods}' at top-level '|'."""
    stripped = label.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        stripped = stripped[1:-1]
    return _split_top_level(stripped, "|")


def _split_dot_lines(compartment: str) -> list[str]:
    """Split Graphviz record compartment lines separated by \\l, \\n, \\r, or newlines."""
    normalized = compartment.replace("\\l", "\n").replace("\\r", "\n").replace("\\n", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _parse_dot_node(
    raw_id: str,
    attrs: dict[str, str],
    pkg_sym: str | None,
    builder: _ClassGraphBuilder,
) -> None:
    """Register a pyreverse Graphviz DOT class node with record compartments."""
    label_raw = attrs.get("label", "").strip()
    shape = attrs.get("shape", "").lower()
    is_record = shape in {"record", "mrecord"} or (
        label_raw.startswith("{") and label_raw.endswith("}") and "|" in label_raw
    )

    members: list[ClassMember] = []
    kind = _NODE_KIND_ALIASES.get(attrs.get("_kind", "").lower(), NodeKind.CLASS)
    header_name = label_raw or raw_id

    if is_record:
        compartments = _split_record_compartments(label_raw)
        header_lines = _split_dot_lines(compartments[0]) if compartments else [raw_id]
        header_joined, stereotypes = _extract_stereotypes(" ".join(header_lines))
        header_name = header_joined or raw_id
        for stype in stereotypes:
            if stype.lower() in _NODE_KIND_ALIASES:
                kind = _NODE_KIND_ALIASES[stype.lower()]
        for idx, comp in enumerate(compartments[1:], start=1):
            force_method = (
                False if idx == 1 and len(compartments) >= 3 else (True if idx >= 2 else None)
            )
            for mline in _split_dot_lines(comp):
                member = _parse_uml_member(mline, force_method=force_method)
                if member is not None:
                    members.append(member)

    file_path, line_span = _parse_src_loc(attrs.get("_src_loc"), None)
    explicit_generics = _coerce_str_tuple(attrs.get("generic_params"))
    if not explicit_generics:
        _, explicit_generics = _parse_generic_expr(header_name)
    builder.register_class(
        _ClassRegistration(
            raw_name=raw_id,
            kind=kind,
            parent_pkg=pkg_sym,
            explicit_sym=attrs.get("_sym_id", "").strip() or None,
            label=header_name,
            explicit_generics=explicit_generics,
            members=tuple(members),
            attributes=attrs,
            aliases=(raw_id, header_name),
            file_path=file_path,
            line_span=line_span,
        )
    )


def _infer_dot_relation(attrs: dict[str, str]) -> tuple[EdgeRelation, bool]:
    """Infer OOP EdgeRelation and direction reversal from Graphviz DOT edge attributes."""
    rel_override = attrs.get("_rel_kind", "").strip().lower()
    if rel_override in _RELATION_ALIASES:
        return _RELATION_ALIASES[rel_override], False
    arrowhead = attrs.get("arrowhead", "").strip().lower()
    arrowtail = attrs.get("arrowtail", "").strip().lower()
    direction = attrs.get("dir", "").strip().lower()
    style = attrs.get("style", "").strip().lower()

    result: tuple[EdgeRelation, bool] = (EdgeRelation.ASSOCIATES, False)
    if direction == "back" and arrowtail in {"empty", "onormal"}:
        result = (EdgeRelation.IMPLEMENTS if "dashed" in style else EdgeRelation.INHERITS, True)
    elif arrowhead in {"empty", "onormal"}:
        result = (EdgeRelation.IMPLEMENTS if "dashed" in style else EdgeRelation.INHERITS, False)
    elif arrowhead in {"diamond", "ediamond"} or arrowtail in {"diamond", "ediamond"}:
        result = (EdgeRelation.COMPOSES, False)
    elif arrowhead == "odiamond" or arrowtail == "odiamond":
        result = (EdgeRelation.AGGREGATES, False)
    return result


def _walk_dot_container(
    container: Any,
    parent_pkg: str | None,
    builder: _ClassGraphBuilder,
) -> None:
    """Recursively traverse pydot Graph/Subgraph for pyreverse class compartments and edges."""
    raw_name = _unquote_dot(container.get_name() or "")
    attrs = _clean_dot_attrs(container.get_attributes())
    current_pkg = parent_pkg

    if raw_name.startswith("cluster"):
        current_pkg = builder.register_package(
            _PackageRegistration(
                raw_name=raw_name,
                parent_pkg=parent_pkg,
                explicit_sym=attrs.get("_sym_id", "").strip() or None,
                label=attrs.get("label", "").strip() or raw_name,
                attributes=attrs,
            )
        )
    elif (
        parent_pkg is None and raw_name.lower() not in _DOT_PSEUDO and builder.graph_id == "default"
    ):
        builder.graph_id = raw_name

    for node in container.get_nodes():
        node_name = _unquote_dot(node.get_name() or "")
        if node_name.lower() in _DOT_PSEUDO:
            continue
        _parse_dot_node(node_name, _clean_dot_attrs(node.get_attributes()), current_pkg, builder)

    for edge in container.get_edges():
        src = _unquote_dot(edge.get_source() or "")
        dst = _unquote_dot(edge.get_destination() or "")
        if src.lower() in _DOT_PSEUDO or dst.lower() in _DOT_PSEUDO:
            continue
        edge_attrs = _clean_dot_attrs(edge.get_attributes())
        relation, reverse = _infer_dot_relation(edge_attrs)
        eff_src, eff_dst = (dst, src) if reverse else (src, dst)
        builder.add_edge(_RawEdge(eff_src, eff_dst, relation, current_pkg, edge_attrs))

    for subgraph in container.get_subgraphs():
        _walk_dot_container(subgraph, current_pkg, builder)


def _ingest_dot(text: str, builder: _ClassGraphBuilder) -> None:
    """Parse pyreverse Graphviz .dot text into _ClassGraphBuilder."""
    parsed = pydot.graph_from_dot_data(text)
    if not parsed:
        msg = "Failed to parse Graphviz DOT class hierarchy input"
        raise ValueError(msg)
    for root in parsed:
        _walk_dot_container(root, None, builder)


class ClassHierarchyIngestor(StructuralIngestorPort):
    """Ingest OOP class descriptors (JSON) and pyreverse (.puml / .dot) into StructuralGraphIR."""

    @property
    def extractor_name(self) -> str:
        """Canonical extractor identifier recorded in OBSERVED provenance stamps."""
        return "oop_class_ingestor"

    def ingest(self, source: Path | str) -> StructuralGraphIR:
        """Parse JSON, PlantUML, or DOT OOP class metadata into StructuralGraphIR."""
        text, input_sha256, default_file = _read_source(source)
        if not text.strip():
            msg = "Cannot ingest empty OOP class hierarchy input"
            raise ValueError(msg)

        builder = _ClassGraphBuilder(
            extractor=self.extractor_name,
            input_sha256=input_sha256,
            default_file=default_file,
        )
        fmt = _detect_format(text, default_file)
        if fmt == "json":
            _ingest_json(text, builder)
        elif fmt == "dot":
            _ingest_dot(text, builder)
        else:
            _PumlParser(builder).parse(text)

        return builder.build()
