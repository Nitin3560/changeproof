from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]


class Service(StrictModel):
    name: NonEmpty
    endpoint: Annotated[str, Field(pattern=r"^/[^\s]*$")]
    method: Literal["POST"] = "POST"


class Invariant(StrictModel):
    id: NonEmpty
    type: Literal["unique-side-effect"]
    key: Literal["idempotency_key"]
    effect: Literal["payment_charge"]
    max_occurrences: Literal[1] = 1


class Scenarios(StrictModel):
    sequential_duplicates: bool = True
    concurrent_duplicates: bool = True
    concurrent_requests: Annotated[int, Field(ge=2, le=100)] = 10
    iterations: Annotated[int, Field(ge=1, le=1000)] = 100
    timeout_before_charge: bool = False
    timeout_after_charge: bool = False
    failure_before_save: bool = False
    retry_attempts: Annotated[int, Field(ge=1, le=5)] = 2

    @model_validator(mode="after")
    def enabled_scenario(self):
        if not (self.sequential_duplicates or self.concurrent_duplicates or self.timeout_before_charge
                or self.timeout_after_charge or self.failure_before_save):
            raise ValueError("Enable at least one duplicate or retry scenario")
        return self


class CampaignConfig(StrictModel):
    service: Service
    invariants: Annotated[list[Invariant], Field(min_length=1)]
    scenarios: Scenarios = Field(default_factory=Scenarios)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.invariants]
        if len(set(ids)) != len(ids):
            raise ValueError("Invariant IDs must be unique")
        return self
