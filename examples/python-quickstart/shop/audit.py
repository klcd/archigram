"""Audit stub: a leaf helper that records events and depends on nothing else."""

from __future__ import annotations


def record(event: str) -> str:
    """Return the audit line for an event."""
    return f"audit:{event}"
