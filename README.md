# Event-Driven Order Processing System (AWS)

An educational, production-minded event-driven order processing application built with AWS Serverless components and Python 3.12.

---

## 1. Architecture Summary

```
Client/API Request
       |
       v
 Amazon API Gateway (POST /orders)
       |
       v
 AWS Lambda: order-processing-intake
       |
       +----> Amazon DynamoDB (Store initial order state: PENDING)
       |
       +----> Amazon SQS (Enqueue order event for asynchronous processing)
                     |
                     v
              AWS Lambda: order-processing-worker
                     |
                     v
              Amazon DynamoDB (Update order state: COMPLETED / FAILED)
```

---

## 2. Project Structure

```text
event-driven-order-processing/
├── docs/
│   ├── ARCHITECTURE.md       # Detailed system design, schemas, and failure handling
│   ├── EXECUTION_LOG.md      # Audit log of changes, why made, and results
│   ├── TEST_LOG.md           # Verifiable test results log
│   └── DECISIONS.md          # Architectural decisions and trade-offs
├── src/                      # Application source code
│   ├── validation.py         # Order schema and input validation logic
│   ├── order_service.py      # Domain models, state transitions, idempotency
│   ├── lambda_function.py    # Intake Lambda entry point
│   └── worker.py             # SQS Worker Lambda entry point
├── tests/                    # Unit tests using Python standard unittest
├── events/                   # Sample API Gateway and SQS test payloads
├── scripts/                  # Repeatable packaging scripts for Lambda deployment
├── requirements.txt          # Python dependencies declaration (Standard library first)
├── .gitignore                # Git ignore rules
└── README.md                 # Project documentation
```

---

## 3. Guiding Principles & Constraints

- **AWS Region:** `ap-south-1` (Mumbai).
- **Python Compatibility:** Python 3.12 (matching existing Lambda runtime).
- **Cost Policy:** Strict zero out-of-pocket spending (utilizing AWS Free Tier limits).
- **Security:** Least-privilege IAM policies, no hardcoded secrets or credentials.
- **Traceability:** Every change is documented in `docs/EXECUTION_LOG.md` before and after execution.

---

## 4. Local Development & Testing

Activate your virtual environment and run the test suite:

```powershell
# In PowerShell:
.\.venv\Scripts\Activate.ps1

# Run unit tests:
python -m unittest discover -s tests -v
```
