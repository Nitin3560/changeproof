from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
    amount: Annotated[int, Field(gt=0, le=9_223_372_036_854_775_807)]


class ChargeRequest(PaymentRequest):
    variant: Literal["vulnerable", "corrected"]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
