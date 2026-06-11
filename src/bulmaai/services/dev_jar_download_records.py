from bulmaai.database.db import get_pool


async def has_completed_dev_jar_download(discord_user_id: int, file_name: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT 1
            FROM dev_jar_user_downloads
            WHERE discord_user_id = $1
              AND file_name = $2
            """,
            int(discord_user_id),
            file_name,
        )
    return value is not None


async def record_completed_dev_jar_download(discord_user_id: int, file_name: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO dev_jar_user_downloads (discord_user_id, file_name, downloaded_at)
            VALUES ($1, $2, now())
            ON CONFLICT (discord_user_id, file_name) DO NOTHING
            """,
            int(discord_user_id),
            file_name,
        )
