import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

from changeproof.execution.runner import run_case
from changeproof.execution.scheduler import Scheduler
from changeproof.invariants.loader import load_config
from changeproof.scenarios.generator import Scenario, generate


class ScenarioTests(TestCase):
    def test_bounds_and_unique_keys(self):
        config = load_config(Path(__file__).resolve().parents[1] / "configs/payment-idempotency.yaml")
        config.scenarios.iterations = 3
        scenarios = list(generate(config))
        self.assertEqual(len(scenarios), 9)
        self.assertEqual(len({case.key for case in scenarios}), 9)
        self.assertEqual([case.request_count for case in scenarios[:3]], [1, 2, 10])
        config.scenarios.concurrent_duplicates = False
        self.assertTrue(all(not case.concurrent for case in generate(config)))


class Response:
    def __init__(self, status):
        self.status_code = status

    def json(self):
        return {"status": self.status_code}


class RunnerTests(IsolatedAsyncioTestCase):
    async def test_broken_barrier_is_error_and_cancels_waiter(self):
        scheduler = Scheduler(2)
        finished = asyncio.Event()

        class Client:
            async def post(self, *args, **kwargs):
                try:
                    await scheduler("after_key_lookup", "key")
                finally:
                    finished.set()
                return Response(200)

        async def effects(key):
            raise AssertionError("Must not read effects after a broken schedule")

        result = await run_case(Client(), Scenario("single_payment", "key", 1), effects,
                                scheduler, timeout=0.05)
        self.assertEqual(result.status, "error")
        self.assertIn("TimeoutError", result.error)
        self.assertTrue(finished.is_set())

    async def test_failures_never_pass(self):
        class Client:
            async def post(self, *args, **kwargs):
                return Response(502)

        async def effects(key):
            return [{"charge_id": "one"}]

        result = await run_case(Client(), Scenario("single_payment", "key", 1), effects, Scheduler())
        self.assertEqual(result.status, "error")

        async def no_effects(key):
            return []

        result = await run_case(Client(), Scenario("single_payment", "key", 1), no_effects, Scheduler())
        self.assertEqual(result.status, "error")

        async def unavailable(key):
            raise ConnectionError("Ledger unavailable")

        result = await run_case(Client(), Scenario("single_payment", "key", 1), unavailable, Scheduler())
        self.assertEqual(result.status, "error")
        self.assertIn("ConnectionError", result.error)

    async def test_duplicate_effects_are_violation_even_with_http_errors(self):
        class Client:
            async def post(self, *args, **kwargs):
                return Response(500)

        async def effects(key):
            return [{"charge_id": "one"}, {"charge_id": "two"}]

        result = await run_case(Client(), Scenario("single_payment", "key", 1), effects, Scheduler())
        self.assertEqual(result.status, "violation")

    async def test_request_exception_cancels_other_requests(self):
        waiting = asyncio.Event()
        cancelled = asyncio.Event()
        calls = 0

        class Client:
            async def post(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    waiting.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                await waiting.wait()
                raise ConnectionError("Request failed")

        async def effects(key):
            return []

        result = await run_case(Client(), Scenario("concurrent_duplicates", "key", 2, True),
                                effects, Scheduler(2))
        self.assertEqual(result.status, "error")
        self.assertTrue(cancelled.is_set())
