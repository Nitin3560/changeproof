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
    analyze = commands.add_parser("analyze", help="Inspect Python source without executing it")
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--config", required=True)
    analyze.add_argument("--base", help="Git revision to compare with the current worktree")
    analyze.add_argument("--output", help="Write analysis JSON to this file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.command == "analyze":
        return analyze_target(args, config)
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


def analyze_target(args, config):
    from pathlib import Path

    from .analyzer.diff_analyzer import AnalysisError
    from .analyzer.repository import analyze_repository

    try:
        report = analyze_repository(args.repo, config, base=args.base)
        rendered = json.dumps(report, indent=2) + "\n"
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (AnalysisError, OSError) as exc:
        print(f"Analysis error: {exc}", file=sys.stderr)
        return 2
    return 2 if report["diagnostics"] else 0
