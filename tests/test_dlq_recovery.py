"""Local tests for conditional DLQ triage and operator-approved redrive."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from dlq_recovery import operator_redrive_record, process_dlq_message


HASH = "b" * 64


class FakeRepository:
    def __init__(self, item):
        self.item = dict(item) if item else None
        self.exhaust_calls = 0
        self.redrive_calls = 0
        self.mark_calls = 0
        self.exhaust_result = True
        self.redrive_result = True
        self.mark_result = True
        self.on_mark = None
        self.on_redrive = None

    def get_order(self, order_id):
        return dict(self.item) if self.item else None

    def mark_retry_exhausted(self, order_id, payload_hash, generation, now):
        self.exhaust_calls += 1
        if not self.exhaust_result:
            return False
        self.item.update(order_status="RETRY_EXHAUSTED")
        self.item.pop("lease_owner", None)
        self.item.pop("lease_until", None)
        return True

    def begin_redrive(self, order_id, payload_hash, generation, now):
        self.redrive_calls += 1
        if not self.redrive_result:
            if self.on_redrive:
                self.on_redrive(self)
            return None
        next_generation = generation + 1
        self.item.update(order_status="PENDING", enqueue_state="NEEDS_ENQUEUE",
                         recovery_generation=next_generation, attempt_count=0)
        self.item.pop("lease_owner", None)
        self.item.pop("lease_until", None)
        return next_generation

    def mark_enqueued(self, order_id, payload_hash, generation, now):
        self.mark_calls += 1
        if self.on_mark:
            self.on_mark(self)
        if not self.mark_result:
            return False
        if (self.item["payload_hash"] == payload_hash
                and self.item["recovery_generation"] == generation
                and self.item.get("enqueue_state") == "NEEDS_ENQUEUE"):
            self.item["enqueue_state"] = "ENQUEUED"
            return True
        return False


class FakePublisher:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or {"MessageId": "new-message-id"}
        self.error = error

    def send_message(self, body):
        self.calls.append(json.loads(body))
        if self.error:
            raise self.error
        return self.result


class FakeDlqClient:
    def __init__(self, error=None):
        self.deleted = []
        self.error = error
        self.delete_calls = []
    def delete_message(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.error:
            raise self.error
        self.deleted.append(kwargs)


def order(status="PENDING", generation=2, **extra):
    item = {"order_id": "order-123", "payload_hash": HASH,
            "recovery_generation": generation, "order_status": status}
    if status == "PENDING":
        item["enqueue_state"] = "ENQUEUED"
    item.update(extra)
    return item


def dlq_body(generation=2, payload_hash=HASH):
    return {"body": json.dumps({"order_id": "order-123", "payload_hash": payload_hash,
                                "recovery_generation": generation, "version": 1})}


class TestDlqRecovery(unittest.TestCase):
    def test_terminal_order_is_safe_to_acknowledge(self):
        repo, publisher = FakeRepository(order("COMPLETED")), FakePublisher()
        result = process_dlq_message(dlq_body(), repo, publisher, now=lambda: 100)
        self.assertEqual(result.classification, "TERMINAL")
        self.assertTrue(result.acknowledge_dlq_message)
        self.assertEqual(publisher.calls, [])

    def test_operator_helper_deletes_only_after_safe_terminal_classification(self):
        queue = FakeDlqClient()
        record = {**dlq_body(), "receiptHandle": "receipt-1"}
        safe = operator_redrive_record(record, FakeRepository(order("FAILED")), FakePublisher(),
                                       queue, "dlq-url", operator_id="operator-7",
                                       approved=True, now=lambda: 100)
        self.assertTrue(safe.acknowledge_dlq_message)
        self.assertEqual(queue.deleted, [{"QueueUrl": "dlq-url", "ReceiptHandle": "receipt-1"}])

        queue.deleted.clear()
        mismatched = order("PENDING")
        mismatched["payload_hash"] = "c" * 64
        unsafe = operator_redrive_record(record, FakeRepository(mismatched), FakePublisher(),
                                         queue, "dlq-url", operator_id="operator-7",
                                         approved=True, now=lambda: 100)
        self.assertFalse(unsafe.acknowledge_dlq_message)
        self.assertEqual(queue.deleted, [])

    def test_failed_dlq_delete_is_unresolved_and_terminal_redelivery_is_safe(self):
        record = {**dlq_body(), "receiptHandle": "receipt-delete-fails"}
        repo = FakeRepository(order("COMPLETED"))
        queue = FakeDlqClient(error=TimeoutError("delete response lost"))
        publisher = FakePublisher()

        first = operator_redrive_record(
            record, repo, publisher, queue, "dlq-url", operator_id="operator-7",
            approved=True, now=lambda: 100,
        )
        self.assertEqual(first.classification, "DLQ_DELETE_UNCERTAIN")
        self.assertFalse(first.acknowledge_dlq_message)
        self.assertEqual(queue.delete_calls, [{
            "QueueUrl": "dlq-url", "ReceiptHandle": "receipt-delete-fails",
        }])
        self.assertEqual(queue.deleted, [])

        # The same message can be investigated again: terminal state is safe,
        # so a later successful delete attempt does not repeat publication.
        queue.error = None
        second = operator_redrive_record(
            record, repo, publisher, queue, "dlq-url", operator_id="operator-7",
            approved=True, now=lambda: 101,
        )
        self.assertEqual(second.classification, "TERMINAL")
        self.assertTrue(second.acknowledge_dlq_message)
        self.assertEqual(len(queue.delete_calls), 2)
        self.assertEqual(len(queue.deleted), 1)
        self.assertEqual(publisher.calls, [])

    def test_stale_generation_is_safe_to_acknowledge(self):
        repo, publisher = FakeRepository(order("PENDING", generation=3,
                                               enqueue_state="ENQUEUED")), FakePublisher()
        result = process_dlq_message(dlq_body(generation=2), repo, publisher, now=lambda: 100)
        self.assertEqual(result.classification, "STALE_GENERATION")
        self.assertTrue(result.acknowledge_dlq_message)

    def test_newer_pending_needs_enqueue_is_not_mistaken_for_safely_stale(self):
        repo = FakeRepository(order("PENDING", generation=5, enqueue_state="NEEDS_ENQUEUE"))
        result = process_dlq_message(dlq_body(generation=2), repo, FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "NEWER_RECOVERY_PENDING")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_payload_mismatch_is_retained_for_investigation(self):
        repo, publisher = FakeRepository(order("PENDING")), FakePublisher()
        result = process_dlq_message(dlq_body(payload_hash="c" * 64), repo, publisher, now=lambda: 100)
        self.assertEqual(result.classification, "PAYLOAD_MISMATCH")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_future_generation_is_retained(self):
        repo, publisher = FakeRepository(order("PENDING", generation=2)), FakePublisher()
        result = process_dlq_message(dlq_body(generation=3), repo, publisher, now=lambda: 100)
        self.assertEqual(result.classification, "FUTURE_GENERATION")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_missing_order_is_retained(self):
        result = process_dlq_message(dlq_body(), FakeRepository(None), FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "MISSING_ORDER")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_active_lease_is_not_marked_exhausted(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=120))
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "ACTIVE_LEASE")
        self.assertEqual(repo.exhaust_calls, 0)

    def test_retry_exhausted_record_with_unexpired_lease_is_not_redriven(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED", lease_owner="worker-a", lease_until=120))
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100,
                                     operator_id="operator-7", approved=True)
        self.assertEqual(result.classification, "ACTIVE_LEASE")
        self.assertEqual(repo.redrive_calls, 0)

    def test_corrupt_order_status_is_retained(self):
        repo = FakeRepository(order("UNKNOWN"))
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100,
                                     operator_id="operator-7", approved=True)
        self.assertEqual(result.classification, "CORRUPT_ORDER")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_first_triage_conditionally_marks_expired_order_exhausted(self):
        repo = FakeRepository(order("PROCESSING", lease_owner="worker-a", lease_until=100))
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "RETRY_EXHAUSTED_READY")
        self.assertFalse(result.acknowledge_dlq_message)
        self.assertEqual(repo.item["order_status"], "RETRY_EXHAUSTED")
        self.assertEqual(repo.redrive_calls, 0)

    def test_approved_redrive_bumps_once_sends_new_generation_and_marks_enqueued(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED"))
        publisher = FakePublisher()
        result = process_dlq_message(dlq_body(), repo, publisher, now=lambda: 100,
                                     operator_id="operator-7", approved=True)
        self.assertEqual(result.classification, "REDRIVEN")
        self.assertTrue(result.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 3)
        self.assertEqual(repo.item["enqueue_state"], "ENQUEUED")
        self.assertEqual(publisher.calls[0]["recovery_generation"], 3)
        self.assertEqual(repo.redrive_calls, 1)

        repeated = process_dlq_message(dlq_body(), repo, publisher, now=lambda: 101,
                                       operator_id="operator-7", approved=True)
        self.assertEqual(repeated.classification, "STALE_GENERATION")
        self.assertTrue(repeated.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 3)
        self.assertEqual(repo.redrive_calls, 1)
        self.assertEqual(len(publisher.calls), 1)

    def test_concurrent_initial_redrive_loser_does_not_increment_generation_again(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED"))
        repo.redrive_result = False

        def winner_began_redrive(fake):
            fake.item.update(order_status="PENDING", enqueue_state="NEEDS_ENQUEUE",
                             recovery_generation=3, attempt_count=0)

        repo.on_redrive = winner_began_redrive
        publisher = FakePublisher()
        result = process_dlq_message(dlq_body(), repo, publisher, now=lambda: 100,
                                     operator_id="operator-7", approved=True)
        self.assertEqual(result.classification, "STATE_CHANGED")
        self.assertFalse(result.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 3)
        self.assertEqual(repo.redrive_calls, 1)
        self.assertEqual(publisher.calls, [])

        retry = process_dlq_message(dlq_body(), repo, publisher, now=lambda: 101,
                                    operator_id="operator-7", approved=True,
                                    retry_pending_send=True)
        self.assertEqual(retry.classification, "REDRIVEN")
        self.assertEqual(repo.item["recovery_generation"], 3)
        self.assertEqual(publisher.calls[0]["recovery_generation"], 3)

    def test_send_error_keeps_dlq_and_generation_for_explicit_same_generation_retry(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED"))
        first_publisher = FakePublisher(error=TimeoutError("response lost"))
        first_dlq = FakeDlqClient()
        first = operator_redrive_record(
            {**dlq_body(), "receiptHandle": "receipt-ambiguous"}, repo,
            first_publisher, first_dlq, "dlq-url", now=lambda: 100,
            operator_id="operator-7", approved=True,
        )
        self.assertEqual(first.classification, "SEND_UNCERTAIN")
        self.assertFalse(first.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 3)
        self.assertEqual(first_dlq.deleted, [])

        no_retry = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 101,
                                       operator_id="operator-7", approved=True)
        self.assertEqual(no_retry.classification, "PENDING_REDRIVE_SEND")
        self.assertFalse(no_retry.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 3)

        retry_pub = FakePublisher()
        retried = process_dlq_message(dlq_body(), repo, retry_pub, now=lambda: 102,
                                      operator_id="operator-7", approved=True,
                                      retry_pending_send=True)
        self.assertEqual(retried.classification, "REDRIVEN")
        self.assertEqual(retry_pub.calls[0]["recovery_generation"], 3)
        self.assertEqual(repo.item["recovery_generation"], 3)

    def test_fast_worker_terminal_transition_does_not_leave_dlq_record_stuck(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED"))
        def worker_finishes(fake):
            fake.item.update(order_status="COMPLETED")
        repo.on_mark = worker_finishes
        repo.mark_result = False  # Simulate harmless conditional marker race.
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100,
                                     operator_id="operator-7", approved=True)
        self.assertEqual(result.classification, "REDRIVEN")
        self.assertTrue(result.acknowledge_dlq_message)

    def test_newer_pending_needs_enqueue_after_marker_race_retains_dlq_message(self):
        repo = FakeRepository(order("RETRY_EXHAUSTED"))
        repo.mark_result = False

        def newer_redrive_started(fake):
            fake.item.update(order_status="PENDING", enqueue_state="NEEDS_ENQUEUE",
                             recovery_generation=4)

        repo.on_mark = newer_redrive_started
        dlq = FakeDlqClient()
        record = {**dlq_body(), "receiptHandle": "receipt-uncertain"}
        result = operator_redrive_record(
            record, repo, FakePublisher(), dlq, "dlq-url",
            operator_id="operator-7", approved=True, now=lambda: 100,
        )
        self.assertEqual(result.classification, "ENQUEUE_STATE_UNCERTAIN")
        self.assertFalse(result.acknowledge_dlq_message)
        self.assertEqual(repo.item["recovery_generation"], 4)
        self.assertEqual(repo.item["enqueue_state"], "NEEDS_ENQUEUE")
        self.assertEqual(dlq.deleted, [])

    def test_newer_generation_with_missing_or_invalid_marker_retains_dlq_message(self):
        for marker in (None, "BROKEN"):
            with self.subTest(marker=marker):
                repo = FakeRepository(order("RETRY_EXHAUSTED"))
                repo.mark_result = False

                def newer_state(fake):
                    fake.item.update(order_status="PENDING", recovery_generation=4)
                    if marker is None:
                        fake.item.pop("enqueue_state", None)
                    else:
                        fake.item["enqueue_state"] = marker

                repo.on_mark = newer_state
                dlq = FakeDlqClient()
                record = {**dlq_body(), "receiptHandle": "receipt-invalid"}
                result = operator_redrive_record(
                    record, repo, FakePublisher(), dlq, "dlq-url",
                    operator_id="operator-7", approved=True, now=lambda: 100,
                )
                self.assertEqual(result.classification, "ENQUEUE_STATE_UNCERTAIN")
                self.assertFalse(result.acknowledge_dlq_message)
                self.assertEqual(dlq.deleted, [])

    def test_missing_or_malformed_reread_after_send_retains_dlq_message(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                repo = FakeRepository(order("RETRY_EXHAUSTED"))
                repo.mark_result = False
                if malformed:
                    def malformed_newer_state(fake):
                        fake.item.update(order_status="PENDING", enqueue_state="ENQUEUED",
                                         recovery_generation=True)
                    repo.on_mark = malformed_newer_state
                else:
                    original_get_order = repo.get_order
                    reads = 0

                    def disappear_after_send(order_id):
                        nonlocal reads
                        reads += 1
                        return None if reads > 1 else original_get_order(order_id)

                    repo.get_order = disappear_after_send
                dlq = FakeDlqClient()
                record = {**dlq_body(), "receiptHandle": "receipt-missing-state"}
                result = operator_redrive_record(
                    record, repo, FakePublisher(), dlq, "dlq-url",
                    operator_id="operator-7", approved=True, now=lambda: 100,
                )
                self.assertEqual(result.classification, "ENQUEUE_STATE_UNCERTAIN")
                self.assertFalse(result.acknowledge_dlq_message)
                self.assertEqual(dlq.deleted, [])

    def test_newer_enqueued_generation_with_inconsistent_lease_retains_dlq_message(self):
        for status, lease_fields in (("PENDING", {"lease_until": 120}),
                                     ("PROCESSING", {"lease_until": 120})):
            with self.subTest(status=status):
                repo = FakeRepository(order("RETRY_EXHAUSTED"))
                repo.mark_result = False

                def newer_inconsistent_state(fake):
                    fake.item.update(order_status=status, enqueue_state="ENQUEUED",
                                     recovery_generation=4, **lease_fields)

                repo.on_mark = newer_inconsistent_state
                dlq = FakeDlqClient()
                record = {**dlq_body(), "receiptHandle": "receipt-inconsistent-lease"}
                result = operator_redrive_record(
                    record, repo, FakePublisher(), dlq, "dlq-url",
                    operator_id="operator-7", approved=True, now=lambda: 100,
                )
                self.assertEqual(result.classification, "ENQUEUE_STATE_UNCERTAIN")
                self.assertFalse(result.acknowledge_dlq_message)
                self.assertEqual(dlq.deleted, [])

    def test_newer_enqueued_or_terminal_generation_safely_supersedes_redrive(self):
        for status, marker, lease_fields in (
            ("PENDING", "ENQUEUED", {}),
            ("PROCESSING", "ENQUEUED", {"lease_owner": "worker-a", "lease_until": 120}),
            ("COMPLETED", None, {}),
        ):
            with self.subTest(status=status):
                repo = FakeRepository(order("RETRY_EXHAUSTED"))
                repo.mark_result = False

                def newer_resolved_state(fake):
                    fake.item.update(order_status=status, recovery_generation=4,
                                     **lease_fields)
                    if marker is None:
                        fake.item.pop("enqueue_state", None)
                    else:
                        fake.item["enqueue_state"] = marker

                repo.on_mark = newer_resolved_state
                dlq = FakeDlqClient()
                record = {**dlq_body(), "receiptHandle": "receipt-resolved"}
                result = operator_redrive_record(
                    record, repo, FakePublisher(), dlq, "dlq-url",
                    operator_id="operator-7", approved=True, now=lambda: 100,
                )
                self.assertEqual(result.classification, "SUPERSEDED")
                self.assertTrue(result.acknowledge_dlq_message)
                self.assertEqual(len(dlq.deleted), 1)

    def test_failed_exhaustion_condition_rereads_active_lease(self):
        repo = FakeRepository(order("PENDING"))
        repo.exhaust_result = False
        original_get = repo.get_order
        def get_order(order_id):
            repo.item.update(order_status="PROCESSING", lease_owner="worker-b", lease_until=130)
            return original_get(order_id)
        repo.get_order = get_order
        result = process_dlq_message(dlq_body(), repo, FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "ACTIVE_LEASE")
        self.assertFalse(result.acknowledge_dlq_message)

    def test_invalid_generation_is_retained(self):
        body = {"body": json.dumps({"order_id": "order-123", "payload_hash": HASH,
                                    "recovery_generation": True})}
        result = process_dlq_message(body, FakeRepository(order()), FakePublisher(), now=lambda: 100)
        self.assertEqual(result.classification, "INVALID_MESSAGE")
        self.assertFalse(result.acknowledge_dlq_message)


if __name__ == "__main__":
    unittest.main()
