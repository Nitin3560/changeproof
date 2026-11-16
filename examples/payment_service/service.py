from contextlib import asynccontextmanager
import os
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
import httpx

from .models import PaymentRequest
from .storage import Store


def create_app(variant, store=None, gateway_transport=None, *, hook=None, amount_guarded=False):
    if variant not in {"vulnerable", "corrected"}:
        raise ValueError("Unknown service variant")
    store = store or Store()

    @asynccontextmanager
    async def lifespan(app):
        await store.initialize()
        yield

    app = FastAPI(title=f"ChangeProof {variant} benchmark", lifespan=lifespan)

    async def checkpoint(stage, key):
        if hook is not None:
            await hook(stage, key)

    @app.get("/health")
    async def health():
        return {"status": "ok", "variant": variant}

    @app.post("/payments")
    async def payment(
        request: PaymentRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200, pattern=r"\S")],
    ):
        existing = await store.payment(variant, idempotency_key)
        await checkpoint("after_key_lookup", idempotency_key)
        if existing:
            return saved_result(existing, request)

        if variant == "corrected" or (amount_guarded and request.amount < 1000):
            owner = await store.claim(variant, idempotency_key, request.account_id, request.amount)
            if not owner:
                existing = await store.payment(variant, idempotency_key)
                return saved_result(existing, request)

        await checkpoint("before_charge", idempotency_key)
        # The vulnerable path deliberately charges before claiming the key.
        try:
            async with httpx.AsyncClient(
                base_url=os.environ.get("GATEWAY_URL", "http://gateway:8000"),
                transport=gateway_transport, timeout=10,
            ) as client:
                response = await client.post("/charges", json={
                    **request.model_dump(), "variant": variant,
                    "idempotency_key": idempotency_key,
                })
                response.raise_for_status()
                charge_id = response.json()["charge_id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # Keep a corrected claim after an uncertain outcome to prevent retry charges.
            raise HTTPException(502, "Gateway outcome uncertain") from exc
        await checkpoint("before_save", idempotency_key)
        await store.save(variant, idempotency_key, request.account_id, request.amount, charge_id)
        await checkpoint("after_save", idempotency_key)
        return {"charge_id": charge_id, **request.model_dump()}

    return app


def saved_result(existing, request):
    if existing["account_id"] != request.account_id or existing["amount"] != request.amount:
        raise HTTPException(409, "Idempotency key belongs to a different payment")
    if existing["charge_id"] is None:
        raise HTTPException(409, "Payment in progress or outcome uncertain")
    return {"charge_id": str(existing["charge_id"]), "account_id": existing["account_id"],
            "amount": existing["amount"]}
