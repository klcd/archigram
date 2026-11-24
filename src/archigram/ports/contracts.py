"""Hexagonal Port Protocols bridging feature slices and the Canonical IR domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from archigram.domain.ir import CanonicalIR, InteractionTraceIR, StructuralGraphIR


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
