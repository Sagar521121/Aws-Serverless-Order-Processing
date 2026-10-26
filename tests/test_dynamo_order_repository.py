"""Contract checks for DynamoDB conditional expressions used by recovery."""

from __future__ import annotations

import os
import sys
import unittest
from copy import deepcopy
from decimal import Decimal

try:
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
except ModuleNotFoundError as exc:
    if exc.name != "boto3":
        raise
    TypeDeserializer = None
    TypeSerializer = None

_REQUIRE_BOTO3 = os.environ.get("REQUIRE_BOTO3") == "1"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from dynamo_order_repository import DynamoOrderRepository


class FakeTable:
    def __init__(self):
        self.updates = []
        self.puts = []
        self.next_error = None
        self.item = None

    def get_item(self, **kwargs):
        self.last_get = kwargs
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.next_error:
            error, self.next_error = self.next_error, None
            raise error
        return {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        if self.next_error:
            error, self.next_error = self.next_error, None
            raise error
        return {}


class WriteSpyTable:
    """Record persistence calls and fail immediately when a write is forbidden."""

    def __init__(self, reject_writes=True):
        self.reject_writes = reject_writes
        self.write_calls = []

    def _write(self, method, **kwargs):
        self.write_calls.append((method, kwargs))
        if self.reject_writes:
            raise AssertionError(f"Unexpected persistence call: {method}")
        return {}

    def put_item(self, **kwargs):
        return self._write("put_item", **kwargs)

    def update_item(self, **kwargs):
        return self._write("update_item", **kwargs)

    def transact_write_items(self, **kwargs):
        return self._write("transact_write_items", **kwargs)

    def batch_write_item(self, **kwargs):
        return self._write("batch_write_item", **kwargs)


def _snapshot(value):
    """Capture nested values without Decimal NaN equality semantics."""
    if isinstance(value, Decimal):
        return ("Decimal", value.as_tuple())
    if isinstance(value, dict):
        return ("dict", tuple((key, _snapshot(nested)) for key, nested in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_snapshot(nested) for nested in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_snapshot(nested) for nested in value))
    return (type(value).__name__, value)


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class TestDynamoOrderRepository(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.repository = DynamoOrderRepository(self.table)

    def test_order_read_is_consistent(self):
        self.repository.get_order("order-123")
        self.assertTrue(self.table.last_get["ConsistentRead"])

    def test_initial_order_creation_is_conditional_and_reports_duplicate(self):
        item = {"order_id": "order-123", "order_status": "PENDING"}
        self.assertTrue(self.repository.create_pending(item))
        self.assertEqual(self.table.puts[-1]["Item"], item)
        self.assertEqual(self.table.puts[-1]["ConditionExpression"],
                         "attribute_not_exists(order_id)")
        self.table.next_error = AwsError("ConditionalCheckFailedException")
        self.assertFalse(self.repository.create_pending(item))

    def test_create_pending_converts_nested_floats_without_changing_integers_or_input(self):
        item = {
            "order_id": "order-123",
            "total_amount": 0.3,
            "items": [
                {"item_id": "item-1", "quantity": 3, "price": 0.1},
                {"item_id": "item-2", "quantity": 2, "price": 19.99},
            ],
        }

        self.assertTrue(self.repository.create_pending(item))
        persisted = self.table.puts[-1]["Item"]
        self.assertEqual(persisted["total_amount"], Decimal("0.3"))
        self.assertEqual(persisted["items"][0]["price"], Decimal("0.1"))
        self.assertEqual(persisted["items"][1]["price"], Decimal("19.99"))
        self.assertEqual(persisted["items"][0]["quantity"], 3)
        self.assertIs(type(persisted["items"][0]["quantity"]), int)
        self.assertIsInstance(item["total_amount"], float)
        self.assertIsInstance(item["items"][0]["price"], float)

    def test_create_pending_rejects_values_outside_dynamodb_number_limits_before_write(self):
        for value in (Decimal("1e126"), Decimal("1e-131"),
                      Decimal("1.123456789012345678901234567890123456789"),
                      float("nan"), float("inf"), 10**126):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.repository.create_pending({"order_id": "order-123", "amount": value})
        self.assertEqual(self.table.puts, [])

    def test_create_pending_preserves_supported_high_precision_decimal(self):
        value = Decimal("0.12345678901234567890123456789012345678")
        item = {"order_id": "order-123", "nested": {"amount": value}}
        self.assertTrue(self.repository.create_pending(item))
        self.assertEqual(self.table.puts[-1]["Item"]["nested"]["amount"], value)
        self.assertIs(item["nested"]["amount"], value)

    def test_repository_rejects_unsupported_numbers_before_any_persistence_call(self):
        cases = (
            ("top_level_numeric", {"amount": Decimal("1E+126")}),
            ("nested_dict_in_list_in_dict", {
                "details": [{"amounts": {"amount": Decimal("1E+126")}}],
            }),
            ("list_element", {"amounts": [Decimal("1E-131")]}),
            ("nan", {"amount": Decimal("NaN")}),
            ("signaling_nan", {"amount": Decimal("sNaN")}),
            ("positive_infinity", {"amount": Decimal("Infinity")}),
            ("negative_infinity", {"amount": Decimal("-Infinity")}),
            ("above_positive_exponent", {"amount": Decimal("1E+126")}),
            ("below_positive_exponent", {"amount": Decimal("1E-131")}),
            ("above_negative_exponent", {"amount": Decimal("-1E+126")}),
            ("below_negative_exponent", {"amount": Decimal("-1E-131")}),
            ("39_significant_digits", {
                "amount": Decimal("1." + "2" * 38),
            }),
            ("integer_above_max_exponent", {"amount": 10**126}),
        )

        for name, fields in cases:
            with self.subTest(case=name):
                table = WriteSpyTable()
                repository = DynamoOrderRepository(table)
                item = {"order_id": "order-123", **deepcopy(fields)}
                before = _snapshot(item)

                with self.assertRaises(ValueError):
                    repository.create_pending(item)

                self.assertEqual(table.write_calls, [])
                self.assertEqual(_snapshot(item), before)

    def test_supported_numeric_boundaries_reach_the_repository_write(self):
        maximum = Decimal("9.9999999999999999999999999999999999999E+125")
        cases = (
            ("maximum_positive_magnitude", maximum),
            ("maximum_negative_magnitude", maximum.copy_negate()),
            ("minimum_positive_magnitude", Decimal("1E-130")),
            ("minimum_negative_magnitude", Decimal("-1E-130")),
            ("38_significant_digits", Decimal("1." + "2" * 37)),
            ("integer_10_to_37", 10**37),
            ("integer_10_to_38", 10**38),
            ("zero", Decimal("0")),
            ("negative_zero", Decimal("-0")),
        )

        for name, value in cases:
            with self.subTest(case=name):
                table = WriteSpyTable(reject_writes=False)
                repository = DynamoOrderRepository(table)
                item = {"order_id": "order-123", "amount": value}
                before = _snapshot(item)

                self.assertTrue(repository.create_pending(item))

                self.assertEqual([method for method, _ in table.write_calls], ["put_item"])
                self.assertEqual(_snapshot(item), before)

    def test_boolean_is_preserved_as_a_dynamodb_boolean_not_a_number(self):
        table = WriteSpyTable(reject_writes=False)
        repository = DynamoOrderRepository(table)
        item = {"order_id": "order-123", "amount": True}

        self.assertTrue(repository.create_pending(item))
        persisted = table.write_calls[0][1]["Item"]
        self.assertIs(persisted["amount"], True)
        self.assertIs(type(persisted["amount"]), bool)

    def test_api_json_fraction_is_decimal_before_repository_persistence(self):
        import json

        payload = json.loads('{"amount":0.1}', parse_float=Decimal)
        self.assertIs(type(payload["amount"]), Decimal)
        table = WriteSpyTable(reject_writes=False)
        repository = DynamoOrderRepository(table)
        item = {"order_id": "order-123", **payload}

        self.assertTrue(repository.create_pending(item))
        persisted = table.write_calls[0][1]["Item"]
        self.assertEqual(persisted["amount"], Decimal("0.1"))
        self.assertIs(type(persisted["amount"]), Decimal)

    @unittest.skipIf(
        TypeSerializer is None and not _REQUIRE_BOTO3,
        "boto3 is not installed locally; set REQUIRE_BOTO3=1 to require it",
    )
    def test_create_pending_item_serializes_with_boto3_type_serializer(self):
        if TypeSerializer is None or TypeDeserializer is None:
            self.fail(
                "REQUIRE_BOTO3=1 but boto3 is unavailable; install requirements-dev.txt"
            )
        high_precision = Decimal("0.12345678901234567890123456789012345678")
        maximum = Decimal("9.9999999999999999999999999999999999999E+125")
        item = {
            "order_id": "order-123",
            "total_amount": Decimal("59.97"),
            "items": [{
                "item_id": "item-1", "quantity": 3, "price": 19.99,
                "is_gift": True,
            }],
            "details": {
                "amounts": [
                    high_precision,
                    {"adjustment": Decimal("1.2300")},
                    maximum,
                    Decimal("1E-130"),
                ],
            },
        }
        original = deepcopy(item)

        self.assertTrue(self.repository.create_pending(item))
        persisted = self.table.puts[-1]["Item"]
        serializer = TypeSerializer()
        serialized = serializer.serialize(persisted)["M"]
        self.assertEqual(serialized["total_amount"], {"N": "59.97"})
        self.assertEqual(serialized["items"]["L"][0]["M"]["quantity"], {"N": "3"})
        self.assertEqual(serialized["items"]["L"][0]["M"]["price"], {"N": "19.99"})
        self.assertEqual(serialized["items"]["L"][0]["M"]["is_gift"], {"BOOL": True})
        self.assertEqual(Decimal(serialized["details"]["M"]["amounts"]["L"][0]["N"]),
                         high_precision)
        self.assertEqual(Decimal(
            serialized["details"]["M"]["amounts"]["L"][1]["M"]["adjustment"]["N"]
        ), Decimal("1.23"))
        self.assertEqual(
            serialized["details"]["M"]["amounts"]["L"][2]["N"],
            str(maximum),
        )
        self.assertEqual(
            serialized["details"]["M"]["amounts"]["L"][3]["N"],
            "1E-130",
        )
        self.assertIs(type(persisted["items"][0]["quantity"]), int)
        self.assertIsInstance(persisted["total_amount"], Decimal)
        self.assertIsInstance(persisted["items"][0]["price"], Decimal)
        self.assertEqual(item, original)

        deserializer = TypeDeserializer()
        restored = deserializer.deserialize({"M": serialized})
        self.assertEqual(restored["details"]["amounts"][0], high_precision)
        self.assertEqual(restored["details"]["amounts"][1]["adjustment"], Decimal("1.23"))
        self.assertEqual(restored["details"]["amounts"][2], maximum)
        self.assertEqual(restored["details"]["amounts"][3], Decimal("1E-130"))
        self.assertEqual(restored["items"][0]["quantity"], Decimal("3"))
        self.assertEqual(restored["items"][0]["price"], Decimal("19.99"))

    def test_lease_acquisition_condition_checks_generation_hash_status_and_stored_expiry(self):
        self.repository.acquire_lease("order-123", "a" * 64, 5, "lease-token", 100, 20)
        call = self.table.updates[-1]
        condition = call["ConditionExpression"]
        self.assertIn("recovery_generation = :generation", condition)
        self.assertIn("payload_hash = :hash", condition)
        self.assertIn("order_status = :pending", condition)
        self.assertIn("order_status = :processing AND attribute_exists(lease_owner)", condition)
        self.assertIn("lease_until <= :now", condition)
        self.assertEqual(call["ExpressionAttributeValues"][":owner"], "lease-token")

    def test_race_loser_reports_conditional_failure_without_retrying_update(self):
        self.table.next_error = AwsError("ConditionalCheckFailedException")
        acquired = self.repository.acquire_lease("order-123", "a" * 64, 5, "t", 100, 20)
        self.assertFalse(acquired)
        self.assertEqual(len(self.table.updates), 1)

    def test_completion_requires_current_owner_generation_hash_and_unexpired_lease(self):
        self.repository.complete("order-123", "a" * 64, 5, "owner-1", 100)
        condition = self.table.updates[-1]["ConditionExpression"]
        for clause in ("lease_owner = :owner", "lease_until > :now",
                       "recovery_generation = :generation", "payload_hash = :hash"):
            self.assertIn(clause, condition)

    def test_mark_exhausted_requires_expired_lease_for_processing_state(self):
        self.repository.mark_retry_exhausted("order-123", "a" * 64, 5, 100)
        condition = self.table.updates[-1]["ConditionExpression"]
        self.assertIn("order_status = :processing AND attribute_exists(lease_owner)", condition)
        self.assertIn("lease_until <= :now", condition)

    def test_redrive_bump_requires_exhausted_state_and_no_active_lease(self):
        self.repository.begin_redrive("order-123", "a" * 64, 5, 100)
        call = self.table.updates[-1]
        condition = call["ConditionExpression"]
        self.assertIn("order_status = :exhausted", condition)
        self.assertIn("lease_until <= :now", condition)
        values = call["ExpressionAttributeValues"]
        self.assertEqual(values[":new_generation"], 6)
        self.assertEqual(values[":needs_enqueue"], "NEEDS_ENQUEUE")


if __name__ == "__main__":
    unittest.main()
