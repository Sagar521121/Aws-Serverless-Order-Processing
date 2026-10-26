# Console Deployment Package

This guide prepares a manual AWS Management Console deployment. It does not authorize or perform deployment. `template.yaml` and `samconfig.toml` remain available for a separately approved SAM workflow; the console workflow uses the independent standard CloudFormation template `template.console.yaml`.

## Current packaging blocker and resolution

The existing `template.yaml` uses `Transform: AWS::Serverless-2016-10-31`, `AWS::Serverless::Function`, and local `CodeUri: src/`. Those are SAM build inputs, not a direct console-ready Lambda artifact reference. A CloudFormation console stack needs a template CloudFormation can process directly and Lambda code already hosted in S3.

The separate console template converts only the two SAM function resources to `AWS::Lambda::Function` and accepts `ArtifactBucket` and `ArtifactKey` parameters for the manually uploaded ZIP. It retains the architecture, handler names, runtime, template settings, and DynamoDB retention policy. The packaging script has an explicit eight-module allowlist for the two handlers and their imports. The canonical ZIP was verified during CHG-029b: it passed archive integrity checks, contained exactly the expected eight root-level modules, and each module matched current source byte-for-byte. Both configured handlers imported successfully from the ZIP without invocation; this local verification does not establish AWS runtime compatibility. The local Boto3 development dependency is not packaged; the Lambda Python runtime supplies its AWS SDK.

## Build and verify the local ZIP

From the repository root, run:

```powershell
\.venv\Scripts\python.exe scripts\package_console_lambda.py
```

The repeatable, local-only script writes `artifacts/console-deployment/order-processing-lambda.zip`. It includes the runtime modules at the ZIP root, so the configured handlers resolve as `lambda_function.lambda_handler` and `worker.lambda_handler`. It checks the root-level module layout and imports both configured handlers from the ZIP without invoking them or contacting AWS.

The script does not package the repository root. Therefore it excludes `.venv`, `.aws-sam`, tests, `.git`, credentials, local environment files, events, docs, and unrelated files. Review the archive listing before upload; the expected entries are Python source modules only.

## Prerequisites and parameters

Before starting in the AWS Console, confirm the target account and region through the Console header, your authorization to create these resources, and the expected costs for API Gateway, Lambda, DynamoDB on-demand, SQS, CloudWatch Logs, and S3. No account ID, credentials, secret, or account-specific ARN is embedded in these artifacts.

The template parameters are:

| Parameter | Required value |
| --- | --- |
| `ResourcePrefix` | Unique lowercase resource prefix, 2–15 characters, beginning with a letter; default `edop-local` may conflict. |
| `WorkerLeaseSeconds` | 11–60 seconds; default 20. Keep it at or below the queue's 60-second visibility timeout. |
| `ArtifactBucket` | Exact name of the S3 bucket containing the ZIP. |
| `ArtifactKey` | Exact object key including any prefix and the ZIP filename, for example `manual-deploy/order-processing-lambda.zip`. |

The artifact bucket must be in the same AWS Region as the Lambda functions. Use an account-controlled bucket with Block Public Access enabled and appropriate encryption/access controls. The identity CloudFormation uses to create/update the stack must be allowed to read the object as required by the account's bucket policy. Do not place credentials or unrelated files in the bucket.

## Manual AWS Management Console procedure

1. In the S3 Console, create or select an approved artifact bucket in the target stack Region. Check ownership and existing use before selecting a bucket; do not change a shared bucket's policy or settings for this deployment without review.
2. Run the local ZIP build and inspect `artifacts/console-deployment/order-processing-lambda.zip`. Upload that exact file through the S3 Console. Record the exact bucket name and object key shown by S3.
3. In CloudFormation, choose **Create stack** and upload `template.console.yaml` as the template file. Use a reviewed, unique stack name.
4. Enter `ResourcePrefix`, `WorkerLeaseSeconds`, `ArtifactBucket`, and `ArtifactKey`. The bucket and key must exactly match the uploaded object. Acknowledge `CAPABILITY_IAM`, because the template creates two scoped IAM roles.
5. Review all stack options and create a **change set**. Inspect resource additions/replacements, IAM policies, named resources, and the retained DynamoDB table. Execute only after the change set matches the reviewed architecture and target account.
6. Follow **Events** during creation. Resolve the first failed resource and its reason before retrying or updating; do not repeatedly execute an unexplained failure. After completion, inspect **Outputs** for the API URL, table and queue names/URLs, and function ARNs. Inspect each Lambda's configuration, environment, handler, execution role, and CloudWatch log group retention. Confirm the event source mapping, queue redrive/visibility configuration, and API route authorization.
7. The API route requires AWS IAM (`AWS_IAM`) authorization. A caller also needs a separately reviewed `execute-api:Invoke` identity policy scoped to this API's `$default/POST/orders` route. The stack does not grant that caller permission.

The template defines an IAM-authenticated API, intake and worker functions, one on-demand encrypted DynamoDB table, an SQS queue and DLQ, separate narrowly scoped function roles, seven-day log groups, queue-triggered batch-size-one worker, and the current timeouts, lease, retention, and throttle settings. Worker completion still represents the deterministic local simulation callback; payment, inventory, shipping, and fulfillment integrations are not included.

## Pilot processing and DLQ operations

The worker's business processing is a deterministic local simulation. It does not charge payments, reserve inventory, ship orders, or perform other external business effects. The DLQ recovery helper in `src/dlq_recovery.py` is local-only and is not in the packaging script's explicit eight-module allowlist. The SAM template's `CodeUri: src/` points to the source directory containing the helper module, but neither template defines a recovery handler, Lambda, or API. There is no deployed recovery interface.

For the pilot, inspect DLQ messages in the AWS Console and investigate the matching order state and sanitized logs before deciding on any action. No automatic redrive or manual DynamoDB state edits are authorized by this guide. Do not redrive a message until the cause and current order state have been investigated and a separately reviewed procedure is approved. Local fake-based tests check application branching and expected adapter calls; they do not prove real SQS, DynamoDB, Lambda, IAM, or CloudFormation behavior.

## Safe cleanup

1. Stop callers and review the work queue and DLQ. Deleting these queues deletes messages that have not been retained elsewhere.
2. In CloudFormation, review the stack's resources and delete the stack only after deciding what should happen to outstanding messages and logs. CloudFormation removes stack-managed resources subject to their retention policies.
3. `OrdersTable` has both `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`. Stack deletion leaves the DynamoDB table behind and it may continue to incur charges. Inspect and preserve/export required order data. Delete the retained table separately only after an explicit data-retention decision and verification of its exact name and contents.
4. The S3 artifact object is not managed by this stack. In S3, verify the exact bucket and key before deleting only this uploaded object. Do not empty or delete a shared bucket. Delete the bucket only if it was created solely for this artifact, is confirmed empty after cleanup, and is approved for deletion.
5. Verify the CloudFormation stack status and remaining resources, retained table, artifact object/bucket, and billing. Do not assume stack deletion removed retained or externally managed artifacts.

## Local validation

The package build and import checks are local. `samconfig.toml` and SAM CLI deployment settings are not used by this console procedure. Do not run `sam deploy`, `sam sync`, AWS CLI/SDK commands, or CloudFormation operations as part of local validation.
