"""Validation logic for Event-Driven Order Processing System.

Provides pure functions to validate incoming order payloads according to the
schema and business rules established in docs/ARCHITECTURE.md and docs/DECISIONS.md.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

# Order ID must be alphanumeric with underscores/hyphens, length 8 to 64.
ORDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
DYNAMODB_MAX_SIGNIFICANT_DIGITS = 38
DYNAMODB_MIN_ADJUSTED_EXPONENT = -130
DYNAMODB_MAX_ADJUSTED_EXPONENT = 125


def is_dynamodb_compatible_number(value: Decimal) -> bool:
    """Return whether a finite Decimal fits DynamoDB's number limits.

    DynamoDB numbers support up to 38 significant digits and nonzero values
    from 1E-130 through 9.999...E+125 (with the same magnitude limits for
    negative values). Trailing zeros do not add significant digits.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    if value.is_zero():
        return True

    digits = value.as_tuple().digits
    significant_digits = len(digits)
    while significant_digits > 1 and digits[significant_digits - 1] == 0:
        significant_digits -= 1
    return (
        significant_digits <= DYNAMODB_MAX_SIGNIFICANT_DIGITS
        and DYNAMODB_MIN_ADJUSTED_EXPONENT <= value.adjusted()
        <= DYNAMODB_MAX_ADJUSTED_EXPONENT
    )


def _finite_decimal(value: Any) -> Optional[Decimal]:
    """Return supported finite numeric inputs in decimal form.

    Decimal values are used by DynamoDB reads. Converting floats through
    ``str`` preserves their user-visible decimal representation and avoids
    importing the binary approximation. Booleans and numeric strings are not
    numeric inputs under the order API contract.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        return None
    return number if number.is_finite() else None


def _format_fraction_2(value: Fraction) -> str:
    """Format the diagnostic item sum without converting it to binary float."""
    with localcontext() as context:
        context.prec = max(28, len(str(abs(value.numerator))) + 5)
        amount = Decimal(value.numerator) / Decimal(value.denominator)
        return f"{amount:.2f}"


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
    calculated_sum = Fraction(0)
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
                quantity = None
                if qty is None:
                    errors.append(f"Item at index {index} is missing 'quantity'.")
                elif isinstance(qty, bool):
                    errors.append(f"Item at index {index} 'quantity' must be an integer.")
                elif isinstance(qty, int):
                    if qty <= 0:
                        errors.append(f"Item at index {index} 'quantity' must be greater than 0.")
                    elif not is_dynamodb_compatible_number(Decimal(qty)):
                        errors.append(
                            f"Item at index {index} 'quantity' exceeds DynamoDB numeric limits."
                        )
                    else:
                        qty_valid = True
                        quantity = qty
                elif isinstance(qty, Decimal) and qty.is_finite() and qty == qty.to_integral_value():
                    if qty <= 0:
                        errors.append(f"Item at index {index} 'quantity' must be greater than 0.")
                    elif not is_dynamodb_compatible_number(qty):
                        errors.append(
                            f"Item at index {index} 'quantity' exceeds DynamoDB numeric limits."
                        )
                    else:
                        qty_valid = True
                        # DynamoDB resource reads numbers as Decimal, including
                        # values originally written as integer quantities.
                        quantity = int(qty)
                else:
                    errors.append(f"Item at index {index} 'quantity' must be an integer.")

                # price validation: must be numeric and strictly positive
                price_value = item.get("price")
                price = _finite_decimal(price_value)
                price_valid = False
                if price_value is None:
                    errors.append(f"Item at index {index} is missing 'price'.")
                elif price is None:
                    errors.append(f"Item at index {index} 'price' must be a numeric value.")
                elif price <= 0:
                    errors.append(f"Item at index {index} 'price' must be greater than 0.00.")
                elif not is_dynamodb_compatible_number(price):
                    errors.append(
                        f"Item at index {index} 'price' exceeds DynamoDB numeric limits."
                    )
                else:
                    price_valid = True

                if qty_valid and price_valid:
                    # Fraction(Decimal) preserves the decimal value exactly,
                    # including when the quantity is an int and input prices
                    # came from mixed int/float/Decimal sources.
                    calculated_sum += quantity * Fraction(price)

    # 4. Validate total_amount (supporting total_amount or totalAmount alias)
    total_amount = data.get("total_amount")
    if total_amount is None:
        total_amount = data.get("totalAmount")

    total = _finite_decimal(total_amount)
    if total_amount is None:
        errors.append("Missing required field: 'total_amount'.")
    elif total is None:
        errors.append("Field 'total_amount' must be a numeric value.")
    elif total <= 0:
        errors.append("Field 'total_amount' must be greater than 0.00.")
    elif not is_dynamodb_compatible_number(total):
        errors.append("Field 'total_amount' exceeds DynamoDB numeric limits.")
    elif items_valid:
        # Retain the existing 0.01 threshold, with exact decimal arithmetic.
        if abs(Fraction(total) - calculated_sum) >= Fraction(1, 100):
            errors.append(
                f"Field 'total_amount' ({total_amount}) does not match the sum of item prices "
                f"({_format_fraction_2(calculated_sum)})."
            )

    # 5. Validate currency (optional, defaults to USD)
    if "currency" in data and data["currency"] is not None:
        currency = data["currency"]
        if not isinstance(currency, str) or not CURRENCY_PATTERN.match(currency.upper()):
            errors.append("Field 'currency' must be a 3-letter ISO code (e.g. 'USD', 'INR').")

    is_valid = len(errors) == 0
    return is_valid, errors
