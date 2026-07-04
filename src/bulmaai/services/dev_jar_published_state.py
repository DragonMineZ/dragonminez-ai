from bulmaai.database.db import get_pool


async def get_published_dev_jar_file_name() -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT artifact_file_name FROM dev_jar_published_state WHERE id = 1"
        )
    return value


async def set_published_dev_jar_file_name(file_name: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO dev_jar_published_state (id, artifact_file_name, published_at)
            VALUES (1, $1, now())
            ON CONFLICT (id) DO UPDATE SET
                artifact_file_name = EXCLUDED.artifact_file_name,
                published_at = now()
            """,
            file_name,
        )
