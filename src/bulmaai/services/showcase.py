from collections.abc import Sequence

from bulmaai.database.db import get_pool


def should_highlight_message(
    *,
    reaction_count: int,
    threshold: int,
    channel_id: int,
    source_channel_ids: Sequence[int],
    is_bot_author: bool,
) -> bool:
    if is_bot_author:
        return False
    if channel_id not in source_channel_ids:
        return False
    return reaction_count >= threshold


async def try_reserve_highlight(message_id: int) -> bool:
    """Atomically claim a message for highlighting.

    Returns True if this call won the race (no prior row existed), so the
    caller is the one responsible for posting the highlight. Two near-
    simultaneous reactions crossing the threshold at once will only have one
    caller get True back.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO showcase_highlights (message_id)
            VALUES ($1)
            ON CONFLICT (message_id) DO NOTHING
            """,
            message_id,
        )
    return result == "INSERT 0 1"


async def set_highlight_message_id(message_id: int, highlight_message_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE showcase_highlights SET highlight_message_id = $1 WHERE message_id = $2",
            highlight_message_id,
            message_id,
        )
