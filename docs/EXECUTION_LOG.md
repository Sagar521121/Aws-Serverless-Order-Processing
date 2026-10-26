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

---

## Change Record: CHG-005

- **Change ID:** CHG-005
- **Date/Time:** 2026-10-18T16:21:00+05:30
- **Goal / Requirement:**
  1. Correct AWS inventory statement in `PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md` to accurately reflect existing AWS Lambda function `order-processing-intake` and IAM execution role `order-processing-intake-role`.
  2. Scan all tracked project files for accidentally committed credentials, tokens, private keys, or secrets.
  3. Verify repository status, branch, commit hash, and exclusion of `.venv` and generated artifacts from version control.
  4. Author comprehensive Stage 3 AWS Integration Design document without modifying application code or performing cloud mutations.
- **Why:**
  Mandatory pre-Stage-3 quality and security gate requested by user. Ensures documentation integrity matches the real baseline state of the AWS account (neither over-stating nor under-stating existing resources), guarantees zero secret leakage before proceeding, and prepares the architectural blueprint for Stage 3 without making premature cloud or code changes.
- **Before:**
  `PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md` contained an outdated statement indicating no application resources had been intentionally created in AWS.
- **Changed Files/Resources:**
  - `PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md` (updated AWS inventory & checkpoint)
  - `docs/TEST_LOG.md` (recorded TST-007a, TST-007b, TST-007c)
  - `docs/EXECUTION_LOG.md` (this CHG-005 entry)
  - `docs/STAGE_3_DESIGN.md` (created Stage 3 AWS Integration Design document)
- **Dependencies / Configuration:**
  - Standard library only, no cloud mutations, no AWS API calls, no remote Git operations.
- **Tests Run:**
  - TST-007a: Regex scan across all 17 tracked repository files for credentials, tokens, keys.
  - TST-007b: `.gitignore` verification via `git status --ignored`.
  - TST-007c: Full regression unit tests: `python -m unittest discover -s tests -v`.
- **Results:**
  - TST-007a: PASS (0 secrets found across all 17 files).
  - TST-007b: PASS (`.venv/`, `src/__pycache__/`, `tests/__pycache__/` properly ignored).
  - TST-007c: PASS (58/58 tests passed in 0.006s).
- **Rollback Plan:**
  Revert changes to `PROJECT_CONTEXT_AND_AI_DEVELOPMENT_GUIDE.md`, `docs/TEST_LOG.md`, `docs/EXECUTION_LOG.md` and remove `docs/STAGE_3_DESIGN.md` if requested.
- **Next Recommended Action:**
  Present Stage 3 AWS Integration Design document to user for review and await approval before any Stage 3 implementation.

---

## Change Record: CHG-006

- **Change ID:** CHG-006
- **Date/Time:** 2026-10-18T20:32:27+05:30
- **Goal / Requirement:** Revise the Stage 3 AWS design in response to the independent Gemini review. Documentation only; no AWS integration implementation.
- **Changed Files:**
  - `docs/STAGE_3_DESIGN.md` — replaced the prior draft with a revised, explicitly unapproved design covering lease recovery, dual-write limitations/reconciliation, API security, least-privilege IAM, current cost evidence requirements, DLQ operations, and separated approval/test gates.
  - `docs/EXECUTION_LOG.md` — recorded this documentation-only change.
- **Not Changed:** Application code, tests, AWS resources, IAM policies, credentials, Git history, and Git remotes. No AWS APIs or account connection were used.
- **Verification:** Documentation and Git working-tree review only. No tests were run, as no application code or tests changed and the request was documentation-only.
- **Remaining Evidence Gaps:** Exact AWS account ID and ARNs; live resource and effective IAM policy state; AWS plan, trial, credits, regional prices/free-tier eligibility, existing resources, billing alerts, and DynamoDB mode/workload all require a later read-only audit. No live AWS state is claimed as verified.
- **Status / Next Action:** Design is ready for another independent review. It is not approved for implementation. Stop here and await explicit user approval before any subsequent work.

---

## Change Record: CHG-007

- **Change ID:** CHG-007
- **Date/Time:** 2026-10-18T20:47:00+05:30
- **Goal / Requirement:**
  Documentation-only correction to `docs/STAGE_3_DESIGN.md` addressing three specific findings from independent review:
  1. Worker IAM permissions: Added `sqs:ChangeMessageVisibility` to the worker execution role policy, scoped strictly to `orders-queue`, explaining that the Lambda SQS event source mapping requires this to manage visibility during processing. Updated offline IAM acceptance criteria.
  2. Worker lease-acquisition failure: Exhaustively specified behavior for all outcomes following a conditional lease acquisition failure (terminal states, another valid active lease, expired lease race, missing order records, corrupt/inconsistent lease fields, DynamoDB read failures). Established that unverified nonterminal states must never be acknowledged, and that SQS `maxReceiveCount = 5` and visibility timeout (60s) act as hard circuit breakers against uncontrolled retry loops.
  3. Recovery-state transitions: Defined explicit DynamoDB conditional transitions for initial order creation, normal enqueue, failed SendMessage, ambiguous SendMessage outcomes, operator reconciliation, and approved redrive. Specified interactions between `enqueue_state`, `order_status`, and `recovery_generation` (stale message prevention). Explicitly documented remaining limitations of DynamoDB/SQS dual writes.
- **Changed Files:**
  - `docs/STAGE_3_DESIGN.md` (updated sections 2.2, 3.1–3.4, 6.2, 9)
  - `docs/EXECUTION_LOG.md` (appended this CHG-007 entry)
- **Not Changed:**
  - Application code, unit tests, IAM policies, credentials, or AWS resources.
  - No AWS APIs called, no packages installed, no Git remotes configured, no commits created.
  - Prior working-tree changes preserved without disturbance.
- **Verification:**
  - Documentation and working-tree review.
  - Full local unit test regression run: 58/58 tests passing in 0.005s.
- **Status / Next Action:**
Stage 3 design is updated and ready for independent review. Stage 3 is NOT approved for implementation. No cloud resources, IAM changes, deployment, or integration tests are authorized. Stop here and await explicit user review.

---

## Change Record: CHG-008

- **Change ID:** CHG-008
- **Date/Time:** 2026-10-18T20:41:20+05:30
- **Goal:** Documentation-only correction of the active-lease duplicate and recovery-generation handling inconsistencies.
- **Changed Files:** `docs/STAGE_3_DESIGN.md` (failure matrix, generation processing rules, retry/acknowledgment rules, and acceptance criteria); `docs/EXECUTION_LOG.md` (this record).
- **Summary:** A confirmed duplicate with another worker's valid unexpired lease is acknowledged without order side effects; missing, corrupt, unverified, and read-failure cases remain retryable. Stale, future, and missing/invalid generations now have distinct, consistent outcomes. Expired-lease reconciliation remains required.
- **Verification:** Inspected the final documentation diff and working-tree status. No tests or AWS operations were performed. No application code or tests, IAM policies, resources, credentials, Git history, or remotes were changed.
- **Status:** Documentation remains subject to the existing approval gates; no implementation approval is implied.

---

## Change Record: CHG-009

- **Change ID:** CHG-009
- **Date/Time:** 2026-10-20T12:18:01+05:30
- **Goal / Requirement:** Implement worker lease recovery so active-lease collisions are retried rather than acknowledged, and expired work is reclaimed only through current-state reads and conditional DynamoDB writes.
- **Why:** A duplicate delivery deleted during another worker's active lease cannot provide a later retry. Conditional acquisition, owner-token transitions, and Lambda/SQS failure semantics preserve retry opportunities while preventing a race loser from taking another worker's lease.
- **Before:** No worker module or DynamoDB adapter existed. The Stage 3 matrix acknowledged a delivery that observed another worker's active lease.
- **Changed Files:**
  - `src/worker.py` (new: SQS event handling, message validation, lease acquisition, terminal/stale handling, safe retry outcomes)
  - `src/dynamo_order_repository.py` (new: consistent reads and conditional lease/status writes)
  - `tests/test_worker.py` and `tests/test_dynamo_order_repository.py` (new offline unit/adapter contract tests)
  - `docs/STAGE_3_DESIGN.md` (corrected lease matrix, recovery rules, runbook and acceptance criteria)
  - `docs/TEST_LOG.md` and `docs/EXECUTION_LOG.md` (verification and change records)
- **Summary:** Worker messages validate order ID, SHA-256 payload hash, and generation. Terminal and stale duplicates return successfully; missing/corrupt/future messages and active-lease collisions fail the invocation. Redelivery consistently reads the item and uses a conditional update that checks status, stored expiry, generation, and hash. Lease-owner, generation, hash, and unexpired-lease conditions guard completion/failure transitions. No exactly-once guarantee is claimed; repeated deliveries and business-operation idempotency remain necessary.
- **Dependencies / Configuration:** Standard library code; boto3 is imported lazily from the AWS Lambda runtime for default adapters. No package installation, environment change, IAM change, resource change, or AWS call was performed. Lambda event-source handling assumes the documented batch size of one.
- **Tests Run:** Focused worker/DLQ/repository suite and full `unittest` regression; see TST-008a through TST-008f in `docs/TEST_LOG.md` for initial failures and final results.
- **Results:** Final focused recovery suite: 34/34 PASS. Final full regression: 92/92 PASS. Earlier test-fixture failures and their fixes are preserved in the test ledger.
- **Evidence:** Final command `\.venv\Scripts\python.exe -m unittest discover -s tests -v` reported `Ran 92 tests ... OK`.
- **Risks / Limitations:** `_default_processor` fails closed until a project-specific business-processing callback is supplied; this change does not invent payment, inventory, or fulfillment behavior. No live Lambda mapping, queue settings, or IAM permission was verified or changed. External side effects still require idempotency.
- **Rollback:** Revert only the CHG-009 additions in the listed files, preserving pre-existing unrelated working-tree changes; remove the newly added worker/repository modules and their tests if reverting the feature.
- **Follow-up:** Deployment wiring, business processor implementation, and separately authorized AWS/IAM configuration remain outside this local implementation.

---

## Change Record: CHG-010

- **Change ID:** CHG-010
- **Date/Time:** 2026-10-20T12:18:01+05:30
- **Goal / Requirement:** Implement validated, conditional, operator-approved DLQ triage and generation-safe redrive.
- **Why:** Native SQS redrive does not assign the application's incremented recovery generation. A conditional application workflow prevents mismatched or unsafe messages from being replayed and handles the non-atomic DynamoDB/SQS send boundary explicitly.
- **Before:** The design described manual DLQ inspection/redrive without an implemented classifier or safe send/marker protocol.
- **Changed Files:**
  - `src/dlq_recovery.py` (new: classification, exhaustion transition, approved redrive, ambiguous-send handling, and safe original-message deletion)
  - `src/dynamo_order_repository.py` (conditional exhaustion, generation reset, and enqueue marker operations)
  - `tests/test_dlq_recovery.py` and `tests/test_dynamo_order_repository.py` (offline tests for classification, write races, ambiguous sends, and deletion rules)
  - `docs/STAGE_3_DESIGN.md` (DLQ workflow and operator runbook)
  - `docs/TEST_LOG.md` and `docs/EXECUTION_LOG.md` (verification and change records)
- **Summary:** DLQ messages are validated against a consistent order read, including payload hash, generation, status, and lease state. Terminal/stale and unsafe cases are classified explicitly. Eligible state is conditionally marked `RETRY_EXHAUSTED`; approved redrive increments generation once, sends that generation, and conditionally marks `ENQUEUED`. Ambiguous sends retain the original DLQ message and generation for explicit reconciliation/retry. Enqueue-marker races re-read state so already-terminal worker progress does not strand a resolved DLQ record. Original DLQ deletion occurs only for safe classifications.
- **Dependencies / Configuration:** Standard library; boto3 is lazily used by the Lambda runtime adapter. No dependencies installed, AWS APIs called, resources created, or IAM policies changed. Live execution requires separately reviewed configuration and permissions.
- **Tests Run:** Focused recovery/adapter suite and full `unittest` regression; see TST-008a through TST-008f in `docs/TEST_LOG.md`.
- **Results:** Final focused recovery suite: 34/34 PASS. Final full regression: 92/92 PASS.
- **Evidence:** Offline fakes exercised terminal/stale/mismatch/missing/future/active cases, conditional-write races, generation increment, ambiguous sends, fast worker completion, and safe DLQ deletion. No AWS behavior was exercised.
- **Risks / Limitations:** An ambiguous SQS send requires operator reconciliation; an explicitly approved same-generation retry may create a duplicate, which the worker must safely tolerate. The operator helper is not deployed or IAM-wired. No exactly-once behavior is claimed.
- **Rollback:** Revert only the CHG-010 portions of the listed files, preserving unrelated work; remove `src/dlq_recovery.py` and its tests if reverting the feature.
- **Follow-up:** DLQ utility deployment, exact least-privilege access review, and live integration validation require separate approval. CHG-010 leaves the existing `maxReceiveCount` and `RETRY_EXHAUSTED` policy unchanged.

---

## Change Record: CHG-011A

- **Change ID:** CHG-011A
- **Date/Time:** 2026-10-20T12:29:07+05:30
- **Goal / Requirement:** Connect local intake, domain service, DynamoDB repository, SQS publisher, worker, and operator DLQ recovery paths with injectable adapters and offline integration coverage.
- **Changed Files:** `src/intake_service.py`, `src/sqs_adapter.py`, `src/local_adapters.py`, and `src/business_processing.py` (new); `src/lambda_function.py`, `src/dynamo_order_repository.py`, and `src/worker.py`; `tests/test_local_integration.py` (new), `tests/test_lambda_function.py`, `tests/test_dynamo_order_repository.py`; `docs/TEST_LOG.md` and this record.
- **Summary:** Intake validates/canonicalizes client fields, computes a SHA-256 hash, conditionally creates `PENDING/NEEDS_ENQUEUE`, sends a generation-tagged message, and records `ENQUEUED`. Same-payload retries reuse the record; changed payload under the same ID conflicts. Send failures leave `NEEDS_ENQUEUE` for retry. Worker leases/terminal duplicates and the existing approved DLQ generation/redrive protocol are exercised against a shared local repository. AWS adapters are lazy and injectable.
- **Local business behavior:** `process_order_locally` validates the stored order and exercises the existing `OrderService` state machine, producing deterministic simulated acceptance. It does not charge payment, reserve inventory, arrange shipping, fulfill orders, or call another service. Retries and duplicates remain possible; no exactly-once guarantee is claimed.
- **Tests Run:** Initial focused run: 14 passed, 1 failed because a sample-event test had not injected local adapters after AWS default wiring; the test was corrected. Corrected focused run: 15/15 passed. Intermediate full suite: 99/99 passed. Final full suite after extra coverage: 102/102 passed. See TST-009a through TST-009d in `docs/TEST_LOG.md`.
- **Final Test Command / Evidence:** `\.venv\Scripts\python.exe -m unittest discover -s tests -v` reported `Ran 102 tests ... OK`. All integrations used local fakes; no network or AWS service was accessed.
- **Risks / Limitations:** Local adapters test coordination logic, not AWS behavior. Real Lambda/SQS event-source batch settings, DynamoDB table schema, IAM, queue URLs, visibility timeout, and deployment wiring were not verified. SQS send and DynamoDB marker updates are non-atomic, so duplicate messages remain possible and must be handled idempotently.
- **AWS Activity:** No resources created, deployment performed, live AWS API called, IAM changed, credentials accessed, files staged, or commit created.
- **Follow-up:** CHG-010 deployment/readiness and any real payment, inventory, shipping, or fulfillment integration remain separate work.

---

## Change Record: CHG-011B (Local Deployment Preparation)

- **Change ID:** CHG-011B
- **Date/Time:** 2026-10-20T13:08:32+05:30
- **Goal:** Prepare local Lambda packaging and least-privilege infrastructure definitions for the existing intake/worker architecture without accessing AWS or deploying.
- **Changed Files:** `template.yaml`, `samconfig.toml`, `.gitignore`, `docs/DEPLOYMENT.md`, `README.md`, `src/worker.py`, `src/lambda_function.py`, `tests/test_deployment_config.py`, `tests/test_lambda_function.py`, `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **Summary:** Added an AWS SAM template for the IAM-authorized HTTP API, intake and worker Lambda functions, on-demand encrypted DynamoDB table, standard SQS queue and DLQ, batch-size-one event mapping, finite-retention log groups, scoped runtime roles, and API Gateway invoke permission. Defaults match the design: 10-second Lambda timeout, 60-second visibility timeout, 20-second lease, five receives to DLQ, 4/14-day queue retention, seven-day logs, and 1 rps/burst-2 API throttle. Account/region ARNs use pseudo parameters; `ResourcePrefix` is configurable. The table has a Retain policy. Added SAM build/deploy/cleanup instructions and a 64 KiB intake-body guard. Worker lease duration now reads the deployment environment parameter (default 20 seconds; allowed range 11–60).
- **Simulated business behavior:** The deployed worker handler still uses the deterministic local simulation callback. No payment, inventory, shipping, fulfillment, or other external business integration is defined.
- **Verification:** Focused configuration/handler/worker suite: initial 24 passed, 1 failed because oversized whitespace was rejected as empty before checking the size cap; after correcting the handler order, 25/25 passed. Final full suite: 107/107 passed. See TST-010a through TST-010c in `docs/TEST_LOG.md`.
- **Commands:** `\.venv\Scripts\python.exe -m unittest tests.test_deployment_config tests.test_lambda_function tests.test_worker -v`; final `\.venv\Scripts\python.exe -m unittest discover -s tests -v` (107 tests, OK). `git diff --check` was run after the final changes.
- **Initial template validation status:** At the time this preparation record was written, SAM CLI and `cfn-lint` were unavailable. The Python checks were configuration-contract checks only; the separate CHG-011B Validation Gate below records the later SAM and CloudFormation lint results.
- **Cost / cleanup:** No free-tier or zero-cost claim is made. DynamoDB on-demand use, deployed service usage, retained table data, CloudWatch logs, and SAM's S3 artifact bucket can incur charges. Cleanup instructions explain queue-message loss on stack removal and the retained DynamoDB table.
- **AWS Activity:** No credentials accessed, AWS API calls made, resources created, IAM permissions changed, deployment attempted, or charges incurred. No staging or commit performed.
- **Remaining Operational Work:** Review the target account, effective deployment permissions, generated change set, costs, and caller's narrow `execute-api:Invoke` grant. The current DLQ recovery module is not deployed as an operator tool; a production approval/redrive execution path remains separate work.

---

## CHG-011B Validation Gate Follow-up

- **Date/Time:** 2026-10-20T13:49:50+05:30
- **Installed tools:** AWS SAM CLI 1.169.0 installed for the current user from AWS's official Windows MSI; `cfn-lint` 1.57.2 installed into the existing `.venv`. No AWS credentials or IAM settings were changed.
- **Template correction:** `cfn-lint template.yaml` initially reported W3005 because `OrdersRoute` explicitly depended on `IntakeIntegration`, which it already references through its `Target`. Removed that redundant `DependsOn`; the final `cfn-lint template.yaml` completed with exit code 0 and no findings.
- **SAM validation:** `sam validate --lint --template-file template.yaml` completed with exit code 0 and reported `template.yaml is a valid SAM Template`. SAM CLI's config directory was redirected to a temporary local path and telemetry was disabled for the command.
- **SAM build:** `sam build --template-file template.yaml` completed with exit code 0 (`Build Succeeded`) and packaged `IntakeFunction` and `WorkerFunction` under `.aws-sam/build`. SAM reported that `src/requirements.txt` was absent and continued without dependencies; the current source package therefore builds without a project dependency installation step. The first sandboxed attempt hit `WinError 5` while cleaning SAM's local scratch directory; the same local build was rerun with the required filesystem access and succeeded. No AWS service was contacted.
- **Full tests:** `\.venv\Scripts\python.exe -m unittest discover -s tests -v` — 107 tests ran, all passed (`OK`). No test used AWS credentials or live services.
- **Patch check:** `git diff --check` completed with exit code 0 and no whitespace errors. Git emitted only existing working-copy LF-to-CRLF conversion notices.
- **Changed Files for this validation follow-up:** `template.yaml` (removed the redundant dependency), `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **AWS Activity / Scope:** No `sam deploy`, `sam sync`, CloudFormation operation, AWS API call, resource creation, IAM change, credential access, or cloud cost. Build output is local and ignored by Git. No files were staged or committed.
- **Remaining Blockers:** None for local SAM validation/build. Live deployment still requires separate review and authorization; this gate does not verify live account permissions or service behavior.

---

## Change Record: CHG-013

- **Change ID:** CHG-013
- **Date/Time:** 2026-10-21T00:27:01+05:30
- **Goal / Requirement:** Convert decimal-valued Python floats to DynamoDB-compatible `Decimal` values at the persistence boundary.
- **Why:** Boto3's DynamoDB Table resource rejects Python floats, so a valid intake could fail during its conditional order write before SQS publishing.
- **Changed Files:** `src/dynamo_order_repository.py`, `tests/test_dynamo_order_repository.py`, `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **Summary:** `create_pending` now passes a recursively copied item to `put_item`, converting each float using `Decimal(str(value))`. Nested order-item prices and totals are converted; integer quantities and other values are preserved. The input item is not mutated. Intake canonicalization, payload hashing, response handling, validation, and enqueue flow are unchanged.
- **Verification:** Focused repository suite: 8 passed, 1 skipped. Full suite: 108 passed, 1 skipped. See TST-012a and TST-012b in `docs/TEST_LOG.md`.
- **Boto3 limitation:** Boto3 is not installed in the existing virtual environment. The optional `TypeSerializer` regression test was skipped; no package was installed. The available tests verify exact Decimal values at the `put_item` boundary and unchanged integer quantities.
- **AWS Activity / Scope:** No AWS credentials or services accessed; no API calls, deployment, resource changes, IAM changes, or package installation. No files staged or committed.
- **Remaining Risk / Follow-up:** CHG-014 remains separate: worker-side payload validation currently has its own numeric-type behavior and was not changed here. No claim is made that this change fixes worker validation.

---

## Change Record: CHG-014

- **Change ID:** CHG-014
- **Date/Time:** 2026-10-21T00:47:53+05:30
- **Goal / Requirement:** Make shared order validation compatible with DynamoDB Decimal values while preserving numeric and quantity safeguards.
- **Why:** DynamoDB resource reads return numeric attributes as `Decimal`; the worker's shared validator previously accepted only `int` and `float`, potentially sending valid records into retries and the DLQ.
- **Changed Files:** `src/validation.py`, `src/business_processing.py`, `src/worker.py`, `tests/test_validation.py`, `tests/test_worker.py`, `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **Summary:** Validation now accepts finite `Decimal`, `int`, and `float` prices/totals, rejects booleans, numeric strings, and non-finite numbers, and accepts integer-valued Decimal quantities returned by DynamoDB while rejecting fractional, zero, negative, and boolean quantities. Line-item totals are compared with exact rational arithmetic using the existing 0.01 threshold. Invalid stored business data raises a dedicated error which the worker records as `FAILED`; unrelated exceptions retain retry behavior. Intake JSON handling and response formats were not changed.
- **Verification:** Focused validation/worker/local integration suite: 40/40 passed. Full suite: 113 passed, 1 skipped (114 tests run). See TST-013a and TST-013b in `docs/TEST_LOG.md`.
- **Limitations:** The optional Boto3 `TypeSerializer` regression test remains skipped because Boto3 is unavailable locally. Worker integration was verified with offline fakes and the deterministic local callback, not live AWS services.
- **AWS Activity / Scope:** No AWS credentials, APIs, services, or resources accessed; no deployment, IAM changes, dependency installation, staging, or commit.
- **Rollback:** Revert only the CHG-014 edits in the listed application and test files, preserving CHG-013 and all unrelated work.

---

## Change Record: CHG-016

- **Change ID:** CHG-016
- **Date/Time:** 2026-10-21T01:19:06+05:30
- **Goal / Requirement:** Prevent an older-generation SQS delivery from being acknowledged while the stored current generation still requires enqueue reconciliation.
- **Root Cause:** `worker._classify_item` returned `STALE` immediately when the message generation was lower than the stored generation. That bypassed `enqueue_state`; after a redrive incremented the generation but its send was unresolved, the old queued delivery could be acknowledged while the current order remained `PENDING/NEEDS_ENQUEUE`.
- **Changed Files:** `src/worker.py`, `tests/test_worker.py`, `tests/test_local_integration.py`, `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **Implementation:** A stale message is acknowledged only when the stored order is terminal or the active PENDING/PROCESSING generation is marked `ENQUEUED`. When it is `NEEDS_ENQUEUE`, missing a valid marker, or in an unsupported state, the worker raises `RetryableDeliveryError`. A matching-generation message continues through existing conditional lease acquisition even when enqueue marker update is still pending, because the delivered message itself can exist before that marker update completes. Existing lease owner, expiry, and generation conditions remain unchanged.
- **Regression Coverage:** Added tests for stale messages against PENDING/ENQUEUED, PROCESSING/ENQUEUED, PENDING/NEEDS_ENQUEUE, terminal, and inconsistent states; verifies retry behavior after a race re-read and current-generation processing with NEEDS_ENQUEUE; added a local redrive-state integration regression reproducing generation increment with an unresolved enqueue marker. Existing competing-worker lease-race coverage remains passing.
- **Verification:** Focused worker/repository/local-integration/DLQ suite: 54 passed, 1 skipped (55 run). Full suite: 120 passed, 1 skipped (121 run). See TST-014a and TST-014b in `docs/TEST_LOG.md`.
- **Commands:** `\.venv\Scripts\python.exe -m unittest tests.test_worker tests.test_dynamo_order_repository tests.test_local_integration tests.test_dlq_recovery -v`; `\.venv\Scripts\python.exe -m unittest discover -s tests -v`.
- **Limitations:** The skipped test is the optional Boto3 serializer test because Boto3 is not installed. Tests use local fakes; no live SQS/DynamoDB behavior was exercised. SQS remains at-least-once; this change does not claim exactly-once delivery.
- **AWS Activity / Scope:** No AWS CLI, SDK, API, deployment, upload, or resource operation was performed. No package was installed. No files staged or committed.

---

## Change Record: CHG-017A

- **Change ID:** CHG-017A
- **Date/Time:** 2026-10-21T01:47:37+05:30
- **Goal / Requirement:** Prevent a confirmed recovery send from causing the original DLQ message to be deleted when a newer generation is not demonstrably safe.
- **Root Cause:** After `SendMessage` succeeded but marking the generation `ENQUEUED` failed, `_safe_after_enqueue` treated any newer generation as `SUPERSEDED` without checking its status or enqueue marker. A newer `PENDING/NEEDS_ENQUEUE` or malformed state could leave recovery unresolved after the DLQ receipt was deleted.
- **Changed Files:** `src/dlq_recovery.py`, `tests/test_dlq_recovery.py`, `docs/STAGE_3_DESIGN.md`, `docs/EXECUTION_LOG.md`, and `docs/TEST_LOG.md`.
- **Implementation:** A newer generation is now considered resolved only when it is terminal, or is a supported `PENDING`/`PROCESSING` state marked `ENQUEUED` with consistent lease metadata. Invalid generations, missing or mismatched rereads, `NEEDS_ENQUEUE`, missing/invalid markers, unsupported states, and inconsistent leases return `ENQUEUE_STATE_UNCERTAIN` and retain the DLQ message. Existing generation conditions, lease model, idempotent same-generation retry, and ambiguous-send behavior remain in place. No new states or exactly-once claim were introduced.
- **Regression Coverage:** Added post-send marker-race cases for newer `PENDING/NEEDS_ENQUEUE`, missing/invalid enqueue markers, inconsistent lease data, valid newer enqueued states and terminal states, missing/malformed reread state, same-generation retry, and competing initial redrive. Ambiguous send coverage asserts the original DLQ receipt is not deleted. Stage 3 stale-generation prose now matches CHG-016. The recovery helper remains local code and is not exposed through a deployed operator interface.
- **Verification:** Focused DLQ/worker/integration/repository suite: 60 passed, 1 skipped (61 run). Full suite: 126 passed, 1 skipped (127 run). `git diff --check` passed with no whitespace errors. See TST-015a through TST-015c in `docs/TEST_LOG.md`.
- **Limitations:** The optional Boto3 serializer test remains skipped because Boto3 is not installed; no dependency was installed. Tests use local fakes and do not exercise live SQS or DynamoDB semantics. There is no deployed operator recovery interface, and ambiguous send outcomes still require controlled same-generation operator reconciliation.
- **AWS Activity / Scope:** No AWS CLI, SDK, live API, deployment, upload, CloudFormation operation, or resource change was performed. No files staged or committed.

---

## Change Record: CHG-019

- **Change ID:** CHG-019
- **Date:** 2026-10-21
- **Goal:** Record pilot deployment-readiness evidence locally without deploying or enabling recovery.
- **Changed Files:** `tests/test_dlq_recovery.py`, `README.md`, `docs/DEPLOYMENT.md`, `docs/STAGE_3_DESIGN.md`, `docs/TEST_LOG.md`, and this execution log. Rebuilt `artifacts/console-deployment/order-processing-lambda.zip`.
- **Implementation:** Added a regression where DLQ deletion raises: the helper reports `DLQ_DELETE_UNCERTAIN`, sets acknowledgment false, and records no successful deletion. A retry of the same terminal message is safely classified and can delete after the fake client recovers, without publishing. Updated pilot documentation to state that business processing is simulated, recovery is local-only, operators should inspect and investigate DLQ messages, and neither automatic redrive nor manual DynamoDB edits are authorized. Documented that fake tests do not prove AWS behavior.
- **Package Evidence:** Rebuilt the console ZIP with `scripts/package_console_lambda.py`; it contains eight runtime modules at the ZIP root and both configured handler modules import successfully from the archive. Path: `artifacts/console-deployment/order-processing-lambda.zip`; size: 16,395 bytes.
- **Verification:** Focused DLQ suite 23 passed; full suite 127 passed and 1 optional Boto3 test skipped (128 run); deployment configuration suite 4 passed; `cfn-lint` reported no findings for both templates; `git diff --check` passed. See CHG-019 in `docs/TEST_LOG.md`.
- **Limitations:** All application and deployment checks are local. Fakes and static template lint do not prove real SQS, DynamoDB, Lambda, IAM, or CloudFormation behavior. The optional Boto3 serializer test remains skipped because Boto3 is unavailable. No automatic or operator recovery interface was added or deployed.
- **AWS Activity / Scope:** No AWS CLI/SDK, live API, deployment, upload, CloudFormation operation, resource creation/change, staging, or commit was performed. No dependency was installed.

## Change Record: CHG-027

- **Date:** 2026-10-21
- **Scope:** Local test-only characterization of DynamoDB commit-then-raise outcomes for create, enqueue marker update, worker lease acquisition, and completion.
- **Evidence:** Each one-shot wrapper asserted the underlying `LocalOrderRepository` operation committed before raising. Focused integration tests: 19 passed; repository tests: 15 passed; worker tests: 20 passed; full suite: 153 passed, 0 failed, 0 errors, 0 skipped. The recorded full run used project `.venv` Python 3.14.0. See TST-016 in `docs/TEST_LOG.md`.
- **Limitations:** This establishes the local code's response to injected ambiguity only. It does not establish real DynamoDB consistency, conditional-write behavior, or SQS behavior. No AWS operation was performed.

## Change Record: CHG-028

- **Date:** 2026-10-21
- **Scope:** Documentation reconciliation only. Updated architecture, README, project context, test log, execution log, deployment guide, Stage 3 design, and decisions. No application, test, template, package, or AWS resource was changed.
- **Evidence basis:** Current source and both templates were reviewed. Template values are documented as configuration definitions, not deployed settings. CHG-027 local test evidence is labeled with its environment and limitations; earlier test results remain historical.
- **Limitations:** No tests, deployment, live AWS checks, ZIP rebuild, or artifact parity check were performed for this documentation change. No AWS operation was performed.
