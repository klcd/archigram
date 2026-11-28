"""Pure Hexagonal Domain Core: Canonical IR, Curation Spec, Diagnostics & Provenance."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_non_blank(value: str) -> str:
    """Validate that an identifier or text field is non-empty after stripping whitespace."""
    if not value.strip():
        msg = "Identifier or text field must not be blank"
        raise ValueError(msg)
    return value


NonBlankStr = Annotated[str, AfterValidator(validate_non_blank)]


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
