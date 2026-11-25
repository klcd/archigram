"""Business layer: validates orders, persists them, and audits the result."""

from __future__ import annotations

from shop import audit, repository
from shop.models import Order


def create_order(order_id: str, total_cents: int) -> Order:
    """Create and store an order."""
    order = Order(order_id=order_id, total_cents=total_cents)
    saved = repository.save_order(order)
    audit.record(f"created:{saved.order_id}")
    return saved
