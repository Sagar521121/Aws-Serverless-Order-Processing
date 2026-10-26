"""Unit tests for the order intake Lambda handler (src/lambda_function.py).

Uses Python's built-in unittest module. Compatible with Python 3.12.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from decimal import Decimal

# Ensure src directory is on sys.path for direct module import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from lambda_function import lambda_handler
from intake_service import EnqueueFailure, IntakeService
from local_adapters import LocalOrderRepository, LocalSqsAdapter


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
        self.repository = LocalOrderRepository()
        self.queue = LocalSqsAdapter()
        self.intake = IntakeService(self.repository, self.queue, now=lambda: 100)
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

    def _assert_no_sensitive_log_values(self, captured, sentinels) -> None:
        formatter = logging.Formatter()
        output = "\n".join(captured.output)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, output)

        for record in captured.records:
            record_dict = repr(record.__dict__)
            record_args = repr(record.args)
            formatted = formatter.format(record)
            exception_text = (
                formatter.formatException(record.exc_info)
                if record.exc_info else (record.exc_text or "")
            )
            combined = "\n".join((
                record.getMessage(), record_args, record_dict,
                formatted, exception_text,
            ))
            for sentinel in sentinels:
                self.assertNotIn(sentinel, combined)

    @staticmethod
    def _expected_headers() -> dict:
        return {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        }

    def test_valid_request_returns_201_created(self) -> None:
        """A valid API Gateway proxy event should return HTTP 201 and accepted status."""
        event = {
            "version": "2.0",
            "body": json.dumps(self.valid_order_body),
        }
        response = lambda_handler(event, self.context, self.intake)

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")

        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["data"]["order_id"], "ord-2026-98234-a1b2")
        self.assertEqual(body["data"]["order_status"], "PENDING")
        self.assertIn("created_at", body["data"])

    def test_fractional_json_numbers_are_parsed_as_decimal(self) -> None:
        raw = (
            '{"order_id":"ord-precision-01","customer_id":"cust-1",'
            '"items":[{"item_id":"item-1","quantity":1,'
            '"price":0.12345678901234567890123456789012345678}],'
            '"total_amount":0.12345678901234567890123456789012345678}'
        )
        response = lambda_handler({"body": raw}, self.context, self.intake)
        self.assertEqual(response["statusCode"], 201)
        stored = self.repository.get_order("ord-precision-01")
        expected = Decimal("0.12345678901234567890123456789012345678")
        self.assertEqual(stored["total_amount"], expected)
        self.assertEqual(stored["items"][0]["price"], expected)

    def test_direct_dict_body_returns_201(self) -> None:
        """Invoking with a pre-parsed dictionary body should also succeed."""
        event = {"body": self.valid_order_body}
        response = lambda_handler(event, self.context, self.intake)

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
        response = lambda_handler(event, self.context, self.intake)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Invalid JSON in request body")
        self.assertTrue(any("Malformed JSON payload" in err for err in body["errors"]))

    def test_oversized_body_returns_413_before_json_parsing(self) -> None:
        response = lambda_handler({"body": " " * (64 * 1024 + 1)}, self.context,
                                  self.intake)
        self.assertEqual(response["statusCode"], 413)
        self.assertEqual(json.loads(response["body"])["message"], "Request body is too large")

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
        response = lambda_handler(event, self.context, self.intake)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Validation failed")
        self.assertTrue(len(body["errors"]) >= 2)

    def test_validation_mismatch_response_is_unchanged_and_logs_omit_order_data(self) -> None:
        invalid_body = dict(self.valid_order_body)
        invalid_body["customer_id"] = "CUSTOMER_SENTINEL_025"
        invalid_body["items"] = [{
            "item_id": "ITEM_SENTINEL_025",
            "quantity": 1,
            "price": 654321.09,
        }]
        invalid_body["total_amount"] = 123456.78

        with self.assertLogs(level="DEBUG") as captured:
            response = lambda_handler(
                {"body": json.dumps(invalid_body)}, self.context, self.intake
            )

        expected_body = json.dumps({
            "status": "error",
            "message": "Validation failed",
            "errors": [
                "Field 'total_amount' (123456.78) does not match the sum of item prices "
                "(654321.09)."
            ],
        })
        self.assertEqual(response, {
            "statusCode": 400,
            "headers": self._expected_headers(),
            "body": expected_body,
        })
        self.assertIn("123456.78", response["body"])
        self.assertIn("654321.09", response["body"])
        self.assertTrue(any(
            "code=ORDER_VALIDATION_FAILED" in message
            and self.context.aws_request_id in message
            for message in captured.output
        ))
        self._assert_no_sensitive_log_values(captured, (
            "123456.78", "654321.09", "CUSTOMER_SENTINEL_025", "ITEM_SENTINEL_025",
        ))

    def test_malformed_json_response_is_unchanged_and_parser_text_is_not_logged(self) -> None:
        with self.assertLogs(level="DEBUG") as captured:
            response = lambda_handler(
                {"body": "{order_id: not-valid-json}"}, self.context, self.intake
            )

        self.assertEqual(response, {
            "statusCode": 400,
            "headers": self._expected_headers(),
            "body": json.dumps({
                "status": "error",
                "message": "Invalid JSON in request body",
                "errors": [
                    "Malformed JSON payload: Expecting property name enclosed in double "
                    "quotes: line 1 column 2 (char 1)"
                ],
            }),
        })
        self.assertTrue(any(
            "code=MALFORMED_JSON" in message
            and self.context.aws_request_id in message
            for message in captured.output
        ))
        self.assertFalse(any(
            "Expecting property name enclosed in double quotes" in message
            for message in captured.output
        ))

    def test_enqueue_failure_response_is_unchanged_and_exception_text_is_not_logged(self) -> None:
        sentinel = "ENQUEUE_EXCEPTION_SENTINEL_025"

        class FailingIntake:
            def submit(self, payload):
                raise EnqueueFailure(sentinel)

        with self.assertLogs(level="DEBUG") as captured:
            response = lambda_handler(
                {"body": json.dumps(self.valid_order_body)},
                self.context,
                FailingIntake(),
            )

        self.assertEqual(response, {
            "statusCode": 503,
            "headers": self._expected_headers(),
            "body": json.dumps({
                "status": "error",
                "message": sentinel,
                "errors": ["Retry this request with the same order data."],
            }),
        })
        self.assertTrue(any(
            "code=ENQUEUE_UNRESOLVED" in message
            and self.context.aws_request_id in message
            for message in captured.output
        ))
        self._assert_no_sensitive_log_values(captured, (sentinel,))

    def test_unexpected_exception_logs_exc_info_for_diagnostics(self) -> None:
        """logger.exception() must attach exc_info so CloudWatch shows the traceback.

        The response body must remain the generic HTTP 500 error shape.
        The exception message (sentinel) must NOT appear in the log text itself
        (it is only present in the traceback attached to exc_info), satisfying
        the requirement that sensitive data is not exposed in plain log lines.
        """
        sentinel = "UNEXPECTED_EXCEPTION_SENTINEL_025"

        class FailingIntake:
            def submit(self, payload):
                raise RuntimeError(sentinel)

        with self.assertLogs(level="DEBUG") as captured:
            response = lambda_handler(
                {"body": json.dumps(self.valid_order_body)},
                self.context,
                FailingIntake(),
            )

        self.assertEqual(response, {
            "statusCode": 500,
            "headers": self._expected_headers(),
            "body": json.dumps({
                "status": "error",
                "message": "Internal server error",
                "errors": ["An unexpected error occurred while processing the order."],
            }),
        })
        self.assertTrue(any(
            "code=UNEXPECTED_ERROR" in message
            and self.context.aws_request_id in message
            for message in captured.output
        ))
        # logger.exception() attaches exc_info; verify the traceback is present
        # on the UNEXPECTED_ERROR record so CloudWatch can show the root cause.
        error_records = [
            r for r in captured.records
            if "UNEXPECTED_ERROR" in r.getMessage()
        ]
        self.assertTrue(error_records, "Expected at least one UNEXPECTED_ERROR log record")
        unexpected_record = error_records[0]
        self.assertIsNotNone(
            unexpected_record.exc_info,
            "exc_info must be attached so the traceback is visible in CloudWatch",
        )
        exc_type, exc_value, _ = unexpected_record.exc_info
        self.assertIs(exc_type, RuntimeError)
        # The sentinel value must NOT appear in the formatted log MESSAGE itself
        # (it may appear in the traceback, which is acceptable for diagnosis).
        self.assertNotIn(sentinel, unexpected_record.getMessage())

    def test_events_directory_valid_order_file(self) -> None:
        """Verify the sample events/valid_order.json file succeeds through the handler."""
        event_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "events", "valid_order.json")
        )
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)

        response = lambda_handler(event, self.context, self.intake)
        self.assertEqual(response["statusCode"], 201)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")

    def test_successful_response_body_is_valid_json_with_int_created_at(self) -> None:
        """HTTP 201 response body must be valid JSON with created_at as an integer.

        Regression test for the production HTTP 500 caused by the deployed
        _result() returning created_at as Decimal (no int() cast), which made
        json.dumps raise TypeError.  The fix is the int() cast in _result().
        """
        event = {"body": json.dumps(self.valid_order_body)}
        response = lambda_handler(event, self.context, self.intake)
        self.assertEqual(response["statusCode"], 201)

        # Body must be valid JSON (no TypeError from Decimal)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")

        data = body["data"]
        self.assertIn("order_id", data)
        self.assertIn("order_status", data)
        self.assertIn("created_at", data)
        self.assertIn("enqueue_state", data)

        # created_at must serialise as a JSON integer, not Decimal
        self.assertIsInstance(
            data["created_at"], int,
            "created_at in the API response must be a plain int, not Decimal",
        )

    def test_decimal_created_at_from_dynamodb_refetch_does_not_cause_500(self) -> None:
        """Simulates the DynamoDB re-fetch that caused the production HTTP 500.

        After mark_enqueued succeeds, submit() calls get_order() again to
        confirm state.  The boto3 Table resource returns numeric attributes
        (including created_at) as Decimal.  _result() must cast created_at to
        int before returning so that json.dumps in _build_response never sees
        a Decimal.
        """
        repo = LocalOrderRepository()
        queue = LocalSqsAdapter()
        svc = IntakeService(repo, queue, now=lambda: 1761360000)

        # Wrap get_order to simulate boto3 DynamoDB Table resource behaviour:
        # every numeric attribute (int, float) is returned as Decimal.
        orig_get = repo.get_order

        def dynamo_style_get(order_id: str):
            item = orig_get(order_id)
            if item is None:
                return None

            def to_decimal(obj):
                if isinstance(obj, dict):
                    return {k: to_decimal(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [to_decimal(v) for v in obj]
                if isinstance(obj, bool):
                    return obj  # bool is a subclass of int; keep as bool
                if isinstance(obj, int):
                    return Decimal(obj)
                if isinstance(obj, float):
                    return Decimal(str(obj))
                return obj

            return to_decimal(item)

        repo.get_order = dynamo_style_get

        event = {
            "body": json.dumps({
                "order_id": "ord-decimal-repro-001",
                "customer_id": "cust-repro",
                "items": [{"item_id": "item-repro", "quantity": 1, "price": 25.00}],
                "total_amount": 25.00,
                "currency": "INR",
            })
        }
        # Must return 201, not 500
        response = lambda_handler(event, self.context, svc)
        self.assertEqual(
            response["statusCode"], 201,
            f"Expected 201 but got {response['statusCode']}; "
            f"body={response.get('body')}",
        )

        body = json.loads(response["body"])
        self.assertEqual(body["status"], "success")
        data = body["data"]
        self.assertIsInstance(
            data["created_at"], int,
            "created_at must be int even when the DynamoDB re-fetch returns Decimal",
        )

    def test_idempotent_resubmission_returns_201_not_409_or_500(self) -> None:
        """Resubmitting the same order ID with identical payload data must return 201.

        The first call creates and enqueues.  The second call hits the
        idempotency path (create_pending returns False, existing record is
        found with matching hash) and must also return 201 with a valid JSON
        body.
        """
        event = {"body": json.dumps(self.valid_order_body)}
        r1 = lambda_handler(event, self.context, self.intake)
        r2 = lambda_handler(event, self.context, self.intake)

        self.assertEqual(r1["statusCode"], 201)
        self.assertEqual(r2["statusCode"], 201)

        b1 = json.loads(r1["body"])
        b2 = json.loads(r2["body"])
        self.assertEqual(b1["status"], "success")
        self.assertEqual(b2["status"], "success")
        self.assertEqual(b1["data"]["order_id"], b2["data"]["order_id"])
        # Both responses must have a serialisable created_at
        self.assertIsInstance(b1["data"]["created_at"], int)
        self.assertIsInstance(b2["data"]["created_at"], int)

    def test_different_payload_same_order_id_returns_409(self) -> None:
        """Reusing an order_id with different payload data must return HTTP 409."""
        event1 = {"body": json.dumps(self.valid_order_body)}
        lambda_handler(event1, self.context, self.intake)

        conflicting_body = dict(self.valid_order_body)
        conflicting_body["total_amount"] = 99.99
        conflicting_body["items"] = [
            {"item_id": "item-different", "quantity": 1, "price": 99.99}
        ]
        event2 = {"body": json.dumps(conflicting_body)}
        response = lambda_handler(event2, self.context, self.intake)

        self.assertEqual(response["statusCode"], 409)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")

    def test_enqueue_failure_returns_503_not_500(self) -> None:
        """A confirmed SQS send failure must return HTTP 503, not 500."""
        from intake_service import EnqueueFailure

        class EnqueueFailingIntake:
            def submit(self, payload):
                raise EnqueueFailure("Queue unavailable")

        event = {"body": json.dumps(self.valid_order_body)}
        response = lambda_handler(event, self.context, EnqueueFailingIntake())
        self.assertEqual(response["statusCode"], 503)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "error")
        self.assertIn("Retry", body["errors"][0])

    def test_successful_intake_does_not_create_duplicate_order(self) -> None:
        """Two sequential submissions of the same order must create exactly one SQS message."""
        queue = LocalSqsAdapter()
        repo = LocalOrderRepository()
        svc = IntakeService(repo, queue, now=lambda: 1761360000)

        event = {"body": json.dumps(self.valid_order_body)}
        lambda_handler(event, self.context, svc)
        lambda_handler(event, self.context, svc)

        self.assertEqual(
            len(queue.messages), 1,
            "Exactly one SQS message must be enqueued regardless of duplicate submissions",
        )

    def test_monetary_values_not_exposed_as_strings_in_response(self) -> None:
        """The API response must not silently convert monetary Decimal to strings.

        The response schema for a successful intake does not include monetary
        fields (total_amount, item prices) - they are internal to the service.
        This test verifies that the response body only contains the four
        expected fields and that no unexpected Decimal-to-string conversions
        have been introduced.
        """
        event = {"body": json.dumps(self.valid_order_body)}
        response = lambda_handler(event, self.context, self.intake)
        self.assertEqual(response["statusCode"], 201)

        body = json.loads(response["body"])
        data = body["data"]
        expected_keys = {"order_id", "order_status", "created_at", "enqueue_state"}
        self.assertEqual(
            set(data.keys()), expected_keys,
            f"Response data must contain exactly {expected_keys}; got {set(data.keys())}",
        )
        # No monetary amounts in the response
        self.assertNotIn("total_amount", data)
        self.assertNotIn("items", data)


if __name__ == "__main__":
    unittest.main()
