"""Local tests for SQS worker lease acquisition and retry behavior."""

from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from business_processing import process_order_locally
from worker import RetryableDeliveryError, lambda_handler, process_record


HASH = "a" * 64


class FakeRepository:
    def __init__(self, item):
        self.item = dict(item) if item else None
        self.acquire_calls = 0
        self.complete_calls = 0
        self.fail_calls = 0
        self.fail_args = None
        self.acquire_result = True
        self.after_failed_acquire = None

    def get_order(self, order_id):
        return dict(self.item) if self.item else None

    def acquire_lease(self, order_id, payload_hash, generation, owner, now, lease_seconds):
        self.acquire_calls += 1
        if not self.acquire_result:
            if self.after_failed_acquire:
                self.item = dict(self.after_failed_acquire)
            return False
        self.item.update(order_status="PROCESSING", lease_owner=owner,
                         lease_until=now + lease_seconds,
                         recovery_generation=generation)
        return True

    def complete(self, order_id, payload_hash, generation, owner, now):
        self.complete_calls += 1
        if self.item.get("lease_owner") != owner:
            return False
        self.item.update(order_status="COMPLETED")
        self.item.pop("lease_owner", None)
        self.item.pop("lease_until", None)
        return True

    def fail_permanently(self, *args):
        self.fail_calls += 1
        self.fail_args = args
        self.item.update(order_status="FAILED")
        self.item.pop("lease_owner", None)
        self.item.pop("lease_until", None)
        return True


def message(generation=3, payload_hash=HASH):
    return {"body": json.dumps({"order_id": "order-123", "payload_hash": payload_hash,
                                "recovery_generation": generation, "version": 1})}


def order(status="PENDING", generation=3, **extra):
    item = {"order_id": "order-123", "payload_hash": HASH,
            "recovery_generation": generation, "order_status": status}
    item.update(extra)
    return item


class TestWorkerLeaseRecovery(unittest.TestCase):
    def test_dynamodb_decimal_record_passes_worker_business_validation(self):
        stored_order = order(
            "PENDING", customer_id="customer-1",
            items=[{"item_id": "item-1", "quantity": Decimal("2"),
                    "price": Decimal("5.25")}],
            total_amount=Decimal("10.50"), currency="USD",
        )
        repo = FakeRepository(stored_order)

        result = process_record(message(), repo, process_order_locally, now=lambda: 100)

        self.assertEqual(result, "COMPLETED")
        self.assertEqual(repo.complete_calls, 1)
        self.assertEqual(repo.item["order_status"], "COMPLETED")

    def test_invalid_stored_business_data_is_recorded_failed_not_retried(self):
        stored_order = order(
            "PENDING", customer_id="customer-1",
            items=[{"item_id": "item-1", "quantity": 1, "price": Decimal("-5.00")}],
            total_amount=Decimal("-5.00"), currency="USD",
        )
        repo = FakeRepository(stored_order)

        result = process_record(message(), repo, process_order_locally, now=lambda: 100)

        self.assertEqual(result, "FAILED")
        self.assertEqual(repo.fail_calls, 1)
        self.assertEqual(repo.fail_args[-1], "INVALID_STORED_ORDER")
        self.assertEqual(repo.item["order_status"], "FAILED")
        self.assertEqual(repo.complete_calls, 0)

    def test_terminal_duplicate_returns_without_processing(self):
        repo = FakeRepository(order("COMPLETED"))
        processed = []
        result = process_record(message(), repo, processed.append, now=lambda: 100)
        self.assertEqual(result, "TERMINAL")
        self.assertEqual(processed, [])
        self.assertEqual(repo.acquire_calls, 0)

    def test_valid_active_lease_fails_delivery_without_acquiring_or_processing(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=120))
        processed = []
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(), repo, processed.append, now=lambda: 100)
        self.assertEqual(repo.acquire_calls, 0)
        self.assertEqual(processed, [])

    def test_expired_lease_is_conditionally_reclaimed(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=100))
        result = process_record(message(), repo, lambda item: None, now=lambda: 100)
        self.assertEqual(result, "COMPLETED")
        self.assertEqual(repo.acquire_calls, 1)
        self.assertEqual(repo.item["order_status"], "COMPLETED")

    def test_race_loser_rereads_active_lease_and_fails_delivery(self):
        repo = FakeRepository(order("PENDING"))
        repo.acquire_result = False
        repo.after_failed_acquire = order("PROCESSING", lease_owner="worker-b", lease_until=130)
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(), repo, lambda item: self.fail("must not process"), now=lambda: 100)
        self.assertEqual(repo.item["lease_owner"], "worker-b")

    def test_stale_generation_is_acknowledgeable_without_work(self):
        repo = FakeRepository(order("PENDING", generation=4, enqueue_state="ENQUEUED"))
        result = process_record(message(generation=3), repo, lambda item: self.fail(), now=lambda: 100)
        self.assertEqual(result, "STALE")
        self.assertEqual(repo.acquire_calls, 0)

    def test_stale_generation_needs_enqueue_remains_retryable(self):
        repo = FakeRepository(order("PENDING", generation=4,
                                    enqueue_state="NEEDS_ENQUEUE"))
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(generation=3), repo,
                           lambda item: self.fail("stale message must not process"),
                           now=lambda: 100)
        self.assertEqual(repo.acquire_calls, 0)

    def test_stale_generation_needs_enqueue_is_retryable_after_acquire_race(self):
        repo = FakeRepository(order("PENDING", generation=3, enqueue_state="ENQUEUED"))
        repo.acquire_result = False
        repo.after_failed_acquire = order("PENDING", generation=4,
                                          enqueue_state="NEEDS_ENQUEUE")
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(generation=3), repo,
                           lambda item: self.fail("must not process"), now=lambda: 100)

    def test_stale_generation_with_enqueued_processing_order_is_acknowledged(self):
        repo = FakeRepository(order("PROCESSING", generation=4,
                                    enqueue_state="ENQUEUED", lease_owner="worker-a",
                                    lease_until=120))
        result = process_record(message(generation=3), repo,
                                lambda item: self.fail("must not process"), now=lambda: 100)
        self.assertEqual(result, "STALE")
        self.assertEqual(repo.acquire_calls, 0)

    def test_stale_generation_terminal_order_is_acknowledged(self):
        repo = FakeRepository(order("COMPLETED", generation=4,
                                    enqueue_state="NEEDS_ENQUEUE"))
        result = process_record(message(generation=3), repo,
                                lambda item: self.fail("must not process"), now=lambda: 100)
        self.assertEqual(result, "TERMINAL")

    def test_stale_generation_with_inconsistent_enqueue_state_is_retryable(self):
        repo = FakeRepository(order("PENDING", generation=4))
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(generation=3), repo,
                           lambda item: self.fail("must not process"), now=lambda: 100)

    def test_current_generation_message_can_process_while_marker_needs_enqueue(self):
        repo = FakeRepository(order("PENDING", generation=3,
                                    enqueue_state="NEEDS_ENQUEUE"))
        result = process_record(message(generation=3), repo, lambda item: None,
                                now=lambda: 100)
        self.assertEqual(result, "COMPLETED")
        self.assertEqual(repo.acquire_calls, 1)

    def test_future_generation_is_retryable_investigation(self):
        repo = FakeRepository(order("PENDING", generation=3))
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(generation=4), repo, lambda item: self.fail(), now=lambda: 100)

    def test_missing_order_and_payload_mismatch_are_retryable_investigation(self):
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(), FakeRepository(None), lambda item: self.fail(), now=lambda: 100)
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(payload_hash="c" * 64), FakeRepository(order()),
                           lambda item: self.fail(), now=lambda: 100)

    def test_processing_order_with_malformed_lease_is_not_acknowledged(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=None))
        with self.assertRaises(RetryableDeliveryError):
            process_record(message(), repo, lambda item: self.fail(), now=lambda: 100)

    def test_invalid_message_generation_is_rejected(self):
        repo = FakeRepository(order())
        with self.assertRaises(RetryableDeliveryError):
            process_record({"body": json.dumps({"order_id": "order-123", "payload_hash": HASH,
                                                "recovery_generation": True})}, repo,
                           lambda item: self.fail(), now=lambda: 100)

    def test_transient_processing_failure_leaves_state_for_expiry_and_retry(self):
        repo = FakeRepository(order())
        def fail(_item):
            raise RuntimeError("temporary")
        with self.assertRaises(RuntimeError):
            process_record(message(), repo, fail, now=lambda: 100)
        self.assertEqual(repo.item["order_status"], "PROCESSING")
        self.assertEqual(repo.complete_calls, 0)

    def test_lambda_handler_raises_on_active_lease_so_mapping_retries_batch(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=120))
        with self.assertRaises(RetryableDeliveryError):
            lambda_handler({"Records": [message()]}, None, repository=repo,
                           process_order=lambda item: self.fail("must not process"),
                           now=lambda: 100)

    def test_lambda_handler_success_returns_only_after_safe_ack_outcome(self):
        repo = FakeRepository(order("COMPLETED"))
        response = lambda_handler({"Records": [message()]}, None, repository=repo,
                                  process_order=lambda item: self.fail("terminal duplicate must not run"),
                                  now=lambda: 100)
        self.assertEqual(response, {"batchItemFailures": []})


if __name__ == "__main__":
    unittest.main()
