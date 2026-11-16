import asyncio
from dataclasses import dataclass, field

from .scheduler import request_id


@dataclass
class CaseResult:
    scenario: str
    key: str
    status: str
    effects: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    error: str | None = None
    input: dict = field(default_factory=dict)


async def run_case(client, scenario, read_effects, scheduler, *, endpoint="/payments", timeout=15):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    responses = []
    tasks = []

    async def send(index):
        token = request_id.set(index)
        try:
            scheduler.record("request_started", key=scenario.key)
            reply = await client.post(endpoint, headers={"Idempotency-Key": scenario.key},
                                      json={"account_id": scenario.account_id, "amount": scenario.amount})
            response = {"request_id": index, "status_code": reply.status_code,
                        "body": reply.json()}
            responses.append(response)
            scheduler.record("request_finished", status_code=reply.status_code)
        finally:
            request_id.reset(token)

    try:
        async with asyncio.timeout(timeout):
            if scenario.concurrent:
                tasks = [asyncio.create_task(send(i)) for i in range(scenario.request_count)]
                await asyncio.gather(*tasks)
            else:
                for i in range(scenario.request_count):
                    await send(i)
            effects = await read_effects(scenario.key)
            scheduler.record("effects_observed", count=len(effects))
            allowed = {200, 409} if scenario.concurrent else {200}
            if len(effects) > 1:
                status, error = "violation", None
            elif (len(effects) != 1 or not any(r["status_code"] == 200 for r in responses)
                  or any(r["status_code"] not in allowed for r in responses)):
                status, error = "error", "Campaign did not complete a valid payment"
            else:
                status, error = "no_violation_reproduced", None
            return CaseResult(scenario.kind, scenario.key, status, effects, responses,
                              scheduler.trace, error, {"amount": scenario.amount, "account_id": scenario.account_id,
                                                      "request_count": scenario.request_count, "concurrent": scenario.concurrent})
    except Exception as exc:
        scheduler.record("execution_error", error=type(exc).__name__)
        return CaseResult(scenario.kind, scenario.key, "error", responses=responses,
                          trace=scheduler.trace, error=f"{type(exc).__name__}: {exc}",
                          input={"amount": scenario.amount, "account_id": scenario.account_id,
                                 "request_count": scenario.request_count, "concurrent": scenario.concurrent})
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
