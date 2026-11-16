import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from uuid import uuid4

from changeproof.github.archive import extract_snapshot
from changeproof.github.checks import report_conclusion
from changeproof.github.executor import DockerExecutor, ExecutionTimeout
from changeproof.github.queue import JobStore, list_jobs
from changeproof.github.worker import process_one
from changeproof.github.worker_settings import ApprovedTarget, WorkerPolicy


def job():
    return {"installation_id": 123, "repository_id": 456, "repository": "nitin3560/changeproof",
            "pull_number": 7, "base_sha": "a" * 40, "head_sha": "b" * 40}


def pull():
    repo = {"id": 456, "full_name": "nitin3560/changeproof"}
    return {"number": 7, "state": "open", "draft": False,
            "base": {"sha": "a" * 40, "repo": repo}, "head": {"sha": "b" * 40, "repo": repo}}


def archive_bytes(name="root/file.py", kind=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind:
            info.type, info.linkname = kind, "/etc/passwd"
            archive.addfile(info)
        else:
            info.size = 4
            archive.addfile(info, io.BytesIO(b"pass"))
    return data.getvalue()


def report(violation=False):
    return {"status": "violation_reproduced" if violation else "no_violation_reproduced",
            "campaign": {"complete": True, "errors": 0, "completed_cases": 1, "expected_cases": 1,
                         "violations": int(violation)},
            "cases": [{"status": "violation" if violation else "no_violation_reproduced",
                       "effects": [{"charge_id": "one"}] + ([{"charge_id": "two"}] if violation else []),
                       "scenario": "concurrent_duplicates", "input": {"amount": 500}, "trace": []}]}


class FakeClient:
    def __init__(self):
        self.reads, self.created, self.finished = 0, 0, []
        self.changed_after = None
        self.fail_publication = False

    def installation_token(self, job):
        return "test-token"

    def current_pr(self, job, token):
        self.reads += 1
        result = pull()
        if self.changed_after is not None and self.reads > self.changed_after:
            result["head"]["sha"] = "c" * 40
        return result

    def ensure_check(self, *args):
        self.created += 1
        return 42

    def archive(self, *args):
        return archive_bytes()

    def finish_check(self, job, check_id, token, conclusion, *args):
        if self.fail_publication:
            raise ConnectionError("test-token must not be persisted")
        self.finished.append((job["head_sha"], check_id, conclusion))


class FakeExecutor:
    def __init__(self, violation=False):
        self.calls, self.violation = 0, violation

    def execute(self, snapshot, target, output, policy):
        self.calls += 1
        output.mkdir(parents=True)
        path = output / "report.json"
        path.write_text(json.dumps(report(self.violation)))
        return path, int(self.violation)


class WorkerTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JobStore(self.root / "queue.sqlite3")
        self.store.accept(str(uuid4()), "pull_request", "digest", job())
        target = ApprovedTarget(id=456, installation_id=123, full_name="nitin3560/changeproof",
            approved_heads=["b" * 40], adapter_path="examples/payment_service/vulnerable", config_path=self.root / "config.yaml")
        target.config_path.write_text("trusted configuration")
        self.policy = WorkerPolicy(targets=[target])
        self.client, self.executor = FakeClient(), FakeExecutor()

    def tearDown(self):
        self.temporary.cleanup()

    def run_job(self):
        return process_one(self.store, self.policy, self.client, self.executor, self.root / "reports")

    def test_success_and_duplicate_worker_poll(self):
        result = self.run_job()
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(self.client.finished, [("b" * 40, 42, "success")])
        self.assertEqual(self.run_job()["status"], "idle")
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(list_jobs(self.store.path)[0]["state"], "completed")

    def test_reproduced_violation_fails_the_check(self):
        self.executor.violation = True
        self.assertEqual(self.run_job()["conclusion"], "failure")
        self.assertEqual(self.client.finished[-1][2], "failure")

    def test_stale_commit_is_skipped_before_execution(self):
        self.client.changed_after = 0
        self.assertEqual(self.run_job()["status"], "skipped")
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.client.created, 0)

    def test_changed_commit_during_execution_cancels_check(self):
        self.client.changed_after = 1
        self.assertEqual(self.run_job()["status"], "skipped")
        self.assertEqual(self.client.finished[-1][2], "cancelled")
        self.assertEqual(self.executor.calls, 1)

    def test_unapproved_commit_never_executes(self):
        self.policy.targets[0].approved_heads = ["c" * 40]
        self.assertEqual(self.run_job()["status"], "skipped")
        self.assertEqual(self.client.reads, 0)
        self.assertEqual(self.executor.calls, 0)

    def test_publication_retry_reuses_check_and_report(self):
        self.client.fail_publication = True
        self.assertEqual(self.run_job()["status"], "retry")
        with self.store.connect() as conn:
            row = conn.execute("SELECT last_error FROM jobs").fetchone()
            self.assertEqual(row[0], "ConnectionError")
            conn.execute("UPDATE jobs SET next_attempt=0")
        self.client.fail_publication = False
        self.assertEqual(self.run_job()["conclusion"], "success")
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.client.created, 1)

    def test_policy_change_invalidates_cached_verification(self):
        self.client.fail_publication = True
        self.assertEqual(self.run_job()["status"], "retry")
        self.policy.targets[0].config_path.write_text("changed trusted configuration")
        with self.store.connect() as conn:
            conn.execute("UPDATE jobs SET next_attempt=0")
        self.client.fail_publication = False
        self.assertEqual(self.run_job()["conclusion"], "success")
        self.assertEqual(self.executor.calls, 2)

    def test_lease_recovery_fences_old_owner(self):
        old = self.store.claim(now=100, lease_seconds=10)
        self.assertIsNone(self.store.claim(now=105))
        new = self.store.claim(now=111)
        self.assertNotEqual(new["lease_token"], old["lease_token"])
        with self.assertRaises(RuntimeError):
            self.store.update_claim(old, state="completed", now=112)
        self.store.update_claim(new, state="completed", now=112)

    def test_retry_budget_and_operator_requeue(self):
        def failed(*args):
            raise RuntimeError("secret-must-not-be-stored")
        self.executor.execute = failed
        for attempt in range(3):
            result = self.run_job()
            self.assertEqual(result["status"], "retry" if attempt < 2 else "failed")
            with self.store.connect() as conn:
                conn.execute("UPDATE jobs SET next_attempt=0")
        self.assertEqual(self.client.finished[-1][2], "action_required")
        self.assertTrue(self.store.retry(result["job_id"]))
        self.assertEqual(list_jobs(self.store.path)[0]["state"], "queued")

    def test_interrupted_final_attempt_is_finalized_without_execution(self):
        for index in range(3):
            claim = self.store.claim(now=100 + index * 2, lease_seconds=1)
            self.store.update_claim(claim, check_id=42, now=100.1 + index * 2)
        result = self.run_job()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.client.finished[-1][2], "action_required")
        self.assertEqual(self.executor.calls, 0)

    def test_incomplete_report_cannot_pass(self):
        data = report()
        data["campaign"]["complete"] = False
        self.assertEqual(report_conclusion(data, 2), "action_required")
        for mutation in ("zero_effects", "wrong_exit", "bad_counts"):
            data = report()
            if mutation == "zero_effects":
                data["cases"][0]["effects"] = []
            elif mutation == "bad_counts":
                data["campaign"]["expected_cases"] = 2
            with self.assertRaises(ValueError):
                report_conclusion(data, 1 if mutation == "wrong_exit" else 0)


class ArchiveTests(TestCase):
    def test_safe_archive_and_reject_links_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            extract_snapshot(archive_bytes(), Path(directory) / "good")
            self.assertEqual((Path(directory) / "good/file.py").read_text(), "pass")
            for index, (name, kind) in enumerate((("root/../escape", None), ("/absolute", None),
                    ("root/link", tarfile.SYMTYPE), ("root/hard", tarfile.LNKTYPE))):
                with self.assertRaises(ValueError):
                    extract_snapshot(archive_bytes(name, kind), Path(directory) / f"bad{index}")


class ExecutorTests(TestCase):
    def test_runtime_is_bounded_and_cleanup_runs_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            class TimedExecutor(DockerExecutor):
                def command(self, *args, **kwargs):
                    calls.append(args)
                    if args[0] == "wait":
                        raise ExecutionTimeout("deadline")
                    return SimpleNamespace(returncode=0, stdout="sha256:test")
            target = ApprovedTarget(id=456, installation_id=123, full_name="nitin3560/changeproof",
                approved_heads=["b" * 40], adapter_path="examples/payment_service/vulnerable", config_path=root / "config")
            with self.assertRaises(ExecutionTimeout):
                TimedExecutor().execute(root, target, root / "out", WorkerPolicy(targets=[target]))
            runner = next(call for call in calls if call[0] == "run" and "1000:1000" in call)
            for flag in ("--read-only", "--memory", "--cpus", "--pids-limit", "--cap-drop"):
                self.assertIn(flag, runner)
            self.assertTrue(any(call[:3] == ("network", "create", "--internal") for call in calls))
            self.assertTrue(any(call[:2] == ("rm", "-f") for call in calls))
            self.assertTrue(any(call[:2] == ("network", "rm") for call in calls))
            self.assertNotIn("test-token", str(runner))


@skipUnless(importlib.util.find_spec("jwt") and importlib.util.find_spec("httpx"), "GitHub client dependencies required")
class ClientTests(TestCase):
    def test_jwt_scoped_token_check_recovery_and_safe_redirect(self):
        import httpx
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from changeproof.github.client import GitHubClient
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode()
        calls = []
        def respond(request):
            calls.append(request)
            if request.url.path.endswith("access_tokens"):
                decoded = jwt.decode(request.headers["authorization"].split()[1], private.public_key(), algorithms=["RS256"])
                self.assertEqual(decoded["iss"], "1234")
                self.assertLessEqual(decoded["exp"] - decoded["iat"], 600)
                self.assertEqual(json.loads(request.content)["repository_ids"], [456])
                return httpx.Response(201, json={"token": "test-token"})
            if request.url.path.endswith("check-runs"):
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json={"check_runs": [{"id": 42, "external_id": "job-id", "app": {"id": 1234}}]})
            if request.url.host == "api.github.com":
                return httpx.Response(302, headers={"location": "https://codeload.github.com/repo/archive"})
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=b"archive")
        client = GitHubClient("1234", pem, transport=httpx.MockTransport(respond))
        try:
            token = client.installation_token(job())
            self.assertEqual(client.ensure_check(job(), "job-id", token), 42)
            self.assertEqual(client.archive(job(), token), b"archive")
        finally:
            client.close()

    def test_check_creation_completion_and_redirect_rejection(self):
        import httpx
        from changeproof.github.client import GitHubClient, GitHubAPIError
        calls = []
        def respond(request):
            calls.append(request)
            self.assertEqual(request.url.host, "api.github.com")
            if "/tarball/" in request.url.path:
                return httpx.Response(302, headers={"location": "https://example.com/archive"})
            if request.method == "GET":
                return httpx.Response(200, json={"check_runs": []})
            data = json.loads(request.content)
            if request.method == "POST":
                self.assertEqual(data["head_sha"], "b" * 40)
                self.assertEqual(data["external_id"], "job-id")
                self.assertEqual(data["status"], "in_progress")
                return httpx.Response(201, json={"id": 42})
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["conclusion"], "failure")
            return httpx.Response(200, json={"id": 42})
        client = GitHubClient("1234", "unused", transport=httpx.MockTransport(respond))
        try:
            check = client.ensure_check(job(), "job-id", "token")
            client.finish_check(job(), check, "token", "failure", "Reproduced violation")
            with self.assertRaises(GitHubAPIError):
                client.archive(job(), "token")
            self.assertEqual(len(calls), 4)
        finally:
            client.close()
