# Project Execution Log

This document tracks all changes made to the project, why they were made, their verification results, and any rollback procedures, in accordance with `AI_PROJECT_EXECUTION_AND_CHANGE_TRACKING.md`.

---

## Change Record: CHG-001

- **Change ID:** CHG-001
- **Date/Time:** 2026-10-18T00:55:00+05:30
- **Goal / Requirement:** 
  Initialize the project governance files, repository directory structure, documentation baseline, and project configuration without provisioning AWS resources or implementing handlers yet.
- **Why:** 
  Mandatory under Stage 1 working rules. Establishing change tracking, architecture documentation, decision logs, and a standard directory structure before implementing application code guarantees auditability, prevents regression, and ensures strict adherence to zero-cost, least-privilege principles.
- **Before:** 
  Empty project repository containing only a newly created virtual environment (`.venv`) and a basic `.gitignore`.
- **Changed Files/Resources:**
  - `PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md` (copied from local downloads, verified byte-identical)
  - `AI_PROJECT_EXECUTION_AND_CHANGE_TRACKING.md` (copied from local downloads, verified byte-identical)
  - `docs/` (directory created)
  - `src/` (directory created)
  - `tests/` (directory created)
  - `events/` (directory created)
  - `scripts/` (directory created)
  - `docs/EXECUTION_LOG.md` (created)
  - `docs/TEST_LOG.md` (created)
  - `docs/DECISIONS.md` (created)
  - `docs/ARCHITECTURE.md` (created)
  - `README.md` (created)
  - `requirements.txt` (created)
- **Summary of Change:**
  - Located and copied the two canonical governance documents into the workspace root. Verified SHA256 hashes against original downloads.
  - Created required directories for separation of concerns (`docs/`, `src/`, `tests/`, `events/`, `scripts/`).
  - Drafted architecture overview, decision records, execution tracking, test tracking, project README, and requirements manifest.
- **Dependencies / Configuration:**
  - Python: 3.12 compatibility baseline (developed on local Python 3.14 venv).
  - AWS Region: `ap-south-1`.
  - Zero third-party dependencies introduced in Stage 1; standard library only.
- **Tests Run:**
  - SHA256 hash comparison between downloaded governance documents and copied workspace files.
  - Filesystem inspection verifying directories and files exist.
- **Results:** 
  PASS (Hashes match identically; directory structure and documents verified).
- **Evidence:**
  ```text
  Algorithm: SHA256
  PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md: FBA71096870D5CC64DA775A8C7B3... (identical)
  AI_PROJECT_EXECUTION_AND_CHANGE_TRACKING.md: 8180E3B7102894BBDDCC41B7F565... (identical)
  ```
- **Remaining Risks:**
  - No application code or tests implemented yet (intentionally deferred per Stage 1 scope).
- **Rollback Plan:**
  Remove the created directories and files using `Remove-Item` if reverted.
- **Next Recommended Action:**
  Proceed with Stage 1 implementation (CHG-002): create schema validation, intake handler, test events, and unit test suites.

---

## Change Record: CHG-002

- **Change ID:** CHG-002
- **Date/Time:** 2026-10-18T01:26:00+05:30
- **Goal / Requirement:** 
  Implement Stage 1 core schema validation logic, API Gateway proxy intake Lambda handler (`lambda_function.lambda_handler`), representative valid/invalid test event payloads, and automated unit test suites using Python `unittest`.
- **Why:** 
  Establishes the local foundation for request validation and HTTP proxy integration. Isolating validation logic into a pure module allows thorough automated testing of business constraints (types, positive numbers, total amount reconciliation) locally without requiring AWS provisioning, internet access, or external dependencies.
- **Before:** 
  Workspace had project scaffolding, tracking documents, and virtual environment, but no source code, test events, or unit tests.
- **Changed Files/Resources:**
  - `docs/DECISIONS.md` (updated with DEC-005: Order schema specification and validation rules)
  - `events/valid_order.json` (created with realistic API Gateway proxy event payload)
  - `events/invalid_order.json` (created with invalid schema payload for negative testing)
  - `src/validation.py` (created with pure validation function `validate_order_payload`)
  - `src/lambda_function.py` (created with `lambda_handler` and `_build_response`)
  - `tests/test_validation.py` (created with 14 unit test cases for validation rules)
  - `tests/test_lambda_function.py` (created with 8 unit test cases for Lambda handler)
  - `docs/TEST_LOG.md` (updated with TST-003, TST-004a, TST-004b test outcomes)
  - `docs/EXECUTION_LOG.md` (updated with this CHG-002 entry)
- **Summary of Change:**
  - Added DEC-005 defining field names, order_id regex (`^[a-zA-Z0-9_-]{8,64}$`), customer_id constraints, positive item quantity/price, total_amount sum verification (within 0.01 tolerance), and error format.
  - Implemented `validate_order_payload` in `src/validation.py`.
  - Implemented `lambda_handler` in `src/lambda_function.py` to parse proxy requests, invoke validation, return HTTP 201 for valid payloads, and return HTTP 400 with structured error lists for malformed or invalid payloads.
  - Authored comprehensive test suites and verified execution via `unittest`.
- **Dependencies / Configuration:**
  - Standard library only (`json`, `re`, `logging`, `datetime`, `unittest`, `typing`).
  - Python 3.12 compatible.
  - No external packages or AWS API calls.
- **Tests Run:**
  - Command: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
  - Total Tests: 22 unit tests.
- **Results:** 
  - Initial Run: 21 Passed, 1 Failed (TST-004a: 4 subtests failed in `test_missing_or_empty_body_returns_400`).
  - Recovery Run: 22 Passed, 0 Failed (TST-004b: 100% PASS).
- **Errors Encountered and Resolution:**
  - *Error:* `test_missing_or_empty_body_returns_400` asserted that `body["errors"]` contained the word `"required"`, but handler initially returned `["Missing or empty request body."]`.
  - *Resolution:* Updated `src/lambda_function.py` line 78 to return `"errors": ["Request body is required and cannot be empty."]`. Re-ran all tests; all 22 passed cleanly.
- **Evidence:**
  ```text
  Ran 22 tests in 0.002s
  OK
  ```
- **Remaining Risks:**
  - DynamoDB persistence and SQS enqueueing are not yet connected (deferred to Stage 2 & Stage 3).
  - Idempotency storage is mocked locally via successful status return.
- **Rollback Plan:**
  Remove `src/validation.py`, `src/lambda_function.py`, `tests/test_validation.py`, `tests/test_lambda_function.py`, `events/valid_order.json`, `events/invalid_order.json`.
- **Next Recommended Action:**
  Present Stage 1 results to user. Await explicit approval before beginning Stage 2 (Local order-processing logic, idempotency, and status transitions).

---

## Change Record: CHG-003

- **Change ID:** CHG-003
- **Date/Time:** 2026-10-18T01:37:00+05:30
- **Goal / Requirement:**
  Implement Stage 2: local order-processing service layer with domain model, state machine, idempotency, error handling, and comprehensive unit tests. Approved with strict conditions: no AWS operations, explicit idempotency limitation disclosure, deterministic tests only.
- **Why:**
  The intake handler (CHG-002) accepts and validates requests but does not model the order lifecycle. Stage 2 defines the domain logic that will directly back both the intake Lambda and the worker Lambda in Stage 3: what an order record looks like, which state transitions are valid, how duplicates are detected, and how failures are recorded.
- **Before:**
  `src/` contained only `validation.py` and `lambda_function.py`. No domain model, no state machine, no lifecycle management.
- **Changed Files/Resources:**
  - `src/order_service.py` (created — OrderStatus enum, ALLOWED_TRANSITIONS map, OrderRecord dataclass, custom exceptions, OrderService class)
  - `tests/test_order_service.py` (created — 36 unit tests across 7 test classes)
  - `docs/DECISIONS.md` (updated with DEC-006: Stage 2 design decisions and mandatory idempotency limitation disclosure)
  - `docs/TEST_LOG.md` (updated with TST-005a, TST-005b, TST-005c)
  - `docs/EXECUTION_LOG.md` (updated with this CHG-003 entry)
- **Summary of Change:**
  - Defined `OrderStatus` as a `str` Enum (PENDING, PROCESSING, COMPLETED, FAILED) for JSON-serializable values.
  - Declared `ALLOWED_TRANSITIONS` as an adjacency dict for an auditable, data-driven state machine.
  - Implemented `OrderRecord` as a `dataclass` with `to_dict()` for serialization.
  - Implemented three custom exceptions: `DuplicateOrderError`, `OrderNotFoundError`, `InvalidStateTransitionError`.
  - Implemented `OrderService` with a per-instance `dict` store and public methods: `create_order`, `get_order`, `begin_processing`, `complete_order`, `fail_order`, `retry_order`.
  - Documented in code and DEC-006 that the local idempotency check is NOT concurrent-safe and does NOT substitute for DynamoDB `attribute_not_exists()`.
- **Dependencies / Configuration:**
  - Standard library only (`enum`, `dataclasses`, `datetime`, `logging`, `typing`, `time`).
  - Python 3.12 compatible.
  - No external packages, no AWS API calls.
- **Tests Run:**
  - Baseline: `python -m unittest discover -s tests -v` before new code (22 Stage 1 tests — PASS).
  - Stage 2 only: `python -m unittest tests/test_order_service.py -v` (36 tests — PASS on first run).
  - Full regression: `python -m unittest discover -s tests -v` (58 total tests — all PASS, 0.006s).
- **Results:** 58/58 PASS. Zero failures. Zero regressions against Stage 1.
- **Errors Encountered:** None. All 36 new tests passed on the first run.
- **Evidence:**
  ```text
  Ran 58 tests in 0.006s
  OK
  ```
- **Stage 1 Files Modified?** No. `src/validation.py`, `src/lambda_function.py`, `tests/test_validation.py`, `tests/test_lambda_function.py`, `events/*.json` are all unchanged.
- **Remaining Risks and Unresolved Assumptions:**
  1. **Local idempotency is not concurrency-safe** (documented in DEC-006 and source code). Must be replaced by DynamoDB conditional write in Stage 3.
  2. **`lambda_function.py` does not yet call `OrderService`**: The intake handler still returns a static PENDING response without persisting anything. Wire-up is Stage 3.
  3. **`src/worker.py` is not yet implemented**: SQS message consumption, `begin_processing` / `complete_order` / `fail_order` calls, and DynamoDB update are Stage 3.
  4. **Git repository has no commits yet**: The repo was initialized (`git init` is present) but no files have been added or committed. Consider committing Stage 1 and Stage 2 as separate checkpoints before Stage 3.
- **Rollback Plan:**
  Remove `src/order_service.py` and `tests/test_order_service.py`. Revert `docs/DECISIONS.md`, `docs/TEST_LOG.md`, `docs/EXECUTION_LOG.md` to prior state. Stage 1 files are unaffected.
- **Next Recommended Action:**
  Present Stage 2 results to user. Await explicit approval before beginning Stage 3 (AWS integration design: DynamoDB write, SQS enqueue, IAM policy design, and failure handling specification).

---

## Change Record: CHG-004

- **Change ID:** CHG-004
- **Date/Time:** 2026-10-18T02:10:00+05:30
- **Goal / Requirement:**
  Pre-commit review: inspect Git status, scan for credentials, review Stage 1 and Stage 2 code against CHG-003 design, fix identified defects, run final regression, and create an initial Git checkpoint.
- **Why:**
  User-approved pre-Stage-3 quality gate. Verifies that no credentials, unrelated files, or silent behavioral deviations exist before establishing a rollback checkpoint.
- **Findings and Defects Fixed:**

  1. **Bug: `total_amount` lookup used `or` idiom (latent defect)** — Location: `src/order_service.py` `create_order`. Fixed with explicit `is None` check.
  2. **Defect: `time.sleep(0.001)` + `assertGreaterEqual` weaker than intent** — Location: `tests/test_order_service.py` `test_updated_at_changes_on_transition`. Fixed by adding clock injection to `OrderService.__init__` and using deterministic fake clock in test. `assertGreaterEqual` replaced with `assertNotEqual`.
  3. **No credentials found** — credential scan returned zero matches.
  4. **Git local config** — no remote, no credentials stored. Strictly local.
  5. **Staged file set** — exactly 16 project files; no `.venv`, no `__pycache__`, no parent-directory noise.

- **Changed Files/Resources:**
  - `src/order_service.py` — `Callable` import; clock injection; `self._clock()` replaces `_utc_now()`; `total_amount` fix.
  - `tests/test_order_service.py` — deterministic fake clock; `assertNotEqual`; `time.sleep` removed.
  - `docs/TEST_LOG.md` — TST-006a, TST-006b, TST-006c.
  - `docs/EXECUTION_LOG.md` — this CHG-004 entry.
- **Tests Run:** `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
- **Results:** 58/58 PASS. Evidence: `Ran 58 tests in 0.006s — OK`
- **Rollback Plan:** `git checkout <hash>` restores all files once checkpoint commit exists.
- **Next Recommended Action:** Await explicit approval for Stage 3 AWS integration design.
