"""Coordinate validated order creation and its first SQS enqueue."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from order_service import OrderService
from validation import validate_order_payload


def _as_decimal(value: Any) -> Decimal:
    """Convert supported JSON numeric values without a binary-float round trip."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError("Order monetary values must be numeric")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _canonical_decimal(value: Decimal) -> str:
    """Render a decimal without exponent notation or insignificant zeros."""
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        rendered = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            rendered = coefficient[:point] + "." + coefficient[point:]
        else:
            rendered = "0." + ("0" * -point) + coefficient
    return ("-" if parts.sign else "") + rendered


def _canonical_json(value: Any) -> str:
    """Serialize canonical data with Decimal values as exact JSON numbers.

    Values that round-trip through the legacy float representation keep that
    representation (including ``1.0``), preserving existing hashes for normal
    orders. Values that need more precision use their exact normalized decimal
    token, so distinct accepted values cannot collapse through float rounding.
    """
    if isinstance(value, Decimal):
        try:
            legacy_float = float(value)
            if Decimal(str(legacy_float)) == value:
                return json.dumps(legacy_float, allow_nan=False)
        except (OverflowError, ValueError):
            pass
        return _canonical_decimal(value)
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the validated business fields without losing decimal precision.

    Monetary Decimal values stay Decimal for repository persistence. Hashing
    emits normalized numeric tokens with insignificant trailing zeros removed,
    so 1, 1.0, and 1.00 have the same idempotency representation.
    """
    total = payload.get("total_amount")
    if total is None:
        total = payload.get("totalAmount")
    return {
        "order_id": payload["order_id"],
        "customer_id": payload["customer_id"].strip(),
        "items": [
            {"item_id": item["item_id"].strip(),
             "quantity": int(item["quantity"]),
             "price": _as_decimal(item["price"])}
            for item in payload["items"]
        ],
        "total_amount": _as_decimal(total),
        "currency": str(payload.get("currency") or "USD").upper(),
    }


def payload_hash(payload: Dict[str, Any]) -> str:
    canonical = canonical_order(payload)
    packed = _canonical_json(canonical)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


class IntakeValidationError(ValueError):
    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class IdempotencyConflict(ValueError):
    """An order_id was reused for different normalized order data."""


class EnqueueFailure(RuntimeError):
    """The order exists but the queue send or safe marker is unresolved."""


class IntakeService:
    """Persist idempotently, enqueue, then record the enqueue state.

    `repository`, `publisher`, domain service factory, and clock are injected;
    local tests can therefore run without boto3 or AWS credentials.
    """

    def __init__(self, repository: Any, publisher: Any,
                 *, now: Callable[[], int] = lambda: int(datetime.now(timezone.utc).timestamp()),
                 order_service_factory: Callable[[], OrderService] = OrderService) -> None:
        self.repository = repository
        self.publisher = publisher
        self.now = now
        self.order_service_factory = order_service_factory

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        valid, errors = validate_order_payload(payload)
        if not valid:
            raise IntakeValidationError(errors)

        order = canonical_order(payload)
        digest = payload_hash(order)
        timestamp = self.now()
        # Use the existing domain service to construct its canonical PENDING
        # record; the repository conditional write remains the source of truth.
        domain_record = self.order_service_factory().create_order(order).to_dict()
        # Strip the domain object's internal 'status' so only the authoritative
        # 'order_status' attribute is persisted to DynamoDB.
        domain_record.pop("status", None)
        # Replace the domain record's monetary values with the exact canonical
        # Decimals before persistence.
        domain_record["items"] = order["items"]
        domain_record["total_amount"] = order["total_amount"]
        item = {
            **domain_record,
            "order_status": "PENDING",
            "payload_hash": digest,
            "recovery_generation": 0,
            "enqueue_state": "NEEDS_ENQUEUE",
            "attempt_count": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

        if not self.repository.create_pending(item):
            existing = self.repository.get_order(order["order_id"])
            if existing is None:
                raise RuntimeError("Order create condition failed and record is not readable")
            if existing.get("payload_hash") != digest:
                raise IdempotencyConflict("order_id already exists with different order data")
            if existing.get("order_status") != "PENDING" or existing.get("enqueue_state") == "ENQUEUED":
                return self._result(existing)
            item = existing

        if item.get("order_status") != "PENDING":
            return self._result(item)
        if item.get("enqueue_state") == "ENQUEUED":
            return self._result(item)
        if item.get("enqueue_state") != "NEEDS_ENQUEUE":
            raise RuntimeError("Order has an unsupported enqueue state")

        message = {
            "order_id": order["order_id"], "payload_hash": digest,
            "recovery_generation": item["recovery_generation"], "version": 1,
        }
        try:
            response = self.publisher.send_message(
                json.dumps(message, sort_keys=True, separators=(",", ":"))
            )
        except Exception as exc:
            raise EnqueueFailure("Order saved, but queue send failed; retry intake safely") from exc
        if not isinstance(response, dict) or not response.get("MessageId"):
            raise EnqueueFailure("Order saved, but queue send was not confirmed")
        try:
            marked = self.repository.mark_enqueued(
                order["order_id"], digest, item["recovery_generation"], self.now()
            )
        except Exception:
            marked = False
        if not marked:
            latest = self.repository.get_order(order["order_id"])
            if latest is None or latest.get("payload_hash") != digest:
                raise EnqueueFailure("Could not verify order after enqueue")
            if latest.get("recovery_generation") != item["recovery_generation"]:
                raise EnqueueFailure("Order generation changed during enqueue")
            if latest.get("enqueue_state") != "ENQUEUED" and latest.get("order_status") not in {
                "PROCESSING", "COMPLETED", "FAILED", "RETRY_EXHAUSTED"
            }:
                raise EnqueueFailure("Enqueue could not be recorded safely")
            item = latest
        else:
            item = self.repository.get_order(order["order_id"]) or item
        return self._result(item)

    @staticmethod
    def _result(item: Dict[str, Any]) -> Dict[str, Any]:
        return {"order_id": item["order_id"],
                "order_status": item.get("order_status", "PENDING"),
                "created_at": int(item["created_at"]) if item.get("created_at") is not None else None,
                "enqueue_state": item.get("enqueue_state")}
