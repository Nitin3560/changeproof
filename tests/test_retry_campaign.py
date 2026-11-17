import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless

from pydantic import ValidationError

from changeproof.github.checks import report_conclusion
from changeproof.invariants.loader import load_config
from changeproof.invariants.models import CampaignConfig
from changeproof.scenarios.generator import Scenario, expected_cases, generate
from changeproof.verification.checker import classify_case

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/payment-retries.yaml"


def retry_scenario(point="timeout_after_charge"):
    return Scenario(point, "retry-key", 3, fault=point)


def replies(*codes):
    return [{"status_code": code} for code in codes]


class RetryPlanTests(TestCase):
    def test_old_campaign_is_unchanged_and_fault_plan_is_bounded(self):
        baseline = load_config(ROOT / "configs/payment-idempotency.yaml")
        self.assertEqual(expected_cases(baseline), 300)
        self.assertTrue(all(case.fault is None for case in generate(baseline)))
        config = load_config(CONFIG)
        scenarios = list(generate(config))
        self.assertEqual(len(scenarios), 20)
        self.assertEqual(len({case.key for case in scenarios}), 20)
        self.assertEqual([case.kind for case in scenarios[:4]], ["single_payment", "timeout_before_charge",
            "timeout_after_charge", "failure_before_save"])
        self.assertTrue(all(case.request_count == 3 and not case.concurrent for case in scenarios if case.fault))
        for value in (0, 6, True, "2"):
            data = config.model_dump()
            data["scenarios"]["retry_attempts"] = value
            with self.assertRaises(ValidationError):
                CampaignConfig.model_validate(data)
        with self.assertRaises(ValueError):
            Scenario("unknown", "key", 3, fault="arbitrary_failure")
        with self.assertRaises(ValueError):
            Scenario("timeout_after_charge", "key", 3, True, fault="timeout_after_charge")

    def test_safety_and_recovery_outcomes_are_distinct(self):
        scenario = retry_scenario()
        observed = {"fault_injected": True, "payment_state": "completed"}
        self.assertEqual(classify_case(scenario, replies(502, 200, 200), [{}, {}], observed)[0], "violation")
        self.assertEqual(classify_case(scenario, replies(502, 200, 200), [{}], observed)[0], "no_violation_reproduced")
        for effects in ([], [{}]):
            self.assertEqual(classify_case(scenario, replies(502, 409, 409), effects, observed)[0], "inconclusive")
        self.assertEqual(classify_case(scenario, replies(502, 200, 200), [{}], {})[0], "error")
        self.assertEqual(classify_case(scenario, replies(502, 500, 200), [{}], observed)[0], "error")
        self.assertEqual(classify_case(scenario, replies(502, 200), [{}], observed)[0], "error")
        self.assertEqual(classify_case(retry_scenario("failure_before_save"), replies(500, 409, 409), [{}], observed)[0], "inconclusive")

    def test_a_handled_fault_requires_durable_completion_to_pass(self):
        scenario = retry_scenario("failure_before_save")
        responses = replies(200, 200, 200)
        for state, expected in (("completed", "no_violation_reproduced"), ("reserved", "inconclusive")):
            self.assertEqual(classify_case(scenario, responses, [{}],
                {"fault_injected": True, "payment_state": state})[0], expected)

    def test_github_check_never_passes_unresolved_retries(self):
        report = {"status": "inconclusive", "campaign": {"complete": True, "errors": 0,
            "completed_cases": 1, "expected_cases": 1, "violations": 0, "inconclusive": 1},
            "cases": [{"status": "inconclusive", "effects": []}]}
        self.assertEqual(report_conclusion(report, 2), "action_required")
        report["cases"].append({"status": "violation", "effects": [{}, {}]})
        report["campaign"].update(expected_cases=2, completed_cases=2, violations=1)
        report["status"] = "violation_reproduced"
        self.assertEqual(report_conclusion(report, 1), "failure")
        report["campaign"]["inconclusive"] = 0
        with self.assertRaises(ValueError):
            report_conclusion(report, 1)


@skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for retry fault campaigns")
class RetryCampaignTests(IsolatedAsyncioTestCase):
    async def test_same_fault_campaign_observes_real_durable_effects(self):
        from changeproof.execution.benchmark import run_benchmark
        from changeproof.reporting.reports import write_reports
        config = load_config(CONFIG)
        for variant in ("vulnerable", "corrected"):
            results = await run_benchmark(config, variant)
            self.assertEqual(len(results), 20)
            self.assertFalse(any(result.status == "error" for result in results))
            for case in results:
                with self.subTest(variant=variant, scenario=case.scenario):
                    if case.scenario == "single_payment":
                        self.assertEqual(case.status, "no_violation_reproduced")
                        continue
                    observation = case.observations
                    self.assertTrue(observation["fault_injected"])
                    self.assertEqual(len([event for event in case.trace if event["stage"] == "fault_injected"]), 1)
                    first_effects = 0 if case.scenario == "timeout_before_charge" else 1
                    self.assertEqual(len(observation["effects_at_fault"]), first_effects)
                    self.assertTrue(all(effect["idempotency_key"] == case.key for effect in case.effects))
                    self.assertTrue({str(effect["charge_id"]) for effect in observation["effects_at_fault"]}
                                    <= {str(effect["charge_id"]) for effect in case.effects})
                    first_status = 500 if case.scenario == "failure_before_save" else 502
                    if variant == "vulnerable":
                        expected_effects = 1 if case.scenario == "timeout_before_charge" else 2
                        self.assertEqual(len(case.effects), expected_effects)
                        self.assertEqual(case.status, "violation" if expected_effects > 1 else "no_violation_reproduced")
                        self.assertEqual(observation["payment_state"], "completed")
                        self.assertEqual(observation["gateway_calls"], 2)
                        self.assertEqual([reply["status_code"] for reply in case.responses], [first_status, 200, 200])
                    else:
                        self.assertEqual(len(case.effects), first_effects)
                        self.assertEqual(case.status, "inconclusive")
                        self.assertEqual(observation["payment_state"], "reserved")
                        self.assertEqual(observation["gateway_calls"], 1)
                        self.assertEqual([reply["status_code"] for reply in case.responses], [first_status, 409, 409])
            with tempfile.TemporaryDirectory() as directory:
                report, folder = write_reports(directory, config, {"path": "/sample", "variant": variant}, results)
                self.assertTrue(report["campaign"]["complete"])
                self.assertEqual(report["campaign"]["violations"], 10 if variant == "vulnerable" else 0)
                self.assertEqual(report["campaign"]["inconclusive"], 0 if variant == "vulnerable" else 15)
                self.assertEqual(report["status"], "violation_reproduced" if variant == "vulnerable" else "inconclusive")
                if variant == "corrected":
                    self.assertIn("Unresolved payments", (folder / "report.md").read_text())

    async def test_handled_save_failure_completes_without_another_charge(self):
        from changeproof.execution.benchmark import run_benchmark
        from changeproof.execution.faults import InjectedSaveFailure
        from examples.payment_service.service import create_app
        class SaveRetry:
            def __init__(self, store):
                self.store = store
            def __getattr__(self, name):
                return getattr(self.store, name)
            async def save(self, *args, **kwargs):
                try:
                    return await self.store.save(*args, **kwargs)
                except InjectedSaveFailure:
                    return await self.store.save(*args, **kwargs)
        def recovering(store, transport, hook):
            return create_app("corrected", SaveRetry(store), transport, hook=hook)
        result = (await run_benchmark(load_config(CONFIG), "corrected", app_factory=recovering,
            scenarios=[retry_scenario("failure_before_save")]))[0]
        self.assertEqual(result.status, "no_violation_reproduced", result.error)
        self.assertEqual(len(result.effects), 1)
        self.assertEqual(result.observations["payment_state"], "completed")
        self.assertTrue(result.observations["fault_injected"])
        self.assertEqual(result.observations["gateway_calls"], 1)
        self.assertEqual([reply["status_code"] for reply in result.responses], [200, 200, 200])

    async def test_unexercised_fault_and_ledger_errors_are_not_passing(self):
        from unittest.mock import patch
        from fastapi import FastAPI
        from changeproof.execution.benchmark import run_benchmark
        from examples.payment_service.storage import Store
        config = load_config(CONFIG)
        def no_gateway(store, transport, hook):
            app = FastAPI()
            @app.post("/payments")
            async def payment():
                return {"charge_id": "fake"}
            return app
        result = (await run_benchmark(config, "vulnerable", app_factory=no_gateway,
            scenarios=[retry_scenario()]))[0]
        self.assertEqual(result.status, "error")
        self.assertIn("not exercised", result.error)
        async def unavailable(*args):
            raise ConnectionError("Ledger unavailable")
        with patch.object(Store, "charges", unavailable):
            result = (await run_benchmark(config, "corrected", scenarios=[retry_scenario()]))[0]
        self.assertEqual(result.status, "error")
        self.assertIn("ConnectionError", result.error)

    async def test_cli_reports_and_fault_reproduction(self):
        import subprocess
        import sys
        import yaml
        config = load_config(CONFIG)
        config.scenarios.iterations = 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump(config.model_dump()))
            for variant, expected_exit in (("vulnerable", 1), ("corrected", 2)):
                output = root / variant
                result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "changeproof", "verify",
                    "--repo", str(ROOT / "examples/payment_service" / variant), "--config", str(config_path),
                    "--output", str(output)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                report_file = next(output.glob("run-*/report.json"))
                report = json.loads(report_file.read_text())
                if variant == "corrected":
                    self.assertEqual(report["campaign"]["inconclusive"], 3)
                    self.assertEqual(report_conclusion(report, 2), "action_required")
                    continue
                self.assertEqual(report_conclusion(report, 1), "failure")
                for finding in report["findings"]:
                    script = report_file.parent / finding["reproduction"]
                    replay = await asyncio.to_thread(subprocess.run, [sys.executable, str(script)],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
                    self.assertIn("Duplicate charges: 2", replay.stderr)
                    self.assertNotIn("ERROR:", replay.stderr)
