"""Validation logic for Event-Driven Order Processing System.

Provides pure functions to validate incoming order payloads according to the
schema and business rules established in docs/ARCHITECTURE.md and docs/DECISIONS.md.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Order ID must be alphanumeric with underscores/hyphens, length 8 to 64.
ORDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def validate_order_payload(data: Any) -> Tuple[bool, List[str]]:
    """Validate an incoming order data dictionary.

    Args:
        data: The parsed JSON payload to validate.

    Returns:
        A tuple of (is_valid, errors_list).
        If is_valid is True, errors_list is empty.
        If is_valid is False, errors_list contains descriptive failure reasons.
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return False, ["Payload must be a JSON object (key-value dictionary)."]

    # 1. Validate order_id
    if "order_id" not in data:
        errors.append("Missing required field: 'order_id'.")
    else:
        order_id = data["order_id"]
        if not isinstance(order_id, str):
            errors.append("Field 'order_id' must be a string.")
        elif not ORDER_ID_PATTERN.match(order_id):
            errors.append(
                "Field 'order_id' must be an alphanumeric string (hyphens and underscores allowed) "
                "between 8 and 64 characters in length."
            )

    # 2. Validate customer_id
    if "customer_id" not in data:
        errors.append("Missing required field: 'customer_id'.")
    else:
        customer_id = data["customer_id"]
        if not isinstance(customer_id, str) or not customer_id.strip():
            errors.append("Field 'customer_id' must be a non-empty string.")
        elif len(customer_id) > 64:
            errors.append("Field 'customer_id' must not exceed 64 characters.")

    # 3. Validate items array
    calculated_sum = 0.0
    items_valid = True

    if "items" not in data:
        errors.append("Missing required field: 'items'.")
        items_valid = False
    else:
        items = data["items"]
        if not isinstance(items, list):
            errors.append("Field 'items' must be a non-empty list of objects.")
            items_valid = False
        elif len(items) == 0:
            errors.append("Field 'items' cannot be empty; at least one item is required.")
            items_valid = False
        else:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"Item at index {index} must be an object.")
                    items_valid = False
                    continue

                # item_id validation
                item_id = item.get("item_id")
                if item_id is None:
                    errors.append(f"Item at index {index} is missing 'item_id'.")
                elif not isinstance(item_id, str) or not item_id.strip():
                    errors.append(f"Item at index {index} 'item_id' must be a non-empty string.")

                # quantity validation: must be integer and strictly positive
                qty = item.get("quantity")
                qty_valid = False
                if qty is None:
                    errors.append(f"Item at index {index} is missing 'quantity'.")
                elif isinstance(qty, bool) or not isinstance(qty, int):
                    errors.append(f"Item at index {index} 'quantity' must be an integer.")
                elif qty <= 0:
                    errors.append(f"Item at index {index} 'quantity' must be greater than 0.")
                else:
                    qty_valid = True

                # price validation: must be numeric and strictly positive
                price = item.get("price")
                price_valid = False
                if price is None:
                    errors.append(f"Item at index {index} is missing 'price'.")
                elif isinstance(price, bool) or not isinstance(price, (int, float)):
                    errors.append(f"Item at index {index} 'price' must be a numeric value.")
                elif price <= 0:
                    errors.append(f"Item at index {index} 'price' must be greater than 0.00.")
                else:
                    price_valid = True

                if qty_valid and price_valid:
                    calculated_sum += qty * float(price)

    # 4. Validate total_amount (supporting total_amount or totalAmount alias)
    total_amount = data.get("total_amount")
    if total_amount is None:
        total_amount = data.get("totalAmount")

    if total_amount is None:
        errors.append("Missing required field: 'total_amount'.")
    elif isinstance(total_amount, bool) or not isinstance(total_amount, (int, float)):
        errors.append("Field 'total_amount' must be a numeric value.")
    elif total_amount <= 0:
        errors.append("Field 'total_amount' must be greater than 0.00.")
    elif items_valid:
        # Check calculation consistency within 0.01 tolerance for floating point math
        if abs(float(total_amount) - calculated_sum) >= 0.01:
            errors.append(
                f"Field 'total_amount' ({total_amount}) does not match the sum of item prices ({calculated_sum:.2f})."
            )

    # 5. Validate currency (optional, defaults to USD)
    if "currency" in data and data["currency"] is not None:
        currency = data["currency"]
        if not isinstance(currency, str) or not CURRENCY_PATTERN.match(currency.upper()):
            errors.append("Field 'currency' must be a 3-letter ISO code (e.g. 'USD', 'INR').")

    is_valid = len(errors) == 0
    return is_valid, errors
