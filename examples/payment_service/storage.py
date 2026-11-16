import os
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


class Store:
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ["DATABASE_URL"]

    async def connect(self):
        return await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row)

    async def initialize(self):
        async with await self.connect() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(73821401)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    variant TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    amount BIGINT NOT NULL,
                    charge_id UUID,
                    PRIMARY KEY (variant, idempotency_key)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS charges (
                    charge_id UUID PRIMARY KEY,
                    variant TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    amount BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

    async def payment(self, variant, key):
        async with await self.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM payments WHERE variant = %s AND idempotency_key = %s",
                (variant, key),
            )
            return await cursor.fetchone()

    async def claim(self, variant, key, account_id, amount):
        async with await self.connect() as conn:
            cursor = await conn.execute(
                """INSERT INTO payments (variant, idempotency_key, account_id, amount)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                   RETURNING idempotency_key""",
                (variant, key, account_id, amount),
            )
            return await cursor.fetchone() is not None

    async def save(self, variant, key, account_id, amount, charge_id):
        async with await self.connect() as conn:
            await conn.execute(
                """INSERT INTO payments
                   (variant, idempotency_key, account_id, amount, charge_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (variant, idempotency_key)
                   DO UPDATE SET charge_id = EXCLUDED.charge_id""",
                (variant, key, account_id, amount, UUID(charge_id)),
            )

    async def record_charge(self, charge):
        async with await self.connect() as conn:
            await conn.execute(
                """INSERT INTO charges
                   (charge_id, variant, idempotency_key, account_id, amount)
                   VALUES (%s, %s, %s, %s, %s)""",
                (UUID(charge["charge_id"]), charge["variant"],
                 charge["idempotency_key"], charge["account_id"], charge["amount"]),
            )

    async def charges(self, variant, key):
        async with await self.connect() as conn:
            cursor = await conn.execute(
                """SELECT * FROM charges WHERE variant = %s AND idempotency_key = %s
                   ORDER BY created_at, charge_id""", (variant, key),
            )
            return await cursor.fetchall()
