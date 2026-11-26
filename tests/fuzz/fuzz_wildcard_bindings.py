"""Hypothesis fuzz suite: wildcard imports bind re-exported names in python_ast_v1."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain.ir import EdgeRelation
from archigram.features.ingest_python import PythonAstStructuralIngestor

_LOWER = st.from_regex(r"^[a-z][a-z0-9]{2,8}$", fullmatch=True)
_UPPER = st.from_regex(r"^[A-Z][a-zA-Z0-9]{2,8}$", fullmatch=True)


def _ingest_calls(files: dict[str, str]) -> set[tuple[str, str]]:
    """Write files under a temp package root and return (source, target) CALLS pairs."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pkg"
        for rel, text in files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        ir = PythonAstStructuralIngestor().ingest(root)
    return {(e.source, e.target) for e in ir.edges if e.relation is EdgeRelation.CALLS}


@settings(max_examples=25, deadline=None)
@given(
    shown=_LOWER,
    hidden=_LOWER,
    user_fn=_LOWER,
    use_all=st.booleans(),
    chained=st.booleans(),
)
def test_fuzz_wildcard_import_binds_public_names_through_facade(
    shown: str,
    hidden: str,
    user_fn: str,
    use_all: bool,
    chained: bool,
) -> None:
    """`from .impl import *` in __init__ lets consumers resolve names (respecting __all__)."""
    if len({shown, hidden, user_fn}) != 3:
        return
    impl = (
        (f"__all__ = ['{shown}']\n" if use_all else "")
        + f"def {shown}() -> int:\n    return 1\n\n"
        + f"def {hidden}() -> int:\n    return 2\n\n"
        + "def _private() -> int:\n    return 3\n"
    )
    init = "from .impl import *\n"
    files = {"impl.py": impl, "__init__.py": init}
    if chained:
        files = {"core/impl.py": impl, "core/__init__.py": "from .impl import *\n"}
        files["__init__.py"] = "from .core import *\n"
    mod = "pkg.core.impl" if chained else "pkg.impl"
    files["user.py"] = (
        f"from pkg import {shown}, {hidden}, _private\n\n"
        f"def {user_fn}() -> int:\n"
        f"    return {shown}() + {hidden}() + _private()\n"
    )
    calls = _ingest_calls(files)
    caller = f"pkg.user.{user_fn}"
    assert (caller, f"{mod}.{shown}") in calls
    assert ((caller, f"{mod}.{hidden}") in calls) is not use_all
    assert (caller, f"{mod}._private") not in calls


@settings(max_examples=15, deadline=None)
@given(name=_UPPER, ext=_LOWER)
def test_fuzz_unresolvable_wildcard_invents_no_bindings(name: str, ext: str) -> None:
    """A wildcard from a module outside the scanned tree neither crashes nor binds names."""
    files = {
        "__init__.py": f"from {ext}_external_mod import *\n",
        "user.py": f"from pkg import {name}\n\ndef go() -> None:\n    {name}()\n",
    }
    assert not _ingest_calls(files)
