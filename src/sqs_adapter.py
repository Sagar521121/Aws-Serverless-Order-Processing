"""Small injectable adapter for sending application messages to SQS."""

from __future__ import annotations

from typing import Any, Dict


class SqsPublisher:
    """Bind an SQS client to one configured queue URL."""

    def __init__(self, client: Any, queue_url: str) -> None:
        if not queue_url:
            raise ValueError("queue_url is required")
        self.client = client
        self.queue_url = queue_url

    def send_message(self, body: str) -> Dict[str, Any]:
        return self.client.send_message(QueueUrl=self.queue_url, MessageBody=body)
