from dataclasses import dataclass
from uuid import uuid4

from changeproof.invariants.models import CampaignConfig


@dataclass(frozen=True)
class Scenario:
    kind: str
    key: str
    request_count: int
    concurrent: bool = False


def generate(config: CampaignConfig):
    for _ in range(config.scenarios.iterations):
        yield Scenario("single_payment", str(uuid4()), 1)
        if config.scenarios.sequential_duplicates:
            yield Scenario("sequential_duplicates", str(uuid4()), 2)
        if config.scenarios.concurrent_duplicates:
            yield Scenario("concurrent_duplicates", str(uuid4()),
                           config.scenarios.concurrent_requests, concurrent=True)
