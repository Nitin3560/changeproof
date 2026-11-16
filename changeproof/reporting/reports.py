from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from changeproof import __version__
from .reproduction import reproduction_source

LIMITATION = "No violations reproduced does not prove correctness for all executions."


def write_reports(output, config, target, results):
    expected = config.scenarios.iterations * (1 + int(config.scenarios.sequential_duplicates)
                                             + int(config.scenarios.concurrent_duplicates))
    errors = sum(case.status == "error" for case in results)
    violations = [case for case in results if case.status == "violation"]
    complete = len(results) == expected and not errors
    status = "execution_error" if not complete else "violation_reproduced" if violations else "no_violation_reproduced"
    folder = Path(output) / f"run-{uuid4().hex}"
    folder.mkdir(parents=True)
    findings = []
    for index, case in enumerate(violations, 1):
        finding_id = f"CP-{index:03}"
        repro = f"{finding_id}-repro.py"
        (folder / repro).write_text(reproduction_source(target["path"], config, case))
        finding = {**asdict(case), "id": finding_id, "status": "reproduced", "expected_maximum": 1,
                   "observed_charges": len(case.effects), "reproduction": repro,
                   "invariants": [item.id for item in config.invariants]}
        (folder / f"{finding_id}.json").write_text(json.dumps(finding, default=str, indent=2) + "\n")
        (folder / f"{finding_id}.md").write_text(
            f"# {finding_id}: duplicate payment charges\n\nStatus: Reproduced\n\n"
            f"Expected maximum: 1; observed: {len(case.effects)}.\n\n"
            f"Scenario: `{case.scenario}`; key: `{case.key}`.\n\n"
            f"Full ledger, responses, and trace: [{finding_id}.json]({finding_id}.json).\n\n"
            f"Run `python {repro}` from an installed checkout with `DATABASE_URL` set. "
            "The regression test should fail on the vulnerable version.\n")
        findings.append({"id": finding_id, "observed_charges": len(case.effects), "reproduction": repro})
    report = {"schema_version": 1, "changeproof_version": __version__,
              "created_at": datetime.now(timezone.utc).isoformat(), "status": status,
              "target": target, "configuration": config.model_dump(),
              "campaign": {"expected_cases": expected, "completed_cases": len(results),
                           "complete": complete, "errors": errors, "violations": len(violations)},
              "findings": findings, "cases": [asdict(case) for case in results],
              "limitations": [LIMITATION, "Instrumented local ASGI sample; PostgreSQL and fake gateway only.",
                              "Disposable PostgreSQL schema per campaign; local Python adapters execute trusted code.",
                              "No crash recovery, real provider, or arbitrary repository instrumentation."]}
    (folder / "report.json").write_text(json.dumps(report, default=str, indent=2) + "\n")
    lines = ["# ChangeProof verification", "", f"Status: {status}", "",
             f"Target: `{target['path']}` ({target['variant']})", "",
             f"Cases: {len(results)}/{expected}; violations: {len(violations)}; errors: {errors}.", ""]
    for finding in findings:
        lines.append(f"- [{finding['id']}]({finding['id']}.md): {finding['observed_charges']} charges")
    lines.extend(["", "## Limitations", "", *[f"- {item}" for item in report["limitations"]], ""])
    (folder / "report.md").write_text("\n".join(lines))
    return report, folder
