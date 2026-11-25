"""Domain value objects shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    """A customer order."""

    order_id: str
    total_cents: int
