"""Immutable structured diagnostic models for the 5-Gate Linter & 3-Turn MCP Repair Loop."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Sequence


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        msg = "Diagnostic identifier or text field must not be blank"
        raise ValueError(msg)
    return value


NonBlankStr = Annotated[str, AfterValidator(_validate_non_blank)]


class GateId(StrEnum):
    """Canonical identifier for each gate in the 5-Gate ArchiGram Linter."""

    GATE_1_GROUNDING = "gate_1_grounding"
    GATE_2_BOUNDARY_LEAF = "gate_2_boundary_leaf"
    GATE_3_TOPOLOGY_POSET = "gate_3_topology_poset"
    GATE_4_BUDGET_ORPHAN = "gate_4_budget_orphan"
    GATE_5_SYNTAX = "gate_5_syntax"


class DiagnosticSeverity(StrEnum):
    """Severity level determining whether a diagnostic blocks CI / triggers MCP repair."""

    ERROR = "error"
    WARNING = "warning"


class LintDiagnostic(BaseModel):
    """Actionable structured finding emitted by one of the 5 ArchiGram lint gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateId
    severity: DiagnosticSeverity
    code: NonBlankStr
    message: NonBlankStr
    rule_id: NonBlankStr | None = None
    view_id: NonBlankStr | None = None
    offending_symbols: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    witness_path: tuple[NonBlankStr, ...] = Field(default_factory=tuple)
    repair_hint: NonBlankStr


class LintReport(BaseModel):
    """Immutable aggregated lint outcome whose passed flag strictly tracks ERROR presence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    diagnostics: tuple[LintDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_passed_invariant(self) -> LintReport:
        has_errors = any(diag.severity is DiagnosticSeverity.ERROR for diag in self.diagnostics)
        if self.passed and has_errors:
            msg = "LintReport cannot have passed=True when ERROR diagnostics are present"
            raise ValueError(msg)
        if not self.passed and not has_errors:
            msg = "LintReport with passed=False must contain at least one ERROR diagnostic"
            raise ValueError(msg)
        return self

    @classmethod
    def from_diagnostics(cls, diagnostics: Sequence[LintDiagnostic]) -> LintReport:
        """Construct a LintReport automatically deriving passed from ERROR diagnostic presence."""
        items = tuple(diagnostics)
        has_errors = any(diag.severity is DiagnosticSeverity.ERROR for diag in items)
        return cls(passed=not has_errors, diagnostics=items)

    def merge(self, other: LintReport) -> LintReport:
        """Combine two LintReports deterministically while preserving the passed invariant."""
        return LintReport.from_diagnostics((*self.diagnostics, *other.diagnostics))

    @property
    def error_count(self) -> int:
        """Return the number of ERROR-severity diagnostics in this report."""
        return sum(1 for diag in self.diagnostics if diag.severity is DiagnosticSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        """Return the number of WARNING-severity diagnostics in this report."""
        return sum(1 for diag in self.diagnostics if diag.severity is DiagnosticSeverity.WARNING)

    def by_gate(self, gate: GateId) -> tuple[LintDiagnostic, ...]:
        """Return all diagnostics emitted by the specified lint gate."""
        return tuple(diag for diag in self.diagnostics if diag.gate is gate)
