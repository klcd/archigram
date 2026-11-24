"""Pure Hexagonal Domain Core: Canonical IR, Curation Spec, Diagnostics & Provenance."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProvenanceGrade(StrEnum):
    """Epistemic evidence grade mandatory on every Canonical IR node and edge."""

    OBSERVED = "observed"
    DECLARED = "declared"
    DERIVED_DETERMINISTIC = "derived_deterministic"
    INFERRED = "inferred"


class ProvenanceStamp(BaseModel):
    """Immutable provenance record anchoring curated artifacts to factual sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grade: ProvenanceGrade
    extractor: str | None = None
    input_sha256: str | None = None
    file_path: str | None = None
    line_span: tuple[int, int] | None = None
    rule_id: str | None = None
    backing_ids: tuple[str, ...] = Field(default_factory=tuple)
