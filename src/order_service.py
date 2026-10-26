"""Order domain model, state machine, and local in-process service layer.

This module defines:
- OrderStatus: Allowed order states.
- OrderRecord: Immutable-like data record for an order.
- Custom domain exceptions for duplicate orders, not-found, and invalid transitions.
- OrderService: A single-class service that manages order lifecycle.

IMPORTANT LOCAL SIMULATION LIMITATION
--------------------------------------
The idempotency check in this module uses a plain Python dict as the store.
This is intentionally limited to single-threaded, in-process use.

It is NOT equivalent to DynamoDB's atomic conditional write
(attribute_not_exists) used in Stage 3. In a real concurrent environment:
  - Two simultaneous Lambda invocations could both pass the read-before-write
    check and create duplicate records.
  - This local implementation is sufficient for verifying sequential logic
    correctness in unit tests only.

Do not use OrderService's local store as evidence of distributed or
concurrent idempotency. That proof requires a real DynamoDB conditional write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order status enumeration
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    """Allowed order lifecycle statuses.

    Using str as a mixin makes the enum JSON-serializable as a plain string.
    """
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Allowed state transitions (adjacency map)
# ---------------------------------------------------------------------------

# Maps each status to the set of statuses it may transition INTO.
# Any transition not present in this map is illegal and must raise
# InvalidStateTransitionError.
ALLOWED_TRANSITIONS: Dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.PROCESSING}),
    OrderStatus.PROCESSING: frozenset({
        OrderStatus.COMPLETED,
        OrderStatus.FAILED,
        OrderStatus.PENDING,   # Transient failure: SQS re-queues → retried
    }),
    OrderStatus.COMPLETED: frozenset(),   # Terminal state; no further transitions
    OrderStatus.FAILED: frozenset(),      # Terminal state; no further transitions
}


# ---------------------------------------------------------------------------
# Custom domain exceptions
# ---------------------------------------------------------------------------

class DuplicateOrderError(Exception):
    """Raised when create_order is called with an order_id that already exists.

    In Stage 3, this maps to a DynamoDB ConditionalCheckFailedException.
    Locally it is checked via dict key presence (not concurrency-safe).
    """


class OrderNotFoundError(Exception):
    """Raised when an operation references an order_id that does not exist."""


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition is not permitted.

    Example: attempting to move from COMPLETED → PROCESSING.
    """


# ---------------------------------------------------------------------------
# Order data record
# ---------------------------------------------------------------------------

@dataclass
class OrderRecord:
    """Represents a single order and its current lifecycle state.

    Fields are set at creation and updated only through OrderService methods
    to enforce the state machine. Direct attribute mutation is discouraged.
    """

    order_id: str
    customer_id: str
    items: List[dict]
    total_amount: int | float | Decimal
    currency: str
    status: OrderStatus
    created_at: str
    updated_at: str
    error_detail: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        """Serialize the record to a plain dictionary.

        Converts OrderStatus enum to its string value for JSON compatibility.
        """
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "items": self.items,
            "total_amount": self.total_amount,
            "currency": self.currency,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_detail": self.error_detail,
        }


# ---------------------------------------------------------------------------
# OrderService: single-threaded, in-process order lifecycle manager
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return current UTC time as an ISO 8601 string with timezone suffix."""
    return datetime.now(timezone.utc).isoformat()


class OrderService:
    """Manages the order lifecycle against a local in-process store.

    This class is designed for local unit testing and Stage 2 validation.
    It is explicitly NOT safe for concurrent use and does not replace
    the DynamoDB-backed implementation required in Stage 3.

    Each instance maintains its own isolated store, making test isolation
    straightforward: create a new OrderService instance per test case.

    Args:
        _clock: Optional callable returning an ISO 8601 UTC timestamp string.
            Defaults to the real wall clock (_utc_now). Tests may inject a
            deterministic fake to verify timestamp behavior without sleep().
            Production callers should never supply this argument.
    """

    def __init__(self, _clock: Callable[[], str] = _utc_now) -> None:
        # Keyed by order_id → OrderRecord
        # WARNING: In-process only; lost on process exit; not thread-safe.
        self._store: Dict[str, OrderRecord] = {}
        self._clock = _clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_order(self, validated_payload: dict) -> OrderRecord:
        """Create a new order in PENDING status.

        Args:
            validated_payload: A dict that has already passed
                validate_order_payload(). Must contain order_id,
                customer_id, items, total_amount.

        Returns:
            The newly created OrderRecord.

        Raises:
            DuplicateOrderError: If an order with the same order_id
                already exists in the store.
                NOTE: This check is not concurrency-safe locally.
                In Stage 3, DynamoDB attribute_not_exists() provides
                the atomic guarantee.
        """
        order_id: str = validated_payload["order_id"]

        if order_id in self._store:
            logger.warning("Duplicate order submission detected: %s", order_id)
            raise DuplicateOrderError(
                f"Order '{order_id}' already exists. Duplicate submissions are not permitted."
            )

        # Explicit None check rather than `or` to correctly handle 0.0 (falsy but valid
        # after validation would have already rejected it as <= 0).
        raw_total = validated_payload.get("total_amount")
        if raw_total is None:
            raw_total = validated_payload.get("totalAmount")

        now = self._clock()
        record = OrderRecord(
            order_id=order_id,
            customer_id=validated_payload["customer_id"],
            items=list(validated_payload["items"]),
            # Preserve Decimal inputs from decimal-aware JSON parsing; legacy
            # callers that pass floats retain their existing value and type.
            total_amount=raw_total,  # type: ignore[arg-type]
            currency=validated_payload.get("currency", "USD"),
            status=OrderStatus.PENDING,
            created_at=now,
            updated_at=now,
            error_detail=None,
        )
        self._store[order_id] = record
        logger.info("Order %s created with status PENDING.", order_id)
        return record

    def get_order(self, order_id: str) -> OrderRecord:
        """Retrieve an order by ID.

        Args:
            order_id: The unique identifier of the order.

        Returns:
            The matching OrderRecord.

        Raises:
            OrderNotFoundError: If no order with that ID exists.
        """
        record = self._store.get(order_id)
        if record is None:
            raise OrderNotFoundError(f"Order '{order_id}' was not found.")
        return record

    def begin_processing(self, order_id: str) -> OrderRecord:
        """Transition an order from PENDING to PROCESSING.

        Called when a worker Lambda picks up the SQS message.

        Args:
            order_id: The order to transition.

        Returns:
            The updated OrderRecord.

        Raises:
            OrderNotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the order is not in PENDING status.
        """
        return self._transition(order_id, OrderStatus.PROCESSING)

    def complete_order(self, order_id: str) -> OrderRecord:
        """Transition an order from PROCESSING to COMPLETED.

        Args:
            order_id: The order to transition.

        Returns:
            The updated OrderRecord.

        Raises:
            OrderNotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the order is not in PROCESSING status.
        """
        return self._transition(order_id, OrderStatus.COMPLETED)

    def fail_order(self, order_id: str, reason: str) -> OrderRecord:
        """Transition an order from PROCESSING to FAILED.

        Sets error_detail to reason to aid diagnostics. This corresponds to
        a permanent processing failure or DLQ exhaustion in Stage 3.

        Args:
            order_id: The order to transition.
            reason: Human-readable description of the failure cause.

        Returns:
            The updated OrderRecord.

        Raises:
            OrderNotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the order is not in PROCESSING status.
        """
        record = self._transition(order_id, OrderStatus.FAILED)
        record.error_detail = reason
        logger.warning("Order %s marked FAILED code=ORDER_FAILED.", order_id)
        return record

    def retry_order(self, order_id: str) -> OrderRecord:
        """Transition an order from PROCESSING back to PENDING for retry.

        Represents a transient processing failure where SQS will re-deliver
        the message. In Stage 3 this happens automatically when the worker
        Lambda raises an exception without deleting the SQS message.

        Args:
            order_id: The order to retry.

        Returns:
            The updated OrderRecord.

        Raises:
            OrderNotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the order is not in PROCESSING status.
        """
        return self._transition(order_id, OrderStatus.PENDING)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _transition(self, order_id: str, target_status: OrderStatus) -> OrderRecord:
        """Apply a status transition after validating it is allowed.

        Args:
            order_id: The order to update.
            target_status: The desired new status.

        Returns:
            The updated OrderRecord.

        Raises:
            OrderNotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the transition is not permitted.
        """
        record = self.get_order(order_id)
        allowed = ALLOWED_TRANSITIONS.get(record.status, frozenset())

        if target_status not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot transition order '{order_id}' from {record.status.value} "
                f"to {target_status.value}. "
                f"Allowed targets from {record.status.value}: "
                f"{[s.value for s in allowed] if allowed else 'none (terminal state)'}."
            )

        record.status = target_status
        record.updated_at = self._clock()
        logger.info(
            "Order %s transitioned to %s.", order_id, target_status.value
        )
        return record
