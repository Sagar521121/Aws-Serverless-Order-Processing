"""AWS Lambda handler for the order intake service.

Receives HTTP POST requests via Amazon API Gateway, validates the payload,
and returns structured HTTP responses.

Designed for Python 3.12 compatibility and standard-library-first architecture.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict

from dynamo_order_repository import DynamoOrderRepository
from intake_service import EnqueueFailure, IdempotencyConflict, IntakeService, IntakeValidationError
from sqs_adapter import SqsPublisher
from validation import validate_order_payload

# Configure logger for CloudWatch diagnostics
logger = logging.getLogger()
logger.setLevel(logging.INFO)
MAX_REQUEST_BODY_BYTES = 64 * 1024


def _build_response(status_code: int, body_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Format a response conforming to API Gateway HTTP proxy integration.

    Args:
        status_code: HTTP response status code (e.g. 201, 400, 500).
        body_dict: Dictionary to serialize as the JSON response body.

    Returns:
        Dictionary with statusCode, headers, and serialized body.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body_dict),
    }


def _request_id(context: Any) -> str:
    """Return the opaque Lambda request ID for safe log correlation."""
    try:
        value = getattr(context, "aws_request_id", None)
    except Exception:
        return "unavailable"
    return value if isinstance(value, str) and value else "unavailable"


def _default_intake_service() -> IntakeService:
    """Construct AWS adapters lazily; importing this module needs no AWS SDK."""
    import os
    import boto3

    repository = DynamoOrderRepository(
        boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
    )
    publisher = SqsPublisher(boto3.client("sqs"), os.environ["ORDERS_QUEUE_URL"])
    return IntakeService(repository, publisher)


def lambda_handler(event: Dict[str, Any], context: Any,
                   intake_service: Any = None) -> Dict[str, Any]:
    """Main Lambda entry point for order-processing-intake.

    Args:
        event: API Gateway proxy event payload.
        context: Lambda execution context object.

    Returns:
        API Gateway proxy response dictionary.
    """
    logger.info("Received order intake request.")

    # Guard against non-dict event
    if not isinstance(event, dict):
        logger.warning("Invocation event is not a valid dictionary.")
        return _build_response(
            400,
            {
                "status": "error",
                "message": "Invalid invocation event",
                "errors": ["Event must be a dictionary."],
            },
        )

    # Extract request body
    raw_body = event.get("body")
    if isinstance(raw_body, str):
        body_size = len(raw_body.encode("utf-8"))
    elif isinstance(raw_body, dict):
        try:
            body_size = len(json.dumps(raw_body, separators=(",", ":"),
                                       ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            body_size = MAX_REQUEST_BODY_BYTES + 1
    else:
        body_size = 0
    if body_size > MAX_REQUEST_BODY_BYTES:
        return _build_response(
            413,
            {"status": "error", "message": "Request body is too large",
             "errors": ["Request body must not exceed 64 KiB."]},
        )

    if raw_body is None or (isinstance(raw_body, str) and not raw_body.strip()):
        logger.warning("Request rejected: missing or empty body.")
        return _build_response(
            400,
            {
                "status": "error",
                "message": "Request body is required",
                "errors": ["Request body is required and cannot be empty."],
            },
        )

    # Parse JSON body
    if isinstance(raw_body, str):
        try:
            # Keep JSON fractional numbers exact through validation, hashing,
            # and persistence instead of first rounding them to binary float.
            order_data = json.loads(raw_body, parse_float=Decimal)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Request rejected code=MALFORMED_JSON request_id=%s",
                _request_id(context),
            )
            return _build_response(
                400,
                {
                    "status": "error",
                    "message": "Invalid JSON in request body",
                    "errors": [f"Malformed JSON payload: {str(exc)}"],
                },
            )
    elif isinstance(raw_body, dict):
        order_data = raw_body
    else:
        logger.warning("Request rejected: body is neither string nor dict.")
        return _build_response(
            400,
            {
                "status": "error",
                "message": "Invalid body format",
                "errors": ["Request body must be a valid JSON string or object."],
            },
        )

    # Validate against schema and business rules
    try:
        is_valid, validation_errors = validate_order_payload(order_data)
        if not is_valid:
            logger.info(
                "Request rejected code=ORDER_VALIDATION_FAILED request_id=%s",
                _request_id(context),
            )
            return _build_response(
                400,
                {
                    "status": "error",
                    "message": "Validation failed",
                    "errors": validation_errors,
                },
            )

        service = intake_service or _default_intake_service()
        result = service.submit(order_data)
        logger.info("Order %s accepted with status %s.",
                    result["order_id"], result["order_status"])
        return _build_response(
            201,
            {
                "status": "success",
                "message": "Order accepted for processing",
                "data": result,
            },
        )

    except IdempotencyConflict as exc:
        return _build_response(409, {"status": "error", "message": str(exc), "errors": [str(exc)]})
    except IntakeValidationError as exc:
        return _build_response(400, {"status": "error", "message": "Validation failed", "errors": exc.errors})
    except EnqueueFailure as exc:
        logger.warning(
            "Order enqueue unresolved code=ENQUEUE_UNRESOLVED request_id=%s",
            _request_id(context),
        )
        return _build_response(503, {"status": "error", "message": str(exc), "errors": ["Retry this request with the same order data."]})

    except Exception:  # pylint: disable=broad-except
        logger.exception(
            "Order processing failed code=UNEXPECTED_ERROR request_id=%s",
            _request_id(context),
        )
        return _build_response(
            500,
            {
                "status": "error",
                "message": "Internal server error",
                "errors": ["An unexpected error occurred while processing the order."],
            },
        )
