"""Hypothesis property-based fuzz suite for Gate 5 headless syntax validators."""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.features.lint_syntax import (
    D2SyntaxValidator,
    DotSyntaxValidator,
    Gate5SyntaxHarness,
    LikeC4SyntaxValidator,
    MermaidSyntaxValidator,
    PlantUmlSyntaxValidator,
    build_default_validators,
)
from archigram.ports.contracts import SyntaxValidationResult, SyntaxValidatorPort

_IDENT = st.from_regex(r"[A-Za-z][A-Za-z0-9_]{1,10}", fullmatch=True).filter(
    lambda s: (
        s.lower()
        not in {
            "end",
            "subgraph",
            "par",
            "alt",
            "opt",
            "loop",
            "critical",
            "break",
            "group",
            "rect",
            "box",
            "note",
            "else",
            "and",
            "option",
            "graph",
            "digraph",
        }
    )
)
_SAFE_LABEL = st.from_regex(r"[A-Za-z0-9 _-]{1,16}", fullmatch=True)


@st.composite
def valid_mermaid_diagrams(draw: st.DrawFn) -> str:
    """Generate valid synthetic Mermaid diagrams across flowchart, class, and sequence modes."""
    use_fm = draw(st.booleans())
    fm = "---\ntitle: Fuzz Diagram\n---\n" if use_fm else ""
    variant = draw(st.sampled_from(["flowchart", "graph", "classDiagram", "sequenceDiagram"]))
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    lbl1 = draw(_SAFE_LABEL).strip() or "Alpha"
    lbl2 = draw(_SAFE_LABEL).strip() or "Beta"

    if variant in {"flowchart", "graph"}:
        direction = draw(st.sampled_from(["TD", "TB", "BT", "RL", "LR"]))
        quoted_lbl = f'"{lbl1}(v1)"'
        return (
            f"{fm}{variant} {direction}\n"
            f"  subgraph sg_{n1}\n"
            f"    {n1}[{quoted_lbl}] --> {n2}([{lbl2}])\n"
            f"  end\n"
        )
    if variant == "classDiagram":
        return (
            f"{fm}classDiagram\n"
            f"  class {n1} {{\n"
            f"    +id string\n"
            f"    +run(arg int) bool\n"
            f"  }}\n"
            f"  {n1} --> {n2} : calls\n"
        )
    return (
        f"{fm}sequenceDiagram\n"
        f"  participant {n1}\n"
        f"  actor {n2}\n"
        f"  par Async Work\n"
        f"    {n1}->>{n2}: ping#59; ok\n"
        f"  and Parallel\n"
        f"    alt Ready\n"
        f"      {n2}-->>{n1}: ack\n"
        f"    else Retry\n"
        f"      loop Retry Loop\n"
        f"        {n1}->>{n2}: retry\n"
        f"      end\n"
        f"    end\n"
        f"  end\n"
    )


@st.composite
def adversarial_mermaid_diagrams(draw: st.DrawFn) -> str:
    """Generate adversarial Mermaid syntax mutations covering known parser traps."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    lbl = draw(_SAFE_LABEL).strip() or "Node"
    end_kw = draw(st.sampled_from(["end", "End", "END"]))
    bad_block = draw(st.sampled_from(["par", "loop", "opt", "alt", "critical", "break"]))
    case = draw(
        st.sampled_from(
            [
                "",
                "unknownDiagram\n  a --> b",
                "---\ntitle: Unclosed\nflowchart TD\n  A --> B",
                f"flowchart XX\n  {n1} --> {n2}",
                f"flowchart TD\n  subgraph sg1\n    {n1} --> {n2}",
                f"flowchart TD\n  {n1}[{lbl}(with)parens] --> {n2}",
                f'flowchart TD\n  {n1}["{lbl}] --> {n2}',
                f"flowchart TD\n  {n1}[{lbl} --> {n2}",
                f"sequenceDiagram\n  participant {end_kw}\n  {n1}->>{n2}: hi",
                f"sequenceDiagram\n  actor {end_kw}\n  {n1}->>{n2}: hi",
                f"sequenceDiagram\n  {bad_block} Block\n    {n1}->>{n2}: hi",
                f"sequenceDiagram\n  {n1}->>{n2}: hello; world",
                f"sequenceDiagram\n  and\n  {n1}->>{n2}: orphan",
                f"sequenceDiagram\n  else\n  {n1}->>{n2}: orphan",
                f"sequenceDiagram\n  alt Guard\n    {n1}->>{n2}: a\n  and\n"
                f"    {n1}->>{n2}: b\n  end",
            ]
        )
    )
    return case


@st.composite
def valid_d2_diagrams(draw: st.DrawFn) -> str:
    """Generate valid synthetic D2 diagrams with blocks, strings, and arrows."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    lbl = draw(_SAFE_LABEL).strip() or "Service"
    arrow = draw(st.sampled_from(["->", "<-", "<->", "--"]))
    return (
        f"# D2 comment\n"
        f"direction: right\n"
        f"{n1}_group: '{lbl} Group' {{\n"
        f'  {n1}: "{lbl}"\n'
        f"  {n2}: {lbl} B\n"
        f'  {n1} {arrow} {n2}: "calls"\n'
        f"}}\n"
    )


@st.composite
def adversarial_d2_diagrams(draw: st.DrawFn) -> str:
    """Generate adversarial D2 syntax mutations."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    bad_arrow = draw(st.sampled_from(["-->", "->>", "==>"]))
    return draw(
        st.sampled_from(
            [
                "",
                f"{n1} -> {n2} {{",
                f"{n1} -> {n2} }}",
                f'{n1}: "unclosed',
                f"{n1}: 'unclosed",
                f"{n1} {bad_arrow} {n2}",
                f"{n1} ->",
                f"-> {n2}",
                f"{n1} -> -> {n2}",
            ]
        )
    )


@st.composite
def valid_plantuml_diagrams(draw: st.DrawFn) -> str:
    """Generate valid synthetic PlantUML diagrams."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    stereo = draw(_IDENT)
    frag = draw(st.sampled_from(["par", "alt", "opt", "loop", "group"]))
    else_part = "else Fallback\n  Bob -> Alice : err\n" if frag in {"alt", "par"} else ""
    return (
        f"@startuml\n"
        f"' PlantUML comment\n"
        f"skinparam class<<{stereo}>> {{\n"
        f"  BackgroundColor<<{stereo}>> Wheat\n"
        f"}}\n"
        f"package Core {{\n"
        f"  class {n1} <<{stereo}>> {{\n"
        f"    +call()\n"
        f"  }}\n"
        f"}}\n"
        f"{frag} Branch\n"
        f"  {n1} -> {n2} : ok\n"
        f"{else_part}"
        f"end\n"
        f"@enduml\n"
    )


@st.composite
def adversarial_plantuml_diagrams(draw: st.DrawFn) -> str:
    """Generate adversarial PlantUML syntax mutations."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    frag = draw(st.sampled_from(["par", "alt", "opt", "loop", "group"]))
    return draw(
        st.sampled_from(
            [
                "",
                f"{n1} -> {n2} : missing framing",
                f"@startuml\n{n1} -> {n2} : missing footer",
                f"@startuml\npackage P {{\n  class {n1}\n@enduml",
                f"@startuml\n{frag} Guard\n  {n1} -> {n2} : open\n@enduml",
                f"@startuml\n{n1} -> {n2} : stray\nend\n@enduml",
                f"@startuml\nelse\n  {n1} -> {n2} : bad\n@enduml",
                f"@startuml\nskinparam class <<{n1}>> {{\n  BackgroundColor Wheat\n}}\n@enduml",
                f"@startuml\nskinparam class<<{n1}>> {{\n"
                f"  BackgroundColor <<{n1}>> Wheat\n}}\n@enduml",
                f"@startuml\nskinparam class<<{n1} {n2}>> {{\n}}\n@enduml",
            ]
        )
    )


@st.composite
def valid_likec4_diagrams(draw: st.DrawFn) -> str:
    """Generate valid synthetic LikeC4 DSL diagrams."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    lbl = draw(_SAFE_LABEL).strip() or "Component"
    arrow = draw(st.sampled_from(["->", "-[async]->", "<-"]))
    return (
        f"// LikeC4 spec\n"
        f"specification {{\n"
        f"  element service\n"
        f"  relationship async\n"
        f"}}\n"
        f"model {{\n"
        f"  {n1} = service '{lbl} A'\n"
        f'  {n2} = service "{lbl} B"\n'
        f"  {n1} {arrow} {n2} 'calls'\n"
        f"}}\n"
        f"views {{\n"
        f"  view index {{\n"
        f"    include *\n"
        f"  }}\n"
        f"}}\n"
    )


@st.composite
def adversarial_likec4_diagrams(draw: st.DrawFn) -> str:
    """Generate adversarial LikeC4 syntax mutations."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    n3 = draw(_IDENT.filter(lambda x: x not in {n1, n2}))
    return draw(
        st.sampled_from(
            [
                "",
                f"{n1} = service 'unframed'",
                f"model {{\n  {n1} = service 'A'\n",
                f"model {{\n  {n1} = service 'unclosed\n}}",
                f"model {{\n  {n1} <- {n2} <- {n3}\n}}",
                f"model {{\n  {n1} -> {n2} -> {n3}\n}}",
                f"model {{\n  {n1} ->\n}}",
            ]
        )
    )


@st.composite
def valid_dot_diagrams(draw: st.DrawFn) -> str:
    """Generate valid synthetic Graphviz DOT diagrams."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    lbl = draw(_SAFE_LABEL).strip() or "Node"
    directed = draw(st.booleans())
    header = "digraph G" if directed else "graph G"
    edge_op = "->" if directed else "--"
    return (
        f"{header} {{\n"
        f'  {n1} [label="{lbl}"];\n'
        f"  subgraph cluster_0 {{\n"
        f"    {n1} {edge_op} {n2};\n"
        f"  }}\n"
        f"}}\n"
    )


@st.composite
def adversarial_dot_diagrams(draw: st.DrawFn) -> str:
    """Generate adversarial DOT syntax mutations."""
    n1 = draw(_IDENT)
    n2 = draw(_IDENT.filter(lambda x: x != n1))
    return draw(
        st.sampled_from(
            [
                "",
                f"{n1} -> {n2};",
                f"digraph G {{ {n1} -> ; }}",
                f"digraph G {{ {n1} -> {n2};",
                f"digraph G {{ {n1} -> {n2}; }} }}",
                f'digraph G {{ {n1} [label="unclosed]; }}',
                f"digraph G {{ {n1} -- {n2}; }}",
                f"graph G {{ {n1} -> {n2}; }}",
            ]
        )
    )


@settings(max_examples=80, deadline=None)
@given(
    mermaid_src=valid_mermaid_diagrams(),
    d2_src=valid_d2_diagrams(),
    puml_src=valid_plantuml_diagrams(),
    likec4_src=valid_likec4_diagrams(),
    dot_src=valid_dot_diagrams(),
)
def test_valid_synthetic_diagrams_pass_100_percent(
    mermaid_src: str,
    d2_src: str,
    puml_src: str,
    likec4_src: str,
    dot_src: str,
) -> None:
    harness = Gate5SyntaxHarness(build_default_validators(use_cli=False))
    samples = {
        "mermaid": mermaid_src,
        "d2": d2_src,
        "plantuml": puml_src,
        "likec4": likec4_src,
        "dot": dot_src,
    }
    for fmt, src in samples.items():
        validator = harness.get_validator(fmt)
        assert isinstance(validator, SyntaxValidatorPort)
        res = harness.validate(fmt, src)
        assert res.valid, f"Expected valid {fmt}: {res.errors}\n{src}"
        assert res.target_format == fmt
        assert res.errors == ()


@settings(max_examples=100, deadline=None)
@given(
    mermaid_bad=adversarial_mermaid_diagrams(),
    d2_bad=adversarial_d2_diagrams(),
    puml_bad=adversarial_plantuml_diagrams(),
    likec4_bad=adversarial_likec4_diagrams(),
    dot_bad=adversarial_dot_diagrams(),
)
def test_adversarial_mutations_rejected_with_diagnostics(
    mermaid_bad: str,
    d2_bad: str,
    puml_bad: str,
    likec4_bad: str,
    dot_bad: str,
) -> None:
    harness = Gate5SyntaxHarness(build_default_validators(use_cli=False))
    samples = {
        "mermaid": mermaid_bad,
        "d2": d2_bad,
        "plantuml": puml_bad,
        "likec4": likec4_bad,
        "dot": dot_bad,
    }
    for fmt, src in samples.items():
        res = harness.validate(fmt, src)
        assert not res.valid, f"Expected invalid {fmt} to fail:\n{src}"
        assert res.target_format == fmt
        assert len(res.errors) > 0
        assert all(bool(err.strip()) for err in res.errors)


@settings(max_examples=80, deadline=None)
@given(
    fmt=st.sampled_from(
        ["mermaid", "d2", "plantuml", "likec4", "dot", "mmd", "puml", "c4", "other"]
    ),
    raw_text=st.text(max_size=240),
)
def test_harness_never_crashes_on_arbitrary_input(fmt: str, raw_text: str) -> None:
    harness = Gate5SyntaxHarness(build_default_validators(use_cli=False))
    result = harness.validate(fmt, raw_text)
    assert isinstance(result, SyntaxValidationResult)
    if not result.valid:
        assert len(result.errors) > 0
    if fmt == "other":
        assert not result.valid


@settings(max_examples=20, deadline=None)
@given(
    valid_src=valid_d2_diagrams(),
    err_msg=st.from_regex(r"[a-z]{3,12}: syntax error", fullmatch=True),
)
def test_cli_error_normalization_for_d2_and_plantuml(valid_src: str, err_msg: str) -> None:
    puml_src = "@startuml\nAlice -> Bob : hi\n@enduml\n"
    d2_validator = D2SyntaxValidator(use_cli=True, cli_timeout=5.0)
    puml_validator = PlantUmlSyntaxValidator(use_cli=True, cli_timeout=5.0)
    failed_proc = subprocess.CompletedProcess(args=["cli"], returncode=1, stdout="", stderr=err_msg)
    with (
        patch(
            "archigram.features.lint_syntax.validators.shutil.which", return_value="/usr/bin/mock"
        ),
        patch("archigram.features.lint_syntax.validators.subprocess.run", return_value=failed_proc),
    ):
        d2_res = d2_validator.validate(valid_src)
        puml_res = puml_validator.validate(puml_src)
    assert not d2_res.valid
    assert err_msg in d2_res.errors[0]
    assert not puml_res.valid
    assert err_msg in puml_res.errors[0]


@settings(max_examples=8, deadline=None)
@given(valid_src=valid_dot_diagrams(), bad_src=adversarial_dot_diagrams())
def test_dot_validator_with_optional_cli_when_installed(valid_src: str, bad_src: str) -> None:
    validator = DotSyntaxValidator(use_cli=shutil.which("dot") is not None)
    assert isinstance(MermaidSyntaxValidator(), SyntaxValidatorPort)
    assert isinstance(LikeC4SyntaxValidator(), SyntaxValidatorPort)
    assert validator.validate(valid_src).valid
    bad_res = validator.validate(bad_src)
    assert not bad_res.valid
    assert len(bad_res.errors) > 0
