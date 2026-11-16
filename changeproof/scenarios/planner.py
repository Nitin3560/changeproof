from uuid import uuid4

from .generator import Scenario


def plan_probes(analysis, config, suggestions):
    if analysis["status"] != "analysis_complete":
        raise ValueError("Incomplete source analysis cannot drive generated verification")
    probes, origins = [], []
    if analysis["candidates"] and config.scenarios.concurrent_duplicates:
        probes.append(Scenario("concurrent_duplicates", str(uuid4()), 2, True))
        origins.append({"source": "static_analysis", "candidates": analysis["candidates"][:10]})
    for item in suggestions.suggestions:
        scenario = item.scenario
        probes.append(Scenario(scenario.type, str(uuid4()), scenario.request_count,
                               scenario.type == "concurrent_duplicates", scenario.amount, scenario.account_id))
        origins.append({"source": "suggestion", "hypothesis": item.hypothesis,
                        "function": item.function, "confidence": "candidate"})
    return probes, origins


def input_strategy(config):
    from hypothesis import strategies as st

    kinds = []
    if config.scenarios.sequential_duplicates:
        kinds.append("sequential_duplicates")
    if config.scenarios.concurrent_duplicates:
        kinds.append("concurrent_duplicates")

    @st.composite
    def inputs(draw):
        kind = draw(st.sampled_from(kinds))
        count = draw(st.integers(2, min(10, config.scenarios.concurrent_requests))) if kind == "concurrent_duplicates" else draw(st.integers(2, 10))
        return {"kind": kind, "request_count": count,
                "amount": draw(st.integers(1, 2000)),
                "account_id": draw(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=1, max_size=12))}
    return inputs()
