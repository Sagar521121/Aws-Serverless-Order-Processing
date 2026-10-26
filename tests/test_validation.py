"""Unit tests for the order payload validation logic (src/validation.py).

Uses Python's built-in unittest module. Compatible with Python 3.12.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

# Ensure src directory is on sys.path for direct module import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from validation import validate_order_payload


class TestOrderValidation(unittest.TestCase):
    """Test suite for validate_order_payload."""

    def setUp(self) -> None:
        """Create a standard valid order payload for use across test cases."""
        self.valid_payload = {
            "order_id": "ord-2026-98234-a1b2",
            "customer_id": "cust-5501",
            "items": [
                {"item_id": "item-101", "quantity": 2, "price": 19.99},
                {"item_id": "item-102", "quantity": 1, "price": 10.00},
            ],
            "total_amount": 49.98,
            "currency": "USD",
        }

    def test_valid_payload_passes(self) -> None:
        """A fully compliant order payload should be valid with no errors."""
        is_valid, errors = validate_order_payload(self.valid_payload)
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])

    def test_decimal_prices_and_total_pass_with_mixed_numeric_types(self) -> None:
        payload = dict(self.valid_payload)
        payload["items"] = [
            {"item_id": "item-1", "quantity": Decimal("2"), "price": Decimal("19.99")},
            {"item_id": "item-2", "quantity": 1, "price": 10.0},
            {"item_id": "item-3", "quantity": 1, "price": 1},
        ]
        payload["total_amount"] = Decimal("50.98")

        self.assertEqual(validate_order_payload(payload), (True, []))

    def test_decimal_total_uses_the_existing_one_cent_tolerance(self) -> None:
        for total, expected_valid in (
            (Decimal("10.009"), True),
            (Decimal("10.01"), False),
        ):
            with self.subTest(total=total):
                payload = dict(self.valid_payload)
                payload["items"] = [{"item_id": "item-1", "quantity": 1,
                                     "price": Decimal("10.00")}]
                payload["total_amount"] = total
                valid, _ = validate_order_payload(payload)
                self.assertEqual(valid, expected_valid)

    def test_dynamodb_decimal_precision_and_exponent_boundaries(self) -> None:
        accepted = Decimal("12345678901234567890123456789012345678")
        payload = dict(self.valid_payload)
        payload["items"] = [{"item_id": "item-1", "quantity": 1, "price": accepted}]
        payload["total_amount"] = accepted
        self.assertEqual(validate_order_payload(payload), (True, []))

        for value in (Decimal("123456789012345678901234567890123456789"),
                      Decimal("1e126"), Decimal("1e-131")):
            with self.subTest(value=value):
                payload["items"] = [{"item_id": "item-1", "quantity": 1, "price": value}]
                payload["total_amount"] = value
                valid, errors = validate_order_payload(payload)
                self.assertFalse(valid)
                self.assertTrue(any("DynamoDB numeric limits" in error for error in errors))

    def test_smallest_supported_positive_exponent_is_accepted(self) -> None:
        payload = dict(self.valid_payload)
        payload["items"] = [{"item_id": "item-1", "quantity": 1,
                             "price": Decimal("1e-130")}]
        payload["total_amount"] = Decimal("1e-130")
        self.assertEqual(validate_order_payload(payload), (True, []))

    def test_oversized_integer_quantity_fails_dynamodb_numeric_limits(self) -> None:
        payload = dict(self.valid_payload)
        payload["items"] = [{"item_id": "item-1", "quantity": 10**126, "price": 1}]
        payload["total_amount"] = 10**126
        valid, errors = validate_order_payload(payload)
        self.assertFalse(valid)
        self.assertTrue(any("quantity" in error and "DynamoDB numeric limits" in error
                            for error in errors))

    def test_price_and_total_excessive_precision_are_rejected(self) -> None:
        excessive = Decimal("1.123456789012345678901234567890123456789")
        payload = dict(self.valid_payload)
        payload["items"] = [{"item_id": "item-1", "quantity": 1, "price": excessive}]
        payload["total_amount"] = excessive
        valid, errors = validate_order_payload(payload)
        self.assertFalse(valid)
        self.assertTrue(any("price" in error and "DynamoDB numeric limits" in error
                            for error in errors))

        payload["items"] = [{"item_id": "item-1", "quantity": 1, "price": 1}]
        payload["total_amount"] = excessive
        valid, errors = validate_order_payload(payload)
        self.assertFalse(valid)
        self.assertTrue(any("total_amount" in error and "DynamoDB numeric limits" in error
                            for error in errors))

    def test_valid_payload_with_total_amount_alias(self) -> None:
        """Payload using camelCase totalAmount alias should pass validation."""
        payload = dict(self.valid_payload)
        del payload["total_amount"]
        payload["totalAmount"] = 49.98
        is_valid, errors = validate_order_payload(payload)
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])

    def test_valid_payload_without_optional_currency(self) -> None:
        """Payload omitting currency should pass validation."""
        payload = dict(self.valid_payload)
        del payload["currency"]
        is_valid, errors = validate_order_payload(payload)
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])

    def test_non_dict_payload_fails(self) -> None:
        """Non-dictionary payloads must be rejected."""
        for invalid_input in ["a string", [1, 2, 3], 12345, None]:
            with self.subTest(invalid_input=invalid_input):
                is_valid, errors = validate_order_payload(invalid_input)
                self.assertFalse(is_valid)
                self.assertTrue(any("must be a JSON object" in err for err in errors))

    def test_missing_order_id_fails(self) -> None:
        """Payload missing order_id must fail validation."""
        payload = dict(self.valid_payload)
        del payload["order_id"]
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("Missing required field: 'order_id'" in err for err in errors))

    def test_invalid_order_id_format_fails(self) -> None:
        """Order IDs with invalid characters or bad length must fail."""
        invalid_ids = [
            "short",              # under 8 chars
            "ord-with-$-chars!",  # illegal characters
            "a" * 65,             # exceeds 64 chars
            12345678,             # not a string
        ]
        for bad_id in invalid_ids:
            with self.subTest(bad_id=bad_id):
                payload = dict(self.valid_payload)
                payload["order_id"] = bad_id
                is_valid, errors = validate_order_payload(payload)
                self.assertFalse(is_valid)
                self.assertTrue(any("order_id" in err for err in errors))

    def test_missing_customer_id_fails(self) -> None:
        """Payload missing customer_id must fail."""
        payload = dict(self.valid_payload)
        del payload["customer_id"]
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("Missing required field: 'customer_id'" in err for err in errors))

    def test_empty_or_too_long_customer_id_fails(self) -> None:
        """Empty or excessively long customer_id must fail."""
        for bad_cust in ["", "   ", "c" * 65]:
            with self.subTest(bad_cust=bad_cust):
                payload = dict(self.valid_payload)
                payload["customer_id"] = bad_cust
                is_valid, errors = validate_order_payload(payload)
                self.assertFalse(is_valid)
                self.assertTrue(any("customer_id" in err for err in errors))

    def test_missing_or_empty_items_fails(self) -> None:
        """Payload without items or with an empty list must fail."""
        payload = dict(self.valid_payload)
        del payload["items"]
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)

        payload["items"] = []
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("items" in err for err in errors))

    def test_invalid_item_structure_fails(self) -> None:
        """Items that are not objects or missing required item fields must fail."""
        payload = dict(self.valid_payload)
        payload["items"] = ["not-a-dict"]
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)

        payload["items"] = [{"quantity": 1, "price": 10.0}]  # missing item_id
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("item_id" in err for err in errors))

    def test_invalid_quantity_fails(self) -> None:
        """Non-positive or non-integer quantities must fail."""
        for bad_qty in [0, -1, 1.5, Decimal("1.5"), Decimal("0"), "2", True, False]:
            with self.subTest(bad_qty=bad_qty):
                payload = dict(self.valid_payload)
                payload["items"] = [{"item_id": "item-1", "quantity": bad_qty, "price": 10.0}]
                payload["total_amount"] = 10.0
                is_valid, errors = validate_order_payload(payload)
                self.assertFalse(is_valid)
                self.assertTrue(any("quantity" in err for err in errors))

    def test_invalid_price_fails(self) -> None:
        """Non-positive or non-numeric prices must fail."""
        for bad_price in [0, -5.0, "free", "10.00", True, False,
                          float("nan"), float("inf"), float("-inf"),
                          Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]:
            with self.subTest(bad_price=bad_price):
                payload = dict(self.valid_payload)
                payload["items"] = [{"item_id": "item-1", "quantity": 1, "price": bad_price}]
                payload["total_amount"] = 10.0
                is_valid, errors = validate_order_payload(payload)
                self.assertFalse(is_valid)
                self.assertTrue(any("price" in err for err in errors))

    def test_invalid_totals_including_boolean_strings_and_nonfinite_values_fail(self) -> None:
        for bad_total in [0, -1, True, False, "10.00", float("nan"),
                          float("inf"), float("-inf"), Decimal("NaN"),
                          Decimal("Infinity"), Decimal("-Infinity")]:
            with self.subTest(bad_total=bad_total):
                payload = dict(self.valid_payload)
                payload["items"] = [{"item_id": "item-1", "quantity": 1,
                                     "price": 10}]
                payload["total_amount"] = bad_total
                valid, errors = validate_order_payload(payload)
                self.assertFalse(valid)
                self.assertTrue(any("total_amount" in err for err in errors))

    def test_total_amount_mismatch_fails(self) -> None:
        """total_amount that does not match item sum must fail."""
        payload = dict(self.valid_payload)
        payload["total_amount"] = 999.99  # Actual items sum is 49.98
        is_valid, errors = validate_order_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("does not match the sum" in err for err in errors))

    def test_invalid_currency_fails(self) -> None:
        """Invalid currency string must fail."""
        for bad_curr in ["US", "USDD", "123", "$$$"]:
            with self.subTest(bad_curr=bad_curr):
                payload = dict(self.valid_payload)
                payload["currency"] = bad_curr
                is_valid, errors = validate_order_payload(payload)
                self.assertFalse(is_valid)
                self.assertTrue(any("currency" in err for err in errors))


if __name__ == "__main__":
    unittest.main()
