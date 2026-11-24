"""Performance benchmark suite for 250-class OOP hierarchy ingestion."""

from __future__ import annotations

import json
from typing import Any

from archigram.features.ingest_class import ClassHierarchyIngestor


def _build_250_class_puml(package_count: int = 25, classes_per_package: int = 10) -> str:
    """Build a 250-class pyreverse PlantUML hierarchy with compartments and UML edges."""
    lines: list[str] = ["@startuml classes_250"]
    class_refs: list[str] = []

    for p_idx in range(package_count):
        pkg_name = f"svc_{p_idx}"
        lines.append(f"package {pkg_name} {{")
        for c_idx in range(classes_per_package):
            global_idx = p_idx * classes_per_package + c_idx
            cls_name = (
                f"Repository[Entity_{global_idx}]" if c_idx % 5 == 0 else f"Component_{global_idx}"
            )
            kind_kw = "interface" if c_idx == 0 else ("enum" if c_idx == 1 else "class")
            lines.append(f"  {kind_kw} {cls_name} {{")
            lines.append("    -id: str")
            lines.append(f"    #state_{c_idx}: dict[str, int]")
            lines.append("    +execute(payload: bytes, retry: int): bool")
            lines.append("    ~{static} version(): int")
            lines.append("  }")
            class_refs.append(f"{pkg_name}.{cls_name}")
        lines.append("}")

    total = len(class_refs)
    for idx in range(1, total):
        prev_ref = class_refs[idx - 1]
        curr_ref = class_refs[idx]
        if idx % 4 == 0:
            lines.append(f"{prev_ref} <|-- {curr_ref}")
        elif idx % 4 == 1:
            lines.append(f"{prev_ref} <|.. {curr_ref}")
        elif idx % 4 == 2:
            lines.append(f"{curr_ref} *-- {prev_ref}")
        else:
            lines.append(f"{curr_ref} o-- {prev_ref}")

    lines.append("@enduml")
    return "\n".join(lines)


def _build_250_class_json(package_count: int = 25, classes_per_package: int = 10) -> str:
    """Build a 250-class structured OOP JSON descriptor with compartments and relations."""
    packages: list[dict[str, Any]] = []
    for p_idx in range(package_count):
        pkg_name = f"svc_{p_idx}"
        classes: list[dict[str, Any]] = []
        for c_idx in range(classes_per_package):
            global_idx = p_idx * classes_per_package + c_idx
            is_generic = c_idx % 5 == 0
            cls_entry: dict[str, Any] = {
                "name": "Repository" if is_generic else f"Component_{global_idx}",
                "kind": "interface" if c_idx == 0 else ("enum" if c_idx == 1 else "class"),
                "generic_params": [f"Entity_{global_idx}"] if is_generic else [],
                "fields": [
                    {"name": "id", "visibility": "-", "type": "str"},
                    {"name": f"state_{c_idx}", "visibility": "#", "type": "dict[str, int]"},
                ],
                "methods": [
                    {
                        "name": "execute",
                        "visibility": "+",
                        "params": [
                            {"name": "payload", "type": "bytes"},
                            {"name": "retry", "type": "int"},
                        ],
                        "return_type": "bool",
                    },
                    {
                        "name": "version",
                        "visibility": "~",
                        "params": [],
                        "return_type": "int",
                        "modifiers": ["static"],
                    },
                ],
            }
            if c_idx > 0:
                prev_idx = global_idx - 1
                prev_is_gen = (c_idx - 1) % 5 == 0
                prev_ref = (
                    f"{pkg_name}.Repository[Entity_{prev_idx}]"
                    if prev_is_gen
                    else f"{pkg_name}.Component_{prev_idx}"
                )
                cls_entry["inherits" if c_idx % 2 == 0 else "composes"] = [prev_ref]
            classes.append(cls_entry)
        packages.append({"name": pkg_name, "classes": classes})
    return json.dumps({"graph_id": "bench_250_json", "packages": packages})


_PUML_250_FIXTURE = _build_250_class_puml(25, 10)
_JSON_250_FIXTURE = _build_250_class_json(25, 10)
_INGESTOR = ClassHierarchyIngestor()


def test_ingest_250_class_puml_hierarchy_benchmark(benchmark: Any) -> None:
    ir = benchmark(_INGESTOR.ingest, _PUML_250_FIXTURE)
    # 25 packages + 250 classes = 275 nodes
    assert len(ir.nodes) == 275
    assert len(ir.edges) == 249


def test_ingest_250_class_json_hierarchy_benchmark(benchmark: Any) -> None:
    ir = benchmark(_INGESTOR.ingest, _JSON_250_FIXTURE)
    # 25 packages + 250 classes = 275 nodes
    assert len(ir.nodes) == 275
    assert len(ir.edges) == 25 * 9
