"""Persistence layer: the only module that stores orders."""

from __future__ import annotations

from shop.models import Order

_STORE: dict[str, Order] = {}


def save_order(order: Order) -> Order:
    """Persist an order and return it."""
    _STORE[order.order_id] = order
    return order
