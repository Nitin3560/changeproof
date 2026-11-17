from dataclasses import dataclass
from uuid import uuid4

from changeproof.invariants.models import CampaignConfig


@dataclass(frozen=True)
class Scenario:
    kind: str
    key: str
    request_count: int
    concurrent: bool = False
    amount: int = 500
    account_id: str = "A100"
    fault: str | None = None

    def __post_init__(self):
        if self.fault is not None:
            if self.fault not in {"timeout_before_charge", "timeout_after_charge", "failure_before_save"}:
                raise ValueError("Unknown fault point")
            if self.concurrent or not 2 <= self.request_count <= 6 or self.kind != self.fault:
                raise ValueError("Fault scenarios require 2–6 sequential attempts and a matching kind")


def generate(config: CampaignConfig):
    for _ in range(config.scenarios.iterations):
        yield Scenario("single_payment", str(uuid4()), 1)
        if config.scenarios.sequential_duplicates:
            yield Scenario("sequential_duplicates", str(uuid4()), 2)
        if config.scenarios.concurrent_duplicates:
            yield Scenario("concurrent_duplicates", str(uuid4()),
                           config.scenarios.concurrent_requests, concurrent=True)

        for point in ("timeout_before_charge", "timeout_after_charge", "failure_before_save"):
            if getattr(config.scenarios, point):
                yield Scenario(point, str(uuid4()), 1 + config.scenarios.retry_attempts, fault=point)


def expected_cases(config):
    scenarios = config.scenarios
    return scenarios.iterations * (1 + int(scenarios.sequential_duplicates)
        + int(scenarios.concurrent_duplicates) + int(scenarios.timeout_before_charge)
        + int(scenarios.timeout_after_charge) + int(scenarios.failure_before_save))
