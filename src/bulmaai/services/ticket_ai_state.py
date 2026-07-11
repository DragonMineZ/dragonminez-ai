from bulmaai.database.db import get_pool


async def get_ai_disabled_ticket_channels() -> set[int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT channel_id FROM ai_ticket_disabled_channels")
    return {row["channel_id"] for row in rows}


async def set_ticket_ai_disabled(channel_id: int, disabled: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if disabled:
            await conn.execute(
                """
                INSERT INTO ai_ticket_disabled_channels (channel_id)
                VALUES ($1)
                ON CONFLICT (channel_id) DO NOTHING
                """,
                channel_id,
            )
        else:
            await conn.execute(
                "DELETE FROM ai_ticket_disabled_channels WHERE channel_id = $1",
                channel_id,
            )
