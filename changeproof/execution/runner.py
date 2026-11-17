import asyncio
from dataclasses import dataclass, field

from .scheduler import request_id
from changeproof.verification.checker import classify_case


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
    observations: dict = field(default_factory=dict)


async def run_case(client, scenario, read_effects, scheduler, *, endpoint="/payments", timeout=15, faults=None):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    responses = []
    tasks = []
    effects = []

    async def send(index):
        token = request_id.set(index)
        try:
            scheduler.record("request_started", key=scenario.key)
            reply = await client.post(endpoint, headers={"Idempotency-Key": scenario.key},
                                      json={"account_id": scenario.account_id, "amount": scenario.amount})
            try:
                body = reply.json()
            except ValueError:
                body = {"text": reply.text[:1000]}
            response = {"request_id": index, "status_code": reply.status_code, "body": body}
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
            observations = await faults.observe() if faults else {}
            status, error = classify_case(scenario, responses, effects, observations)
            return CaseResult(scenario.kind, scenario.key, status, effects, responses,
                              scheduler.trace, error, {"amount": scenario.amount, "account_id": scenario.account_id,
                                                      "request_count": scenario.request_count, "concurrent": scenario.concurrent,
                                                      "fault": scenario.fault}, observations)
    except Exception as exc:
        scheduler.record("execution_error", error=type(exc).__name__)
        return CaseResult(scenario.kind, scenario.key, "error", effects=effects, responses=responses,
                          trace=scheduler.trace, error=f"{type(exc).__name__}: {exc}",
                          input={"amount": scenario.amount, "account_id": scenario.account_id,
                                 "request_count": scenario.request_count, "concurrent": scenario.concurrent,
                                 "fault": scenario.fault}, observations=faults.evidence() if faults else {})
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
