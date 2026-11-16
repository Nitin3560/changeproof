import argparse
import json
import sys

from . import __version__
from .invariants.loader import ConfigurationError, load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="changeproof")
    parser.add_argument("--version", action="version", version=f"ChangeProof {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate an explicit campaign configuration")
    validate.add_argument("--config", required=True)
    verify = commands.add_parser("verify", help="Verify a local instrumented payment target")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--config", required=True)
    verify.add_argument("--output", default="reports")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.command == "verify":
        return verify_target(args, config)
    print(json.dumps({"status": "configuration_valid", "configuration": config.model_dump()}, indent=2))
    return 0


def verify_target(args, config):
    import asyncio

    try:
        from .execution.benchmark import run_benchmark
        from .execution.target import load_target
        from .reporting.reports import write_reports
    except ImportError:
        print("Install benchmark dependencies: pip install -e '.[benchmark]'", file=sys.stderr)
        return 2
    try:
        target, metadata = load_target(args.repo)
        results = asyncio.run(run_benchmark(config, target.VARIANT, app_factory=target.create_app))
        report, folder = write_reports(args.output, config, metadata, results)
    except Exception as exc:
        print(f"Verification error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    summary = report["campaign"]
    print(f"{report['status']}: {summary['completed_cases']} cases, "
          f"{summary['violations']} violations, {summary['errors']} errors")
    print(f"Reports: {folder / 'report.json'} and {folder / 'report.md'}")
    print("No violations reproduced does not prove correctness for all executions.")
    return 2 if report["status"] == "execution_error" else 1 if summary["violations"] else 0
