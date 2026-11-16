from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI

from ..models import ChargeRequest
from ..storage import Store


def create_app(store=None):
    store = store or Store()

    @asynccontextmanager
    async def lifespan(app):
        await store.initialize()
        yield

    app = FastAPI(title="ChangeProof fake gateway", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/charges", status_code=201)
    async def charge(request: ChargeRequest):
        # Every call is a separate effect, even when the key is repeated.
        result = {**request.model_dump(), "charge_id": str(uuid4())}
        await store.record_charge(result)
        return result

    @app.get("/charges")
    async def charges(variant: str, idempotency_key: str):
        return await store.charges(variant, idempotency_key)

    return app
