def reproduction_source(repo, config, case):
    count = case.input.get("request_count", len(case.responses))
    return f'''"""Replay the recorded schedule with a fresh key in a disposable database."""
import asyncio
import unittest
from uuid import uuid4

from changeproof.execution.benchmark import run_benchmark
from changeproof.execution.target import load_target
from changeproof.invariants.models import CampaignConfig
from changeproof.scenarios.generator import Scenario


class ReproduceViolation(unittest.TestCase):
    def test_payment_idempotency(self):
        target, _ = load_target({str(repo)!r})
        config = CampaignConfig.model_validate({config.model_dump()!r})
        scenario = Scenario({case.scenario!r}, str(uuid4()), {count!r},
                            concurrent={case.input.get("concurrent", case.scenario == "concurrent_duplicates")!r},
                            amount={case.input.get("amount", 500)!r}, account_id={case.input.get("account_id", "A100")!r},
                            fault={case.input.get("fault")!r})
        results = asyncio.run(run_benchmark(config, target.VARIANT,
            app_factory=target.create_app, scenarios=[scenario]))
        result = results[0]
        if result.status in {"error", "inconclusive"}:
            raise RuntimeError(result.error)
        self.assertLessEqual(len(result.effects), 1,
            f"Duplicate charges: {{len(result.effects)}}; key={{result.key}}")


if __name__ == "__main__":
    unittest.main()
'''
