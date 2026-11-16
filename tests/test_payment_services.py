import asyncio
import os
from unittest import IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4

if os.environ.get("DATABASE_URL"):
    import httpx

    from examples.payment_service.fake_gateway.app import create_app as gateway_app
    from examples.payment_service.service import create_app
    from examples.payment_service.storage import Store


@skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for PostgreSQL integration tests")
class PaymentServicesTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        await self.store.initialize()
        self.gateway = gateway_app(self.store)
        self.transport = httpx.ASGITransport(app=self.gateway)

    def client(self, variant):
        app = create_app(variant, self.store, self.transport)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service")

    async def test_sequential_duplicates_and_durable_ledger(self):
        for variant in ("vulnerable", "corrected"):
            with self.subTest(variant=variant):
                key = str(uuid4())
                async with self.client(variant) as client:
                    kwargs = dict(headers={"Idempotency-Key": key},
                                  json={"account_id": "A100", "amount": 500})
                    first = await client.post("/payments", **kwargs)
                    second = await client.post("/payments", **kwargs)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.json(), first.json())
                charges = await Store().charges(variant, key)
                self.assertEqual(len(charges), 1)
                self.assertEqual(str(charges[0]["charge_id"]), first.json()["charge_id"])
                self.assertEqual(charges[0]["amount"], 500)

    async def test_corrected_concurrent_claim_has_one_owner(self):
        key = str(uuid4())
        async with self.client("corrected") as client:
            replies = await asyncio.gather(*[
                client.post("/payments", headers={"Idempotency-Key": key},
                            json={"account_id": "A100", "amount": 500})
                for _ in range(10)
            ])
        self.assertTrue(all(reply.status_code in (200, 409) for reply in replies))
        self.assertTrue(any(reply.status_code == 200 for reply in replies))
        self.assertEqual(len(await self.store.charges("corrected", key)), 1)

    async def test_invalid_requests_do_not_charge(self):
        for variant in ("vulnerable", "corrected"):
            async with self.client(variant) as client:
                key = str(uuid4())
                for body in ({"account_id": "A100", "amount": 0},
                             {"account_id": "A100", "amount": True},
                             {"account_id": "", "amount": 500}):
                    reply = await client.post("/payments", headers={"Idempotency-Key": key}, json=body)
                    self.assertEqual(reply.status_code, 422)
                reply = await client.post("/payments", json={"account_id": "A100", "amount": 500})
                self.assertEqual(reply.status_code, 422)
                self.assertEqual(await self.store.charges(variant, key), [])

    async def test_payload_conflict_does_not_charge_again(self):
        for variant in ("vulnerable", "corrected"):
            key = str(uuid4())
            async with self.client(variant) as client:
                first = await client.post("/payments", headers={"Idempotency-Key": key},
                                          json={"account_id": "A100", "amount": 500})
                self.assertEqual(first.status_code, 200)
                conflict = await client.post("/payments", headers={"Idempotency-Key": key},
                                             json={"account_id": "A100", "amount": 600})
                self.assertEqual(conflict.status_code, 409)
            self.assertEqual(len(await self.store.charges(variant, key)), 1)

    async def test_gateway_records_every_attempt(self):
        key = str(uuid4())
        async with httpx.AsyncClient(transport=self.transport, base_url="http://gateway") as client:
            for _ in range(2):
                reply = await client.post("/charges", json={"variant": "vulnerable",
                    "idempotency_key": key, "account_id": "A100", "amount": 500})
                self.assertEqual(reply.status_code, 201)
            reply = await client.get("/charges", params={"variant": "vulnerable", "idempotency_key": key})
        self.assertEqual(len(reply.json()), 2)

    async def test_uncertain_gateway_outcome_keeps_claim(self):
        key = str(uuid4())
        async def uncertain(request):
            raise httpx.ReadTimeout("Outcome uncertain", request=request)
        app = create_app("corrected", self.store, httpx.MockTransport(uncertain))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service") as client:
            kwargs = dict(headers={"Idempotency-Key": key}, json={"account_id": "A100", "amount": 500})
            self.assertEqual((await client.post("/payments", **kwargs)).status_code, 502)
            self.assertEqual((await client.post("/payments", **kwargs)).status_code, 409)
        self.assertIsNone((await self.store.payment("corrected", key))["charge_id"])
