import asyncio
from uuid import uuid4

from changeproof.scenarios.generator import Scenario
from changeproof.scenarios.planner import input_strategy, plan_probes
from changeproof.scenarios.suggestions import validate_policy

from .benchmark import run_benchmark
from .runner import CaseResult


class CampaignError(RuntimeError):
    pass


def run_generated(config, target, analysis, suggestions, *, max_examples=30, seed=0, dsn=None):
    from hypothesis import find, settings, Phase
    from hypothesis.errors import NoSuchExample
    import random

    if not 1 <= max_examples <= 100:
        raise ValueError("Generated example budget must be between 1 and 100")
    validate_policy(suggestions, config)
    probes, origins = plan_probes(analysis, config, suggestions)
    results = []
    execution_limit = min(500, max(50, max_examples * 5))
    metadata = {"mode": "generated", "seed": seed, "max_examples": max_examples,
                "execution_limit": execution_limit, "probe_origins": origins,
                "analysis": analysis, "suggestions": suggestions.model_dump(),
                "search_status": "not_started", "minimized_input": None, "complete": False}

    with asyncio.Runner() as runner:
        def execute(scenario):
            if len(results) >= execution_limit:
                raise CampaignError("Generated execution budget exhausted, including shrinking")
            result = runner.run(run_benchmark(config, target.VARIANT, dsn=dsn,
                                app_factory=target.create_app, scenarios=[scenario]))[0]
            results.append(result)
            if result.status in {"error", "inconclusive"}:
                raise CampaignError(result.error)
            return result

        def violates(value):
            scenario = Scenario(value["kind"], str(uuid4()), value["request_count"],
                                value["kind"] == "concurrent_duplicates", value["amount"], value["account_id"])
            return execute(scenario).status == "violation"

        try:
            for scenario in probes:
                execute(scenario)
            try:
                minimal = find(input_strategy(config), violates,
                    settings=settings(max_examples=max_examples, deadline=None, database=None,
                                      phases=[Phase.generate, Phase.shrink]), random=random.Random(seed))
                metadata["minimized_input"] = minimal
                # Independently replay the shrunk input before treating it as a reproducer.
                if not violates(minimal):
                    raise CampaignError("Minimized input did not reproduce independently")
                metadata["search_status"] = "counterexample_found"
            except NoSuchExample:
                metadata["search_status"] = "no_counterexample_in_budget"
            metadata["complete"] = True
        except Exception as exc:
            metadata["search_status"] = "execution_error"
            if not results or results[-1].status != "error":
                results.append(CaseResult("generated_search", "", "error",
                                          error=f"{type(exc).__name__}: {exc}"))
    metadata["expected_cases"] = len(results)
    return results, metadata
