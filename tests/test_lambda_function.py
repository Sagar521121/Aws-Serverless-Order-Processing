"""Unit tests for the order intake Lambda handler (src/lambda_function.py).

Uses Python's built-in unittest module. Compatible with Python 3.12.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

# Ensure src directory is on sys.path for direct module import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from lambda_function import lambda_handler


class DummyContext:
    """Mock Lambda execution context for unit testing."""
    def __init__(self) -> None:
        self.function_name = "order-processing-intake"
        self.memory_limit_in_mb = 128
        self.invoked_function_arn = "arn:aws:lambda:ap-south-1:123456789012:function:order-processing-intake"
        self.aws_request_id = "test-request-id-1234"


class TestLambdaFunction(unittest.TestCase):
    """Test suite for lambda_function.lambda_handler."""

    def setUp(self) -> None:
        self.context = DummyContext()
        self.valid_order_body = {
            "order_id": "ord-2026-98234-a1b2",
            "customer_id": "cust-5501",
            "items": [
                {"item_id": "item-101", "quantity": 2, "price": 19.99},
                {"item_id": "item-102", "quantity": 1, "price": 10.00},
            ],
            "total_amount": 49.98,
            "currency": "USD",
        }

    def test_valid_request_returns_201_created(self) -> None:
        """A valid API Gateway proxy event should return HTTP 201 and accepted status."""
        event = {
            "version": "2.0",
            "body": json.dumps(self.valid_order_body),
        }
        response = lambda_handler(event, self.context)

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")

        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["data"]["order_id"], "ord-2026-98234-a1b2")
        self.assertEqual(body["data"]["order_status"], "PENDING")
        self.assertIn("created_at", body["data"])

    def test_direct_dict_body_returns_201(self) -> None:
        """Invoking with a pre-parsed dictionary body should also succeed."""
        event = {"body": self.valid_order_body}
        response = lambda_handler(event, self.context)

        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")

    def test_missing_or_empty_body_returns_400(self) -> None:
        """Missing, null, or empty string bodies must return HTTP 400."""
        for bad_event in [{}, {"body": None}, {"body": ""}, {"body": "   "}]:
            with self.subTest(bad_event=bad_event):
                response = lambda_handler(bad_event, self.context)
                self.assertEqual(response["statusCode"], 400)
                body = json.loads(response["body"])
                self.assertEqual(body["status"], "error")
                self.assertTrue(any("required" in err.lower() for err in body["errors"]))

    def test_malformed_json_body_returns_400(self) -> None:
        """Malformed JSON string in body must return HTTP 400 with descriptive error."""
        event = {"body": "{order_id: not-valid-json}"}
        response = lambda_handler(event, self.context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Invalid JSON in request body")
        self.assertTrue(any("Malformed JSON payload" in err for err in body["errors"]))

    def test_non_dict_event_returns_400(self) -> None:
        """Non-dictionary event objects must return HTTP 400."""
        response = lambda_handler("string-event", self.context)  # type: ignore[arg-type]
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")

    def test_validation_failure_returns_400_with_errors(self) -> None:
        """Payload that fails schema validation must return HTTP 400 with failure list."""
        invalid_body = dict(self.valid_order_body)
        invalid_body["order_id"] = "bad"  # too short
        invalid_body["total_amount"] = -5.0

        event = {"body": json.dumps(invalid_body)}
        response = lambda_handler(event, self.context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Validation failed")
        self.assertTrue(len(body["errors"]) >= 2)

    def test_events_directory_valid_order_file(self) -> None:
        """Verify the sample events/valid_order.json file succeeds through the handler."""
        event_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "events", "valid_order.json")
        )
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)

        response = lambda_handler(event, self.context)
        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")

    def test_events_directory_invalid_order_file(self) -> None:
        """Verify the sample events/invalid_order.json file fails through the handler."""
        event_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "events", "invalid_order.json")
        )
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)

        response = lambda_handler(event, self.context)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Validation failed")
        self.assertTrue(len(body["errors"]) > 0)


if __name__ == "__main__":
    unittest.main()
