import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from pydantic import SecretStr, ValidationError

from changeproof.cli import main
from changeproof.github.events import prepare_job, valid_signature
from changeproof.github.queue import DeliveryConflict, JobStore, list_jobs
from changeproof.github.settings import GitHubSettings, RepositoryAccess

HAS_HTTP = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("httpx") is not None
SECRET = "local-test-secret-" * 3


def payload():
    repository = {"id": 456, "full_name": "Nitin3560/changeproof"}
    return {"action": "opened", "number": 7, "installation": {"id": 123},
            "repository": repository, "pull_request": {"number": 7, "state": "open", "draft": False,
            "base": {"sha": "a" * 40, "repo": repository},
            "head": {"sha": "b" * 40, "repo": repository}},
            "unneeded_github_fields": {"title": "Not persisted"}}


def settings(path, **kwargs):
    return GitHubSettings(webhook_secret=SecretStr(SECRET), queue_path=Path(path),
        repositories=[RepositoryAccess(id=456, full_name="Nitin3560/changeproof", installation_id=123)], **kwargs)


def signature(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


class EventTests(TestCase):
    def test_signature_checks_raw_bytes(self):
        body = '{"title":"café"}'.encode()
        self.assertTrue(valid_signature(body, signature(body), SECRET))
        self.assertFalse(valid_signature(body + b" ", signature(body), SECRET))
        for invalid in (None, "", "sha1=bad", "sha256=" + "x" * 64):
            self.assertFalse(valid_signature(body, invalid, SECRET))

    def test_normalized_job_and_allowlist(self):
        config = settings("unused.sqlite3")
        job, reason = prepare_job(payload(), config)
        self.assertIsNone(reason)
        self.assertEqual(job["repository"], "nitin3560/changeproof")
        self.assertEqual(job["head_sha"], "b" * 40)
        self.assertNotIn("unneeded_github_fields", job)
        for field, value in (("id", 999), ("full_name", "other/repository")):
            data = copy.deepcopy(payload())
            data["repository"][field] = value
            data["pull_request"]["base"]["repo"][field] = value
            _, reason = prepare_job(data, config)
            self.assertEqual(reason, "repository_not_allowed")
        data = payload()
        data["installation"]["id"] = 999
        self.assertEqual(prepare_job(data, config)[1], "repository_not_allowed")

    def test_drafts_forks_closed_and_invalid_identity(self):
        config = settings("unused.sqlite3")
        for state, draft in (("open", True), ("closed", False)):
            data = payload()
            data["pull_request"].update(state=state, draft=draft)
            self.assertEqual(prepare_job(data, config)[1], "pull_request_not_ready")
        data = payload()
        data["pull_request"]["head"]["repo"] = {"id": 999, "full_name": "fork/changeproof"}
        self.assertEqual(prepare_job(data, config)[1], "fork_or_missing_head_repository")
        data["pull_request"]["head"]["repo"] = None
        self.assertEqual(prepare_job(data, config)[1], "fork_or_missing_head_repository")
        data = payload()
        data["number"] = 99
        with self.assertRaises(ValueError):
            prepare_job(data, config)
        data = payload()
        data["pull_request"]["head"]["sha"] = "main; echo bad"
        with self.assertRaises(ValidationError):
            prepare_job(data, config)

    def test_settings_fail_closed(self):
        with self.assertRaises(ValidationError):
            GitHubSettings(webhook_secret=SecretStr("short"), repositories=[], queue_path=Path("unused"))
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(KeyError):
            GitHubSettings.from_environment()
        entry = RepositoryAccess(id=456, full_name="Nitin3560/changeproof", installation_id=123)
        with self.assertRaises(ValidationError):
            GitHubSettings(webhook_secret=SecretStr(SECRET), repositories=[entry, entry], queue_path=Path("unused"))


class QueueTests(TestCase):
    def test_delivery_and_commit_deduplication_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            store = JobStore(path)
            job, _ = prepare_job(payload(), settings(path))
            delivery = str(uuid4())
            first = store.accept(delivery, "pull_request", "digest", job)
            self.assertFalse(first["duplicate"])
            restarted = JobStore(path)
            self.assertTrue(restarted.accept(delivery, "pull_request", "digest", job)["duplicate"])
            self.assertTrue(restarted.accept(str(uuid4()), "pull_request", "different_digest", job)["duplicate"])
            self.assertEqual(len(list_jobs(path)), 1)
            with self.assertRaises(DeliveryConflict):
                restarted.accept(delivery, "pull_request", "tampered", job)
            changed = {**job, "head_sha": "c" * 40}
            self.assertFalse(restarted.accept(str(uuid4()), "pull_request", "newhead", changed)["duplicate"])
            changed = {**job, "base_sha": "d" * 40}
            self.assertFalse(restarted.accept(str(uuid4()), "pull_request", "newbase", changed)["duplicate"])
            self.assertEqual(len(list_jobs(path)), 3)
            self.assertNotIn("Not persisted", path.read_bytes().decode(errors="ignore"))

    def test_parallel_redeliveries_queue_one_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            store = JobStore(path)
            job, _ = prepare_job(payload(), settings(path))
            delivery = str(uuid4())
            with ThreadPoolExecutor(max_workers=8) as pool:
                receipts = list(pool.map(lambda _: store.accept(delivery, "pull_request", "digest", job), range(16)))
            self.assertEqual(sum(not item["duplicate"] for item in receipts), 1)
            self.assertEqual(len(list_jobs(path)), 1)

    def test_readonly_cli_and_ignored_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            store = JobStore(path)
            receipt = store.accept(str(uuid4()), "ping", "digest", reason="ping")
            self.assertEqual(receipt["outcome"], "ignored")
            self.assertEqual(list_jobs(path), [])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["github-jobs", "--database", str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"jobs": []})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["github-jobs", "--database", str(path.parent / "missing")]), 2)
            self.assertFalse((path.parent / "missing").exists())


@skipUnless(HAS_HTTP, "GitHub HTTP dependencies are not installed")
class WebhookTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from changeproof.github.app import create_app
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "queue.sqlite3"
        self.config = settings(self.path)
        self.app = create_app(self.config)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://webhook")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temporary.cleanup()

    async def send(self, data=None, *, body=None, event="pull_request", delivery=None, signed=True):
        body = body if body is not None else json.dumps(data if data is not None else payload()).encode()
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid4())}
        if signed:
            headers["X-Hub-Signature-256"] = signature(body)
        return await self.client.post("/github/webhooks", content=body, headers=headers)

    async def test_signed_delivery_acknowledged_and_deduplicated(self):
        delivery = str(uuid4())
        first = await self.send(delivery=delivery)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["outcome"], "queued")
        duplicate = await self.send(delivery=delivery)
        self.assertTrue(duplicate.json()["duplicate"])
        data = payload()
        data["action"] = "synchronize"
        same_head = await self.send(data)
        self.assertTrue(same_head.json()["duplicate"])
        self.assertEqual(len(list_jobs(self.path)), 1)
        conflict = await self.send(data, delivery=delivery)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((await self.client.get("/health")).json()["mode"], "intake_only")

    async def test_signature_precedes_json_processing(self):
        response = await self.send(body=b"not-json", signed=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.send(body=b"not-json")).status_code, 422)
        self.assertEqual((await self.send(body=b'{"action":"opened","action":"closed"}')).status_code, 422)
        self.assertEqual((await self.send(body=b'[]')).status_code, 422)
        self.assertEqual((await self.send({"action": []})).status_code, 422)
        self.assertEqual((await self.send(delivery="invalid-id")).status_code, 422)
        self.assertEqual(list_jobs(self.path), [])

    async def test_ignored_deliveries_do_not_queue_work(self):
        self.assertEqual((await self.send({}, event="ping")).json()["reason"], "ping")
        self.assertEqual((await self.send({}, event="push")).json()["reason"], "unsupported_event")
        self.assertEqual((await self.send({"action": "closed"})).json()["reason"], "unsupported_action")
        data = payload()
        data["pull_request"]["draft"] = True
        self.assertEqual((await self.send(data)).json()["reason"], "pull_request_not_ready")
        data = payload()
        data["installation"]["id"] = 999
        self.assertEqual((await self.send(data)).json()["reason"], "repository_not_allowed")
        self.assertEqual(list_jobs(self.path), [])

    async def test_streamed_body_limit_and_queue_failure(self):
        from changeproof.github.app import create_app
        import httpx
        app = create_app(settings(Path(self.temporary.name) / "limited.sqlite3", max_body_bytes=10))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://webhook") as client:
            async def stream():
                yield b"12345"
                yield b"678901"
            response = await client.post("/github/webhooks", content=stream())
            self.assertEqual(response.status_code, 413)
        with patch("changeproof.github.queue.JobStore.accept", side_effect=sqlite3.OperationalError("locked")):
            response = await self.send()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertEqual(list_jobs(self.path), [])

    async def test_environment_factory_over_real_http_and_restart(self):
        import asyncio
        import os
        import socket
        import subprocess
        import sys
        import httpx

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "CHANGEPROOF_GITHUB_WEBHOOK_SECRET": SECRET,
               "CHANGEPROOF_GITHUB_ALLOWLIST": json.dumps([
                   {"id": 456, "full_name": "Nitin3560/changeproof", "installation_id": 123}]),
               "CHANGEPROOF_GITHUB_QUEUE_PATH": str(self.path)}
        body = json.dumps(payload()).encode()
        headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": str(uuid4()),
                   "X-Hub-Signature-256": signature(body)}
        for restarted in (False, True):
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "changeproof.github.app:create_app",
                "--factory", "--host", "127.0.0.1", "--port", str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            self.fail("Webhook server exited before startup")
                        try:
                            health = await client.get("/health")
                            if health.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("Webhook server did not start within the deadline")
                    response = await client.post("/github/webhooks", content=body, headers=headers)
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json()["duplicate"], restarted)
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
        self.assertEqual(len(list_jobs(self.path)), 1)
