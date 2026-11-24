"""Performance benchmark suite verifying sub-5ms Gate 5 in-process validation throughput."""

from __future__ import annotations

from typing import Any

from archigram.features.lint_syntax import (
    D2SyntaxValidator,
    DotSyntaxValidator,
    LikeC4SyntaxValidator,
    MermaidSyntaxValidator,
    PlantUmlSyntaxValidator,
)


def _build_100_line_mermaid() -> str:
    lines = ["flowchart TD", "  subgraph Core"]
    for idx in range(96):
        lines.append(f'    n{idx}["Node {idx}(v1)"] --> n{idx + 1}["Node {idx + 1}"]')
    lines.append("  end")
    lines.append("  n0 --> n96")
    return "\n".join(lines)


def _build_100_line_d2() -> str:
    lines = ["direction: right", "core: Core {"]
    for idx in range(96):
        lines.append(f'  n{idx} -> n{idx + 1}: "step {idx}"')
    lines.append("}")
    lines.append("core.n0 -> core.n96")
    return "\n".join(lines)


def _build_100_line_plantuml() -> str:
    lines = ["@startuml", "skinparam class<<svc>> {", "  BackgroundColor<<svc>> Wheat", "}"]
    for idx in range(94):
        lines.append(f"p{idx} -> p{idx + 1} : call {idx}")
    lines.append("alt ok")
    lines.append("  p0 -> p1 : done")
    lines.append("end")
    lines.append("@enduml")
    return "\n".join(lines)


def _build_100_line_likec4() -> str:
    lines = ["specification {", "  element service", "}", "model {"]
    for idx in range(47):
        lines.append(f"  s{idx} = service 'Service {idx}'")
    for idx in range(46):
        lines.append(f"  s{idx} -> s{idx + 1} 'calls'")
    lines.append("}")
    lines.append("views {")
    lines.append("  view index {")
    lines.append("    include *")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def _build_40_line_dot() -> str:
    lines = ["digraph G {"]
    for idx in range(38):
        lines.append(f"  n{idx} -> n{idx + 1};")
    lines.append("}")
    return "\n".join(lines)


_MERMAID_100 = _build_100_line_mermaid()
_D2_100 = _build_100_line_d2()
_PUML_100 = _build_100_line_plantuml()
_LIKEC4_100 = _build_100_line_likec4()
_DOT_40 = _build_40_line_dot()

_MERMAID_VALIDATOR = MermaidSyntaxValidator()
_D2_VALIDATOR = D2SyntaxValidator(use_cli=False)
_PUML_VALIDATOR = PlantUmlSyntaxValidator(use_cli=False)
_LIKEC4_VALIDATOR = LikeC4SyntaxValidator()
_DOT_VALIDATOR = DotSyntaxValidator(use_cli=False)


def _assert_sub_5ms(benchmark: Any) -> None:
    if benchmark.stats is not None:
        assert benchmark.stats.stats.median < 0.005


def test_mermaid_100_line_validation_sub_5ms(benchmark: Any) -> None:
    result = benchmark(_MERMAID_VALIDATOR.validate, _MERMAID_100)
    assert result.valid
    _assert_sub_5ms(benchmark)


def test_d2_100_line_validation_sub_5ms(benchmark: Any) -> None:
    result = benchmark(_D2_VALIDATOR.validate, _D2_100)
    assert result.valid
    _assert_sub_5ms(benchmark)


def test_plantuml_100_line_validation_sub_5ms(benchmark: Any) -> None:
    result = benchmark(_PUML_VALIDATOR.validate, _PUML_100)
    assert result.valid
    _assert_sub_5ms(benchmark)


def test_likec4_100_line_validation_sub_5ms(benchmark: Any) -> None:
    result = benchmark(_LIKEC4_VALIDATOR.validate, _LIKEC4_100)
    assert result.valid
    _assert_sub_5ms(benchmark)


def test_dot_40_line_validation_benchmark(benchmark: Any) -> None:
    result = benchmark(_DOT_VALIDATOR.validate, _DOT_40)
    assert result.valid
