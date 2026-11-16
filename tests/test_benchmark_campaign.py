import os
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, skipUnless

from changeproof.invariants.loader import load_config

if os.environ.get("DATABASE_URL"):
    from changeproof.execution.benchmark import run_benchmark


@skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for PostgreSQL campaign tests")
class BenchmarkCampaignTests(IsolatedAsyncioTestCase):
    async def test_same_campaign_distinguishes_both_services(self):
        config = load_config(Path(__file__).resolve().parents[1] / "configs/payment-idempotency.yaml")
        config.scenarios.iterations = 5
        keys = set()
        for request_count in (2, 10):
            config.scenarios.concurrent_requests = request_count
            for variant in ("vulnerable", "corrected"):
                results = await run_benchmark(config, variant)
                self.assertEqual(len(results), 15)
                for result in results:
                    with self.subTest(variant=variant, case=result.scenario, count=request_count):
                        self.assertNotIn(result.key, keys)
                        keys.add(result.key)
                        concurrent = result.scenario == "concurrent_duplicates"
                        expected_count = request_count if concurrent and variant == "vulnerable" else 1
                        expected_status = "violation" if expected_count > 1 else "no_violation_reproduced"
                        self.assertEqual(result.status, expected_status, result.error)
                        self.assertEqual(len(result.effects), expected_count)
                        self.assertTrue(all(effect["idempotency_key"] == result.key
                                            and effect["variant"] == variant for effect in result.effects))
                        self.assertEqual(len(result.responses), request_count if concurrent else
                                         (2 if result.scenario == "sequential_duplicates" else 1))
                        if concurrent:
                            lookups = [event["sequence"] for event in result.trace
                                       if event["stage"] == "after_key_lookup"]
                            charges = [event["sequence"] for event in result.trace
                                       if event["stage"] == "before_charge"]
                            self.assertEqual(len(lookups), request_count)
                            self.assertLess(max(lookups), min(charges))
                            self.assertEqual(len(charges), expected_count)
