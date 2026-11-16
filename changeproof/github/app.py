import hashlib
import json
import sqlite3
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .events import ACTIONS, prepare_job, valid_signature
from .queue import DeliveryConflict, JobStore
from .settings import GitHubSettings


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def create_app(settings=None):
    settings = settings or GitHubSettings.from_environment()
    store = JobStore(settings.queue_path)
    app = FastAPI(title="ChangeProof GitHub intake", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "intake_only"}

    @app.post("/github/webhooks", status_code=202)
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.max_body_bytes:
                raise HTTPException(413, "Webhook payload too large")
            body.extend(chunk)
        body = bytes(body)
        if not valid_signature(body, request.headers.get("x-hub-signature-256"),
                               settings.webhook_secret.get_secret_value()):
            raise HTTPException(401, "Invalid webhook signature")
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        try:
            if not event or len(event) > 100 or len(delivery) != 36:
                raise ValueError("Missing webhook headers")
            delivery_id = str(UUID(delivery))
            if delivery_id != delivery.lower():
                raise ValueError("Invalid delivery ID")
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_mapping)
            if not isinstance(payload, dict):
                raise ValueError("Webhook body must be an object")
            job, reason = None, "unsupported_event"
            if event == "pull_request":
                if not isinstance(payload.get("action"), str):
                    raise ValueError("Missing pull request action")
                if payload["action"] in ACTIONS:
                    job, reason = prepare_job(payload, settings)
                else:
                    reason = "unsupported_action"
            elif event == "ping":
                reason = "ping"
        except (ValueError, ValidationError, UnicodeError, RecursionError):
            raise HTTPException(422, "Malformed webhook payload or headers") from None
        try:
            return await run_in_threadpool(store.accept, delivery_id, event,
                                           hashlib.sha256(body).hexdigest(), job, reason)
        except DeliveryConflict:
            raise HTTPException(409, "Delivery ID reused with different content") from None
        except sqlite3.Error:
            raise HTTPException(503, "Webhook queue unavailable", headers={"Retry-After": "5"}) from None

    return app
