"""Hypothesis fuzz suite for pyproject-derived root package IDs in the Python AST ingestor."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.features.ingest_python import PythonAstStructuralIngestor

_IDENT = st.from_regex(r"^[a-z][a-z0-9]{2,8}$", fullmatch=True)


def _write_pkg(parent: Path, dirname: str, pyproject: str | None) -> Path:
    pkg = parent / dirname
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
    if pyproject is not None:
        (parent / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return pkg


@given(real=_IDENT, scratch=_IDENT, dashed=st.booleans())
@settings(max_examples=40, deadline=None)
def test_scratch_dir_name_does_not_leak_into_ids(real: str, scratch: str, dashed: bool) -> None:
    """A renamed package copy resolves to the pyproject project name, never its basename."""
    if real == scratch:
        return
    dist = f"{real}-x" if dashed else real
    pyproject = f'[project]\nname = "{dist}"\nversion = "0"\n'
    with tempfile.TemporaryDirectory() as tmp:
        pkg = _write_pkg(Path(tmp), scratch, pyproject)
        ir = PythonAstStructuralIngestor().ingest(pkg)
    expected = f"{real}_x" if dashed else real
    assert expected in ir.nodes
    assert f"{expected}.core" in ir.nodes
    assert not any(n == scratch or n.startswith(f"{scratch}.") for n in ir.nodes)


@given(name=_IDENT, with_pyproject=st.booleans())
@settings(max_examples=40, deadline=None)
def test_matching_or_missing_pyproject_keeps_basename_ids(name: str, with_pyproject: bool) -> None:
    """Packages already resolving correctly (or with no pyproject) keep basename IDs."""
    pyproject = f'[project]\nname = "{name}"\nversion = "0"\n' if with_pyproject else None
    with tempfile.TemporaryDirectory() as tmp:
        pkg = _write_pkg(Path(tmp), name, pyproject)
        ir = PythonAstStructuralIngestor().ingest(pkg)
    assert f"{name}.core" in ir.nodes


@given(scratch=_IDENT, garbage=st.text(max_size=40))
@settings(max_examples=30, deadline=None)
def test_malformed_pyproject_falls_back_to_basename(scratch: str, garbage: str) -> None:
    """Unparseable or nameless pyproject.toml never breaks ingestion; basename is used."""
    with tempfile.TemporaryDirectory() as tmp:
        pkg = _write_pkg(Path(tmp), scratch, f"[[[{garbage}\n")
        ir = PythonAstStructuralIngestor().ingest(pkg)
    assert f"{scratch}.core" in ir.nodes
