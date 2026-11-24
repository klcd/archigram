"""Native Python AST and runtime call-trace ingestion slice."""

from __future__ import annotations

from archigram.features.ingest_python.ast_ingestor import PythonAstStructuralIngestor
from archigram.features.ingest_python.trace_ingestor import (
    PythonTraceIngestor,
    capture_python_trace,
)

__all__ = [
    "PythonAstStructuralIngestor",
    "PythonTraceIngestor",
    "capture_python_trace",
]
