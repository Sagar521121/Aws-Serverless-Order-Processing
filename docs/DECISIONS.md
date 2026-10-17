# Architecture and Engineering Decision Log

This document records key decisions, their justification, alternatives evaluated, and trade-offs regarding cost, security, reliability, and maintenance.

---

## DEC-001: Standard Library First (No external frameworks for MVP)

- **Date:** 2026-10-18
- **Decision:** Use Python standard library modules (`json`, `re`, `uuid`, `datetime`, `unittest`, `dataclasses`, `typing`) for core request validation, order modeling, and testing in Stages 1 & 2. Introduce `boto3` only when implementing AWS integrations in Stage 3.
- **Why Selected:**
  - Keeps Lambda deployment packages minimal (under a few kilobytes) and fast to cold-start.
  - Eliminates third-party dependency vulnerabilities and version pinning headaches.
  - Ensures clean, readable code where students/beginners understand exact mechanics without framework magic.
- **Alternatives Considered:**
  - *Pydantic / Marshmallow:* Powerful validation but adds external wheel dependencies, increases packaging complexity, and may have compatibility subtleties across Python versions.
  - *pytest:* Feature-rich, but standard `unittest` comes pre-installed in all Python environments and requires zero external pip installations.
- **Trade-offs:**
  - *Cost:* $0 (no external bandwidth, minimal package size).
  - *Security:* Highly secure (minimal attack surface, no transitive supply chain risks).
  - *Reliability:* High stability across minor Python versions.
  - *Maintenance:* Requires explicit manual schema checks in `validation.py`, which is simpler and educational.

---

## DEC-002: Python 3.12 Compatibility Baseline

- **Date:** 2026-10-18
- **Decision:** Target Python 3.12 compatibility for all application source code, packaging scripts, and tests, even if the local development workstation runs a newer Python version (e.g. 3.14).
- **Why Selected:**
  - The existing AWS Lambda function `order-processing-intake` is deployed with Python 3.12 runtime.
  - Prevents syntax or standard-library feature divergence (e.g. avoiding 3.13/3.14-only syntax) that would fail upon deployment to AWS Lambda.
- **Alternatives Considered:**
  - Upgrading Lambda runtime to Python 3.13: Rejected as it introduces unexpected AWS environment changes without user necessity.
- **Trade-offs:**
  - *Security/Reliability:* High. Python 3.12 is actively supported and stable on AWS Lambda.

---

## DEC-003: Target AWS Region (`ap-south-1`) & Zero Out-of-Pocket Guardrail

- **Date:** 2026-10-18
- **Decision:** Confine all proposed AWS architecture and resource planning strictly to `ap-south-1` (Asia Pacific, Mumbai) and operate strictly within AWS Free Tier limits with zero out-of-pocket costs.
- **Why Selected:**
  - Region requirement specified by user (`ap-south-1`).
  - Strict compliance with user's limited promotional credits and zero-cost policy.
  - Architecture relies on pay-per-use, serverless free-tier components:
    - AWS Lambda: 1,000,000 free requests per month.
    - Amazon SQS: 1,000,000 free requests per month.
    - Amazon DynamoDB: 25 GB storage + 25 Read/Write Capacity Units (On-Demand free tier allowance: 2.5 million read/write request units per month under new tiers, or Provisioned 25 WCU / 25 RCU).
    - Amazon CloudWatch: 5 GB log ingestion free per month.
- **Alternatives Considered:**
  - Multi-region or provisioned clusters (ECS/EKS): Rejected due to immediate non-zero cost and unnecessary complexity.
- **Trade-offs:**
  - *Cost:* Guaranteed $0 under low volume test usage.
  - *Reliability:* High durability and built-in redundancy within `ap-south-1`.

---

## DEC-004: Layered Project Architecture

- **Date:** 2026-10-18
- **Decision:** Split the codebase into distinct modules:
  - `src/validation.py`: Pure schema and data integrity validation (no AWS dependencies).
  - `src/order_service.py`: Business logic, order transitions, idempotency checking.
  - `src/lambda_function.py`: AWS Lambda HTTP Intake entry point (API Gateway event parsing and response formatting).
  - `src/worker.py`: AWS Lambda SQS Worker entry point (SQS message processing).
- **Why Selected:**
  - Separation of concerns: Pure validation and business logic can be unit-tested locally without mocking complex AWS event shapes.
  - Modular packaging: Intake and Worker can share common logic or be packaged independently without code duplication.
- **Alternatives Considered:**
  - Single monolithic file `lambda_function.py`: Difficult to maintain, messy unit testing, harder to decouple intake from worker.
- **Trade-offs:**
  - *Maintainability:* High clarity and modularity.

---

## DEC-005: Order Schema Specification and Validation Rules

- **Date:** 2026-10-18
- **Decision:** Establish exact validation rules for order intake in Stage 1:
  1. **Field Naming:** Standardize on `snake_case` (`order_id`, `customer_id`, `items`, `total_amount`, `currency`) per `docs/ARCHITECTURE.md`. Also accept `totalAmount` as an alias for `total_amount` for client flexibility.
  2. **Order ID Format:** Must be a string of length 8 to 64 characters matching regex `^[a-zA-Z0-9_-]{8,64}$`. Alphanumeric, underscores, and hyphens only.
  3. **Customer ID:** Must be a non-empty string between 1 and 64 characters.
  4. **Items:** Must be a non-empty list. Each item must be an object with:
     - `item_id`: Non-empty string.
     - `quantity`: Positive integer (`int > 0`). Float quantities are rejected.
     - `price`: Positive number (`int` or `float` > 0).
  5. **Total Amount:** Required number (`int` or `float` > 0). Must match the calculated sum of `(quantity * price)` across all items within 0.01 tolerance. This detects cart calculation discrepancies or client manipulation.
  6. **Currency:** Optional 3-letter ISO code string (e.g. `USD`, `INR`). Defaults to `USD` if omitted.
  7. **Error Handling:**
     - Missing body or malformed JSON: Returns HTTP 400 with a descriptive error message.
     - Missing or invalid fields: Returns HTTP 400 with an array detailing each validation failure.
- **Why Selected:**
  - Strict type and sum checking prevents poisoned messages from reaching the queue or database.
  - Tolerating minor float discrepancy (0.01) avoids IEEE 754 precision issues while strictly enforcing price consistency.
- **Alternatives Considered:**
  - *Calculate total on server only without requiring client total:* Rejected because requiring client total validates mutual client-server price agreement and prevents stale checkout charges.
  - *Allow negative or zero prices:* Rejected as invalid domain state for an order.
- **Trade-offs:**
  - *Security/Reliability:* Prevents bad state from polluting the system early at the perimeter.

---

## DEC-006: Stage 2 Order Model, State Machine, and Local Idempotency Design

- **Date:** 2026-10-18
- **Decision:** Implement `src/order_service.py` with the following design choices:

  **1. OrderStatus as a `str` Enum:**
  Using `class OrderStatus(str, Enum)` makes the enum values directly JSON-serializable as plain strings without a custom serializer. The alternative (a plain `Enum`) would require explicit `.value` extraction at every serialization boundary.

  **2. Adjacency-map state machine (`ALLOWED_TRANSITIONS` dict):**
  A declarative dict that maps each status to the set of allowed target statuses. This makes the rules visible, testable in isolation, and easy to extend. The alternative — a set of `if/elif` branches — would scatter transition logic and be harder to audit or test exhaustively.

  **3. `OrderRecord` as a `dataclass`:**
  Standard-library `dataclasses` give typed, readable field definitions with default values and no external dependency. `attrs` or `pydantic` were considered but rejected per DEC-001 (standard-library-first).

  **4. `OrderService` as a class with a per-instance `dict` store:**
  Each test creates a fresh `OrderService()` instance, guaranteeing test isolation without patching global state or using `setUp`/`tearDown` module-level cleanup. The alternative (a module-level singleton dict) would require careful cleanup between tests and risks hidden coupling.

  **5. `error_detail` field on `OrderRecord`:**
  Set only on `FAILED` transitions (via `fail_order(reason)`). Kept as `Optional[str] = None` to avoid needing a union type on every code path that does not fail. This field maps to a DynamoDB attribute in Stage 3 to support diagnostics.

  **6. `retry_order` as an explicit method (PROCESSING → PENDING):**
  Explicitly modelling the retry path in the service layer makes the SQS re-delivery semantics clear and testable locally. Without this, the PROCESSING→PENDING transition is invisible in local tests, which would leave a gap between Stage 2 behavior and Stage 3 reality.

- **LOCAL IDEMPOTENCY LIMITATION (mandatory disclosure):**
  - The `create_order` duplicate check reads `order_id in self._store` then writes. This is a **read-check-then-write** pattern.
  - It is **not atomic** and **not concurrency-safe**.
  - In a real concurrent environment (two simultaneous Lambda invocations), both could pass the check before either writes, creating duplicate records.
  - **Stage 3 resolution:** Replace with DynamoDB `PutItem` using `ConditionExpression=Attr('order_id').not_exists()`. This is atomic at the storage layer and safe under concurrent Lambda execution.
  - **What the Stage 2 tests prove:** Sequential duplicate detection logic is correct. The exception is raised on re-submission in the single-threaded sequential path.
  - **What the Stage 2 tests do NOT prove:** Thread safety, concurrent correctness, or Lambda retry safety under real SQS re-delivery.

- **Alternatives Considered:**
  - *Module-level singleton store:* Rejected — harder to isolate tests and risks state leakage between test cases.
  - *Abstract base class / interface for the store:* Deferred to Stage 3 when the DynamoDB implementation is introduced. Premature abstraction at Stage 2 would add complexity without benefit.
- **Trade-offs:**
  - *Correctness:* High for sequential correctness. Explicitly insufficient for concurrent production use (documented above).
  - *Testability:* High — each test gets a fresh instance.
  - *Simplicity:* High — plain dict, no mocking framework needed.
  - *Stage 3 migration path:* Clean — `OrderService` will be adapted (or subclassed) to call DynamoDB/SQS instead of the local dict.

