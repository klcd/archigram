"""Hexagonal Port Protocols isolating feature slices from entrypoints and I/O."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SliceDescriptor(Protocol):
    """Minimal capability descriptor implemented by independent feature slices."""

    @property
    def slice_name(self) -> str: ...
