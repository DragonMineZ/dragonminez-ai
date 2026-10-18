"""Per-uploader rolling 24h limit for the AI diagnosis on crash-log cards (table log_ai_uses).

ponytail: remaining() and record() are separate queries, so two uploads at the same moment can both pass
and go one over the limit; cheap enough not to lock for."""

from bulmaai.database.db import get_pool


async def remaining(user_id: int, limit: int) -> int:
    pool = await get_pool()
    used = await pool.fetchval(
        "SELECT count(*) FROM log_ai_uses WHERE user_id = $1 AND used_at > now() - interval '24 hours'",
        user_id,
    )
    return max(0, limit - int(used or 0))


async def record(user_id: int) -> None:
    pool = await get_pool()
    await pool.execute("INSERT INTO log_ai_uses (user_id) VALUES ($1)", user_id)


async def purge() -> int:
    """Deletes uses older than 2 days; returns how many."""
    pool = await get_pool()
    status = await pool.execute("DELETE FROM log_ai_uses WHERE used_at < now() - interval '2 days'")
    return int(status.split()[-1]) if status else 0
