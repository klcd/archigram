"""HTTP-facing layer: the entrypoint of every request."""

from __future__ import annotations

from shop import service


def place_order(order_id: str, total_cents: int) -> dict[str, object]:
    """Handle a `POST /orders` request."""
    order = service.create_order(order_id, total_cents)
    return {"id": order.order_id, "total_cents": order.total_cents}
