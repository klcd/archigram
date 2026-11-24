"""Hexagonal Port Protocols bridging feature slices and the Canonical IR domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from archigram.domain.diagnostics import GateId, LintReport
    from archigram.domain.ir import CanonicalIR, InteractionTraceIR, StructuralGraphIR
    from archigram.domain.spec import (
        CurationSpecV1,
        DiagramViewSpec,
        GlobalAbstractionRule,
        TraceCompactionConfig,
    )


@dataclass(frozen=True, slots=True)
class SyntaxValidationResult:
    """Outcome of validating emitted diagram syntax against a target checker."""

    valid: bool
    target_format: str
    errors: tuple[str, ...] = ()


@runtime_checkable
class StructuralIngestorPort(Protocol):
    """Port for ingesting factual topology or OOP class metadata into StructuralGraphIR."""

    @property
    def extractor_name(self) -> str: ...

    def ingest(self, source: Path | str) -> StructuralGraphIR: ...


@runtime_checkable
class TraceIngestorPort(Protocol):
    """Port for ingesting concurrent execution traces into InteractionTraceIR."""

    @property
    def extractor_name(self) -> str: ...

    def ingest(self, source: Path | str) -> InteractionTraceIR: ...


@runtime_checkable
class StructuralReducerPort(Protocol):
    """Port for deterministic structural graph reduction (prune, bypass, collapse, slice)."""

    def reduce(
        self,
        ir: StructuralGraphIR,
        view: DiagramViewSpec,
        global_abstractions: tuple[GlobalAbstractionRule, ...] = (),
    ) -> StructuralGraphIR: ...


@runtime_checkable
class TraceCompactorPort(Protocol):
    """Port for deterministic interaction trace & concurrency compaction (R1-R6 + SP-Poset)."""

    def compact(
        self,
        ir: InteractionTraceIR,
        config: TraceCompactionConfig,
    ) -> InteractionTraceIR: ...


@runtime_checkable
class LintGatePort(Protocol):
    """Port for a single semantic, topological, partial-order, budget, or syntax lint gate."""

    @property
    def gate_id(self) -> GateId: ...

    def check(
        self,
        observed_ir: CanonicalIR,
        spec: CurationSpecV1,
        curated_views: Mapping[str, CanonicalIR] | None = None,
    ) -> LintReport: ...


@runtime_checkable
class DigestGeneratorPort(Protocol):
    """Port for generating compact token-efficient digests from Canonical IR."""

    def digest(self, ir: CanonicalIR) -> object: ...

    def to_compact_markdown(
        self,
        ir: CanonicalIR,
        max_items_per_section: int = 15,
    ) -> str: ...


@runtime_checkable
class DiagramEmitterPort(Protocol):
    """Port for deterministically emitting target diagram syntax from Canonical IR."""

    @property
    def target_format(self) -> str: ...

    def emit(self, ir: CanonicalIR) -> str: ...


@runtime_checkable
class SyntaxValidatorPort(Protocol):
    """Port for validating emitted diagram source via headless syntax checkers."""

    @property
    def target_format(self) -> str: ...

    def validate(self, diagram_source: str) -> SyntaxValidationResult: ...
