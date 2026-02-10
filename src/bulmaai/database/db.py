from typing import Optional

import asyncpg
from dotenv import load_dotenv

from bulmaai.config import load_settings

_pool: Optional[asyncpg.Pool] = None
load_dotenv()
settings = load_settings()


def _build_dsn() -> str:
    """
    Build a PostgreSQL DSN string from environment variables.

    POSTGRES_DSN (full URL) overrides the individual pieces.
    Otherwise uses PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE.
    """
    dsn = settings.POSTGRES_DSN
    if dsn:
        return dsn

    host = settings.PGHOST
    port = settings.PGPORT
    user = settings.PGUSER
    password = settings.PGPASSWORD
    database = settings.PGDB

    # postgresql://user:password@host:port/dbname
    if password:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    else:
        return f"postgresql://{user}@{host}:{port}/{database}"


async def init_db_pool(
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> asyncpg.Pool:
    """
    Initialize the global asyncpg connection pool.
    """
    global _pool
    if _pool is not None:
        return _pool

    dsn = _build_dsn()
    async with asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=60,
    ) as _pool:
        await _pool.fetch('SELECT 1')
    return _pool


async def get_pool() -> asyncpg.Pool:
    """
    Get the global connection pool.

    Raises RuntimeError if init_db_pool() was not called.
    """
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db_pool() on startup.")
    return _pool


async def close_db_pool() -> None:
    """
    Close the connection pool gracefully (e.g. on shutdown).
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
