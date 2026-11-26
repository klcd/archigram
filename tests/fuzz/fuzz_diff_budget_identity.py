"""Hypothesis fuzz suite: budget diagnostics match by (gate, code, view_id) across base and head."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from archigram.domain.diagnostics import DiagnosticSeverity, GateId, LintDiagnostic
from archigram.entrypoints.cli.diff_report import diff_diagnostics

_BUDGET_CODES = ("EDGE_BUDGET_EXCEEDED", "NODE_BUDGET_EXCEEDED", "PAR_BRANCH_BUDGET_EXCEEDED")
_symbols = st.lists(st.from_regex(r"[a-z]{1,6}", fullmatch=True), max_size=4, unique=True)


def _diag(code: str, view_id: str, message: str, symbols: list[str]) -> LintDiagnostic:
    return LintDiagnostic(
        gate=GateId.GATE_4_BUDGET_ORPHAN,
        severity=DiagnosticSeverity.ERROR,
        code=code,
        message=message,
        view_id=view_id,
        offending_symbols=tuple(symbols),
        repair_hint="reduce",
    )


@given(
    code=st.sampled_from(_BUDGET_CODES),
    view_id=st.from_regex(r"v[a-z]{1,4}", fullmatch=True),
    base_count=st.integers(1, 500),
    head_count=st.integers(1, 500),
    base_syms=_symbols,
    head_syms=_symbols,
)
def test_persisting_budget_error_is_never_new_or_resolved(
    code: str,
    view_id: str,
    base_count: int,
    head_count: int,
    base_syms: list[str],
    head_syms: list[str],
) -> None:
    base = (_diag(code, view_id, f"edge count ({base_count}) exceeds", base_syms),)
    head = (_diag(code, view_id, f"edge count ({head_count}) exceeds", head_syms),)
    added, resolved = diff_diagnostics(base, head, DiagnosticSeverity.ERROR)
    assert added == ()
    assert resolved == ()


@given(
    code=st.sampled_from(_BUDGET_CODES),
    view_a=st.from_regex(r"a[a-z]{1,4}", fullmatch=True),
    view_b=st.from_regex(r"b[a-z]{1,4}", fullmatch=True),
)
def test_budget_error_in_other_view_is_still_new(code: str, view_a: str, view_b: str) -> None:
    base = (_diag(code, view_a, "over", []),)
    head = (_diag(code, view_b, "over", []),)
    added, resolved = diff_diagnostics(base, head, DiagnosticSeverity.ERROR)
    assert added == head
    assert resolved == base


@given(
    base_syms=_symbols,
    head_syms=_symbols,
)
def test_non_budget_diagnostics_still_match_by_symbols(
    base_syms: list[str], head_syms: list[str]
) -> None:
    base = (_diag("ACCIDENTAL_ORPHAN_NODE", "v", "m", base_syms),)
    head = (_diag("ACCIDENTAL_ORPHAN_NODE", "v", "m", head_syms),)
    added, _ = diff_diagnostics(base, head, DiagnosticSeverity.ERROR)
    assert bool(added) == (tuple(base_syms) != tuple(head_syms))
