"""DynamoDB conditional-write adapter for worker and DLQ recovery flows.

The adapter uses the boto3 Table API but does not import boto3, so the state
machine can be tested with local fakes and the Lambda runtime can use its
provided SDK.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from validation import is_dynamodb_compatible_number


def _to_dynamodb_compatible(value: Any) -> Any:
    """Copy an item tree while converting and validating DynamoDB numbers.

    Boto3's Table resource rejects float values. Converting their string form
    avoids importing the binary floating-point approximation into Decimal.
    All numeric values are checked against DynamoDB's precision and exponent
    limits; containers are copied recursively and the input is not mutated.
    """
    if not isinstance(value, bool) and isinstance(value, (int, float, Decimal)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not is_dynamodb_compatible_number(number):
            raise ValueError("Numeric value exceeds DynamoDB number limits")
        return number if isinstance(value, float) else value
    if isinstance(value, dict):
        return {key: _to_dynamodb_compatible(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_to_dynamodb_compatible(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_to_dynamodb_compatible(nested) for nested in value)
    return value


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return error.get("Code") == "ConditionalCheckFailedException"


class DynamoOrderRepository:
    """Small conditional-update adapter around a boto3 DynamoDB Table."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        response = self.table.get_item(Key={"order_id": order_id}, ConsistentRead=True)
        return response.get("Item")

    def create_pending(self, item: Dict[str, Any]) -> bool:
        """Create a PENDING/NEEDS_ENQUEUE record once, without a read/write race."""
        try:
            self.table.put_item(
                Item=_to_dynamodb_compatible(item),
                ConditionExpression="attribute_not_exists(order_id)",
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def acquire_lease(
        self, order_id: str, payload_hash: str, generation: int,
        owner: str, now: int, lease_seconds: int,
    ) -> bool:
        try:
            self.table.update_item(
                Key={"order_id": order_id},
                ConditionExpression=(
                    "attribute_exists(order_id) AND payload_hash = :hash AND "
                    "recovery_generation = :generation AND "
                    "(order_status = :pending OR "
                    "(order_status = :processing AND attribute_exists(lease_owner) "
                    "AND lease_until <= :now)) AND "
                    "((attribute_not_exists(lease_owner) AND attribute_not_exists(lease_until)) OR "
                    "(attribute_exists(lease_owner) AND attribute_exists(lease_until) "
                    "AND lease_until <= :now))"
                ),
                UpdateExpression=(
                    "SET order_status = :processing, lease_owner = :owner, "
                    "lease_until = :lease_until, "
                    "attempt_count = if_not_exists(attempt_count, :zero) + :one, "
                    "updated_at = :now"
                ),
                ExpressionAttributeValues={
                    ":hash": payload_hash, ":generation": generation,
                    ":pending": "PENDING", ":processing": "PROCESSING",
                    ":owner": owner, ":now": now,
                    ":lease_until": now + lease_seconds, ":zero": 0, ":one": 1,
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def complete(self, order_id: str, payload_hash: str, generation: int,
                 owner: str, now: int) -> bool:
        return self._owned_transition(
            order_id, payload_hash, generation, owner, now,
            target="COMPLETED", error_detail=None,
        )

    def fail_permanently(self, order_id: str, payload_hash: str, generation: int,
                         owner: str, now: int, error_code: str) -> bool:
        return self._owned_transition(
            order_id, payload_hash, generation, owner, now,
            target="FAILED", error_detail=error_code,
        )

    def _owned_transition(self, order_id: str, payload_hash: str, generation: int,
                          owner: str, now: int, target: str,
                          error_detail: Optional[str]) -> bool:
        values: Dict[str, Any] = {
            ":hash": payload_hash, ":generation": generation,
            ":processing": "PROCESSING", ":owner": owner, ":now": now,
            ":target": target,
        }
        update = "SET order_status = :target, updated_at = :now"
        if error_detail is None:
            update += " REMOVE lease_owner, lease_until"
        else:
            values[":error"] = error_detail
            update = "SET order_status = :target, error_detail = :error, updated_at = :now REMOVE lease_owner, lease_until"
        try:
            self.table.update_item(
                Key={"order_id": order_id},
                ConditionExpression=(
                    "attribute_exists(order_id) AND order_status = :processing AND "
                    "lease_owner = :owner AND lease_until > :now AND "
                    "recovery_generation = :generation AND payload_hash = :hash"
                ),
                UpdateExpression=update,
                ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def mark_retry_exhausted(self, order_id: str, payload_hash: str,
                             generation: int, now: int) -> bool:
        try:
            self.table.update_item(
                Key={"order_id": order_id},
                ConditionExpression=(
                    "attribute_exists(order_id) AND payload_hash = :hash AND "
                    "recovery_generation = :generation AND "
                    "(order_status = :pending OR "
                    "(order_status = :processing AND attribute_exists(lease_owner) "
                    "AND lease_until <= :now)) AND "
                    "((attribute_not_exists(lease_owner) AND attribute_not_exists(lease_until)) OR "
                    "(attribute_exists(lease_owner) AND attribute_exists(lease_until) "
                    "AND lease_until <= :now))"
                ),
                UpdateExpression=(
                    "SET order_status = :exhausted, updated_at = :now "
                    "REMOVE lease_owner, lease_until"
                ),
                ExpressionAttributeValues={
                    ":hash": payload_hash, ":generation": generation,
                    ":pending": "PENDING", ":processing": "PROCESSING",
                    ":exhausted": "RETRY_EXHAUSTED", ":now": now,
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def begin_redrive(self, order_id: str, payload_hash: str,
                      generation: int, now: int) -> Optional[int]:
        new_generation = generation + 1
        try:
            self.table.update_item(
                Key={"order_id": order_id},
                ConditionExpression=(
                    "attribute_exists(order_id) AND order_status = :exhausted AND "
                    "payload_hash = :hash AND recovery_generation = :generation AND "
                    "((attribute_not_exists(lease_owner) AND attribute_not_exists(lease_until)) OR "
                    "(attribute_exists(lease_owner) AND attribute_exists(lease_until) "
                    "AND lease_until <= :now))"
                ),
                UpdateExpression=(
                    "SET order_status = :pending, enqueue_state = :needs_enqueue, "
                    "recovery_generation = :new_generation, attempt_count = :zero, "
                    "updated_at = :now REMOVE lease_owner, lease_until, error_detail"
                ),
                ExpressionAttributeValues={
                    ":exhausted": "RETRY_EXHAUSTED", ":hash": payload_hash,
                    ":generation": generation, ":pending": "PENDING",
                    ":needs_enqueue": "NEEDS_ENQUEUE", ":new_generation": new_generation,
                    ":zero": 0, ":now": now,
                },
            )
            return new_generation
        except Exception as exc:
            if _is_conditional_failure(exc):
                return None
            raise

    def mark_enqueued(self, order_id: str, payload_hash: str,
                      generation: int, now: int) -> bool:
        try:
            # Deliberately does not require PENDING: a fast worker may already
            # have advanced the status after receiving the new-generation message.
            self.table.update_item(
                Key={"order_id": order_id},
                ConditionExpression=(
                    "attribute_exists(order_id) AND payload_hash = :hash AND "
                    "recovery_generation = :generation AND enqueue_state = :needs_enqueue"
                ),
                UpdateExpression="SET enqueue_state = :enqueued, updated_at = :now",
                ExpressionAttributeValues={
                    ":hash": payload_hash, ":generation": generation,
                    ":needs_enqueue": "NEEDS_ENQUEUE", ":enqueued": "ENQUEUED",
                    ":now": now,
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise
