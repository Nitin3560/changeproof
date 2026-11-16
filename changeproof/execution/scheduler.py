import asyncio
from contextvars import ContextVar

request_id = ContextVar("request_id", default=None)


class Scheduler:
    """Hold all lookups until every concurrent request has reached the same point."""

    def __init__(self, participants=1):
        if participants < 1:
            raise ValueError("participants must be positive")
        self.barrier = asyncio.Barrier(participants)
        self.trace = []

    def record(self, stage, **details):
        self.trace.append({"sequence": len(self.trace), "request_id": request_id.get(),
                           "stage": stage, **details})

    async def __call__(self, stage, key):
        self.record(stage, key=key)
        if stage == "after_key_lookup":
            await self.barrier.wait()
            self.record("lookup_released", key=key)
