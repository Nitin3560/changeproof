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

    async def test_cli_reports_and_generated_regression(self):
        import asyncio
        import json
        import subprocess
        import sys
        import tempfile
        import yaml

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            data = yaml.safe_load((root / "configs/payment-idempotency.yaml").read_text())
            data["scenarios"].update(iterations=1, concurrent_requests=2)
            config = directory / "config.yaml"
            config.write_text(yaml.safe_dump(data))
            for variant, code in (("vulnerable", 1), ("corrected", 0)):
                output = directory / variant
                command = [sys.executable, "-m", "changeproof", "verify", "--repo",
                           str(root / "examples/payment_service" / variant), "--config", str(config),
                           "--output", str(output)]
                result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                report_file = next(output.glob("run-*/report.json"))
                report = json.loads(report_file.read_text())
                self.assertTrue(report["campaign"]["complete"])
                self.assertEqual(report["campaign"]["violations"], 1 if variant == "vulnerable" else 0)
                if variant == "vulnerable":
                    script = report_file.parent / "CP-001-repro.py"
                    replay = await asyncio.to_thread(subprocess.run, [sys.executable, str(script)],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
                    self.assertIn("Duplicate charges: 2", replay.stderr)
                    self.assertNotIn("ERROR:", replay.stderr)
