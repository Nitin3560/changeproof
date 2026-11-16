"""Trusted runtime entrypoint for an approved source snapshot."""
import argparse
import asyncio
import importlib.util
from pathlib import Path
import sys

from changeproof.execution.benchmark import run_benchmark
from changeproof.execution.target import load_target
from changeproof.invariants.loader import load_config
from changeproof.reporting.reports import write_reports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--package", required=True)
    args = parser.parse_args()
    root = Path("/source")
    package = root / args.package
    if not (package / "__init__.py").is_file():
        raise ValueError("Approved source package is missing")
    # Keep the verifier and ledger loaded from the trusted image; use snapshot service code.
    for name in list(sys.modules):
        if name == args.package or name.startswith(args.package + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(args.package, package / "__init__.py",
                                                  submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[args.package] = module
    spec.loader.exec_module(module)
    adapter = (root / args.adapter).resolve()
    if not adapter.is_relative_to(root):
        raise ValueError("Adapter escaped the snapshot")
    target, metadata = load_target(adapter)
    config = load_config("/policy/config.yaml")
    results = asyncio.run(run_benchmark(config, target.VARIANT, app_factory=target.create_app))
    report, _ = write_reports("/out", config, metadata, results)
    return 2 if report["status"] == "execution_error" else 1 if report["campaign"]["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
