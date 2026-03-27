import hashlib
import json
from datetime import datetime
from typing import Any

from bulmaai.config import load_settings
from bulmaai.database.db import get_pool

settings = load_settings()


def build_support_cache_key(
    *,
    messages: list[dict[str, str]],
    enabled_tools: list[str],
    language: str,
    channel_id: int,
) -> str:
    payload = {
        "channel_id": channel_id,
        "language": language,
        "enabled_tools": sorted(enabled_tools),
        "messages": messages[-6:],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


async def get_docs_version() -> datetime | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT max(updated_at) FROM docs")


async def fetch_cached_support_response(cache_key: str, docs_version: datetime | None) -> dict[str, Any] | None:
    if not settings.support_response_cache_enabled:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT response_json
            FROM support_response_cache
            WHERE cache_key = $1
              AND docs_version IS NOT DISTINCT FROM $2
            """,
            cache_key,
            docs_version,
        )
        if not row:
            return None
        await conn.execute(
            """
            UPDATE support_response_cache
            SET hit_count = hit_count + 1,
                updated_at = now()
            WHERE cache_key = $1
            """,
            cache_key,
        )
    return dict(row["response_json"])


async def store_cached_support_response(
    cache_key: str,
    docs_version: datetime | None,
    response: dict[str, Any],
) -> None:
    if not settings.support_response_cache_enabled:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO support_response_cache (cache_key, docs_version, response_json, hit_count, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, 0, now(), now())
            ON CONFLICT (cache_key)
            DO UPDATE SET
                docs_version = EXCLUDED.docs_version,
                response_json = EXCLUDED.response_json,
                updated_at = now()
            """,
            cache_key,
            docs_version,
            json.dumps(response, ensure_ascii=False),
        )
