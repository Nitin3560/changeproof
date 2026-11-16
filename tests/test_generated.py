import json
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, skipUnless

from pydantic import ValidationError

from changeproof.analyzer.repository import analyze_repository
from changeproof.invariants.loader import load_config
from changeproof.scenarios.planner import plan_probes
from changeproof.scenarios.suggestions import (SuggestionBatch, load_suggestions,
                                               suggest_with_model, validate_policy)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/payment-idempotency.yaml"


def proposal():
    return {"suggestions": [{"hypothesis": "Possible duplicate charge", "function": "create_app.payment",
        "confidence": "candidate", "scenario": {"type": "concurrent_duplicates", "request_count": 2,
        "amount": 1500, "account_id": "A100", "synchronization_point": "after_key_lookup"}}]}


class SuggestionTests(TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def test_schema_rejects_commands_or_oracle_changes(self):
        for field, value in (("shell", "echo bad"), ("max_occurrences", 2), ("confidence", "confirmed")):
            data = proposal()
            data["suggestions"][0][field] = value
            with self.assertRaises(ValidationError):
                SuggestionBatch.model_validate(data)
        for field, value in (("request_count", 101), ("amount", 0), ("amount", 2001),
                             ("synchronization_point", "arbitrary_hook"), ("request_count", "2")):
            data = proposal()
            data["suggestions"][0]["scenario"][field] = value
            with self.assertRaises(ValidationError):
                SuggestionBatch.model_validate(data)

    def test_policy_respects_explicit_campaign(self):
        batch = SuggestionBatch.model_validate(proposal())
        validate_policy(batch, self.config)
        self.config.scenarios.concurrent_duplicates = False
        with self.assertRaises(ValueError):
            validate_policy(batch, self.config)
        self.config.scenarios.concurrent_duplicates = True
        data = proposal()
        data["suggestions"][0]["scenario"]["request_count"] = 11
        with self.assertRaises(ValueError):
            validate_policy(SuggestionBatch.model_validate(data), self.config)

    def test_model_response_validation_and_no_tools(self):
        batch = SuggestionBatch.model_validate(proposal())
        calls = []
        def parse(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status="completed", output_parsed=batch)
        client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        analysis = {"configured_endpoint": {}, "candidates": [], "functions": []}
        result = suggest_with_model(analysis, self.config, "test-model", client=client)
        self.assertEqual(result, batch)
        self.assertFalse(calls[0]["store"])
        self.assertNotIn("tools", calls[0])
        self.assertEqual(calls[0]["text_format"], SuggestionBatch)
        client.responses.parse = lambda **kwargs: SimpleNamespace(status="incomplete", output_parsed=None)
        with self.assertRaises(ValueError):
            suggest_with_model(analysis, self.config, "test-model", client=client)

    def test_openai_sdk_parses_structured_candidate_response(self):
        try:
            import httpx
            from openai import OpenAI
        except ImportError:
            self.skipTest("Optional OpenAI SDK is not installed")
        payloads = []
        def respond(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 0,
                "model": "test-model", "status": "completed",
                "output": [{"type": "message", "id": "msg_test", "role": "assistant",
                            "status": "completed", "content": [{"type": "output_text",
                            "text": json.dumps(proposal()), "annotations": []}]}],
            })
        with OpenAI(api_key="test-key", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            batch = suggest_with_model({"configured_endpoint": {}, "candidates": [], "functions": []},
                                       self.config, "test-model", client=client)
        self.assertEqual(batch.suggestions[0].scenario.amount, 1500)
        self.assertEqual(payloads[0]["text"]["format"]["type"], "json_schema")
        self.assertTrue(payloads[0]["text"]["format"]["strict"])
        self.assertNotIn("tools", payloads[0])

    @skipUnless(importlib.util.find_spec("hypothesis"), "Optional Hypothesis dependency is not installed")
    def test_generated_execution_error_is_incomplete(self):
        from unittest.mock import patch
        from changeproof.execution.generated import run_generated
        from changeproof.execution.runner import CaseResult
        from changeproof.reporting.reports import write_reports

        async def broken(*args, **kwargs):
            return [CaseResult("concurrent_duplicates", "key", "error", error="Ledger unavailable")]
        analysis = {"status": "analysis_complete", "candidates": []}
        target = SimpleNamespace(VARIANT="corrected", create_app=lambda *args: None)
        with patch("changeproof.execution.generated.run_benchmark", broken):
            results, generation = run_generated(self.config, target, analysis,
                                                SuggestionBatch(suggestions=[]), max_examples=2)
        self.assertFalse(generation["complete"])
        self.assertEqual(generation["search_status"], "execution_error")
        with tempfile.TemporaryDirectory() as directory:
            report, _ = write_reports(directory, self.config, {"path": "/sample", "variant": "corrected"},
                                      results, generation=generation)
        self.assertEqual(report["status"], "execution_error")

    def test_offline_suggestions_and_analysis_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suggestions.json"
            path.write_text(json.dumps(proposal()))
            batch = load_suggestions(path, self.config)
        analysis = analyze_repository(ROOT / "examples/payment_service", self.config)
        probes, origins = plan_probes(analysis, self.config, batch)
        self.assertEqual(len(probes), 2)
        self.assertEqual(origins[0]["source"], "static_analysis")
        self.assertEqual(probes[1].amount, 1500)
        analysis["status"] = "incomplete"
        with self.assertRaises(ValueError):
            plan_probes(analysis, self.config, batch)


@skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for generated campaign tests")
class GeneratedCampaignTests(TestCase):
    def test_generated_inputs_find_defect_missed_by_templates_and_replay(self):
        import asyncio
        import subprocess
        import sys

        from changeproof.execution.benchmark import run_benchmark
        from changeproof.execution.generated import run_generated
        from changeproof.execution.target import load_target
        from changeproof.reporting.reports import write_reports

        config = load_config(CONFIG)
        config.scenarios.iterations = 1
        config.scenarios.concurrent_requests = 2
        target_path = ROOT / "examples/payment_service/amount_sensitive"
        target, metadata = load_target(target_path)
        baseline = asyncio.run(run_benchmark(config, target.VARIANT, app_factory=target.create_app))
        self.assertEqual([case.status for case in baseline], ["no_violation_reproduced"] * 3)
        analysis = analyze_repository(ROOT / "examples/payment_service", config)
        results, generation = run_generated(config, target, analysis, SuggestionBatch(suggestions=[]),
                                             max_examples=30, seed=0)
        self.assertTrue(generation["complete"], generation)
        self.assertEqual(generation["search_status"], "counterexample_found")
        minimal = generation["minimized_input"]
        self.assertEqual(minimal["amount"], 1000)
        self.assertEqual(minimal["request_count"], 2)
        self.assertEqual(minimal["kind"], "concurrent_duplicates")
        self.assertTrue(any(case.status == "violation" for case in results))
        with tempfile.TemporaryDirectory() as directory:
            report, folder = write_reports(directory, config, metadata, results, generation=generation)
            self.assertEqual(report["status"], "violation_reproduced")
            # Replay the final independently verified minimized case, not an earlier larger input.
            script = folder / report["findings"][-1]["reproduction"]
            replay = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
            self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
            self.assertIn("Duplicate charges: 2", replay.stderr)
            self.assertNotIn("ERROR:", replay.stderr)
        corrected, _ = load_target(ROOT / "examples/payment_service/corrected")
        results, generation = run_generated(config, corrected, analysis, SuggestionBatch(suggestions=[]),
                                             max_examples=10, seed=0)
        self.assertTrue(generation["complete"])
        self.assertFalse(any(case.status != "no_violation_reproduced" for case in results))

    def test_cli_generated_campaign_and_offline_suggestion(self):
        import subprocess
        import sys
        import yaml
        config = load_config(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config_path = folder / "config.yaml"
            config_path.write_text(yaml.safe_dump(config.model_dump()))
            suggestion_path = folder / "suggestions.json"
            suggestion_path.write_text(json.dumps(proposal()))
            output = folder / "reports"
            result = subprocess.run([sys.executable, "-m", "changeproof", "verify", "--generated",
                "--repo", str(ROOT / "examples/payment_service/amount_sensitive"),
                "--analysis-root", str(ROOT / "examples/payment_service"),
                "--config", str(config_path), "--suggestions", str(suggestion_path),
                "--max-examples", "30", "--output", str(output)], capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            report = json.loads(next(output.glob("run-*/report.json")).read_text())
            self.assertTrue(report["campaign"]["complete"])
            self.assertEqual(report["generation"]["probe_origins"][1]["source"], "suggestion")
            self.assertEqual(report["generation"]["minimized_input"]["amount"], 1000)
