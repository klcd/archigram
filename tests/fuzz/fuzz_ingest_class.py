"""Hypothesis property-based fuzz suite for OOP Class Hierarchy Ingestor."""

from __future__ import annotations

import hashlib
import json
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain import ProvenanceGrade
from archigram.domain.ir import (
    ClassMember,
    EdgeRelation,
    NodeKind,
    StructuralGraphIR,
    Visibility,
)
from archigram.features.ingest_class import ClassHierarchyIngestor
from archigram.ports.contracts import StructuralIngestorPort

_IDENT = st.from_regex(r"^[A-Za-z][A-Za-z0-9_]{1,10}$", fullmatch=True)
_TYPE_NAMES = st.sampled_from(["int", "str", "bool", "float", "bytes", "Order", "User", "Decimal"])
_MODIFIERS = st.sampled_from(["static", "abstract", "readonly"])
_OOP_RELATIONS = st.sampled_from(
    [
        EdgeRelation.INHERITS,
        EdgeRelation.IMPLEMENTS,
        EdgeRelation.COMPOSES,
        EdgeRelation.AGGREGATES,
        EdgeRelation.ASSOCIATES,
    ]
)
_CLASS_KINDS = st.sampled_from([NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM])


@dataclass(frozen=True)
class _MemberSpec:
    name: str
    visibility: Visibility
    is_method: bool
    type_or_ret: str
    params: tuple[tuple[str, str], ...]
    modifiers: tuple[str, ...]
    generic_params: tuple[str, ...]


@dataclass(frozen=True)
class _ClassSpec:
    pkg_name: str
    base_name: str
    kind: NodeKind
    generic_params: tuple[str, ...]
    members: tuple[_MemberSpec, ...]

    @property
    def qualified_id(self) -> str:
        base = f"{self.pkg_name}.{self.base_name}"
        if self.generic_params:
            return f"{base}[{', '.join(self.generic_params)}]"
        return base


@dataclass(frozen=True)
class _EdgeSpec:
    source_id: str
    target_id: str
    relation: EdgeRelation


@dataclass(frozen=True)
class _HierarchySpec:
    packages: tuple[str, ...]
    classes: tuple[_ClassSpec, ...]
    edges: tuple[_EdgeSpec, ...]


@st.composite
def _member_spec_strategy(draw: st.DrawFn, *, allow_method_generics: bool = True) -> _MemberSpec:
    is_method = draw(st.booleans())
    name = draw(_IDENT)
    vis = draw(st.sampled_from(list(Visibility)))
    type_or_ret = draw(_TYPE_NAMES)
    mods = tuple(draw(st.lists(_MODIFIERS, max_size=2, unique=True)))
    generics = (
        tuple(draw(st.lists(st.sampled_from(["T", "K", "V", "Item"]), max_size=2, unique=True)))
        if (is_method and allow_method_generics and draw(st.booleans()))
        else ()
    )
    params: tuple[tuple[str, str], ...] = ()
    if is_method:
        p_count = draw(st.integers(min_value=0, max_value=2))
        p_names = draw(st.lists(_IDENT, min_size=p_count, max_size=p_count, unique=True))
        params = tuple((pn, draw(_TYPE_NAMES)) for pn in p_names)
    return _MemberSpec(
        name=name,
        visibility=vis,
        is_method=is_method,
        type_or_ret=type_or_ret,
        params=params,
        modifiers=mods,
        generic_params=generics,
    )


@st.composite
def _oop_hierarchy_strategy(draw: st.DrawFn) -> _HierarchySpec:
    pkg_count = draw(st.integers(min_value=1, max_value=3))
    packages = tuple(f"pkg_{idx}" for idx in range(pkg_count))
    cls_count = draw(st.integers(min_value=2, max_value=8))

    classes: list[_ClassSpec] = []
    seen_ids: set[str] = set()
    for idx in range(cls_count):
        pkg = packages[idx % pkg_count]
        use_shared_generic = idx < 2 or draw(st.booleans())
        if use_shared_generic:
            base_name = "Repository"
            gen_arg = f"Entity_{idx}"
            generics: tuple[str, ...] = (gen_arg,)
        else:
            base_name = f"Model_{idx}"
            generics = (
                tuple(draw(st.lists(st.sampled_from(["K", "V", "Order"]), min_size=1, max_size=2)))
                if draw(st.booleans())
                else ()
            )
        spec = _ClassSpec(
            pkg_name=pkg,
            base_name=base_name,
            kind=draw(_CLASS_KINDS),
            generic_params=generics,
            members=tuple(draw(st.lists(_member_spec_strategy(), min_size=1, max_size=4))),
        )
        if spec.qualified_id not in seen_ids:
            seen_ids.add(spec.qualified_id)
            classes.append(spec)

    edge_count = draw(st.integers(min_value=1, max_value=10))
    edges: list[_EdgeSpec] = []
    for _ in range(edge_count):
        src_idx = draw(st.integers(min_value=1, max_value=len(classes) - 1))
        dst_idx = draw(st.integers(min_value=0, max_value=src_idx - 1))
        edges.append(
            _EdgeSpec(
                source_id=classes[src_idx].qualified_id,
                target_id=classes[dst_idx].qualified_id,
                relation=draw(_OOP_RELATIONS),
            )
        )
    return _HierarchySpec(packages=packages, classes=tuple(classes), edges=tuple(edges))


def _render_json_hierarchy(spec: _HierarchySpec) -> str:
    pkg_map: dict[str, list[dict[str, Any]]] = {pkg: [] for pkg in spec.packages}
    for cls in spec.classes:
        fields_json: list[dict[str, Any]] = []
        methods_json: list[dict[str, Any]] = []
        for mem in cls.members:
            if mem.is_method:
                methods_json.append(
                    {
                        "name": mem.name,
                        "visibility": mem.visibility.value,
                        "params": [{"name": pn, "type": pt} for pn, pt in mem.params],
                        "return_type": mem.type_or_ret,
                        "modifiers": list(mem.modifiers),
                        "generic_params": list(mem.generic_params),
                    }
                )
            else:
                fields_json.append(
                    {
                        "name": mem.name,
                        "visibility": mem.visibility.value,
                        "type": mem.type_or_ret,
                        "modifiers": list(mem.modifiers),
                        "generic_params": list(mem.generic_params),
                    }
                )
        pkg_map[cls.pkg_name].append(
            {
                "name": cls.base_name,
                "kind": cls.kind.value,
                "generic_params": list(cls.generic_params),
                "fields": fields_json,
                "methods": methods_json,
            }
        )

    payload = {
        "graph_id": "oop_fuzz",
        "packages": [{"name": pkg, "classes": cls_list} for pkg, cls_list in pkg_map.items()],
        "relations": [
            {"source": e.source_id, "target": e.target_id, "relation": e.relation.value}
            for e in spec.edges
        ],
    }
    return json.dumps(payload)


def _format_puml_member(mem: _MemberSpec) -> str:
    mods = " ".join(f"{{{m}}}" for m in mem.modifiers)
    mod_prefix = f"{mods} " if mods else ""
    gen_suffix = f"[{', '.join(mem.generic_params)}]" if mem.generic_params else ""
    if mem.is_method:
        p_str = ", ".join(f"{pn}: {pt}" for pn, pt in mem.params)
        sig_head = f"{mod_prefix}{mem.visibility.value}{mem.name}{gen_suffix}"
        return f"    {sig_head}({p_str}): {mem.type_or_ret}"
    return f"    {mod_prefix}{mem.visibility.value}{mem.name}{gen_suffix}: {mem.type_or_ret}"


def _render_puml_arrow(edge: _EdgeSpec) -> str:
    if edge.relation is EdgeRelation.INHERITS:
        return f"{edge.target_id} <|-- {edge.source_id}"
    if edge.relation is EdgeRelation.IMPLEMENTS:
        return f"{edge.target_id} <|.. {edge.source_id}"
    if edge.relation is EdgeRelation.COMPOSES:
        return f"{edge.source_id} *-- {edge.target_id}"
    if edge.relation is EdgeRelation.AGGREGATES:
        return f"{edge.source_id} o-- {edge.target_id}"
    return f"{edge.source_id} --> {edge.target_id}"


def _render_puml_hierarchy(spec: _HierarchySpec) -> str:
    lines: list[str] = ["@startuml"]
    by_pkg: dict[str, list[_ClassSpec]] = {pkg: [] for pkg in spec.packages}
    for cls in spec.classes:
        by_pkg[cls.pkg_name].append(cls)

    for pkg in spec.packages:
        lines.append(f"package {pkg} {{")
        for cls in by_pkg[pkg]:
            kw = (
                "interface"
                if cls.kind is NodeKind.INTERFACE
                else ("enum" if cls.kind is NodeKind.ENUM else "class")
            )
            gen = f"[{', '.join(cls.generic_params)}]" if cls.generic_params else ""
            lines.append(f"  {kw} {cls.base_name}{gen} {{")
            for mem in cls.members:
                lines.append(_format_puml_member(mem))
            lines.append("  }")
        lines.append("}")

    for edge in spec.edges:
        lines.append(_render_puml_arrow(edge))
    lines.append("@enduml")
    return "\n".join(lines)


def _assert_member_fidelity(expected: _MemberSpec, actual: ClassMember) -> None:
    assert actual.name == expected.name
    assert actual.visibility is expected.visibility
    assert actual.is_method is expected.is_method
    assert actual.modifiers == expected.modifiers
    assert actual.generic_params == expected.generic_params
    if expected.is_method:
        p_str = ", ".join(f"{pn}: {pt}" for pn, pt in expected.params)
        assert actual.signature == f"({p_str}) -> {expected.type_or_ret}"
    else:
        assert actual.signature == expected.type_or_ret


def _assert_graph_fidelity(
    ir: StructuralGraphIR,
    spec: _HierarchySpec,
    expected_sha: str,
    *,
    use_puml: bool,
) -> None:
    assert ir.metadata.get("diagram_variant") == "class"
    expected_ids = set(spec.packages) | {cls.qualified_id for cls in spec.classes}
    assert set(ir.nodes.keys()) == expected_ids

    for pkg in spec.packages:
        pkg_node = ir.nodes[pkg]
        assert pkg_node.kind is NodeKind.PACKAGE
        assert pkg_node.provenance.grade is ProvenanceGrade.OBSERVED
        assert pkg_node.provenance.extractor == "oop_class_ingestor"
        assert pkg_node.provenance.input_sha256 == expected_sha

    for cls in spec.classes:
        node = ir.nodes[cls.qualified_id]
        assert node.kind is cls.kind
        assert node.parent_id == cls.pkg_name
        assert node.provenance.grade is ProvenanceGrade.OBSERVED
        assert node.provenance.input_sha256 == expected_sha
        if cls.generic_params:
            assert node.attributes.get("generic_params") == ", ".join(cls.generic_params)
        expected_members = (
            cls.members
            if use_puml
            else (
                *[m for m in cls.members if not m.is_method],
                *[m for m in cls.members if m.is_method],
            )
        )
        assert len(node.members) == len(expected_members)
        for exp_m, act_m in zip(expected_members, node.members, strict=True):
            _assert_member_fidelity(exp_m, act_m)

    expected_edges = [(e.source_id, e.target_id, e.relation) for e in spec.edges]
    actual_edges = [(e.source, e.target, e.relation) for e in ir.edges]
    assert sorted(actual_edges) == sorted(expected_edges)
    for edge in ir.edges:
        assert edge.provenance.grade is ProvenanceGrade.OBSERVED
        assert edge.provenance.extractor == "oop_class_ingestor"
        assert edge.provenance.input_sha256 == expected_sha


@settings(max_examples=30, deadline=None)
@given(spec=_oop_hierarchy_strategy(), use_puml=st.booleans())
def test_ingest_class_hierarchy_json_and_puml_fidelity(
    spec: _HierarchySpec,
    use_puml: bool,
) -> None:
    ingestor = ClassHierarchyIngestor()
    assert isinstance(ingestor, StructuralIngestorPort)
    assert ingestor.extractor_name == "oop_class_ingestor"

    raw = _render_puml_hierarchy(spec) if use_puml else _render_json_hierarchy(spec)
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    ir = ingestor.ingest(raw)
    _assert_graph_fidelity(ir, spec, sha, use_puml=use_puml)

    ir_second = ingestor.ingest(raw)
    assert ir.model_dump_json() == ir_second.model_dump_json()


@settings(max_examples=25, deadline=None)
@given(
    arg_a=st.from_regex(r"^[A-Z][a-z]{2,8}$", fullmatch=True),
    arg_b=st.from_regex(r"^[A-Z][a-z]{2,8}$", fullmatch=True),
    use_dot=st.booleans(),
)
def test_generic_instantiation_disambiguation_and_dot_compartments(
    arg_a: str,
    arg_b: str,
    use_dot: bool,
) -> None:
    second_arg = f"{arg_b}Item" if arg_b == arg_a else arg_b
    ingestor = ClassHierarchyIngestor()
    if use_dot:
        lbl_a = f"{{Repository[{arg_a}]|+items: list[{arg_a}]\\l|+save(x: {arg_a}): bool\\l}}"
        lbl_b = f"{{Repository[{second_arg}]|-count: int\\l|+get(id: str): {second_arg}\\l}}"
        dot_src = (
            "digraph Classes {\n"
            f'  "Repository[{arg_a}]" [shape="record", label="{lbl_a}"];\n'
            f'  "Repository[{second_arg}]" [shape="record", label="{lbl_b}"];\n'
            f'  "Repository[{arg_a}]" -> "Repository[{second_arg}]" [arrowhead="odiamond"];\n'
            "}\n"
        )
        ir = ingestor.ingest(dot_src)
        sym_a = f"Repository[{arg_a}]"
        sym_b = f"Repository[{second_arg}]"
        assert sym_a in ir.nodes and sym_b in ir.nodes
        assert ir.nodes[sym_a].attributes["generic_params"] == arg_a
        assert ir.nodes[sym_b].attributes["generic_params"] == second_arg
        assert len(ir.nodes[sym_a].members) == 2
        assert not ir.nodes[sym_a].members[0].is_method
        assert ir.nodes[sym_a].members[1].is_method
        assert ir.edges[0].relation is EdgeRelation.AGGREGATES
        return

    puml_src = (
        "@startuml\n"
        f'class "Repository<{arg_a}>" as RepoA {{\n'
        f"  +items: list[{arg_a}]\n"
        f"  +save[T](item: T): bool\n"
        "}\n"
        f'class "Repository<{second_arg}>" as RepoB {{\n'
        f"  #cache: dict[str, {second_arg}]\n"
        f"  -evict(): None\n"
        "}\n"
        "RepoA *-- RepoB\n"
        "@enduml\n"
    )
    ir = ingestor.ingest(puml_src)
    sym_a = f"Repository[{arg_a}]"
    sym_b = f"Repository[{second_arg}]"
    assert sym_a in ir.nodes and sym_b in ir.nodes
    assert ir.nodes[sym_a].attributes["generic_params"] == arg_a
    assert ir.nodes[sym_b].attributes["generic_params"] == second_arg
    assert ir.nodes[sym_a].members[1].generic_params == ("T",)
    assert ir.nodes[sym_b].members[0].visibility is Visibility.PROTECTED
    assert ir.nodes[sym_b].members[1].visibility is Visibility.PRIVATE
    assert ir.edges[0].source == sym_a
    assert ir.edges[0].target == sym_b
    assert ir.edges[0].relation is EdgeRelation.COMPOSES


@settings(max_examples=15, deadline=None)
@given(spec=_oop_hierarchy_strategy(), pass_as_str=st.booleans())
def test_ingest_class_file_path_and_str_equivalence(
    spec: _HierarchySpec,
    pass_as_str: bool,
) -> None:
    ingestor = ClassHierarchyIngestor()
    puml_src = _render_puml_hierarchy(spec)
    with tempfile.TemporaryDirectory() as tmp_dir:
        puml_file = Path(tmp_dir) / "classes.puml"
        puml_file.write_text(puml_src, encoding="utf-8")

        ir_file = ingestor.ingest(str(puml_file) if pass_as_str else puml_file)
        ir_raw = ingestor.ingest(puml_src)

        assert set(ir_file.nodes.keys()) == set(ir_raw.nodes.keys())
        assert len(ir_file.edges) == len(ir_raw.edges)
        for node in ir_file.nodes.values():
            assert node.provenance.file_path == str(puml_file)


@settings(max_examples=20)
@given(bad_text=st.text(alphabet=string.whitespace, max_size=20))
def test_ingest_class_rejects_blank_input(bad_text: str) -> None:
    ingestor = ClassHierarchyIngestor()
    try:
        ingestor.ingest(bad_text)
    except ValueError:
        return
    raise AssertionError("Expected ValueError for blank input")
