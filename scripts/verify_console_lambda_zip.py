"""Read-only verification for a console Lambda ZIP and its source directory."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable


EXPECTED_MODULES = (
    "business_processing.py",
    "dynamo_order_repository.py",
    "intake_service.py",
    "lambda_function.py",
    "order_service.py",
    "sqs_adapter.py",
    "validation.py",
    "worker.py",
)
HANDLERS = {
    "lambda_function": "lambda_handler",
    "worker": "lambda_handler",
}
HIGH_CONFIDENCE_PATTERNS = (
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private_key_header", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)
POSSIBLE_CREDENTIAL_PATTERN = re.compile(
    rb"(?i)\b(?:aws_secret_access_key|secret_access_key|client_secret|"
    rb"api_key|access_token|refresh_token|password)\b\s*[:=]\s*"
    rb"([\"'])([^\"']{8,})\1"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def credential_findings(name: str, data: bytes) -> list[tuple[str, str, int]]:
    """Return safe member/pattern/line findings without exposing matched text."""
    findings: list[tuple[str, str, int]] = []
    for label, pattern in HIGH_CONFIDENCE_PATTERNS:
        for match in pattern.finditer(data):
            findings.append(("high-confidence", label, data.count(b"\n", 0, match.start()) + 1))
    for match in POSSIBLE_CREDENTIAL_PATTERN.finditer(data):
        findings.append(("unresolved", "credential-like assignment", data.count(b"\n", 0, match.start()) + 1))
    return findings


def import_handlers(zip_path: Path) -> None:
    """Import both configured handlers in an isolated interpreter; never invoke them."""
    code = (
        "import importlib, os, sys\n"
        "archive = os.path.abspath(sys.argv[1])\n"
        "sys.path.insert(0, archive)\n"
        "for module_name in ('lambda_function', 'worker'):\n"
        "    module = importlib.import_module(module_name)\n"
        "    handler = getattr(module, 'lambda_handler', None)\n"
        "    assert callable(handler), module_name + '.lambda_handler is not callable'\n"
        "    assert os.path.abspath(module.__file__).startswith(archive + os.sep), module.__file__\n"
        "    print(module_name + '.lambda_handler callable; imported from ZIP')\n"
        "assert 'boto3' not in sys.modules, 'boto3 imported during handler import'\n"
    )
    env = os.environ.copy()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        env.pop(name, None)
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env["AWS_CONFIG_FILE"] = os.devnull
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(zip_path.resolve())],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        print("Isolated handler import failed (details follow):")
        print(result.stderr, end="")
        raise SystemExit(result.returncode)


def verify(zip_path: Path, source_dir: Path) -> int:
    zip_path = zip_path.resolve()
    source_dir = source_dir.resolve()
    print("Python: {}".format(sys.version.replace("\n", " ")))
    print("ZIP: {}".format(zip_path))
    print("Source: {}".format(source_dir))

    if not zip_path.is_file():
        print("FAIL: ZIP file does not exist")
        return 1
    if not source_dir.is_dir():
        print("FAIL: source directory does not exist")
        return 1

    archive_bytes = zip_path.read_bytes()
    print("ZIP size: {} bytes".format(len(archive_bytes)))
    print("ZIP SHA-256: {}".format(sha256(archive_bytes)))
    failed = False
    high_confidence: list[tuple[str, str, int]] = []
    unresolved: list[tuple[str, str, int]] = []

    try:
        with zipfile.ZipFile(zip_path) as archive:
            corrupt = archive.testzip()
            print("ZIP integrity: {}".format("PASS" if corrupt is None else "FAIL: " + corrupt))
            if corrupt is not None:
                failed = True
            names = archive.namelist()
            expected = sorted(EXPECTED_MODULES)
            if sorted(names) != expected or len(names) != len(set(names)):
                print("FAIL: member list differs from the exact expected eight root modules")
                failed = True
            else:
                print("ZIP members: exact expected list (8 root-level modules)")

            for name in sorted(names):
                info = archive.getinfo(name)
                member = archive.read(name)
                source_path = source_dir / name
                source_exists = source_path.is_file()
                same = source_exists and member == source_path.read_bytes()
                mode = stat.S_IMODE(info.external_attr >> 16)
                print(
                    "MEMBER {} sha256={} source_equal={} mode=0o{:04o}".format(
                        name, sha256(member), same, mode
                    )
                )
                if not same:
                    failed = True
                for classification, label, line in credential_findings(name, member):
                    finding = (name, label, line)
                    if classification == "high-confidence":
                        high_confidence.append(finding)
                    else:
                        unresolved.append(finding)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        print("FAIL: could not inspect ZIP ({})".format(type(exc).__name__))
        return 1

    print("Credential scan: {} high-confidence findings; {} unresolved pattern matches".format(
        len(high_confidence), len(unresolved)
    ))
    for member, label, line in high_confidence + unresolved:
        category = "CONFIRMED-FORMAT" if (member, label, line) in high_confidence else "UNRESOLVED"
        print("{}: {}:{} ({})".format(category, member, line, label))
    if high_confidence or unresolved:
        failed = True

    if not failed:
        import_handlers(zip_path)
    else:
        print("Handler imports not run because a ZIP/content acceptance check failed.")
    print("OVERALL: {}".format("FAIL" if failed else "PASS"))
    return int(failed)


def main(args: Iterable[str] | None = None) -> int:
    values = list(sys.argv[1:] if args is None else args)
    if len(values) != 2:
        print("Usage: verify_console_lambda_zip.py ZIP_PATH SOURCE_DIRECTORY")
        return 2
    return verify(Path(values[0]), Path(values[1]))


if __name__ == "__main__":
    raise SystemExit(main())
