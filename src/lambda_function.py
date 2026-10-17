"""AWS Lambda handler for the order intake service.

Receives HTTP POST requests via Amazon API Gateway, validates the payload,
and returns structured HTTP responses.

Designed for Python 3.12 compatibility and standard-library-first architecture.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from validation import validate_order_payload

# Configure logger for CloudWatch diagnostics
logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
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
            order_data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            logger.warning("Request rejected: malformed JSON (%s).", exc)
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
            logger.info("Validation failed: %s", validation_errors)
            return _build_response(
                400,
                {
                    "status": "error",
                    "message": "Validation failed",
                    "errors": validation_errors,
                },
            )

        # Stage 1: Local foundation accepted response
        # In Stage 3, this will be preceded by DynamoDB conditional write and SQS enqueue.
        order_id = order_data["order_id"]
        created_timestamp = datetime.now(timezone.utc).isoformat()

        logger.info("Order %s accepted successfully.", order_id)
        return _build_response(
            201,
            {
                "status": "success",
                "message": "Order accepted for processing",
                "data": {
                    "order_id": order_id,
                    "order_status": "PENDING",
                    "created_at": created_timestamp,
                },
            },
        )

    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Unexpected error processing order: %s", exc, exc_info=True)
        return _build_response(
            500,
            {
                "status": "error",
                "message": "Internal server error",
                "errors": ["An unexpected error occurred while processing the order."],
            },
        )
