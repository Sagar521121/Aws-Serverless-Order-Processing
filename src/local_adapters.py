"""Thread-safe local adapters for integration tests; no AWS emulation claims."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional


class LocalOrderRepository:
    """In-memory implementation of the worker/intake/DLQ repository contract."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(order_id)
            return dict(item) if item is not None else None

    def create_pending(self, item: Dict[str, Any]) -> bool:
        with self._lock:
            order_id = item["order_id"]
            if order_id in self._items:
                return False
            self._items[order_id] = dict(item)
            return True

    def mark_enqueued(self, order_id: str, payload_hash: str,
                      generation: int, now: int) -> bool:
        with self._lock:
            item = self._items.get(order_id)
            if (item is None or item.get("payload_hash") != payload_hash
                    or item.get("recovery_generation") != generation
                    or item.get("enqueue_state") != "NEEDS_ENQUEUE"):
                return False
            item.update(enqueue_state="ENQUEUED", updated_at=now)
            return True

    def acquire_lease(self, order_id: str, payload_hash: str, generation: int,
                      owner: str, now: int, lease_seconds: int) -> bool:
        with self._lock:
            item = self._items.get(order_id)
            if (item is None or item.get("payload_hash") != payload_hash
                    or item.get("recovery_generation") != generation):
                return False
            status, until = item.get("order_status"), item.get("lease_until")
            if status == "PENDING":
                if item.get("lease_owner") is not None or until is not None:
                    if not isinstance(until, (int, float)) or until > now:
                        return False
            elif status == "PROCESSING":
                if not item.get("lease_owner") or not isinstance(until, (int, float)) or until > now:
                    return False
            else:
                return False
            item.update(order_status="PROCESSING", lease_owner=owner,
                        lease_until=now + lease_seconds,
                        attempt_count=item.get("attempt_count", 0) + 1, updated_at=now)
            return True

    def complete(self, order_id: str, payload_hash: str, generation: int,
                 owner: str, now: int) -> bool:
        return self._owned_transition(order_id, payload_hash, generation, owner,
                                      now, "COMPLETED", None)

    def fail_permanently(self, order_id: str, payload_hash: str, generation: int,
                         owner: str, now: int, error_code: str) -> bool:
        return self._owned_transition(order_id, payload_hash, generation, owner,
                                      now, "FAILED", error_code)

    def _owned_transition(self, order_id: str, payload_hash: str, generation: int,
                          owner: str, now: int, target: str,
                          error_code: Optional[str]) -> bool:
        with self._lock:
            item = self._items.get(order_id)
            if (item is None or item.get("order_status") != "PROCESSING"
                    or item.get("lease_owner") != owner
                    or item.get("lease_until", 0) <= now
                    or item.get("payload_hash") != payload_hash
                    or item.get("recovery_generation") != generation):
                return False
            item.update(order_status=target, updated_at=now)
            item.pop("lease_owner", None)
            item.pop("lease_until", None)
            if error_code is not None:
                item["error_detail"] = error_code
            return True

    def mark_retry_exhausted(self, order_id: str, payload_hash: str,
                             generation: int, now: int) -> bool:
        with self._lock:
            item = self._items.get(order_id)
            if (item is None or item.get("payload_hash") != payload_hash
                    or item.get("recovery_generation") != generation):
                return False
            status = item.get("order_status")
            if status not in {"PENDING", "PROCESSING"}:
                return False
            owner, until = item.get("lease_owner"), item.get("lease_until")
            if status == "PROCESSING" and (not isinstance(owner, str) or not owner.strip()):
                return False
            if owner is not None or until is not None:
                if (not isinstance(owner, str) or not owner.strip()
                        or isinstance(until, bool) or not isinstance(until, (int, float))
                        or until > now):
                    return False
            item.update(order_status="RETRY_EXHAUSTED", updated_at=now)
            item.pop("lease_owner", None)
            item.pop("lease_until", None)
            return True

    def begin_redrive(self, order_id: str, payload_hash: str,
                      generation: int, now: int) -> Optional[int]:
        with self._lock:
            item = self._items.get(order_id)
            if (item is None or item.get("order_status") != "RETRY_EXHAUSTED"
                    or item.get("payload_hash") != payload_hash
                    or item.get("recovery_generation") != generation):
                return None
            owner, until = item.get("lease_owner"), item.get("lease_until")
            if owner is not None or until is not None:
                if (not isinstance(owner, str) or not owner.strip()
                        or isinstance(until, bool) or not isinstance(until, (int, float))
                        or until > now):
                    return None
            new_generation = generation + 1
            item.update(order_status="PENDING", enqueue_state="NEEDS_ENQUEUE",
                        recovery_generation=new_generation, attempt_count=0, updated_at=now)
            item.pop("lease_owner", None)
            item.pop("lease_until", None)
            item.pop("error_detail", None)
            return new_generation


class LocalSqsAdapter:
    """Deterministic queue double with controllable send failure."""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.fail_next_send: Optional[Exception] = None
        self.accept_then_raise_next_send: Optional[Exception] = None
        self._next_id = 1

    def send_message(self, body: str) -> Dict[str, str]:
        if self.fail_next_send is not None:
            failure, self.fail_next_send = self.fail_next_send, None
            raise failure
        accepted_failure, self.accept_then_raise_next_send = (
            self.accept_then_raise_next_send, None
        )
        parsed = json.loads(body)
        message_id = f"local-{self._next_id}"
        self._next_id += 1
        self.messages.append({"MessageId": message_id, "body": body,
                              "parsed_body": parsed})
        if accepted_failure is not None:
            raise accepted_failure
        return {"MessageId": message_id}
