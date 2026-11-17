from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from changeproof import __version__
from changeproof.scenarios.generator import expected_cases
from .reproduction import reproduction_source

LIMITATION = "No violations reproduced does not prove correctness for all executions."


def write_reports(output, config, target, results, *, generation=None):
    expected = expected_cases(config)
    if generation is not None:
        expected = generation["expected_cases"]
    errors = sum(case.status == "error" for case in results)
    violations = [case for case in results if case.status == "violation"]
    inconclusive = [case for case in results if case.status == "inconclusive"]
    complete = bool(results) and len(results) == expected and not errors
    if generation is not None:
        complete = complete and generation["complete"]
    status = "execution_error" if not complete else "violation_reproduced" if violations else "inconclusive" if inconclusive else "no_violation_reproduced"
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
              "target": target, "configuration": config.model_dump(), "generation": generation,
              "campaign": {"expected_cases": expected, "completed_cases": len(results),
                           "complete": complete, "errors": errors, "violations": len(violations),
                           "inconclusive": len(inconclusive)},
              "findings": findings, "cases": [asdict(case) for case in results],
              "limitations": [LIMITATION, "Instrumented local ASGI sample; PostgreSQL and fake gateway only.",
                              "Disposable PostgreSQL schema per campaign; local Python adapters execute trusted code.",
                              "Injected transport/storage failures do not model process death or provider recovery.",
                              "No crash recovery, real provider, or arbitrary repository instrumentation."]}
    (folder / "report.json").write_text(json.dumps(report, default=str, indent=2) + "\n")
    lines = ["# ChangeProof verification", "", f"Status: {status}", "",
             f"Target: `{target['path']}` ({target['variant']})", "",
             f"Cases: {len(results)}/{expected}; violations: {len(violations)}; errors: {errors}; "
             f"inconclusive: {len(inconclusive)}.", ""]
    for finding in findings:
        lines.append(f"- [{finding['id']}]({finding['id']}.md): {finding['observed_charges']} charges")
    if inconclusive:
        lines.extend(["", "## Unresolved payments", ""])
        for case in inconclusive:
            lines.append(f"- `{case.scenario}`; key `{case.key}`; "
                         f"state `{case.observations.get('payment_state', 'unknown')}`: {case.error}")
    lines.extend(["", "## Limitations", "", *[f"- {item}" for item in report["limitations"]], ""])
    (folder / "report.md").write_text("\n".join(lines))
    return report, folder
