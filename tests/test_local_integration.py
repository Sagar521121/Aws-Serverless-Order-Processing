"""Offline integration tests joining intake, repository, queue, worker and DLQ."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from dlq_recovery import operator_redrive_record
from business_processing import process_order_locally
from intake_service import IntakeService, payload_hash
from lambda_function import lambda_handler
from local_adapters import LocalOrderRepository, LocalSqsAdapter
from worker import RetryableDeliveryError, lambda_handler as worker_handler, process_record


class CommitThenRaiseRepository(LocalOrderRepository):
    """One-shot wrapper that raises only after an in-memory write commits.

    These tests characterize application handling of an uncertain write result;
    they do not establish DynamoDB consistency or conditional-write semantics.
    """

    def __init__(self) -> None:
        super().__init__()
        self.raise_after_commit: str | None = None
        self.committed_operations: list[str] = []

    def _after_commit(self, operation: str, committed: bool) -> bool:
        if committed and self.raise_after_commit == operation:
            self.committed_operations.append(operation)
            self.raise_after_commit = None
            raise RuntimeError(f"injected response loss after {operation} commit")
        return committed

    def create_pending(self, item):
        return self._after_commit("create_pending", super().create_pending(item))

    def mark_enqueued(self, order_id, payload_hash, generation, now):
        return self._after_commit(
            "mark_enqueued",
            super().mark_enqueued(order_id, payload_hash, generation, now),
        )

    def acquire_lease(self, order_id, payload_hash, generation, owner, now, lease_seconds):
        return self._after_commit(
            "acquire_lease",
            super().acquire_lease(
                order_id, payload_hash, generation, owner, now, lease_seconds
            ),
        )

    def complete(self, order_id, payload_hash, generation, owner, now):
        return self._after_commit(
            "complete", super().complete(order_id, payload_hash, generation, owner, now)
        )


class FakeDlqClient:
    def __init__(self) -> None:
        self.deleted = []

    def delete_message(self, **kwargs) -> None:
        self.deleted.append(kwargs)


class TestLocalIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100
        self.repository = CommitThenRaiseRepository()
        self.queue = LocalSqsAdapter()
        self.intake = IntakeService(self.repository, self.queue, now=lambda: self.now)
        self.body = {
            "order_id": "order-2026-01",
            "customer_id": "customer-1",
            "items": [{"item_id": "book-1", "quantity": 2, "price": 5.0}],
            "total_amount": 10.0,
            "currency": "USD",
        }

    def submit(self, order_id: str | None = None):
        body = dict(self.body)
        if order_id:
            body["order_id"] = order_id
        return lambda_handler({"body": json.dumps(body)}, None, self.intake)

    def test_successful_intake_persists_and_enqueues_once(self) -> None:
        response = self.submit()
        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)
        item = self.repository.get_order(self.body["order_id"])
        self.assertEqual(item["order_status"], "PENDING")
        self.assertNotIn("status", item)
        self.assertEqual(item["enqueue_state"], "ENQUEUED")
        self.assertEqual(self.queue.messages[0]["parsed_body"]["recovery_generation"], 0)

    def test_persisted_record_omits_redundant_status_and_preserves_api_contract(self) -> None:
        """Ensure newly created orders persist order_status without redundant status, while API response contract is preserved."""
        response = self.submit("order-status-boundary-check")
        self.assertEqual(response["statusCode"], 201)

        # Confirm API response contract: status="success", data.order_status="PENDING", no bare "status" in data
        payload = json.loads(response["body"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["message"], "Order accepted for processing")
        self.assertIn("data", payload)
        self.assertEqual(payload["data"]["order_id"], "order-status-boundary-check")
        self.assertEqual(payload["data"]["order_status"], "PENDING")
        self.assertNotIn("status", payload["data"])
        self.assertEqual(payload["data"]["enqueue_state"], "ENQUEUED")

        # Confirm persisted DynamoDB record contains order_status and NOT bare status
        persisted = self.repository.get_order("order-status-boundary-check")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["order_status"], "PENDING")
        self.assertNotIn("status", persisted, "Redundant domain 'status' attribute must not be persisted in DynamoDB")

    def test_same_payload_is_idempotent_and_changed_payload_conflicts(self) -> None:
        self.assertEqual(self.submit()["statusCode"], 201)
        self.assertEqual(self.submit()["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)
        changed = dict(self.body)
        changed["total_amount"] = 9.0
        changed["items"] = [{"item_id": "book-1", "quantity": 1, "price": 9.0}]
        response = lambda_handler({"body": json.dumps(changed)}, None, self.intake)
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(len(self.queue.messages), 1)

    def test_equivalent_decimal_spellings_share_idempotency_hash(self) -> None:
        prefix = ('{"order_id":"order-decimal-1","customer_id":"customer-1",'
                  '"items":[{"item_id":"book-1","quantity":1,"price":')
        first = prefix + '1' + '}],"total_amount":1}'
        second = prefix + '1.00' + '}],"total_amount":1.00}'
        self.assertEqual(lambda_handler({"body": first}, None, self.intake)["statusCode"], 201)
        self.assertEqual(lambda_handler({"body": second}, None, self.intake)["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(self.repository.get_order("order-decimal-1")["items"][0]["price"],
                         Decimal("1"))

    def test_ordinary_order_hash_matches_legacy_float_canonicalization(self) -> None:
        legacy_shape = {
            "order_id": self.body["order_id"],
            "customer_id": self.body["customer_id"],
            "items": [{"item_id": "book-1", "quantity": 2, "price": 5.0}],
            "total_amount": 10.0,
            "currency": "USD",
        }
        legacy_packed = json.dumps(legacy_shape, sort_keys=True,
                                  separators=(",", ":"), ensure_ascii=False)
        expected = hashlib.sha256(legacy_packed.encode("utf-8")).hexdigest()
        self.assertEqual(payload_hash(self.body), expected)

    def test_high_precision_distinct_money_values_do_not_collapse_in_hash(self) -> None:
        first_value = "0.12345678901234567890123456789012345678"
        second_value = "0.12345678901234567890123456789012345679"
        first = (
            '{"order_id":"order-decimal-2","customer_id":"customer-1",'
            '"items":[{"item_id":"book-1","quantity":1,"price":' + first_value + '}],'
            '"total_amount":' + first_value + '}'
        )
        second = first.replace(first_value, second_value)
        self.assertEqual(lambda_handler({"body": first}, None, self.intake)["statusCode"], 201)
        self.assertEqual(lambda_handler({"body": second}, None, self.intake)["statusCode"], 409)
        stored = self.repository.get_order("order-decimal-2")
        self.assertEqual(stored["total_amount"], Decimal(first_value))
        self.assertEqual(len(self.queue.messages), 1)
        self.assertNotEqual(
            payload_hash({"order_id": "order-123456", "customer_id": "c",
                          "items": [{"item_id": "item-1", "quantity": 1,
                                     "price": Decimal(first_value)}],
                          "total_amount": Decimal(first_value)}),
            payload_hash({"order_id": "order-123456", "customer_id": "c",
                          "items": [{"item_id": "item-1", "quantity": 1,
                                     "price": Decimal(second_value)}],
                          "total_amount": Decimal(second_value)}),
        )

    def test_unsupported_numbers_are_rejected_before_record_or_enqueue(self) -> None:
        invalid_numbers = (
            ("order-invalid-1", "1", "1e126", "1e126"),
            ("order-invalid-2", "1" + ("0" * 126), "1", "1e126"),
            ("order-invalid-3", "1", "NaN", "NaN"),
            ("order-invalid-4", "1", "Infinity", "Infinity"),
            ("order-invalid-5", "1", "1.123456789012345678901234567890123456789",
             "1.123456789012345678901234567890123456789"),
        )
        for order_id, quantity, amount, total in invalid_numbers:
            with self.subTest(order_id=order_id):
                raw = (
                    '{"order_id":"' + order_id + '","customer_id":"customer-1",'
                    '"items":[{"item_id":"book-1","quantity":' + quantity +
                    ',"price":' + amount + '}],"total_amount":' + total + '}'
                )
                response = lambda_handler({"body": raw}, None, self.intake)
                self.assertEqual(response["statusCode"], 400)
                self.assertIsNone(self.repository.get_order(order_id))
        self.assertEqual(self.queue.messages, [])

    def test_enqueue_failure_preserves_order_for_same_request_retry(self) -> None:
        self.queue.fail_next_send = RuntimeError("local send failure")
        first = self.submit()
        self.assertEqual(first["statusCode"], 503)
        self.assertEqual(self.repository.get_order(self.body["order_id"])["enqueue_state"], "NEEDS_ENQUEUE")
        second = self.submit()
        self.assertEqual(second["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(self.repository.get_order(self.body["order_id"])["enqueue_state"], "ENQUEUED")

    def test_ambiguous_accepted_send_characterizes_at_least_once_publication(self) -> None:
        """Characterize duplicate publication after acceptance with a lost response.

        The local fake verifies the application's reaction to this injected
        ambiguity; it does not establish real SQS transport behavior.
        """
        self.queue.accept_then_raise_next_send = RuntimeError("response lost after acceptance")

        first = self.submit()
        self.assertEqual(first["statusCode"], 503)
        stored = self.repository.get_order(self.body["order_id"])
        self.assertEqual(stored["enqueue_state"], "NEEDS_ENQUEUE")
        self.assertEqual(stored["order_status"], "PENDING")
        self.assertEqual(len(self.queue.messages), 1)

        retry = self.submit()
        self.assertEqual(retry["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 2)
        first_message, second_message = self.queue.messages
        self.assertEqual(first_message["parsed_body"], second_message["parsed_body"])
        self.assertEqual(first_message["body"], second_message["body"])
        self.assertEqual(
            self.repository.get_order(self.body["order_id"])["enqueue_state"],
            "ENQUEUED",
        )

        changed = dict(self.body)
        changed["items"] = [{"item_id": "book-1", "quantity": 1, "price": 9.0}]
        changed["total_amount"] = 9.0
        conflict = lambda_handler({"body": json.dumps(changed)}, None, self.intake)
        self.assertEqual(conflict["statusCode"], 409)
        self.assertEqual(len(self.queue.messages), 2)

        calls = []
        first_outcome = process_record(
            first_message, self.repository, calls.append, now=lambda: self.now
        )
        state_after_first = self.repository.get_order(self.body["order_id"])
        second_outcome = process_record(
            second_message, self.repository, calls.append, now=lambda: self.now
        )
        self.assertEqual(first_outcome, "COMPLETED")
        self.assertEqual(second_outcome, "TERMINAL")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.repository.get_order(self.body["order_id"]), state_after_first)

    def test_create_pending_commit_then_raise_is_recovered_by_same_payload_retry(self) -> None:
        """A committed create with a lost response is recovered by intake retry."""
        self.repository.raise_after_commit = "create_pending"

        first = self.submit()
        self.assertEqual(first["statusCode"], 500)
        self.assertEqual(self.repository.committed_operations, ["create_pending"])
        stored = self.repository.get_order(self.body["order_id"])
        self.assertEqual(stored["enqueue_state"], "NEEDS_ENQUEUE")
        self.assertEqual(stored["order_status"], "PENDING")
        self.assertEqual(self.queue.messages, [])

        retry = self.submit()
        self.assertEqual(retry["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(
            self.repository.get_order(self.body["order_id"])["enqueue_state"],
            "ENQUEUED",
        )

        changed = dict(self.body)
        changed["items"] = [{"item_id": "book-1", "quantity": 1, "price": 9.0}]
        changed["total_amount"] = 9.0
        conflict = lambda_handler({"body": json.dumps(changed)}, None, self.intake)
        self.assertEqual(conflict["statusCode"], 409)
        self.assertEqual(len(self.queue.messages), 1)

    def test_mark_enqueued_commit_then_raise_returns_success_without_duplicate_send(self) -> None:
        """A committed enqueue marker is confirmed by the intake reread."""
        self.repository.raise_after_commit = "mark_enqueued"

        first = self.submit()
        self.assertEqual(first["statusCode"], 201)
        self.assertEqual(self.repository.committed_operations, ["mark_enqueued"])
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(
            self.repository.get_order(self.body["order_id"])["enqueue_state"],
            "ENQUEUED",
        )

        retry = self.submit()
        self.assertEqual(retry["statusCode"], 201)
        self.assertEqual(len(self.queue.messages), 1)

    def test_acquire_lease_commit_then_raise_waits_for_expiry_before_retry(self) -> None:
        """A committed lease blocks early redelivery, then expires deterministically."""
        self.assertEqual(self.submit()["statusCode"], 201)
        self.repository.raise_after_commit = "acquire_lease"
        calls = []
        message = self.queue.messages[0]

        with self.assertRaises(RuntimeError):
            process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(self.repository.committed_operations, ["acquire_lease"])
        leased = self.repository.get_order(self.body["order_id"])
        self.assertEqual(leased["order_status"], "PROCESSING")
        self.assertEqual(calls, [])

        with self.assertRaises(RetryableDeliveryError):
            process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(calls, [])

        self.now = leased["lease_until"]
        outcome = process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(outcome, "COMPLETED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.repository.get_order(self.body["order_id"])["order_status"],
            "COMPLETED",
        )

    def test_complete_commit_then_raise_redelivery_acknowledges_terminal_order(self) -> None:
        """A committed completion is acknowledged on redelivery without rerunning work."""
        self.assertEqual(self.submit()["statusCode"], 201)
        self.repository.raise_after_commit = "complete"
        calls = []
        message = self.queue.messages[0]

        with self.assertRaises(RuntimeError):
            process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(self.repository.committed_operations, ["complete"])
        self.assertEqual(len(calls), 1)
        terminal_state = self.repository.get_order(self.body["order_id"])
        self.assertEqual(terminal_state["order_status"], "COMPLETED")

        outcome = process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(outcome, "TERMINAL")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.repository.get_order(self.body["order_id"]), terminal_state)

    def test_worker_success_runs_local_callback_and_marks_complete(self) -> None:
        self.assertEqual(self.submit()["statusCode"], 201)
        result = process_record(self.queue.messages[0], self.repository,
                                process_order_locally, now=lambda: self.now)
        self.assertEqual(result, "COMPLETED")
        self.assertEqual(self.repository.get_order(self.body["order_id"])["order_status"], "COMPLETED")

    def test_worker_handler_uses_deterministic_local_business_callback(self) -> None:
        self.submit()
        result = worker_handler({"Records": [self.queue.messages[0]]}, None,
                                repository=self.repository, now=lambda: self.now)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(self.repository.get_order(self.body["order_id"])["order_status"], "COMPLETED")

    def test_duplicate_delivery_after_completion_does_not_repeat_callback(self) -> None:
        self.submit()
        calls = []
        message = self.queue.messages[0]
        process_record(message, self.repository, calls.append, now=lambda: self.now)
        duplicate = process_record(message, self.repository, calls.append, now=lambda: self.now)
        self.assertEqual(duplicate, "TERMINAL")
        self.assertEqual(len(calls), 1)

    def test_expired_lease_is_reclaimed_after_redelivery(self) -> None:
        self.submit()
        order_id = self.body["order_id"]
        item = self.repository._items[order_id]
        item.update(order_status="PROCESSING", lease_owner="crashed-worker", lease_until=90)
        results = []
        self.now = 100
        outcome = process_record(self.queue.messages[0], self.repository, results.append,
                                 now=lambda: self.now)
        self.assertEqual(outcome, "COMPLETED")
        self.assertEqual(len(results), 1)
        self.assertEqual(self.repository.get_order(order_id)["order_status"], "COMPLETED")

    def test_terminal_order_is_acknowledgeable_without_business_work(self) -> None:
        self.submit()
        order_id = self.body["order_id"]
        self.repository._items[order_id]["order_status"] = "FAILED"
        calls = []
        outcome = process_record(self.queue.messages[0], self.repository, calls.append,
                                 now=lambda: self.now)
        self.assertEqual(outcome, "TERMINAL")
        self.assertEqual(calls, [])

    def test_dlq_redrive_increments_generation_sends_and_deletes_resolved_record(self) -> None:
        self.submit()
        old = self.queue.messages[0]["parsed_body"]
        self.assertTrue(self.repository.mark_retry_exhausted(
            old["order_id"], old["payload_hash"], old["recovery_generation"], self.now
        ))
        dlq_client = FakeDlqClient()
        record = {"body": json.dumps(old), "receiptHandle": "local-receipt"}
        result = operator_redrive_record(
            record, self.repository, self.queue, dlq_client, "local-dlq",
            operator_id="operator-1", approved=True, now=lambda: self.now,
        )
        self.assertEqual(result.classification, "REDRIVEN")
        self.assertTrue(result.acknowledge_dlq_message)
        self.assertEqual(len(self.queue.messages), 2)
        self.assertEqual(self.queue.messages[-1]["parsed_body"]["recovery_generation"], 1)
        self.assertEqual(dlq_client.deleted, [{"QueueUrl": "local-dlq", "ReceiptHandle": "local-receipt"}])
        current = self.repository.get_order(old["order_id"])
        self.assertEqual(current["recovery_generation"], 1)
        self.assertEqual(current["enqueue_state"], "ENQUEUED")

    def test_old_queued_message_is_not_acknowledged_while_redrive_enqueue_is_pending(self) -> None:
        self.assertEqual(self.submit()["statusCode"], 201)
        old_message = self.queue.messages[0]
        old_body = old_message["parsed_body"]
        self.assertTrue(self.repository.mark_retry_exhausted(
            old_body["order_id"], old_body["payload_hash"],
            old_body["recovery_generation"], self.now,
        ))
        self.assertEqual(self.repository.begin_redrive(
            old_body["order_id"], old_body["payload_hash"],
            old_body["recovery_generation"], self.now,
        ), 1)
        current = self.repository.get_order(old_body["order_id"])
        self.assertEqual(current["recovery_generation"], 1)
        self.assertEqual(current["enqueue_state"], "NEEDS_ENQUEUE")

        with self.assertRaises(RetryableDeliveryError):
            process_record(old_message, self.repository,
                           lambda item: self.fail("generation zero must not process"),
                           now=lambda: self.now)

        current = self.repository.get_order(old_body["order_id"])
        self.assertEqual(current["recovery_generation"], 1)
        self.assertEqual(current["enqueue_state"], "NEEDS_ENQUEUE")
        self.assertEqual(current["order_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
