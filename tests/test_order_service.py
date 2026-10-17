"""Unit tests for src/order_service.py.

Tests cover:
  - Valid order creation and initial state
  - Full happy-path status progression: PENDING → PROCESSING → COMPLETED
  - Duplicate order submission (DuplicateOrderError)
  - All disallowed state transitions (InvalidStateTransitionError)
  - Processing failures and resulting FAILED status
  - Retry behaviour: PROCESSING → PENDING
  - Order not found (OrderNotFoundError)
  - Test isolation (each test uses its own OrderService instance)
  - Repeated calls and idempotency limitations (clearly labelled)

IMPORTANT: These tests verify sequential, single-threaded correctness only.
They do NOT prove concurrent or distributed idempotency.
See docs/DECISIONS.md (DEC-006) for the explicit limitation statement.

Uses Python's built-in unittest module. Compatible with Python 3.12.
"""

from __future__ import annotations

import os
import sys
import unittest

# Ensure src directory is on sys.path for direct module import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from order_service import (
    DuplicateOrderError,
    InvalidStateTransitionError,
    OrderNotFoundError,
    OrderRecord,
    OrderService,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Shared fixture factory
# ---------------------------------------------------------------------------

def make_valid_payload(order_id: str = "ord-2026-test-0001") -> dict:
    """Return a validated-format order payload for use in tests.

    This bypasses the HTTP validation layer intentionally: order_service
    assumes the payload has already been validated by validate_order_payload.
    """
    return {
        "order_id": order_id,
        "customer_id": "cust-5501",
        "items": [
            {"item_id": "item-101", "quantity": 2, "price": 19.99},
            {"item_id": "item-102", "quantity": 1, "price": 10.00},
        ],
        "total_amount": 49.98,
        "currency": "USD",
    }


# ---------------------------------------------------------------------------
# Test: Order creation
# ---------------------------------------------------------------------------

class TestCreateOrder(unittest.TestCase):
    """Tests for OrderService.create_order."""

    def setUp(self) -> None:
        """Fresh service instance per test for full isolation."""
        self.service = OrderService()
        self.payload = make_valid_payload()

    def test_create_returns_order_record(self) -> None:
        """create_order returns an OrderRecord instance."""
        record = self.service.create_order(self.payload)
        self.assertIsInstance(record, OrderRecord)

    def test_initial_status_is_pending(self) -> None:
        """Newly created orders must have PENDING status."""
        record = self.service.create_order(self.payload)
        self.assertEqual(record.status, OrderStatus.PENDING)

    def test_created_order_id_matches_payload(self) -> None:
        """order_id on the record must match the payload."""
        record = self.service.create_order(self.payload)
        self.assertEqual(record.order_id, self.payload["order_id"])

    def test_customer_id_preserved(self) -> None:
        """customer_id on the record must match the payload."""
        record = self.service.create_order(self.payload)
        self.assertEqual(record.customer_id, self.payload["customer_id"])

    def test_total_amount_preserved(self) -> None:
        """total_amount on the record must match the payload."""
        record = self.service.create_order(self.payload)
        self.assertAlmostEqual(record.total_amount, self.payload["total_amount"], places=4)

    def test_currency_preserved(self) -> None:
        """currency on the record must match the payload."""
        record = self.service.create_order(self.payload)
        self.assertEqual(record.currency, "USD")

    def test_default_currency_is_usd(self) -> None:
        """If currency is omitted from payload, default must be 'USD'."""
        payload = dict(self.payload)
        del payload["currency"]
        record = self.service.create_order(payload)
        self.assertEqual(record.currency, "USD")

    def test_created_at_and_updated_at_set(self) -> None:
        """created_at and updated_at must be set to non-empty ISO strings."""
        record = self.service.create_order(self.payload)
        self.assertTrue(record.created_at)
        self.assertTrue(record.updated_at)

    def test_error_detail_is_none_initially(self) -> None:
        """A new order must not carry an error_detail."""
        record = self.service.create_order(self.payload)
        self.assertIsNone(record.error_detail)

    def test_to_dict_returns_expected_keys(self) -> None:
        """to_dict must include all expected fields."""
        record = self.service.create_order(self.payload)
        d = record.to_dict()
        for key in ("order_id", "customer_id", "items", "total_amount",
                    "currency", "status", "created_at", "updated_at", "error_detail"):
            with self.subTest(key=key):
                self.assertIn(key, d)

    def test_to_dict_status_is_string(self) -> None:
        """to_dict must serialise status as a plain string, not the enum object."""
        record = self.service.create_order(self.payload)
        self.assertIsInstance(record.to_dict()["status"], str)
        self.assertEqual(record.to_dict()["status"], "PENDING")


# ---------------------------------------------------------------------------
# Test: Duplicate order submission
# ---------------------------------------------------------------------------

class TestDuplicateOrder(unittest.TestCase):
    """Tests for idempotency: duplicate order detection."""

    def setUp(self) -> None:
        self.service = OrderService()
        self.payload = make_valid_payload()

    def test_duplicate_raises_duplicate_order_error(self) -> None:
        """Submitting the same order_id twice must raise DuplicateOrderError.

        NOTE: This proves sequential duplicate detection only.
        Concurrent race conditions are not addressed at this layer.
        """
        self.service.create_order(self.payload)
        with self.assertRaises(DuplicateOrderError):
            self.service.create_order(self.payload)

    def test_original_order_preserved_after_duplicate_attempt(self) -> None:
        """The original order record must not be modified by a failed duplicate attempt."""
        original = self.service.create_order(self.payload)
        created_at_before = original.created_at

        try:
            self.service.create_order(self.payload)
        except DuplicateOrderError:
            pass

        retrieved = self.service.get_order(self.payload["order_id"])
        self.assertEqual(retrieved.created_at, created_at_before)
        self.assertEqual(retrieved.status, OrderStatus.PENDING)

    def test_different_order_ids_are_independent(self) -> None:
        """Two orders with different order_ids must both be creatable."""
        payload_a = make_valid_payload("ord-2026-test-aaaa")
        payload_b = make_valid_payload("ord-2026-test-bbbb")
        record_a = self.service.create_order(payload_a)
        record_b = self.service.create_order(payload_b)
        self.assertNotEqual(record_a.order_id, record_b.order_id)


# ---------------------------------------------------------------------------
# Test: Happy-path status progression
# ---------------------------------------------------------------------------

class TestStatusProgression(unittest.TestCase):
    """Tests for the full lifecycle: PENDING → PROCESSING → COMPLETED."""

    def setUp(self) -> None:
        self.service = OrderService()
        self.payload = make_valid_payload()
        self.service.create_order(self.payload)
        self.order_id = self.payload["order_id"]

    def test_begin_processing_transitions_to_processing(self) -> None:
        """PENDING → PROCESSING must succeed."""
        record = self.service.begin_processing(self.order_id)
        self.assertEqual(record.status, OrderStatus.PROCESSING)

    def test_complete_order_transitions_to_completed(self) -> None:
        """PROCESSING → COMPLETED must succeed."""
        self.service.begin_processing(self.order_id)
        record = self.service.complete_order(self.order_id)
        self.assertEqual(record.status, OrderStatus.COMPLETED)

    def test_updated_at_changes_on_transition(self) -> None:
        """updated_at must be refreshed with each state transition.

        Uses a deterministic FakeClock injected into OrderService so the test
        does not depend on wall-clock resolution or time.sleep().
        Each call to FakeClock advances by one tick, guaranteeing observable
        timestamp differences regardless of the OS or CI runner speed.
        """
        timestamps = [
            "2026-01-01T00:00:00+00:00",  # created_at / initial updated_at
            "2026-01-01T00:00:01+00:00",  # updated_at after begin_processing
        ]
        tick = iter(timestamps)
        service = OrderService(_clock=lambda: next(tick))

        payload = make_valid_payload("ord-2026-clock-test")
        service.create_order(payload)
        created = service.get_order("ord-2026-clock-test")
        original_updated_at = created.updated_at

        service.begin_processing("ord-2026-clock-test")
        after_processing = service.get_order("ord-2026-clock-test")

        # The intent: updated_at must change on transition.
        # assertNotEqual is the correct assertion; assertGreaterEqual
        # would pass even if the value did not change.
        self.assertNotEqual(after_processing.updated_at, original_updated_at)
        self.assertEqual(after_processing.updated_at, timestamps[1])

    def test_get_order_reflects_current_status(self) -> None:
        """get_order must return the live current status, not a stale snapshot."""
        self.service.begin_processing(self.order_id)
        record = self.service.get_order(self.order_id)
        self.assertEqual(record.status, OrderStatus.PROCESSING)

        self.service.complete_order(self.order_id)
        record = self.service.get_order(self.order_id)
        self.assertEqual(record.status, OrderStatus.COMPLETED)


# ---------------------------------------------------------------------------
# Test: Processing failure → FAILED
# ---------------------------------------------------------------------------

class TestFailOrder(unittest.TestCase):
    """Tests for fail_order: PROCESSING → FAILED."""

    def setUp(self) -> None:
        self.service = OrderService()
        self.payload = make_valid_payload()
        self.service.create_order(self.payload)
        self.order_id = self.payload["order_id"]
        self.service.begin_processing(self.order_id)

    def test_fail_order_sets_status_failed(self) -> None:
        """fail_order must set status to FAILED."""
        record = self.service.fail_order(self.order_id, "Card declined")
        self.assertEqual(record.status, OrderStatus.FAILED)

    def test_fail_order_records_error_detail(self) -> None:
        """fail_order must store the reason string in error_detail."""
        reason = "Inventory out of stock for item-101"
        record = self.service.fail_order(self.order_id, reason)
        self.assertEqual(record.error_detail, reason)

    def test_error_detail_visible_via_get_order(self) -> None:
        """error_detail must be persisted and retrievable via get_order."""
        reason = "Payment gateway timeout"
        self.service.fail_order(self.order_id, reason)
        record = self.service.get_order(self.order_id)
        self.assertEqual(record.error_detail, reason)


# ---------------------------------------------------------------------------
# Test: Retry behaviour (PROCESSING → PENDING)
# ---------------------------------------------------------------------------

class TestRetryOrder(unittest.TestCase):
    """Tests for retry_order: PROCESSING → PENDING transient failure path."""

    def setUp(self) -> None:
        self.service = OrderService()
        self.payload = make_valid_payload()
        self.service.create_order(self.payload)
        self.order_id = self.payload["order_id"]
        self.service.begin_processing(self.order_id)

    def test_retry_resets_status_to_pending(self) -> None:
        """retry_order must move status back to PENDING."""
        record = self.service.retry_order(self.order_id)
        self.assertEqual(record.status, OrderStatus.PENDING)

    def test_order_can_be_reprocessed_after_retry(self) -> None:
        """After retry to PENDING, begin_processing must succeed again."""
        self.service.retry_order(self.order_id)
        record = self.service.begin_processing(self.order_id)
        self.assertEqual(record.status, OrderStatus.PROCESSING)

    def test_full_retry_then_complete_cycle(self) -> None:
        """Full cycle with retry: PENDING→PROCESSING→PENDING→PROCESSING→COMPLETED."""
        self.service.retry_order(self.order_id)
        self.service.begin_processing(self.order_id)
        record = self.service.complete_order(self.order_id)
        self.assertEqual(record.status, OrderStatus.COMPLETED)


# ---------------------------------------------------------------------------
# Test: Invalid state transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions(unittest.TestCase):
    """Ensures all disallowed transitions raise InvalidStateTransitionError."""

    def setUp(self) -> None:
        self.service = OrderService()

    def _create(self, order_id: str) -> str:
        self.service.create_order(make_valid_payload(order_id))
        return order_id

    def test_pending_to_completed_is_invalid(self) -> None:
        """Cannot skip PROCESSING: PENDING → COMPLETED must be rejected."""
        oid = self._create("ord-2026-inv-0001")
        with self.assertRaises(InvalidStateTransitionError):
            self.service.complete_order(oid)

    def test_pending_to_failed_is_invalid(self) -> None:
        """Cannot skip PROCESSING: PENDING → FAILED must be rejected."""
        oid = self._create("ord-2026-inv-0002")
        with self.assertRaises(InvalidStateTransitionError):
            self.service.fail_order(oid, "test")

    def test_pending_to_pending_is_invalid(self) -> None:
        """Self-transition PENDING → PENDING must be rejected."""
        oid = self._create("ord-2026-inv-0003")
        with self.assertRaises(InvalidStateTransitionError):
            self.service.retry_order(oid)  # retry is only valid from PROCESSING

    def test_completed_to_processing_is_invalid(self) -> None:
        """COMPLETED is a terminal state: → PROCESSING must be rejected."""
        oid = self._create("ord-2026-inv-0004")
        self.service.begin_processing(oid)
        self.service.complete_order(oid)
        with self.assertRaises(InvalidStateTransitionError):
            self.service.begin_processing(oid)

    def test_completed_to_failed_is_invalid(self) -> None:
        """COMPLETED is a terminal state: → FAILED must be rejected."""
        oid = self._create("ord-2026-inv-0005")
        self.service.begin_processing(oid)
        self.service.complete_order(oid)
        with self.assertRaises(InvalidStateTransitionError):
            self.service.fail_order(oid, "test")

    def test_failed_to_processing_is_invalid(self) -> None:
        """FAILED is a terminal state: → PROCESSING must be rejected."""
        oid = self._create("ord-2026-inv-0006")
        self.service.begin_processing(oid)
        self.service.fail_order(oid, "test")
        with self.assertRaises(InvalidStateTransitionError):
            self.service.begin_processing(oid)

    def test_failed_to_completed_is_invalid(self) -> None:
        """FAILED is a terminal state: → COMPLETED must be rejected."""
        oid = self._create("ord-2026-inv-0007")
        self.service.begin_processing(oid)
        self.service.fail_order(oid, "test")
        with self.assertRaises(InvalidStateTransitionError):
            self.service.complete_order(oid)


# ---------------------------------------------------------------------------
# Test: Order not found
# ---------------------------------------------------------------------------

class TestOrderNotFound(unittest.TestCase):
    """Tests for OrderNotFoundError on missing order_ids."""

    def setUp(self) -> None:
        self.service = OrderService()

    def test_get_nonexistent_order_raises(self) -> None:
        """get_order on a missing ID must raise OrderNotFoundError."""
        with self.assertRaises(OrderNotFoundError):
            self.service.get_order("ord-does-not-exist")

    def test_begin_processing_nonexistent_raises(self) -> None:
        """begin_processing on a missing order must raise OrderNotFoundError."""
        with self.assertRaises(OrderNotFoundError):
            self.service.begin_processing("ord-does-not-exist")

    def test_complete_nonexistent_raises(self) -> None:
        """complete_order on a missing order must raise OrderNotFoundError."""
        with self.assertRaises(OrderNotFoundError):
            self.service.complete_order("ord-does-not-exist")

    def test_fail_nonexistent_raises(self) -> None:
        """fail_order on a missing order must raise OrderNotFoundError."""
        with self.assertRaises(OrderNotFoundError):
            self.service.fail_order("ord-does-not-exist", "reason")


# ---------------------------------------------------------------------------
# Test: Test isolation verification
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):
    """Verifies that each test's OrderService instance is fully independent."""

    def test_separate_instances_do_not_share_state(self) -> None:
        """Orders created in one service instance must not appear in another."""
        service_a = OrderService()
        service_b = OrderService()

        payload = make_valid_payload("ord-2026-iso-0001")
        service_a.create_order(payload)

        with self.assertRaises(OrderNotFoundError):
            service_b.get_order("ord-2026-iso-0001")


if __name__ == "__main__":
    unittest.main()
