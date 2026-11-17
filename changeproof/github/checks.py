import json


def report_conclusion(report, exit_code):
    campaign, cases = report["campaign"], report["cases"]
    if (not campaign["complete"] or campaign["errors"] or report["status"] == "execution_error"):
        return "action_required"
    if not cases or campaign["completed_cases"] != len(cases) or campaign["expected_cases"] != len(cases):
        raise ValueError("Incomplete report counts")
    violations = sum(case["status"] == "violation" for case in cases)
    inconclusive = sum(case["status"] == "inconclusive" for case in cases)
    if campaign.get("inconclusive", 0) != inconclusive:
        raise ValueError("Unresolved case counts disagree")
    if campaign["violations"] != violations:
        raise ValueError("Report finding counts disagree")
    for case in cases:
        if case["status"] not in {"violation", "no_violation_reproduced", "inconclusive"}:
            raise ValueError("Unexpected case outcome")
        if case["status"] == "no_violation_reproduced" and len(case["effects"]) != 1:
            raise ValueError("No successful payment effect was observed")
        if (len(case["effects"]) > 1) != (case["status"] == "violation"):
            raise ValueError("Case effect count disagrees with outcome")
    expected_status = "violation_reproduced" if violations else "inconclusive" if inconclusive else "no_violation_reproduced"
    if report["status"] != expected_status or exit_code != (1 if violations else 2 if inconclusive else 0):
        raise ValueError("Verifier exit code or status disagrees with evidence")
    return "failure" if violations else "action_required" if inconclusive else "success"


def check_output(report, job):
    campaign = report["campaign"]
    summary = (f"Commit `{job['head_sha']}`; base `{job['base_sha']}`.\n\n"
               f"Cases: {campaign['completed_cases']}; reproduced violations: {campaign['violations']}; "
               f"execution errors: {campaign['errors']}; unresolved payments: {campaign.get('inconclusive', 0)}.\n\n"
               "No violations reproduced does not prove correctness for all executions. "
               "Controlled instrumented payment target; no crash-recovery guarantee.")
    case = next((case for case in report["cases"] if case["status"] == "violation"), None)
    text = ""
    if case:
        excerpt = {key: case.get(key) for key in ("scenario", "key", "input", "effects", "responses", "trace", "observations")}
        text = "### First reproduced violation\n\n```json\n" + json.dumps(excerpt, default=str, indent=2)[:45000] + "\n```"
    return summary, text
