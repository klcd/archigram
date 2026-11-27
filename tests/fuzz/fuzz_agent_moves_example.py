"""Hypothesis fuzz suite: the agent-move examples, their docs and the website stay true.

`examples/agent-moves/` holds one spec per move on the quickstart `shop` package. The README
section, the examples README and the website animation (`#moves-data` in `docs/index.html`)
all quote what those specs produce. Every test copies a spec into a temp directory, adds
behavior-neutral YAML comments, and checks the quoted boxes, arrows, gate results and messages
against a fresh run, so none of them can drift from the tool.
"""

from __future__ import annotations

import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner

from archigram.domain.ir import StructuralGraphIR
from archigram.entrypoints.cli.app import EXIT_CLEAN, EXIT_LINT_OR_DRIFT, EXIT_USAGE_ERROR, app
from archigram.entrypoints.cli.pipeline import (
    compile_and_lint_pipeline,
    load_canonical_ir,
    load_curation_spec,
)

if TYPE_CHECKING:
    from collections.abc import Generator

_ROOT = Path(__file__).resolve().parents[2]
_MOVES = _ROOT / "examples" / "agent-moves"
_SHOP = _ROOT / "examples" / "python-quickstart" / "shop"
_SITE = _ROOT / "docs" / "index.html"
_DOCS = (_ROOT / "README.md", _MOVES / "README.md")
_RUNNER = CliRunner()
_SETTINGS = settings(max_examples=16, deadline=None, suppress_health_check=[HealthCheck.too_slow])
_NOISE = st.lists(st.from_regex(r"# [a-z ]{0,20}", fullmatch=True), min_size=0, max_size=4)
_SPECS = sorted(p.name for p in _MOVES.glob("*.yaml"))
_ALLOWED = [name for name in _SPECS if "refused" not in name]


class _SiteDiagnostic(TypedDict):
    gate: str
    code: str
    message: str
    symbols: list[str]
    witness: list[str]


class _Run(TypedDict, total=False):
    """What one spec produces: a graph and diagnostics, or a schema error."""

    view: str
    nodes: list[str]
    edges: list[list[str]]
    diagnostics: list[_SiteDiagnostic]
    schema_error: str


class _SiteStepBase(TypedDict):
    file: str
    rule: str


class _SiteStep(_SiteStepBase, _Run, total=False):
    attempt_edge: list[str]


def _site_steps() -> dict[str, _SiteStep]:
    match = re.search(
        r'<script type="application/json" id="moves-data">(.*?)</script>',
        _SITE.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match, "docs/index.html lost its #moves-data block"
    payload = cast("dict[str, list[_SiteStep]]", json.loads(match.group(1)))
    return {step["file"]: step for step in payload["steps"]}


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _noisy_copy(name: str, noise: list[str], target: Path) -> Path:
    """Copy one spec with comments around it: comments never change what a move does."""
    text = (_MOVES / name).read_text(encoding="utf-8")
    spec = target / name
    spec.write_text("\n".join([*noise, text, *noise]) + "\n", encoding="utf-8")
    return spec


def _quoted_lines(view: str) -> list[str]:
    """Output lines the READMEs quote for a view, without the `...` they cut off with."""
    quoted: list[str] = []
    for doc in _DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if line.startswith("  [ERROR] ") and f"view={view}," in line:
                quoted.append(line.removesuffix(" ...").removesuffix("..."))
    return quoted


def _expected_step(spec: Path) -> _Run:
    """What the website must show for a spec, computed from a fresh run."""
    try:
        curation = load_curation_spec(spec)
    except ValidationError as exc:
        return {"schema_error": str(exc.errors()[0]["msg"])}
    result = compile_and_lint_pipeline(load_canonical_ir(_SHOP), curation)
    ((view_id, artifact),) = result.views.items()
    emitted = artifact.emitted_ir
    assert isinstance(emitted, StructuralGraphIR)
    return {
        "view": view_id,
        "nodes": sorted(emitted.nodes),
        "edges": sorted([e.source, e.target, str(e.relation)] for e in emitted.edges),
        "diagnostics": [
            {
                "gate": str(d.gate),
                "code": d.code,
                "message": d.message,
                "symbols": list(d.offending_symbols),
                "witness": list(d.witness_path),
            }
            for d in result.lint_report.diagnostics
        ],
    }


@contextmanager
def _repo_untouched() -> Generator[None]:
    before = _tree_bytes(_MOVES)
    yield
    assert _tree_bytes(_MOVES) == before


@_SETTINGS
@given(name=st.sampled_from(_SPECS), noise=_NOISE)
def test_site_animation_replays_real_runs(name: str, noise: list[str]) -> None:
    """Every #moves-data step equals a fresh run of its spec, and every spec has a step."""
    steps = _site_steps()
    assert sorted(steps) == _SPECS
    step = steps[name]
    spec_text = (_MOVES / name).read_text(encoding="utf-8")
    for line in step["rule"].splitlines():
        assert line.strip() in spec_text, f"{name}: site shows a rule line the spec lacks: {line}"

    with tempfile.TemporaryDirectory() as tmp:
        expected = _expected_step(_noisy_copy(name, noise, Path(tmp)))

    if "schema_error" in expected:
        assert step.get("schema_error") == expected.get("schema_error")
        source, target = step.get("attempt_edge", ["", ""])
        assert f"source: {source}" in spec_text
        assert f"target: {target}" in spec_text
        return
    actual = {key: step.get(key) for key in ("view", "nodes", "edges", "diagnostics")}
    assert actual == expected, (
        f"docs/index.html #moves-data is stale for {name}; replace its entry with "
        f"{json.dumps(expected)}"
    )


@_SETTINGS
@given(name=st.sampled_from(_SPECS), noise=_NOISE)
def test_cli_verdicts_match_the_readmes(name: str, noise: list[str]) -> None:
    """Allowed moves pass, refused moves fail, and every quoted error line is real output."""
    refused = "refused" in name
    with tempfile.TemporaryDirectory() as tmp:
        spec = _noisy_copy(name, noise, Path(tmp))
        result = _RUNNER.invoke(app, ["lint", str(_SHOP), "--spec", str(spec)])

    if "draw-arrow" in name:
        assert result.exit_code == EXIT_USAGE_ERROR
        assert "Input/Usage Error" in result.output
        assert "Input tag 'add_edge' found using 'op' does not match" in result.output
        return
    assert result.exit_code == (EXIT_LINT_OR_DRIFT if refused else EXIT_CLEAN), result.output
    assert ("FAILED" if refused else "PASSED") in result.output
    view = _expected_step(_MOVES / name).get("view", "")
    quoted = _quoted_lines(view)
    assert bool(quoted) == refused, f"{name}: READMEs should quote exactly the refused moves"
    for line in quoted:
        assert any(out.startswith(line) for out in result.output.splitlines()), line


@_SETTINGS
@given(name=st.sampled_from(_ALLOWED), noise=_NOISE)
def test_allowed_move_diagrams_and_readme_are_current(name: str, noise: list[str]) -> None:
    """`apply --check --sync-md` on a copy: the committed .mmd and README blocks are current."""
    with _repo_untouched(), tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec = _noisy_copy(name, noise, root)
        (root / "diagrams").mkdir()
        for diagram in (_MOVES / "diagrams").glob("*.mmd"):
            (root / "diagrams" / diagram.name).write_bytes(diagram.read_bytes())
        readme = root / "README.md"
        readme.write_bytes((_MOVES / "README.md").read_bytes())

        result = _RUNNER.invoke(
            app,
            [
                *("apply", str(_SHOP), "--spec", str(spec)),
                *("--out-dir", str(root / "diagrams"), "--sync-md", str(readme), "--check"),
            ],
        )

        assert result.exit_code == EXIT_CLEAN, result.output
        assert "Check passed: 1 view(s) up-to-date byte-for-byte." in result.output
        assert readme.read_bytes() == (_MOVES / "README.md").read_bytes()
