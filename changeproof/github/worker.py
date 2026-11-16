import json
import hashlib
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .archive import extract_snapshot
from .checks import check_output, report_conclusion
from .models import PullRequest


def current_revision(data, job):
    pull = PullRequest.model_validate(data)
    return (pull.number == job["pull_number"] and pull.state == "open" and not pull.draft
            and pull.base.sha == job["base_sha"] and pull.head.sha == job["head_sha"]
            and all(revision.repo is not None and revision.repo.id == job["repository_id"]
                    and revision.repo.full_name == job["repository"] for revision in (pull.base, pull.head)))


def process_one(store, policy, client, executor, reports_root):
    claim = store.claim(lease_seconds=1800)
    if claim is None:
        return {"status": "idle"}
    job, check_id = claim["job"], claim["check_id"]
    token = None
    try:
        target = policy.target_for(job)
        if target is None:
            store.update_claim(claim, state="skipped", conclusion="action_required",
                               last_error="Commit is not approved by worker policy")
            return {"status": "skipped", "job_id": claim["id"]}
        fingerprint = hashlib.sha256((json.dumps(target.model_dump(mode="json"), sort_keys=True)
            + policy.runtime_image + policy.database_image + target.config_path.read_text()).encode()).hexdigest()
        token = client.installation_token(job)
        if claim["attempts"] > 3:
            if check_id:
                client.finish_check(job, check_id, token, "action_required", "Worker attempts were interrupted; verification is incomplete.")
            store.update_claim(claim, state="failed", conclusion="action_required", last_error="Retry budget exhausted")
            return {"status": "failed", "job_id": claim["id"]}
        if not current_revision(client.current_pr(job, token), job):
            if check_id:
                client.finish_check(job, check_id, token, "cancelled", "Pull request revision is no longer current.")
            store.update_claim(claim, state="skipped", conclusion="cancelled")
            return {"status": "skipped", "job_id": claim["id"]}
        if check_id is None:
            check_id = client.ensure_check(job, claim["id"], token)
            store.update_claim(claim, check_id=check_id)
        report_path = Path(claim["report_path"]) if claim["report_path"] else None
        if report_path is not None:
            report = json.loads(report_path.read_text())
            if report.get("github_job") != job:
                raise ValueError("Saved report belongs to another revision")
            if report.get("policy_fingerprint") != fingerprint:
                report_path = None
            else:
                conclusion = report_conclusion(report, report["verifier_exit_code"])
        if report_path is None:
            with tempfile.TemporaryDirectory(prefix="changeproof-pr-") as temporary:
                snapshot = extract_snapshot(client.archive(job, token), Path(temporary) / "source")
                output = Path(reports_root).resolve() / claim["id"] / ("attempt-" + uuid4().hex)
                report_path, exit_code = executor.execute(snapshot, target, output, policy)
            report = json.loads(report_path.read_text())
            conclusion = report_conclusion(report, exit_code)
            report["github_job"], report["verifier_exit_code"] = job, exit_code
            report["policy_fingerprint"] = fingerprint
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            store.update_claim(claim, report_path=str(report_path))
        # Recheck both PR freshness and lease ownership immediately before publication.
        store.update_claim(claim, last_error=None)
        if not current_revision(client.current_pr(job, token), job):
            client.finish_check(job, check_id, token, "cancelled", "Pull request changed during verification; results not applied.")
            store.update_claim(claim, state="skipped", conclusion="cancelled")
            return {"status": "skipped", "job_id": claim["id"]}
        summary, text = check_output(report, job)
        if report.get("findings"):
            name = report["findings"][0].get("reproduction", "")
            script = report_path.parent / name
            if name and Path(name).name == name and not script.is_symlink() and script.is_file():
                text += "\n\n### Saved regression test\n\n```python\n" + script.read_text()[:10000] + "\n```"
        client.finish_check(job, check_id, token, conclusion, summary, text)
        store.update_claim(claim, state="completed", conclusion=conclusion)
        return {"status": "completed", "job_id": claim["id"], "conclusion": conclusion,
                "report": str(report_path)}
    except Exception as exc:
        # Keep exception text and request credentials out of persisted/public errors.
        retry = claim["attempts"] < 3
        try:
            store.update_claim(claim, last_error=type(exc).__name__)
            if not retry and check_id and token:
                try:
                    client.finish_check(job, check_id, token, "action_required", "Verification could not complete after three attempts.")
                except Exception:
                    pass
            store.update_claim(claim, state="queued" if retry else "failed",
                               next_attempt=time.time() + 5 * (2 ** (claim["attempts"] - 1)),
                               last_error=type(exc).__name__, conclusion="action_required" if not retry else None)
        except Exception:
            # A lost lease may only be recovered by its new owner.
            pass
        return {"status": "retry" if retry else "failed", "job_id": claim["id"], "error": type(exc).__name__}
