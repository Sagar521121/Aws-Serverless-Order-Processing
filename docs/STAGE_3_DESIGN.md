# Stage 3 AWS Integration Design (CHG-009 / CHG-010 Recovery Revision)

> **Current pilot boundary:** The deployed worker's business processing is a deterministic local simulation. The DLQ recovery helper is local-only and is not deployed. Operators should inspect DLQ messages and investigate the matching order and logs before considering redrive. No automatic redrive or manual DynamoDB state edits are authorized. Local fake-based tests do not prove real AWS behavior. This design's proposed recovery flows and runbook steps below are not operational authorization.

> **Status:** DRAFT — FOR INDEPENDENT REVIEW; NOT APPROVED FOR AWS DEPLOYMENT  
> **Revision date:** 2026-10-20 (CHG-009 lease recovery and CHG-010 DLQ recovery)  
> **Proposed region:** `ap-south-1`  
> This document describes the Stage 3 design; it is not evidence of live AWS state. No AWS provisioning, deployment, integration test, IAM change, or cleanup is authorized by this document. CHG-009/CHG-010 local code and offline test work is recorded in `docs/EXECUTION_LOG.md`; no AWS state was accessed or changed. CHG-007 corrected worker IAM design, lease-acquisition failure behavior, and recovery-state transitions.

> **Implementation status addendum (2026-10-21):** This remains a design/runbook document; its proposed steps are not operational instructions or approval. Local source and tests now implement the intake, queue/repository adapters, worker state/lease handling, and local-only DLQ recovery helper. Business processing remains a deterministic simulation; payment, inventory, shipping, and fulfillment are not implemented. No AWS deployment or live service behavior is verified. For values that describe deployed configuration, the current `template.yaml` and `template.console.yaml` are the source of truth: 10-second function timeouts, 60-second queue visibility, 20-second default lease (parameter range 11-60), batch size one, source/DLQ retention 4/14 days, and `maxReceiveCount` 5. Values elsewhere in this design are proposed or historical unless they match those templates. Operators should inspect DLQ messages and investigate before redrive; automatic redrive and manual DynamoDB edits are not authorized.

## 1. Scope and current evidence

The proposed Stage 3 system accepts an order through API Gateway, records it in DynamoDB, queues asynchronous work in SQS, and processes it with a worker Lambda. This document describes a single-region student project. It does not claim exactly-once processing or zero cost.

Prior project notes report an existing `order-processing-intake` Lambda and `order-processing-intake-role`, plus the `aws-project-dev` identity and `OrderProcessingProjectDevPolicy`. Those are historical user-reported facts, not live-verified facts for this revision. The current account ID, deployed configuration, exact attached policies, and resource state are unavailable here. Recheck them through the separately gated read-only audit.

Proposed resources: HTTP API `order-processing-api`, route `POST /orders`, DynamoDB table `Orders`, standard SQS queue `orders-queue`, DLQ `orders-dlq`, worker `order-processing-worker`, and pre-created CloudWatch log groups with finite retention. No GSI is proposed for this small project. Without an index or automated sweeper, finding old orders requires a bounded operational Scan; this limitation is explicit below.

## 2. Data model and write-to-queue recovery

### 2.1 Order item

Use `order_id` as the partition key. Store the normalized validated order fields, `payload_hash` (SHA-256 over a documented canonical serialization of the normalized client-controlled order fields), `order_status`, `enqueue_state`, `recovery_generation` (initially zero), `created_at`, `updated_at`, and worker lease fields `lease_owner`, `lease_until`, and durable `attempt_count`. Do not hash timestamps or server-generated fields. A hash match means the same normalized request; a mismatch for the same ID is a conflict.

Suggested statuses are `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, and `RETRY_EXHAUSTED`. `FAILED` means a permanent business/domain rejection. `RETRY_EXHAUSTED` means technical processing did not succeed within the retry policy; it is not evidence that the order itself is invalid. `enqueue_state` is `NEEDS_ENQUEUE` or `ENQUEUED` and is independent of order status.

### 2.2 Minimal dual-write design, state interactions, and recovery transitions

For this single-region project, retain DynamoDB plus SQS with an explicit retry-and-reconciliation path rather than adding heavier event infrastructure (such as transactional outbox tables or stream pollers). This design is minimal and recoverable, but it does **not** make the two writes atomic.

#### 2.2.1 State attribute definitions and interactions

Three core attributes govern order lifecycle and queue coordination:

1. `order_status` (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `RETRY_EXHAUSTED`):
   Represents the domain and execution state of the order.
2. `enqueue_state` (`NEEDS_ENQUEUE`, `ENQUEUED`):
   Represents queue transmission progress. Decoupled from `order_status` so intake failures, duplicate submissions, and worker execution remain auditable independently.
3. `recovery_generation` (integer $\ge 0$, monotonically increasing):
   Tracks operator-authorized redrive and recovery epochs. Every SQS message carries the generation at time of send (`msg_gen`). Workers never process an older-generation message. They acknowledge it only when the newer order is terminal or its current `PENDING`/`PROCESSING` generation is marked `ENQUEUED`; if the current generation remains `NEEDS_ENQUEUE` or its state is inconsistent, they fail the delivery so reconciliation remains possible. Future, missing, or invalid generations follow the investigation paths in Section 3.2.

#### 2.2.2 Explicit conditional state transitions

The system enforces deterministic state transitions using DynamoDB conditional expressions:

##### A. Initial Order Creation (Intake)
- **Action:** Intake validates request, calculates `payload_hash` (SHA-256), and writes the initial record.
- **DynamoDB Operation:** `PutItem`
- **ConditionExpression:** `attribute_not_exists(order_id)`
- **Item State Set:**
  - `order_status = 'PENDING'`
  - `enqueue_state = 'NEEDS_ENQUEUE'`
  - `recovery_generation = 0`
  - `payload_hash = :calculated_hash`
  - `created_at = :now`, `updated_at = :now`
  - `attempt_count = 0`

##### B. Normal Enqueue (Intake post-SendMessage)
- **Action:** After SQS `SendMessage` returns a verified message ID, intake flips `enqueue_state`.
- **DynamoDB Operation:** `UpdateItem`
- **ConditionExpression:**
  ```text
  attribute_exists(order_id) AND
  payload_hash = :msg_hash AND
  recovery_generation = :gen AND
  enqueue_state = 'NEEDS_ENQUEUE'
  ```
- **UpdateExpression:** `SET enqueue_state = 'ENQUEUED', updated_at = :now`
- **Result:** `order_status` remains `PENDING`. `enqueue_state` becomes `ENQUEUED`.

##### C. Failed SendMessage (Intake SQS call failure)
- **Action:** `SendMessage` fails with an explicit error (e.g., SQS throttling or network reset).
- **Behavior:**
  - Do **not** delete or mutate the DynamoDB item.
  - The item remains `order_status = 'PENDING'` and `enqueue_state = 'NEEDS_ENQUEUE'`.
  - Intake returns a retryable HTTP 500 error with correlation ID to the client.
  - An identical client retry (matching `payload_hash`) detects `NEEDS_ENQUEUE` and retries `SendMessage`.
  - If no client retry occurs, operator reconciliation scans identify the stranded `NEEDS_ENQUEUE` item.

##### D. Ambiguous SendMessage Outcome (Intake network timeout or dropped connection)
- **Action:** Intake sends `SendMessage`, but the connection drops before the HTTP response is received. SQS may or may not have accepted the message.
- **Behavior:**
  - Intake cannot confirm delivery; DynamoDB item remains `order_status = 'PENDING'`, `enqueue_state = 'NEEDS_ENQUEUE'`.
  - Return HTTP 503 / 500 with retryable correlation ID to client.
  - **Case 1 (Message was delivered):** Worker picks up message, executes business logic, and transitions `order_status` to `PROCESSING` $\rightarrow$ `COMPLETED`. When client retries, intake observes `COMPLETED`, returns status to client, and does **not** re-enqueue.
  - **Case 2 (Message was lost):** Client retry or operator reconciliation observes `PENDING` + `NEEDS_ENQUEUE` and performs a safe conditional re-enqueue.
  - **Conditional Re-enqueue Check:**
    ```text
    attribute_exists(order_id) AND
    payload_hash = :msg_hash AND
    recovery_generation = :gen AND
    order_status = 'PENDING'
    ```
    If `order_status` has already progressed beyond `PENDING`, no new SQS send is performed.

##### E. Operator Reconciliation (Stale work detection via bounded Scan)
- **Action:** Operator runs a bounded Scan for items where `enqueue_state = 'NEEDS_ENQUEUE'` and `created_at < now - 300 seconds`.
- **Pre-check:** Perform a consistent `GetItem`. If `order_status != 'PENDING'`, do not enqueue; the order has already been picked up (ambiguous send succeeded) or was cancelled.
- **Enqueue:** If `order_status == 'PENDING'`, operator tool sends message with `recovery_generation` and executes:
  ```text
  ConditionExpression:
    attribute_exists(order_id) AND
    payload_hash = :hash AND
    recovery_generation = :gen AND
    enqueue_state = 'NEEDS_ENQUEUE' AND
    order_status = 'PENDING'
  UpdateExpression:
    SET enqueue_state = 'ENQUEUED', updated_at = :now
  ```
  If condition fails, another actor resolved the item; no further action taken.

##### F. Approved Redrive (Operator recovery from `RETRY_EXHAUSTED` or DLQ)
- **Action:** After root cause analysis of a failed/poison batch, operator approves redrive of nonterminal exhausted orders.
- **Step 1 — Generation Bump & Status Reset:**
  ```text
  ConditionExpression:
    attribute_exists(order_id) AND
    payload_hash = :hash AND
    recovery_generation = :current_gen AND
    order_status = 'RETRY_EXHAUSTED' AND
    ((attribute_not_exists(lease_owner) AND attribute_not_exists(lease_until)) OR
     (attribute_exists(lease_owner) AND attribute_exists(lease_until) AND lease_until <= :now))
  UpdateExpression:
    SET order_status = 'PENDING',
        enqueue_state = 'NEEDS_ENQUEUE',
        recovery_generation = :current_gen + 1,
        attempt_count = 0,
        updated_at = :now
    REMOVE lease_owner, lease_until, error_detail
  ```
- **Step 2 — Enqueue with New Generation:**
  Send SQS message carrying `recovery_generation = :new_gen`.
- **Step 3 — Mark Enqueued:**
  ```text
  ConditionExpression:
    attribute_exists(order_id) AND
    payload_hash = :hash AND
    recovery_generation = :new_gen AND
    enqueue_state = 'NEEDS_ENQUEUE'
  UpdateExpression:
    SET enqueue_state = 'ENQUEUED', updated_at = :now
  ```
- The mark-enqueued condition intentionally does not require `order_status = 'PENDING'`: a fast worker may have advanced the new-generation order. If the conditional marker update loses a race, consistently re-read the order. A same-generation `ENQUEUED` or terminal state can resolve the original DLQ record. A newer generation resolves it only when terminal or a supported `PENDING`/`PROCESSING` state marked `ENQUEUED`; retain the record if state remains uncertain.
- **Stale Message Safety:** A delayed message with a generation lower than the stored generation is never processed. The worker acknowledges it only when the current order is terminal or the current `PENDING`/`PROCESSING` generation is marked `ENQUEUED`. If it remains `NEEDS_ENQUEUE`, lacks a valid marker, or has inconsistent state, fail the delivery so reconciliation remains possible. Future, missing, and invalid generations are handled separately under Section 3.2.

##### G. DLQ Triage and Safe Redrive (CHG-010)
- Validate `order_id`, a 64-character SHA-256 `payload_hash`, and a non-boolean, non-negative integer `recovery_generation`, then perform a consistent `GetItem`.
- Classify terminal, stale, payload-mismatched, missing, malformed/future-generation, corrupt, active-lease, and eligible states separately. A verified terminal or stale message may be acknowledged. Retain mismatched, missing, invalid, future, corrupt, and active-lease messages for investigation.
- A current-generation `PENDING` order or expired `PROCESSING` order may be conditionally changed to `RETRY_EXHAUSTED`. Require the order ID, payload hash, generation, and either `PENDING` or `PROCESSING` with stored `lease_until <= :now`; remove lease fields. If the condition fails, re-read and classify.
- Redrive requires explicit operator approval after cause and side-effect review. Only `RETRY_EXHAUSTED` with no valid lease may be atomically reset to `PENDING`, `enqueue_state = 'NEEDS_ENQUEUE'`, and generation `current + 1`. Send the new-generation message, then conditionally mark `ENQUEUED` using order ID, hash, generation, and `NEEDS_ENQUEUE`.
- If `SendMessage` is ambiguous, leave `NEEDS_ENQUEUE` and the original DLQ record in place; do not bump generation again or automatically loop sends. Reconcile whether that generation reached the work queue. An explicit retry reuses the same generation and may create a duplicate, so worker idempotency remains required. Do not leave the DLQ record pending solely because a harmless enqueue-marker race followed by worker progress occurred.
- The operator entry point is `dlq_recovery.operator_redrive_record(...)`, which requires an operator ID and explicit approval, and deletes the original DLQ message only when the result is safely resolved. An ambiguous-send retry additionally requires `retry_pending_send=True`. The SQS-triggered handler only triages and cannot approve its own redrive. Native SQS redrive is not the application recovery path because it cannot assign the incremented application generation.

#### 2.2.3 Inherent limitations of DynamoDB and SQS dual writes

1. **Non-Atomic Dual Write:** There is no distributed transaction spanning DynamoDB item creation and SQS `SendMessage`. Network failures between write 1 and write 2 inevitably produce stranded `NEEDS_ENQUEUE` records.
2. **At-Least-Once Delivery & Duplicate Messages:** SQS standard queues and retry paths guarantee at-least-once delivery, not exactly-once. Duplicate messages will occur; consumers must rely entirely on conditional lease acquisition and terminal-state checks.
3. **Visibility Latency on Stranded Items:** Stranded `NEEDS_ENQUEUE` items require either an active client retry or an operator reconciliation scan to be recovered. Without an automated background sweeper or outbox dispatcher, recovery is not instantaneous.
4. **Single-Region Scope:** All conditional write guarantees and idempotency checks are strictly confined to `ap-south-1`.


## 3. Worker state machine, lease, and side effects

### 3.1 Lease protocol and acquisition

Each delivery from SQS carries `{ "order_id": ..., "payload_hash": ..., "recovery_generation": ..., "version": 1 }`.

Before executing business logic, the worker must acquire an exclusive, time-bounded lease in DynamoDB using a single atomic conditional `UpdateItem`:

```text
ConditionExpression:
  attribute_exists(order_id) AND
  recovery_generation = :msg_gen AND
  payload_hash = :msg_hash AND
  (
    order_status = 'PENDING' OR
    (order_status = 'PROCESSING' AND lease_until <= :now)
  )
UpdateExpression:
  SET order_status = 'PROCESSING',
      lease_owner = :new_token,
      lease_until = :now + 20,
      attempt_count = if_not_exists(attempt_count, :zero) + 1,
      updated_at = :now
```

- `:new_token` is a cryptographically fresh, unpredictable UUID.
- `:now` is current UTC epoch timestamp in seconds.
- `lease_until` is set to `:now + 20 seconds` (providing a 10-second buffer beyond the 10-second Lambda hard timeout).
- If acquisition succeeds, the worker holds exclusive rights to process the order for that attempt.
- The worker consistently reads current state before each acquisition attempt. The conditional update remains authoritative after that read: only a matching generation/hash and either `PENDING` or `PROCESSING` with the stored lease expired may acquire. If a competitor wins after the read, the condition fails; the loser re-reads and does not acknowledge an active-lease collision.

### 3.2 Exhaustive lease-acquisition failure handling

If the conditional lease acquisition fails with `ConditionalCheckFailedException`, the worker **must not execute business logic** and **must not assume the state**. It must immediately execute a consistent point read (`GetItem` with `ConsistentRead=True`) to classify the exact failure cause.

Validate that the message generation is present and is a non-boolean, non-negative integer before attempting lease acquisition. The conditional update requires exact generation equality. If it fails, use the consistent item read to validate the stored generation and classify the mismatch before any processing side effects: a missing or invalid message/item value is corrupt input/state and follows the non-acknowledgment and DLQ investigation path; if `msg_gen < item.recovery_generation`, acknowledge only when the newer order is terminal or is `PENDING`/`PROCESSING` with `enqueue_state = 'ENQUEUED'`. If the newer generation remains `NEEDS_ENQUEUE` or its status/marker is missing or inconsistent, fail the delivery for retry and reconciliation. If `msg_gen > item.recovery_generation`, report `ERR_FUTURE_MESSAGE_GENERATION` and fail the delivery for investigation without silently discarding it. Only equal generations proceed to order-status and lease-failure classification.

The worker must behave according to the exhaustive outcome matrix below:

| Outcome Class | Observed DynamoDB State | Worker Action | SQS Acknowledgment Behavior | Reason & Safety Guarantee |
| :--- | :--- | :--- | :--- | :--- |
| **1. Terminal State** | `order_status IN ('COMPLETED', 'FAILED', 'RETRY_EXHAUSTED')` | Do nothing. Log info: duplicate delivery for terminal order. | **ACKNOWLEDGE (Delete from SQS)** | Safe idempotent replay. Order has reached finality; reprocessing would cause duplicate effects. |
| **2. Another Valid Active Lease** | Valid equal generations and payload hash; `order_status = 'PROCESSING'`; `lease_until > :now`; valid `lease_owner` belonging to another worker | Do NOT run business logic or any order side effects. Log info that a verified duplicate was observed while another worker owns the lease. Fail the invocation. | **DO NOT ACKNOWLEDGE OR DELETE** | The delivery remains hidden until the configured SQS visibility timeout expires, then is eligible for redelivery. A later delivery reads current state and may acquire only through the atomic condition if the stored lease is expired. If the order is terminal, the duplicate is acknowledged under Outcome Class 1. |
| **3. Expired Lease Race** | Conditional acquisition loses a race; consistent re-read shows another worker now owns a valid unexpired lease | Do NOT run business logic or side effects. Fail the invocation and treat this as an active-lease collision. | **DO NOT ACKNOWLEDGE OR DELETE** | The failed delivery is eligible for redelivery after the visibility timeout. The atomic condition prevents this race loser from overwriting a newly acquired lease. |
| **4. Missing Order Record** | `GetItem` returns no item (item does not exist in table) | Do NOT write or create records. Log sanitized alert: `ERR_ORPHAN_QUEUE_MESSAGE`. | **DO NOT ACKNOWLEDGE (Fail invocation / report batch item failure)** | A queue message without a backing database record is an anomaly. Retries allow SQS `maxReceiveCount` to route it to DLQ for investigation. |
| **5. Corrupt State or Missing / Invalid Generation or Lease Fields** | Message or item generation is missing, non-integer, boolean, or negative; order status is unknown; or `PROCESSING` has missing/null `lease_until` or `lease_owner` | Do NOT mutate DynamoDB or perform side effects. Log critical structured alert: `ERR_CORRUPT_ORDER_STATE` (include a bounded reason code). | **DO NOT ACKNOWLEDGE (Fail invocation / report batch item failure)** | The input or stored state cannot be trusted. Preserve it for DLQ investigation. |
| **6. Stale Message Generation** | Message generation is lower than a valid stored generation | Do NOT acquire a lease, mutate the order, or perform side effects. | **ACKNOWLEDGE only if newer state is terminal or PENDING/PROCESSING with `enqueue_state = ENQUEUED`; otherwise fail and retain for retry/DLQ reconciliation.** | A generation mismatch alone does not prove current work was enqueued. In particular, do not discard an older delivery while the newer generation remains `NEEDS_ENQUEUE` or its state is inconsistent. |
| **7. Unexpected Future Message Generation** | Both generations are valid integers and `msg_gen > item.recovery_generation` | Do NOT acquire a lease, mutate the order, or perform side effects. Log critical alert: `ERR_FUTURE_MESSAGE_GENERATION`. | **DO NOT ACKNOWLEDGE (Fail invocation / report batch item failure)** | A message ahead of the stored generation is unexpected; retain it for DLQ investigation instead of silently discarding it. |
| **8. DynamoDB Read Failure** | `GetItem` throws `ClientError`, `ProvisionedThroughputExceededException`, or network timeout | Log warning: unable to determine order state due to database read failure. | **DO NOT ACKNOWLEDGE (Raise exception / fail batch item)** | State is unverified. SQS visibility timeout will govern backoff and redelivery. |

#### 3.2.1 Prevention of uncontrolled retry loops

Non-acknowledgment of messages requiring investigation carries the risk of unbounded retry loops if unconstrained. This design enforces strict bounded circuit breakers:

1. **Hard Bound via SQS `maxReceiveCount`:** All unacknowledged invocations (including active-lease collisions, missing record, corrupt state/generation, unexpected future generation, and DynamoDB read failure) increment the message's `ApproximateReceiveCount`. When this reaches `maxReceiveCount = 5`, SQS automatically moves the message to `orders-dlq`. No message can retry indefinitely.
2. **Backoff via SQS Visibility Timeout:** Every failed invocation causes the message to remain hidden for the full 60-second visibility timeout. Workers do not spin or burn CPU polling the same failing item.
3. **No Blind Acknowledgment:** Acknowledge only a verified terminal order or a stale-generation message whose newer state is terminal or PENDING/PROCESSING with `enqueue_state = ENQUEUED`. For a newer `NEEDS_ENQUEUE` state, active-lease collision, missing/corrupt/future-generation, or otherwise unverified state, fail the invocation without acknowledging so bounded retries and DLQ reconciliation remain available.

### 3.3 Status transitions out of PROCESSING

All transitions out of `PROCESSING` must be atomic and conditional on the worker's active ownership token:

- **Completion:**
  ```text
  ConditionExpression:
    order_status = 'PROCESSING' AND lease_owner = :my_token AND
    lease_until > :now AND recovery_generation = :gen AND payload_hash = :hash
  UpdateExpression:
    SET order_status = 'COMPLETED', updated_at = :now
    REMOVE lease_owner, lease_until
  ```
- **Permanent Business Failure (e.g. invalid stock / declined card):**
  ```text
  ConditionExpression:
    order_status = 'PROCESSING' AND lease_owner = :my_token AND
    lease_until > :now AND recovery_generation = :gen AND payload_hash = :hash
  UpdateExpression:
    SET order_status = 'FAILED', error_detail = :sanitized_code, updated_at = :now
    REMOVE lease_owner, lease_until
  ```
- **Transient Worker Fault / Timeout:** Worker raises an exception. It does **not** update DynamoDB or acknowledge the SQS record. Lambda treats the invocation as failed; the message remains hidden until the configured visibility timeout expires and becomes eligible for redelivery. The next delivery consistently reads the current item and attempts the same atomic lease update, which checks current status, stored `lease_until`, recovery generation, and payload hash. A race loser re-reads and fails rather than overwriting another lease. The 20-second lease and 60-second visibility timeout are proposed values, not verified AWS settings.

### 3.4 Idempotency, failure classes, and retries

The worker must validate the message generation and consistently read current state before doing work. Duplicate delivery for `COMPLETED` or permanent-business `FAILED` is acknowledged without repeating effects. `RETRY_EXHAUSTED` is acknowledged by the worker and requires reviewed operator recovery if it remains in the DLQ. A delivery confirmed to have a valid unexpired lease owned by another worker is failed without acknowledgment or side effects, so it can be retried after the visibility timeout. On redelivery, lease acquisition is permitted only by the atomic update against current stored state. SQS/Lambda retries and duplicate deliveries remain possible; external operations must be idempotent, and this design does not provide exactly-once execution.

Classify known permanent domain failures (for example, a validated business rule rejection) as `FAILED`, conditionally record a sanitized error code, and acknowledge the message. Classify timeouts, throttling, transient dependency failures, and unexpected worker faults as transient: keep or allow the lease to expire, fail the SQS record so it is retried, and do not mark a business failure. Never persist raw exception text containing payloads or secrets.

Configure bounded SQS redrive. When the message reaches the redrive limit, it moves to the DLQ; a DLQ reconciler/runbook then inspects DynamoDB. If the item is nonterminal and attempts are exhausted, conditionally set `RETRY_EXHAUSTED` only when no valid lease exists. A business failure remains `FAILED`, separate from retry exhaustion. For a redrive, first inspect root cause and order state; if approved, conditionally return the item to `PENDING`, clear lease, increment a recovery generation, and enqueue a message with that generation (per Section 2.2.2.F). The worker never processes older messages and only acknowledges them when the current generation is terminal or safely enqueued. Redrive is an operator action, not automatic blind replay.

External side effects (payment, email, inventory reservation, fulfillment) need their own idempotency key, normally stable `order_id` plus operation name, and/or an external idempotency API or transactional outbox. A lease can expire while an external request is still in flight, or the worker can crash after the external effect and before recording `COMPLETED`. The replacement attempt may repeat the effect. DynamoDB conditional writes alone do not provide exactly-once side effects.

### 3.3 Detecting stuck work

An old `PENDING/NEEDS_ENQUEUE`, old `PENDING/ENQUEUED`, or expired `PROCESSING` item is stale work. With no GSI/sweeper, an operator-run bounded Scan is the MVP detection method; retain `created_at`, `updated_at`, and lease timestamps for age filters. CloudWatch alarms on visible-message age/count and DLQ visible messages detect queue symptoms, not every orphaned table record. The runbook must compare the queue, DLQ, and table before re-enqueueing. For expired `PROCESSING` with a usable delivery, redelivery may reclaim only through the conditional update against current stored status and lease expiry. Consider an index or scheduled reconciler only after evidence demonstrates this manual process is inadequate.

## 4. SQS, Lambda, and DLQ settings

Initial settings are deliberately coupled:

| Setting | Proposed value | Reason / condition |
|---|---:|---|
| Worker Lambda timeout | 10 seconds | Hard bound for the short student-project handler. |
| SQS visibility timeout | 60 seconds | At least 6× the Lambda timeout as recommended for SQS event source mappings. |
| Processing lease | 20 seconds | Above the 10-second function timeout with margin; expired lease can be reclaimed on redelivery. |
| Event batch size | 1 | Simplifies record-level handling and retry analysis. |
| `maxReceiveCount` | 5 | Bounded retries before DLQ; review with actual transient failure behavior. |
| Main queue retention | 4 days | Bounded recovery window; messages older than this can be lost from the main queue. |
| DLQ retention | 14 days | Finite investigation/redrive window; inspect before expiry. |
| Log retention | 7 days, if verified suitable | Set explicitly; confirm retention need and applicable cost before provisioning. |

The 60-second visibility timeout exceeds both 6× the 10-second Lambda timeout and the 20-second lease. This reduces premature duplicate delivery but does not prevent SQS standard-queue duplicates. If a batching window is introduced, include it in the visibility calculation. Partial batch response configuration is not needed at batch size one; if batch size changes, design and test it explicitly. AWS recommends `maxReceiveCount` of at least 5 for Lambda/SQS; the proposed value follows that baseline. These are proposed settings, not live configuration.

DLQ runbook: alert and capture the order/generation; inspect sanitized logs and consistently read the item; validate order ID, payload hash, generation, status, and lease fields. Acknowledge only verified terminal messages or stale-generation messages whose newer state is terminal or PENDING/PROCESSING with `enqueue_state = ENQUEUED`. Retain missing, mismatched, malformed, future-generation, corrupt, active-lease, and newer `NEEDS_ENQUEUE` records for investigation. For current-generation PENDING or expired PROCESSING state, conditionally mark RETRY_EXHAUSTED; re-read and classify if that write loses a race. After root-cause and side-effect review, an operator explicitly approves redrive through `dlq_recovery.operator_redrive_record` with an operator ID. The helper conditionally increments generation once, sends that generation, then conditionally marks ENQUEUED. It deletes the original DLQ message only after terminal/stale resolution or a safely recorded redrive. If sending is ambiguous, keep the DLQ record and NEEDS_ENQUEUE state; reconcile the queue before explicitly retrying the same generation. If the post-send read finds a newer generation, only treat it as superseding when that state is terminal or a supported PENDING/PROCESSING state marked ENQUEUED; retain the DLQ message for unresolved or inconsistent state. A confirmed send followed by a same-generation marker race is resolved by rereading state, including terminal worker progress. Do not manually edit DynamoDB or use native SQS redrive for application-generation recovery. Re-enable mappings only after the cause is fixed and separately approved.

## 5. API Gateway security and intake behavior

Use an HTTP API `POST /orders` route with `AWS_IAM` authorization. Caller requests must use SigV4 and credentials from an appropriately scoped IAM role or IAM Identity Center session. Test clients should use an AWS SDK/CLI with temporary credentials from the approved developer role/session; do not embed long-lived access keys in scripts or logs. Grant `execute-api:Invoke` only for the intended API, stage, `POST`, and `/orders` route. Missing/invalid signature or missing invoke permission is rejected with 401/403 and does not invoke intake. Validate the deployed route authorization in the live-test gate.

Add a Lambda resource-based permission for principal `apigateway.amazonaws.com` that permits `lambda:InvokeFunction` only from the exact API execution ARN for the intended account, API ID, stage, method, and route (`...:api-id/stage/POST/orders`), with `SourceAccount` set to the same verified account. Do not use an unrestricted API Gateway source ARN or principal grant. This resource-based permission is separate from the intake execution role.

Set route throttling initially to a conservative target of 1 request/second with burst 2 for the test route, then adjust only with a stated workload and reviewed account-level limits. API Gateway throttles are best-effort protective controls and return 429 when exceeded; they are not a hard spending cap. Authentication also does not limit spend from authorized or compromised callers.

The HTTP API documented payload quota is 10 MB, but this application should enforce a much lower 64 KiB request-body cap before parsing/storing. Reject oversized requests with 413; malformed JSON/schema with 400; same ID/different normalized payload with 409; unauthenticated/unauthorized with 401/403; transient AWS/storage/queue uncertainty with generic 5xx and a correlation ID. Do not return internal exception or AWS details. A matching retry returns an idempotent response with existing order identity/status; do not claim delivery confirmation when SQS outcome is ambiguous.

Safe structured logs may include timestamp, severity, request/correlation ID, route, action, order ID (or a keyed/pseudonymous form if considered sensitive), status transition, attempt number, lease/recovery generation, AWS request ID, and bounded error code. Never log request bodies, customer IDs, item lists, payment data, credentials, authorization headers, full SQS bodies, or raw exception strings. Do not place secrets in Lambda environment variables unless separately approved secret handling is designed.

## 6. IAM design and permission boundary

All ARNs below use `ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT` as a documentation placeholder. No exact account ID is present in authorized evidence, so do not invent one. Before policy review, obtain the account ID from an authorized read-only identity/session and substitute it. Verify region, queue ARN/URL, API ID/stage, function names, and log group ARNs as well. The examples are proposed scopes, not evidence of current grants.

### 6.1 Intake execution role

Trust only `lambda.amazonaws.com`. Pre-create the log group so the runtime role needs only `logs:CreateLogStream` and `logs:PutLogEvents`. Proposed runtime permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "OrderItemAccess", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], "Resource": "arn:aws:dynamodb:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:table/Orders" },
    { "Sid": "EnqueueOrders", "Effect": "Allow", "Action": "sqs:SendMessage", "Resource": "arn:aws:sqs:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:orders-queue" },
    { "Sid": "IntakeLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:log-group:/aws/lambda/order-processing-intake:log-stream:*" }
  ]
}
```

### 6.2 Worker execution role

Trust only `lambda.amazonaws.com`. The Lambda SQS event source mapping uses the function execution role to poll, acknowledge, and manage message visibility. The mapping internally calls `sqs:ChangeMessageVisibility` to extend the visibility timeout while a batch is being processed; without this permission the mapping fails to start or loses the ability to prevent premature redelivery during processing. Proposed runtime permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ConsumeOrders", "Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], "Resource": "arn:aws:sqs:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:orders-queue" },
    { "Sid": "UpdateOrderState", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Resource": "arn:aws:dynamodb:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:table/Orders" },
    { "Sid": "WorkerLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:log-group:/aws/lambda/order-processing-worker:log-stream:*" }
  ]
}
```

> **CHG-007 correction:** `sqs:ChangeMessageVisibility` was missing from the CHG-006 design. The Lambda SQS event source mapping requires four SQS actions on the source queue: `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, and `GetQueueAttributes`. All four are scoped to `arn:aws:sqs:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:orders-queue` only — no wildcard queue access is granted. This is a documentation correction; no live IAM role or policy has been modified.

If encryption or additional AWS integrations are introduced, add only the exact required key/service permissions after separate review. Do not grant broad `Resource: "*"` to data-plane actions. Provisioning permissions such as CreateLogGroup belong to the deployment identity, not Lambda runtime roles.

### 6.3 API Gateway invoke permission

Add a Lambda resource-based statement equivalent to: principal `apigateway.amazonaws.com`; action `lambda:InvokeFunction`; resource the exact intake function/alias; `SourceArn` exactly `arn:aws:execute-api:ap-south-1:ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT:API_ID/STAGE/POST/orders`; and `SourceAccount` `ACCOUNT_ID_REQUIRED_AT_DEPLOYMENT`. Replace placeholders only with audited deployment values. This is required in addition to API route integration configuration.

### 6.4 Developer/deployment permissions

The developer identity must not have `iam:AttachUserPolicy`, `iam:AttachRolePolicy`, `iam:PutRolePolicy`, `iam:CreatePolicyVersion`, `iam:SetDefaultPolicyVersion`, unrestricted `iam:PassRole`, role trust-policy editing, or other permissions that permit privilege escalation. Do not let the developer create or modify privileged execution roles or their inline/attached policies. A separately authorized IAM administrator reviews and installs the narrowly scoped roles/policies and Lambda resource permission.

If the developer is permitted to update Lambda function code/configuration, constrain those actions to the two named functions and constrain `iam:PassRole` to the two exact audited role ARNs with `iam:PassedToService=lambda.amazonaws.com`. The developer may not change those roles' policies or trust. Resource provisioning and API configuration permissions should be individually scoped to the named resources and necessary actions; use a controlled deployment identity if the developer policy cannot be safely constrained. Do not use the AWS root user routinely. Any needed administration must be performed by an appropriately authorized, MFA-protected IAM administrative identity under the account's governance.

Permissions still unverified: exact account ID; current developer policy contents and editor warnings; current intake role trust and attached/inline policies; whether any role may be safely passed; function permissions; API permissions; resource ARNs; queue encryption/KMS configuration; log group existence/retention; organization SCPs, permission boundaries, and session policies. An IAM simulation or policy review must test both allowed and explicitly denied operations before provisioning. No live policy was changed for this document.

## 7. Cost, plan, and eligibility review (mandatory before provisioning)

No price, free-tier entitlement, promotional balance, or zero-cost outcome is asserted here. DynamoDB On-Demand requests are not assumed to be covered by an Always Free allowance. Eligibility and prices can vary by plan, date, region, workload, and service configuration. Budget alerts notify; they are not hard spending caps and do not stop resources or guarantee a maximum bill.

Before any provisioning approval, record a current console review of:

- AWS plan, trial status, and trial expiration date.
- Promotional credit balance, expiry, and eligible services/regions.
- Current regional prices and free-tier eligibility for each proposed service/configuration.
- Existing resources and billable usage in all relevant regions, including the existing Lambda/log groups and any queues, tables, APIs, or networking resources.
- Current billing alerts/budgets, notification recipients, thresholds, and cost exposure; explicitly acknowledge they are not caps.
- DynamoDB capacity mode, expected request volume/item size, and whether On-Demand is appropriate for this workload.
- Expected logging volume/retention, queue retention, API calls, and cost of any proposed monitoring/reconciliation.

Do not provision until this evidence is recorded and the user separately approves the resource gate.

## 8. Operations, alarms, and runbook

Minimal CloudWatch alarms for the integration window: (1) any visible message in the DLQ; (2) oldest message age or visible backlog on the main queue above a short project-defined threshold; (3) worker Lambda errors/timeouts above zero over a short evaluation window. Confirm metric availability, dimensions, alarm cost, and notifications during the read-only/cost review. Alarms identify symptoms; the operator-run DynamoDB Scan is needed to find table-only orphans.

Runbook:

1. On a DLQ alarm, worker error burst, or stale-order report, capture alarm time and correlation/order IDs. Disable the event source mapping if ongoing retries could repeat unsafe effects.
2. Inspect sanitized logs, queue/DLQ metadata, and the corresponding DynamoDB item. Never copy customer payloads or secrets into tickets/logs.
3. Compare status, payload hash, enqueue state, attempt count, lease owner/expiry, generation, and queue/DLQ presence. Do not infer a missing item from an empty queue.
4. For old `NEEDS_ENQUEUE` or pending items with no usable message, retry enqueue using the stored ID/hash/generation only after confirming there is no active lease and side effects are idempotent. For expired `PROCESSING`, the next delivery may acquire the expired lease; if no delivery exists, conditionally reset/re-enqueue under an audited recovery action.
5. For DLQ messages, inspect the message and investigate the matching order state and sanitized logs. Distinguish permanent business `FAILED` from technical `RETRY_EXHAUSTED`. Do not redrive or manually edit DynamoDB under the current pilot authorization; use a separately reviewed and approved procedure before any recovery action.
6. Re-enable mapping only after the cause is fixed and the user has approved resuming live processing. After testing, disable it and verify disabled state.

## 9. Approval gates and required verification

Each gate is separate. Approval of one does not authorize later gates. Cleanup is never bundled with integration testing.

| Gate | Work | Required evidence / approval |
|---|---|---|
| 1. Design revision | CHG-006 documentation revision | Present this document and unresolved items for independent review. This gate does not approve implementation. |
| 2. Read-only AWS, IAM, and billing audit | Inspect current account/resource/policy/plan/cost state without mutation | Record account ID, exact ARNs/policies, route/resource baseline, credits, plan, regional prices/eligibility, existing resources, and billing controls. No AWS calls were made in CHG-006. |
| 3. Offline adapter and contract tests | Implement AWS adapters only after separate approval; use local mocks/contracts, no AWS credentials/API | Include cases listed below; tests do not imply live correctness. |
| 4. IAM policy review and explicit approval | Review role trust, identity policy, resource policy, developer denies, PassRole scope | Named reviewer approves exact policy documents and denied-operation cases before any provisioning. |
| 5. Resource provisioning | Create only specifically listed resources | Separate explicit approval after gates 2 and 4 and cost review. Record resource ARNs/configuration. |
| 6. Code deployment and API configuration | Deploy reviewed code, configure IAM route/throttles/resource permission/event mapping | Separate approval; verify route authorization and exact invoke scope before enabling traffic. |
| 7. Live integration tests | Run bounded scenarios with test data and monitoring | Separate approval; define request count/time/cost guardrails and stop conditions. Keep cleanup out of this gate. |
| 8. Data review and cleanup | Review test records/messages/logs; separately approve deletion or cleanup | Explicit separate approval for each destructive cleanup scope. Record retained data and rollback result. |

Offline and live test plan must cover:

- **Worker IAM Policy Acceptance Criteria (CHG-007):**
  - Offline policy verification must assert that the worker role allows exactly: `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility`, and `sqs:GetQueueAttributes` scoped strictly to the `orders-queue` ARN.
  - Policy must allow `dynamodb:GetItem` and `dynamodb:UpdateItem` scoped strictly to `Orders` table ARN.
  - Policy must allow `logs:CreateLogStream` and `logs:PutLogEvents` scoped to the worker log group.
  - Policy must explicitly deny or omit all other SQS actions (including `sqs:SendMessage`, `sqs:PurgeQueue`, `sqs:DeleteQueue`, `sqs:CreateQueue`) and deny actions on any other queue ARN (including `orders-dlq` unless specifically authorized for an audited redrive utility).
- **Intake IAM Policy Acceptance Criteria:**
  - Offline policy verification must assert that the intake role allows `sqs:SendMessage` scoped strictly to `orders-queue` ARN.
  - Policy must allow `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` on `Orders` table ARN.
  - Policy must explicitly deny or omit message consuming/acknowledging actions (`sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility`).
- **Worker Lease Failure & Race Scenarios (CHG-007):**
  - Verify every branch of the Section 3.2 failure matrix: terminal duplicate delivery (`COMPLETED`, permanent `FAILED`, `RETRY_EXHAUSTED`) is acknowledged/deleted without re-executing logic; a confirmed valid active lease owned by another worker is not acknowledged and becomes retryable after visibility timeout; an expired-lease race loser re-reads and does not overwrite or acknowledge another worker's active lease; missing records, corrupt state, missing/invalid lease fields, and DynamoDB read errors are not acknowledged and follow investigation/retry handling.
  - Verify generation cases separately: lower message generation is never processed and is acknowledged only for a terminal order or current PENDING/PROCESSING state marked ENQUEUED; NEEDS_ENQUEUE or inconsistent state fails delivery. Higher message generation logs `ERR_FUTURE_MESSAGE_GENERATION` and is not acknowledged; missing, boolean, negative, or non-integer message/item generation is classified as corrupt state and is not acknowledged.
  - Verify that no missing, corrupt, future-generation, or otherwise unverified state can be acknowledged. Acknowledgment is allowed for verified terminal state or a stale generation with demonstrably resolved current state; an active-lease collision fails without acknowledgment.
  - Verify that redelivery reads current state and can reclaim `PROCESSING` only through a conditional update checking expired stored lease, recovery generation, payload hash, and new owner token; terminal state is acknowledged without repeating work.
  - Verify SQS `maxReceiveCount = 5` and 60-second visibility timeout circuit breakers prevent runaway retry loops.
- **Dual-Write & Recovery Transitions (CHG-007):**
  - Verify normal enqueue flips `enqueue_state` to `ENQUEUED` under `NEEDS_ENQUEUE` condition.
  - Verify failed `SendMessage` leaves item in `NEEDS_ENQUEUE` with `order_status = 'PENDING'`.
  - Verify ambiguous `SendMessage` handling: client retry with identical hash checks if already processed; if not, retries enqueueing.
  - Verify operator redrive increments `recovery_generation`; older messages are never processed and are acknowledged only when the newer state is terminal or PENDING/PROCESSING with `enqueue_state = ENQUEUED`; newer `NEEDS_ENQUEUE` or inconsistent state remains retryable, while future-generation and missing/invalid-generation messages follow their distinct investigation paths.
- **DLQ Triage and Redrive (CHG-010):**
  - Verify terminal/stale messages are safe to acknowledge; missing, payload-mismatched, malformed, future-generation, corrupt, and active-lease cases are retained for investigation.
  - Verify eligible PENDING or expired PROCESSING state becomes `RETRY_EXHAUSTED` only through a conditional write, and a failed condition is re-read and reclassified.
  - Verify redrive requires explicit operator approval, increments generation once, sends the new generation, and marks ENQUEUED without requiring PENDING so fast worker progress is safe.
  - Verify ambiguous sends retain the original DLQ message and reuse the same generation only after explicit reconciliation/retry; a marker race followed by terminal worker progress does not strand the DLQ record.
  - Verify a post-send read of a newer generation resolves the original DLQ record only for terminal or supported ENQUEUED state; missing, invalid, or NEEDS_ENQUEUE state retains it.
- Worker crash immediately after lease acquisition; expired/stale lease recovery including reconciliation when no usable delivery remains; active-lease collision failure and retry; race loser cannot overwrite a newly acquired lease; completed-order duplicate is acknowledged without repeating work; terminal permanent-failure duplicate.
- DynamoDB success followed by SQS send failure; ambiguous successful send/failed response; identical retries; conflicting payload with reused ID; concurrent identical and conflicting submissions.
- Safe pending-order re-enqueue, duplicate messages, side-effect idempotency, transient versus permanent errors, retry exhaustion, DLQ routing/inspection/redrive, and DynamoDB-to-DLQ reconciliation.
- Unauthorized/missing SigV4 API requests, authorized scoped request, oversized body, throttling response, safe error/log behavior.
- IAM allowed actions and denied operations, including attempts to edit/attach role policies, create privileged policy versions, pass an unintended role, and invoke an unintended route.
- Safe cleanup and rollback as a separate, explicitly approved phase; test cleanup logic offline first and verify exact target identifiers before any live deletion.

## 10. Assumptions and unresolved review items

Assumptions requiring evidence: `ap-south-1` remains the intended region; the historical Lambda/role inventory remains accurate; the order workload fits the proposed 64 KiB request and 10-second worker bounds; 20-second lease and queue settings fit processing behavior; IAM-authenticated test clients are acceptable; manual reconciliation is adequate for a student project; downstream effects can be made idempotent; and the proposed log retention/alarm costs are acceptable.

Unresolved until the read-only audit: exact account ID and ARNs, live resource/policy state, IAM warnings and effective permissions, account plan/credits/pricing/free-tier eligibility, existing billable resources, billing alerts, DynamoDB mode/workload, encrypted-resource requirements, and appropriate log/alarm settings. This design is ready for another independent review only. It is not approved, implementation-ready authorization, or evidence of verified live AWS state.

## References

- [AWS Lambda guidance for SQS event source mappings](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html) — visibility timeout, retry, and receive-count guidance.
- [IAM authorization for API Gateway HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-access-control-iam.html) — SigV4 and `execute-api` permission.
- [API Gateway HTTP API throttling](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-throttling.html) — route/account throttling behavior.
- [HTTP API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html) — documented payload quota; application cap is intentionally lower.
- [Lambda AddPermission API](https://docs.aws.amazon.com/lambda/latest/api/API_AddPermission.html) — service principal source restriction.
