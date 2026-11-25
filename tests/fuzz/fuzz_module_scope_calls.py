"""Hypothesis fuzz suite for python_ast_v1 calls recorded at module scope."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from archigram.domain.ir import EdgeRelation
from archigram.features.ingest_python import PythonAstStructuralIngestor

_CLS = st.from_regex(r"^[A-Z][a-z]{2,8}$", fullmatch=True)
_FN = st.from_regex(r"^[a-z]{3,8}_fn$", fullmatch=True)


@settings(max_examples=30, deadline=None)
@given(cls_name=_CLS, fn_name=_FN, wrap=st.sampled_from(["plain", "if", "try", "with"]))
def test_fuzz_module_scope_calls_recorded(cls_name: str, fn_name: str, wrap: str) -> None:
    """Module-scope calls yield module->symbol CALLS; def-body calls stay function-owned."""
    header = {
        "plain": "",
        "if": "if True:\n",
        "try": "try:\n",
        "with": "import contextlib\nwith contextlib.suppress(Exception):\n",
    }[wrap]
    pad = "" if wrap == "plain" else "    "
    tail = "except Exception:\n    pass\n" if wrap == "try" else ""
    user = (
        f"from .lib import {cls_name}, {fn_name}\n"
        f"{header}{pad}DEFAULT = {cls_name}()\n"
        f"{pad}VALUE = {fn_name}()\n"
        f"{tail}"
        "def inner():\n"
        f"    return {cls_name}()\n"
    )
    lib = f"class {cls_name}:\n    pass\n\ndef {fn_name}() -> int:\n    return 1\n"
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkgx"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "lib.py").write_text(lib, encoding="utf-8")
        (pkg / "user.py").write_text(user, encoding="utf-8")
        ir = PythonAstStructuralIngestor().ingest(pkg)
    calls = {(e.source, e.target) for e in ir.edges if e.relation is EdgeRelation.CALLS}
    mod = next(n for n in ir.nodes if n.endswith(".user") or n == "user")
    lib_mod = mod[: -len("user")] + "lib"
    assert (mod, f"{lib_mod}.{cls_name}") in calls
    assert (mod, f"{lib_mod}.{fn_name}") in calls
    assert (f"{mod}.inner", f"{lib_mod}.{cls_name}") in calls
