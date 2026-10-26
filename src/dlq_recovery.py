"""Audited DLQ triage and generation-safe operator redrive workflow."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from dynamo_order_repository import DynamoOrderRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_TERMINAL = {"COMPLETED", "FAILED"}


@dataclass(frozen=True)
class RecoveryResult:
    classification: str
    acknowledge_dlq_message: bool
    detail: str
    new_generation: Optional[int] = None


class RecoveryNeedsOperator(RuntimeError):
    """The DLQ record needs investigation or explicit operator approval."""


def _valid_generation(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return (isinstance(value, Decimal) and value.is_finite()
            and value >= 0 and value == value.to_integral_value())


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH_PATTERN.fullmatch(value))


def _valid_operator_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._:@/-]{1,128}", value))


def _parse_body(record_or_body: Any) -> Dict[str, Any]:
    body = record_or_body.get("body") if isinstance(record_or_body, dict) and "body" in record_or_body else record_or_body
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError("DLQ body must be a JSON object")
    order_id = body.get("order_id")
    payload_hash = body.get("payload_hash")
    generation = body.get("recovery_generation")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("Missing or invalid order_id")
    if not _valid_hash(payload_hash):
        raise ValueError("Missing or invalid payload_hash")
    if not _valid_generation(generation):
        raise ValueError("Missing or invalid recovery_generation")
    return {"order_id": order_id, "payload_hash": payload_hash,
            "recovery_generation": generation}


def _validate_item(item: Optional[Dict[str, Any]], message: Dict[str, Any], now: int) -> RecoveryResult | str:
    if item is None:
        return RecoveryResult("MISSING_ORDER", False, "No DynamoDB order exists")
    stored_hash = item.get("payload_hash")
    generation = item.get("recovery_generation")
    if not _valid_hash(stored_hash) or not _valid_generation(generation):
        return RecoveryResult("CORRUPT_ORDER", False, "Stored hash or generation is invalid")
    if stored_hash != message["payload_hash"]:
        return RecoveryResult("PAYLOAD_MISMATCH", False, "DLQ payload hash differs from order")
    msg_gen = message["recovery_generation"]
    status = item.get("order_status")
    if status not in {"PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRY_EXHAUSTED"}:
        return RecoveryResult("CORRUPT_ORDER", False, "Order status is unknown or unsupported")
    if status == "PROCESSING":
        owner, lease_until = item.get("lease_owner"), item.get("lease_until")
        if not isinstance(owner, str) or not owner.strip():
            return RecoveryResult("CORRUPT_ORDER", False, "PROCESSING order has invalid lease_owner")
        if (isinstance(lease_until, bool) or not isinstance(lease_until, (int, float, Decimal))
                or not math.isfinite(float(lease_until))):
            return RecoveryResult("CORRUPT_ORDER", False, "PROCESSING order has invalid lease_until")
        if lease_until > now:
            return RecoveryResult("ACTIVE_LEASE", False, "Another worker still owns a valid lease")
    if status == "PENDING" and item.get("enqueue_state") not in {"NEEDS_ENQUEUE", "ENQUEUED"}:
        return RecoveryResult("CORRUPT_ORDER", False, "PENDING order has invalid enqueue_state")
    if status in {"PENDING", "RETRY_EXHAUSTED"}:
        owner, lease_until = item.get("lease_owner"), item.get("lease_until")
        if owner is not None or lease_until is not None:
            if not isinstance(owner, str) or not owner.strip():
                return RecoveryResult("CORRUPT_ORDER", False, "Order has invalid lease_owner")
            if (isinstance(lease_until, bool) or not isinstance(lease_until, (int, float, Decimal))
                    or not math.isfinite(float(lease_until))):
                return RecoveryResult("CORRUPT_ORDER", False, "Order has invalid lease_until")
            if lease_until > now:
                return RecoveryResult("ACTIVE_LEASE", False, "Order has an unexpired lease")
    if msg_gen > generation:
        return RecoveryResult("FUTURE_GENERATION", False, "DLQ message generation is ahead of the order")
    if msg_gen < generation:
        if status == "PENDING":
            enqueue_state = item.get("enqueue_state")
            if enqueue_state not in {"NEEDS_ENQUEUE", "ENQUEUED"}:
                return RecoveryResult("CORRUPT_ORDER", False,
                                      "Newer PENDING generation has invalid enqueue_state")
            if enqueue_state == "NEEDS_ENQUEUE":
                if generation == msg_gen + 1:
                    return "PENDING_REDRIVE_SEND"
                return RecoveryResult("NEWER_RECOVERY_PENDING", False,
                                      "A newer generation still needs enqueue; retain for reconciliation")
            if enqueue_state != "ENQUEUED":
                return RecoveryResult("CORRUPT_ORDER", False,
                                      "Newer PENDING generation has invalid enqueue_state")
        # A direct successor generation in NEEDS_ENQUEUE means a prior approved
        # redrive already reset state but its SendMessage result is unresolved.
        if generation == msg_gen + 1 and status == "PROCESSING":
            return RecoveryResult("REDRIVE_IN_PROGRESS", True, "New-generation delivery acquired the order")
        if status in _TERMINAL or status in {"PROCESSING", "RETRY_EXHAUSTED"} or (
            status == "PENDING" and item.get("enqueue_state") == "ENQUEUED"
        ):
            return RecoveryResult("STALE_GENERATION", True, "A newer recovery generation supersedes this DLQ message")
    if status in _TERMINAL:
        return RecoveryResult("TERMINAL", True, "Order is terminal; no redrive is needed")
    if status == "RETRY_EXHAUSTED":
        owner, lease_until = item.get("lease_owner"), item.get("lease_until")
        if owner is not None or lease_until is not None:
            if not isinstance(owner, str) or not owner.strip():
                return RecoveryResult("CORRUPT_ORDER", False, "Exhausted order has invalid lease_owner")
            if (isinstance(lease_until, bool) or not isinstance(lease_until, (int, float, Decimal))
                    or not math.isfinite(float(lease_until))):
                return RecoveryResult("CORRUPT_ORDER", False, "Exhausted order has invalid lease_until")
            if lease_until > now:
                return RecoveryResult("ACTIVE_LEASE", False, "Exhausted order still has a valid lease")
        return "READY_TO_REDRIVE"
    if status == "PENDING":
        if item.get("enqueue_state") not in {"NEEDS_ENQUEUE", "ENQUEUED"}:
            return RecoveryResult("CORRUPT_ORDER", False, "PENDING order has invalid enqueue_state")
        owner, lease_until = item.get("lease_owner"), item.get("lease_until")
        if owner is not None or lease_until is not None:
            if not isinstance(owner, str) or not owner.strip():
                return RecoveryResult("CORRUPT_ORDER", False, "PENDING order has invalid lease_owner")
            if (isinstance(lease_until, bool) or not isinstance(lease_until, (int, float, Decimal))
                    or not math.isfinite(float(lease_until))):
                return RecoveryResult("CORRUPT_ORDER", False, "PENDING order has invalid lease_until")
            if lease_until > now:
                return RecoveryResult("ACTIVE_LEASE", False, "PENDING order has an unexpired lease")
        return "ELIGIBLE_TO_EXHAUST"
    if status == "PROCESSING":
        owner, lease_until = item.get("lease_owner"), item.get("lease_until")
        if not isinstance(owner, str) or not owner.strip():
            return RecoveryResult("CORRUPT_ORDER", False, "PROCESSING order has invalid lease_owner")
        if isinstance(lease_until, bool) or not isinstance(lease_until, (int, float)) or not math.isfinite(lease_until):
            return RecoveryResult("CORRUPT_ORDER", False, "PROCESSING order has invalid lease_until")
        if lease_until > now:
            return RecoveryResult("ACTIVE_LEASE", False, "Another worker still owns a valid lease")
        return "ELIGIBLE_TO_EXHAUST"
    return RecoveryResult("CORRUPT_ORDER", False, "Order status is unknown or unsupported")


def _safe_after_enqueue(repository: Any, message: Dict[str, Any], new_generation: int,
                        now: int) -> RecoveryResult:
    order_id = message["order_id"]
    try:
        if repository.mark_enqueued(order_id, message["payload_hash"], new_generation, now):
            return RecoveryResult("REDRIVEN", True, "New generation sent and enqueue state recorded", new_generation)
    except Exception:
        # SendMessage already returned success. Re-read before deciding whether
        # the original DLQ record is safe to acknowledge.
        logger.warning("Could not mark recovery generation enqueued for %s", order_id)
    try:
        latest = repository.get_order(order_id)
    except Exception:
        logger.warning("Could not re-read order after enqueue-state race for %s", order_id)
        return RecoveryResult("ENQUEUE_STATE_UNCERTAIN", False, "Could not verify recovery state", new_generation)
    if latest is None or latest.get("payload_hash") != message["payload_hash"]:
        return RecoveryResult("ENQUEUE_STATE_UNCERTAIN", False, "Order is missing or mismatched after send", new_generation)
    current_gen = latest.get("recovery_generation")
    status = latest.get("order_status")
    if not _valid_generation(current_gen):
        return RecoveryResult("ENQUEUE_STATE_UNCERTAIN", False,
                              "Stored generation is invalid after recovery send", new_generation)
    if current_gen > new_generation:
        if status in _TERMINAL:
            return RecoveryResult("SUPERSEDED", True,
                                  "A newer terminal generation superseded this recovery", new_generation)
        if status in {"PENDING", "PROCESSING"} and latest.get("enqueue_state") == "ENQUEUED":
            owner, lease_until = latest.get("lease_owner"), latest.get("lease_until")
            if status == "PENDING" and owner is None and lease_until is None:
                return RecoveryResult("SUPERSEDED", True,
                                      "A newer enqueued generation superseded this recovery", new_generation)
            if (isinstance(owner, str) and owner.strip()
                    and not isinstance(lease_until, bool)
                    and isinstance(lease_until, (int, float, Decimal))):
                try:
                    valid_lease_until = math.isfinite(float(lease_until))
                except (OverflowError, ValueError):
                    valid_lease_until = False
                if valid_lease_until:
                    return RecoveryResult("SUPERSEDED", True,
                                          "A newer enqueued generation superseded this recovery", new_generation)
        return RecoveryResult("ENQUEUE_STATE_UNCERTAIN", False,
                              "A newer generation is not in a verified enqueued or terminal state",
                              new_generation)
    if current_gen == new_generation and (
        latest.get("enqueue_state") == "ENQUEUED" or status in _TERMINAL
    ):
        return RecoveryResult("REDRIVEN", True, "Recovery is recorded or the worker already completed", new_generation)
    if current_gen == new_generation and latest.get("enqueue_state") == "NEEDS_ENQUEUE":
        # Retry the harmless marker update once. It intentionally does not
        # require PENDING, since the new worker may already be PROCESSING.
        try:
            if repository.mark_enqueued(order_id, message["payload_hash"], new_generation, now):
                return RecoveryResult("REDRIVEN", True, "Enqueue marker recorded after concurrent progress", new_generation)
        except Exception:
            logger.warning("Retry to mark enqueue state failed for %s", order_id)
    return RecoveryResult("ENQUEUE_STATE_UNCERTAIN", False, "Recovery send is not safely recorded", new_generation)


def process_dlq_message(
    record_or_body: Any, repository: Any, publisher: Any, *, now: Callable[[], int] = lambda: int(time.time()),
    operator_id: Optional[str] = None, approved: bool = False,
    retry_pending_send: bool = False,
) -> RecoveryResult:
    """Triage one DLQ message; redrive requires explicit operator approval.

    `retry_pending_send` is a separate explicit choice used only when a prior
    approved generation bump left `PENDING/NEEDS_ENQUEUE` after an ambiguous
    SendMessage result. It reuses that generation and never increments again.
    """
    try:
        message = _parse_body(record_or_body)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return RecoveryResult("INVALID_MESSAGE", False, str(exc))
    if approved and not _valid_operator_id(operator_id):
        return RecoveryResult("INVALID_APPROVAL", False, "A valid operator ID is required")
    if approved:
        logger.info("DLQ recovery approved operator=%s order=%s generation=%s retry_pending_send=%s",
                    operator_id, message["order_id"], message["recovery_generation"], retry_pending_send)

    order_id = message["order_id"]
    try:
        item = repository.get_order(order_id)
    except Exception:
        logger.warning("DynamoDB read failed during DLQ triage for %s", order_id)
        return RecoveryResult("READ_FAILURE", False, "Could not verify order state")
    classification = _validate_item(item, message, now())
    if isinstance(classification, RecoveryResult):
        return classification

    if classification == "ELIGIBLE_TO_EXHAUST":
        try:
            exhausted = repository.mark_retry_exhausted(
                order_id, message["payload_hash"], message["recovery_generation"], now(),
            )
        except Exception:
            logger.warning("Could not conditionally mark order exhausted: %s", order_id)
            return RecoveryResult("EXHAUSTION_WRITE_FAILURE", False, "Conditional exhaustion update failed")
        if not exhausted:
            # A worker may have acquired a lease or completed while we triaged.
            try:
                latest = repository.get_order(order_id)
            except Exception:
                return RecoveryResult("READ_FAILURE", False, "Could not classify conditional-write race")
            classification = _validate_item(latest, message, now())
            if isinstance(classification, RecoveryResult):
                return classification
            if classification != "READY_TO_REDRIVE":
                return RecoveryResult("STATE_CHANGED", False, "Order changed during exhaustion update")
        else:
            classification = "READY_TO_REDRIVE"

    if classification == "PENDING_REDRIVE_SEND":
        if not (approved and retry_pending_send and operator_id and operator_id.strip()):
            return RecoveryResult("PENDING_REDRIVE_SEND", False, "Prior redrive send is unresolved; operator reconciliation required")
        new_generation = int(item["recovery_generation"])
    elif classification == "READY_TO_REDRIVE":
        if not approved or not _valid_operator_id(operator_id):
            return RecoveryResult("RETRY_EXHAUSTED_READY", False, "Order is exhausted; explicit operator approval is required")
        try:
            new_generation = repository.begin_redrive(
                order_id, message["payload_hash"], message["recovery_generation"], now(),
            )
        except Exception:
            logger.warning("Conditional redrive reset failed for %s", order_id)
            return RecoveryResult("REDRIVE_WRITE_FAILURE", False, "Could not start approved redrive")
        if new_generation is None:
            try:
                latest = repository.get_order(order_id)
            except Exception:
                return RecoveryResult("READ_FAILURE", False, "Could not classify redrive race")
            latest_class = _validate_item(latest, message, now())
            if isinstance(latest_class, RecoveryResult):
                return latest_class
            if latest_class == "PENDING_REDRIVE_SEND" and retry_pending_send:
                new_generation = latest["recovery_generation"]
            else:
                return RecoveryResult("STATE_CHANGED", False, "Order changed during approved redrive")
    else:
        return RecoveryResult("INVESTIGATION_REQUIRED", False, "DLQ state is not eligible for recovery")

    new_message = {
        "order_id": order_id,
        "payload_hash": message["payload_hash"],
        "recovery_generation": new_generation,
        "version": 1,
    }
    try:
        response = publisher.send_message(json.dumps(new_message, separators=(",", ":")))
    except Exception:
        # The send may have succeeded even if the response was lost. Leave
        # NEEDS_ENQUEUE and the original DLQ record for bounded operator review.
        logger.warning("Redrive SendMessage result is uncertain for %s", order_id)
        return RecoveryResult("SEND_UNCERTAIN", False, "Do not bump generation; reconcile before an explicit retry", new_generation)
    if not isinstance(response, dict) or not response.get("MessageId"):
        return RecoveryResult("SEND_UNCERTAIN", False, "Send response did not confirm a MessageId", new_generation)
    logger.info("Operator %s sent redrive generation %s for order %s",
                operator_id, new_generation, order_id)
    return _safe_after_enqueue(repository, message, new_generation, now())


def operator_redrive_record(
    record: Dict[str, Any], repository: Any, publisher: Any, dlq_client: Any,
    dlq_queue_url: str, *, operator_id: str, approved: bool,
    now: Callable[[], int] = lambda: int(time.time()),
    retry_pending_send: bool = False,
) -> RecoveryResult:
    """Run an explicitly approved recovery and delete only a safely resolved DLQ record.

    The queue client is injected so tests remain offline. If this helper is
    called from a Lambda event source mapping instead, successful handler
    return performs the equivalent acknowledgment; do not also delete there.
    """
    result = process_dlq_message(
        record, repository, publisher, now=now, operator_id=operator_id,
        approved=approved, retry_pending_send=retry_pending_send,
    )
    if not result.acknowledge_dlq_message:
        return result
    receipt_handle = record.get("receiptHandle") if isinstance(record, dict) else None
    if not isinstance(receipt_handle, str) or not receipt_handle:
        return RecoveryResult("SAFE_BUT_NOT_DELETED", False,
                              "Recovery is resolved, but no DLQ receipt handle was supplied",
                              result.new_generation)
    try:
        dlq_client.delete_message(QueueUrl=dlq_queue_url, ReceiptHandle=receipt_handle)
    except Exception:
        logger.warning("Could not delete resolved DLQ message for classification %s",
                       result.classification)
        return RecoveryResult("DLQ_DELETE_UNCERTAIN", False,
                              "Recovery is resolved; DLQ delete result is uncertain",
                              result.new_generation)
    return result


def lambda_handler(event: Dict[str, Any], context: Any,
                   repository: Optional[Any] = None,
                   publisher: Optional[Any] = None) -> Dict[str, Any]:
    """DLQ event handler performs triage only; operator approval is separate."""
    del context
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or len(records) != 1:
        raise RecoveryNeedsOperator("DLQ handler requires exactly one SQS record")
    if repository is None or publisher is None:
        import boto3
        table = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
        sqs = boto3.client("sqs")
        repository = repository or DynamoOrderRepository(table)
        publisher = publisher or sqs
        # SQS client needs QueueUrl; the adapter below binds it.
        publisher = _QueuePublisher(publisher, os.environ["ORDERS_QUEUE_URL"])
    result = process_dlq_message(records[0], repository, publisher)
    if not result.acknowledge_dlq_message:
        raise RecoveryNeedsOperator(f"DLQ record retained: {result.classification}: {result.detail}")
    return {"batchItemFailures": []}


class _QueuePublisher:
    def __init__(self, client: Any, queue_url: str) -> None:
        self.client = client
        self.queue_url = queue_url

    def send_message(self, body: str) -> Dict[str, Any]:
        return self.client.send_message(QueueUrl=self.queue_url, MessageBody=body)
