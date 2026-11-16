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

    @model_validator(mode="after")
    def enabled_scenario(self):
        if not (self.sequential_duplicates or self.concurrent_duplicates):
            raise ValueError("Enable at least one duplicate-request scenario")
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
