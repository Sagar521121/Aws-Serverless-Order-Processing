"""Offline contract checks for the SAM deployment template and runtime wiring."""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _resource_block(template: str, logical_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(logical_id)}:\s*\n(.*?)(?=^  [A-Za-z][A-Za-z0-9]+:\s*$|^Outputs:\s*$|\Z)",
        template,
    )
    if match is None:
        raise AssertionError(f"Missing CloudFormation resource {logical_id}")
    return match.group(1)


class TestDeploymentConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = (ROOT / "template.yaml").read_text(encoding="utf-8")

    def test_expected_architecture_resources_and_lambda_entrypoints_exist(self) -> None:
        for resource_type in (
            "AWS::DynamoDB::Table", "AWS::SQS::Queue", "AWS::IAM::Role",
            "AWS::Serverless::Function", "AWS::ApiGatewayV2::Api",
            "AWS::ApiGatewayV2::Integration", "AWS::ApiGatewayV2::Route",
            "AWS::ApiGatewayV2::Stage", "AWS::Lambda::EventSourceMapping",
            "AWS::Logs::LogGroup", "AWS::Lambda::Permission",
        ):
            with self.subTest(resource_type=resource_type):
                self.assertIn(f"Type: {resource_type}", self.template)
        intake = _resource_block(self.template, "IntakeFunction")
        worker = _resource_block(self.template, "WorkerFunction")
        self.assertIn("CodeUri: src/", intake)
        self.assertIn("Handler: lambda_function.lambda_handler", intake)
        self.assertIn("CodeUri: src/", worker)
        self.assertIn("Handler: worker.lambda_handler", worker)
        self.assertIn("Runtime: python3.12", intake)
        self.assertIn("Runtime: python3.12", worker)

    def test_retry_lease_retention_and_auth_settings_match_design(self) -> None:
        queue = _resource_block(self.template, "OrdersQueue")
        dlq = _resource_block(self.template, "OrdersDlq")
        mapping = _resource_block(self.template, "OrdersEventSourceMapping")
        route = _resource_block(self.template, "OrdersRoute")
        worker = _resource_block(self.template, "WorkerFunction")
        self.assertIn("VisibilityTimeout: 60", queue)
        self.assertIn("MessageRetentionPeriod: 345600", queue)
        self.assertIn("maxReceiveCount: 5", queue)
        self.assertIn("MessageRetentionPeriod: 1209600", dlq)
        self.assertIn("BatchSize: 1", mapping)
        self.assertIn("AuthorizationType: AWS_IAM", route)
        self.assertIn("Timeout: 10", worker)
        self.assertIn("WORKER_LEASE_SECONDS: !Ref WorkerLeaseSeconds", worker)
        self.assertIn("Default: '20'", self.template)
        self.assertEqual(self.template.count("RetentionInDays: 7"), 2)
        self.assertIn("ThrottlingRateLimit: 1", self.template)
        self.assertIn("ThrottlingBurstLimit: 2", self.template)
        self.assertIn("AllowedPattern: '^(1[1-9]|[2-5][0-9]|60)$'", self.template)
        permission = _resource_block(self.template, "IntakeInvokePermission")
        self.assertIn("Principal: apigateway.amazonaws.com", permission)
        self.assertIn("SourceAccount: !Ref AWS::AccountId", permission)
        self.assertIn("$default/POST/orders", permission)

    def test_runtime_policies_are_resource_scoped_and_ids_use_pseudo_parameters(self) -> None:
        for role_id in ("IntakeRole", "WorkerRole"):
            role = _resource_block(self.template, role_id)
            self.assertNotRegex(role, r"(?m)^\s+Action:\s*['\"]?\*['\"]?\s*$")
            self.assertNotRegex(role, r"(?m)^\s+Resource:\s*['\"]?\*['\"]?\s*$")
            self.assertIn("!GetAtt OrdersTable.Arn", role)
            self.assertIn("!Sub 'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:", role)
        self.assertIn("!Ref AWS::AccountId", self.template)
        self.assertNotRegex(self.template, r"\b\d{12}\b")
        worker_role = _resource_block(self.template, "WorkerRole")
        for action in ("sqs:ReceiveMessage", "sqs:DeleteMessage",
                       "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"):
            self.assertIn(action, worker_role)
        self.assertIn("DeletionPolicy: Retain", _resource_block(self.template, "OrdersTable"))

    def test_sam_defaults_and_source_lease_setting_are_aligned(self) -> None:
        config_text = (ROOT / "samconfig.toml").read_text(encoding="utf-8")
        config = tomllib.loads(config_text)
        worker_source = (ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        self.assertEqual(config["default"]["global"]["parameters"]["region"], "ap-south-1")
        deploy = config["default"]["deploy"]["parameters"]
        self.assertEqual(deploy["capabilities"], "CAPABILITY_IAM")
        self.assertEqual(deploy["parameter_overrides"],
                         "ResourcePrefix=edop-local WorkerLeaseSeconds=20")
        self.assertIn('os.environ.get("WORKER_LEASE_SECONDS", "20")', worker_source)
        self.assertIn("MAX_REQUEST_BODY_BYTES = 64 * 1024", (ROOT / "src" / "lambda_function.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
