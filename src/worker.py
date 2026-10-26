"""SQS worker with conditional DynamoDB leases and retry-safe outcomes.

For an SQS event source mapping, a successful Lambda return lets the mapping
delete the batch. Raising an exception leaves the batch for redelivery after
the queue visibility timeout. This handler therefore assumes batch size one.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from dynamo_order_repository import DynamoOrderRepository
from business_processing import InvalidStoredOrderError, process_order_locally

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

LEASE_SECONDS = int(os.environ.get("WORKER_LEASE_SECONDS", "20"))
if not 11 <= LEASE_SECONDS <= 60:
    raise ValueError("WORKER_LEASE_SECONDS must be between 11 and 60")
_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_TERMINAL = {"COMPLETED", "FAILED", "RETRY_EXHAUSTED"}


class RetryableDeliveryError(RuntimeError):
    """The SQS record must not be acknowledged; retry or investigate it."""


class PermanentProcessingError(Exception):
    """A known business rejection that can be recorded as FAILED."""

    def __init__(self, error_code: str) -> None:
        safe_code = error_code if isinstance(error_code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", error_code) else "ERR_PERMANENT_FAILURE"
        super().__init__(safe_code)
        self.error_code = safe_code


def _is_generation(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return (isinstance(value, Decimal) and value.is_finite()
            and value >= 0 and value == value.to_integral_value())


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH_PATTERN.fullmatch(value))


def _parse_message(record: Dict[str, Any]) -> Dict[str, Any]:
    body = record.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RetryableDeliveryError("Malformed SQS message body") from exc
    if not isinstance(body, dict):
        raise RetryableDeliveryError("SQS message body must be an object")
    order_id = body.get("order_id")
    payload_hash = body.get("payload_hash")
    generation = body.get("recovery_generation")
    if not isinstance(order_id, str) or not order_id.strip():
        raise RetryableDeliveryError("Missing or invalid order_id")
    if not _valid_hash(payload_hash):
        raise RetryableDeliveryError("Missing or invalid payload_hash")
    if not _is_generation(generation):
        raise RetryableDeliveryError("Missing or invalid recovery_generation")
    return {"order_id": order_id, "payload_hash": payload_hash,
            "recovery_generation": generation}


def _classify_item(item: Optional[Dict[str, Any]], message: Dict[str, Any],
                   now: int) -> str:
    if item is None:
        raise RetryableDeliveryError("Order record is missing")
    generation = item.get("recovery_generation")
    if not _is_generation(generation):
        raise RetryableDeliveryError("Stored recovery_generation is invalid")
    stored_hash = item.get("payload_hash")
    if not _valid_hash(stored_hash):
        raise RetryableDeliveryError("Stored payload_hash is invalid")
    if stored_hash != message["payload_hash"]:
        raise RetryableDeliveryError("Message payload_hash does not match order")
    msg_generation = message["recovery_generation"]
    if msg_generation < generation:
        status = item.get("order_status")
        if status in _TERMINAL:
            return "TERMINAL"
        if status in {"PENDING", "PROCESSING"}:
            enqueue_state = item.get("enqueue_state")
            if enqueue_state == "ENQUEUED":
                return "STALE"
            if enqueue_state == "NEEDS_ENQUEUE":
                raise RetryableDeliveryError(
                    "Stale message cannot be acknowledged while current generation needs enqueue"
                )
        raise RetryableDeliveryError(
            "Stale message found an inconsistent or unsupported current order state"
        )
    if msg_generation > generation:
        raise RetryableDeliveryError("Message has a future recovery_generation")

    status = item.get("order_status")
    if status in _TERMINAL:
        return "TERMINAL"
    if status == "PENDING":
        owner, until = item.get("lease_owner"), item.get("lease_until")
        if owner is not None or until is not None:
            if not isinstance(owner, str) or not owner.strip():
                raise RetryableDeliveryError("PENDING order has invalid lease_owner")
            if (isinstance(until, bool) or not isinstance(until, (int, float, Decimal))
                    or not math.isfinite(float(until))):
                raise RetryableDeliveryError("PENDING order has invalid lease_until")
            return "ELIGIBLE" if until <= now else "ACTIVE_LEASE"
        return "ELIGIBLE"
    if status == "PROCESSING":
        owner = item.get("lease_owner")
        until = item.get("lease_until")
        if not isinstance(owner, str) or not owner.strip():
            raise RetryableDeliveryError("PROCESSING order has invalid lease_owner")
        if (isinstance(until, bool) or not isinstance(until, (int, float, Decimal))
                or not math.isfinite(float(until))):
            raise RetryableDeliveryError("PROCESSING order has invalid lease_until")
        return "ELIGIBLE" if until <= now else "ACTIVE_LEASE"
    raise RetryableDeliveryError("Order has unknown or unsupported state")


def process_record(
    record: Dict[str, Any], repository: Any,
    process_order: Callable[[Dict[str, Any]], None],
    *, now: Callable[[], int] = lambda: int(time.time()),
) -> str:
    """Process one SQS record; return only for outcomes safe to acknowledge."""
    message = _parse_message(record)
    order_id = message["order_id"]
    item = repository.get_order(order_id)  # adapter uses ConsistentRead=True
    classification = _classify_item(item, message, now())
    if classification in {"STALE", "TERMINAL"}:
        logger.info("Acknowledging %s worker duplicate for order %s", classification.lower(), order_id)
        return classification
    if classification == "ACTIVE_LEASE":
        raise RetryableDeliveryError("Another worker holds a valid active lease")

    owner = str(uuid.uuid4())
    acquired = repository.acquire_lease(
        order_id, message["payload_hash"], message["recovery_generation"],
        owner, now(), LEASE_SECONDS,
    )
    if not acquired:
        # The atomic update lost a race. Read again and classify the winner's
        # current state; never acknowledge an active lease or overwrite it.
        latest = repository.get_order(order_id)
        after_race = _classify_item(latest, message, now())
        if after_race in {"STALE", "TERMINAL"}:
            return after_race
        raise RetryableDeliveryError("Conditional lease acquisition lost a race")

    try:
        process_order(item or {})
    except (PermanentProcessingError, InvalidStoredOrderError) as exc:
        error_code = (exc.error_code if isinstance(exc, PermanentProcessingError)
                      else "INVALID_STORED_ORDER")
        recorded = repository.fail_permanently(
            order_id, message["payload_hash"], message["recovery_generation"],
            owner, now(), error_code,
        )
        if not recorded:
            raise RetryableDeliveryError("Lease ownership changed before failure could be recorded") from exc
        return "FAILED"
    except Exception:
        # Do not release/reset the lease: it expires naturally, while the
        # failed Lambda invocation makes this SQS record retryable.
        logger.warning("Transient worker failure for order %s", order_id)
        raise

    completed = repository.complete(
        order_id, message["payload_hash"], message["recovery_generation"],
        owner, now(),
    )
    if not completed:
        latest = repository.get_order(order_id)
        after_completion_race = _classify_item(latest, message, now())
        if after_completion_race == "TERMINAL":
            return after_completion_race
        raise RetryableDeliveryError("Lease ownership changed before completion was recorded")
    return "COMPLETED"


def _default_processor(order: Dict[str, Any]) -> None:
    """Run the deterministic local simulation; no external business action."""
    result = process_order_locally(order)
    logger.info("Local business simulation completed for order %s: %s",
                result["order_id"], result["outcome"])


def lambda_handler(event: Dict[str, Any], context: Any,
                   repository: Optional[Any] = None,
                   process_order: Optional[Callable[[Dict[str, Any]], None]] = None,
                   now: Callable[[], int] = lambda: int(time.time())) -> Dict[str, Any]:
    """Lambda entry point; successful return acknowledges the SQS batch."""
    del context
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or not records:
        raise RetryableDeliveryError("SQS event must contain Records")
    if len(records) != 1:
        raise RetryableDeliveryError("Configure SQS event source batch size to one")
    if repository is None:
        import boto3  # Available in the AWS Lambda Python runtime.
        table_name = os.environ["ORDERS_TABLE"]
        repository = DynamoOrderRepository(boto3.resource("dynamodb").Table(table_name))
    process_record(records[0], repository, process_order or _default_processor, now=now)
    return {"batchItemFailures": []}
