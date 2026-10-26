"""Deterministic local business callback used until real operations exist.

This callback models the order-domain lifecycle only. It does not charge a
payment method, reserve inventory, arrange shipping, or call another service.
"""

from __future__ import annotations

from typing import Any, Dict

from order_service import OrderService
from validation import validate_order_payload


class InvalidStoredOrderError(ValueError):
    """Stored business data is invalid and should not be retried as transient."""


def process_order_locally(item: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and simulate accepting an order without external side effects."""
    payload = {
        "order_id": item.get("order_id"),
        "customer_id": item.get("customer_id"),
        "items": item.get("items"),
        "total_amount": item.get("total_amount"),
        "currency": item.get("currency", "USD"),
    }
    valid, errors = validate_order_payload(payload)
    if not valid:
        raise InvalidStoredOrderError("Stored order is not processable: " + "; ".join(errors))

    # Exercise the existing domain state machine on an isolated local model.
    domain = OrderService(_clock=lambda: "2000-01-01T00:00:00+00:00")
    domain.create_order(payload)
    domain.begin_processing(payload["order_id"])
    domain.complete_order(payload["order_id"])

    return {
        "order_id": payload["order_id"],
        "outcome": "SIMULATED_ACCEPTED",
        "item_count": len(payload["items"]),
        "total_amount": float(payload["total_amount"]),
        "currency": payload["currency"].upper(),
    }
