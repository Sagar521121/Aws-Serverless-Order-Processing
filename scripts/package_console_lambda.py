"""Build and locally verify the shared ZIP used by template.console.yaml."""

from __future__ import annotations

import importlib
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
OUTPUT = ROOT / "artifacts" / "console-deployment" / "order-processing-lambda.zip"
REQUIRED_HANDLERS = {
    "lambda_function.py": "lambda_function",
    "worker.py": "worker",
}
RUNTIME_MODULES = {
    "business_processing.py",
    "dynamo_order_repository.py",
    "intake_service.py",
    "lambda_function.py",
    "order_service.py",
    "sqs_adapter.py",
    "validation.py",
    "worker.py",
}


def main() -> None:
    sources = [SOURCE / name for name in sorted(RUNTIME_MODULES)]
    missing_sources = [str(source) for source in sources if not source.is_file()]
    if missing_sources:
        raise SystemExit(f"Required runtime modules are missing: {missing_sources}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            archive.write(source, arcname=source.name)

    with zipfile.ZipFile(OUTPUT) as archive:
        names = set(archive.namelist())
        missing = set(REQUIRED_HANDLERS) - names
        if missing:
            raise SystemExit(f"ZIP is missing handler modules: {sorted(missing)}")
        if any("/" in name or "\\" in name for name in names):
            raise SystemExit("Expected handler modules at the ZIP root")

    # Import directly from the archive to verify each configured handler and
    # its in-package imports, without invoking either handler or an AWS SDK.
    sys.path.insert(0, str(OUTPUT))
    try:
        for module_name in REQUIRED_HANDLERS.values():
            module = importlib.import_module(module_name)
            handler = getattr(module, "lambda_handler", None)
            if not callable(handler):
                raise SystemExit(f"{module_name}.lambda_handler is not callable in ZIP")
    finally:
        sys.path.remove(str(OUTPUT))

    print(f"Created {OUTPUT.relative_to(ROOT)}")
    print(f"Included {len(names)} Python modules; both configured handlers imported from ZIP.")


if __name__ == "__main__":
    main()
