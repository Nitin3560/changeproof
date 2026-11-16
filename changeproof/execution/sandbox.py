from contextlib import asynccontextmanager
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


@asynccontextmanager
async def isolated_database(dsn=None):
    """Use a disposable schema in the configured benchmark PostgreSQL database."""
    dsn = dsn or os.environ["DATABASE_URL"]
    schema = "cp_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={schema} -c statement_timeout=10000 -c lock_timeout=5000",
                                connect_timeout=5)
        finally:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
