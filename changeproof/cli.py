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
    verify.add_argument("--base", help="Git revision for generated scenario analysis")
    verify.add_argument("--generated", action="store_true", help="Search and shrink generated payment inputs")
    verify.add_argument("--analysis-root", help="Source directory to analyze (defaults to target directory)")
    verify.add_argument("--max-examples", type=int, choices=range(1, 101), default=30, metavar="1..100")
    verify.add_argument("--seed", type=int, default=0)
    suggestions = verify.add_mutually_exclusive_group()
    suggestions.add_argument("--suggestions", help="Validated JSON candidate suggestions")
    suggestions.add_argument("--llm-model", help="Explicit OpenAI model for optional candidate suggestions")
    analyze = commands.add_parser("analyze", help="Inspect Python source without executing it")
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--config", required=True)
    analyze.add_argument("--base", help="Git revision to compare with the current worktree")
    analyze.add_argument("--output", help="Write analysis JSON to this file")
    jobs = commands.add_parser("github-jobs", help="Inspect queued GitHub jobs without modifying them")
    jobs.add_argument("--database", required=True)
    jobs.add_argument("--limit", type=int, choices=range(1, 101), default=50, metavar="1..100")
    args = parser.parse_args(argv)
    if args.command == "github-jobs":
        return inspect_jobs(args)
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
        if (args.suggestions or args.llm_model or args.analysis_root or args.base) and not args.generated:
            raise ValueError("Analysis and suggestion options require --generated")
        target, metadata = load_target(args.repo)
        generation = None
        if args.generated:
            from .analyzer.repository import analyze_repository
            from .scenarios.suggestions import SuggestionBatch, load_suggestions, suggest_with_model
            from .execution.generated import run_generated
            analysis = analyze_repository(args.analysis_root or args.repo, config, base=args.base)
            if analysis["diagnostics"]:
                raise ValueError("Source analysis is incomplete; inspect it with changeproof analyze")
            batch = (load_suggestions(args.suggestions, config) if args.suggestions else
                     suggest_with_model(analysis, config, args.llm_model) if args.llm_model else
                     SuggestionBatch(suggestions=[]))
            results, generation = run_generated(config, target, analysis, batch,
                                                max_examples=args.max_examples, seed=args.seed)
        else:
            results = asyncio.run(run_benchmark(config, target.VARIANT, app_factory=target.create_app))
        report, folder = write_reports(args.output, config, metadata, results, generation=generation)
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


def inspect_jobs(args):
    import sqlite3
    from .github.queue import list_jobs

    try:
        print(json.dumps({"jobs": list_jobs(args.database, args.limit)}, indent=2))
    except (OSError, ValueError, sqlite3.Error):
        print("Could not read the GitHub job database", file=sys.stderr)
        return 2
    return 0
