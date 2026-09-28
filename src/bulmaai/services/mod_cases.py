"""Moderation case log (table mod_cases): panel/command actions, automod hits and synced Dyno/Discord cases."""

from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


@dataclass(frozen=True, slots=True)
class ModCase:
    id: int
    guild_id: int
    user_id: int
    moderator_id: int | None
    action: str
    reason: str | None
    duration_seconds: int | None
    source: str
    created_at: datetime
    external_id: str | None = None
    active: bool = True
    expires_at: datetime | None = None


def _case(row) -> ModCase:
    return ModCase(
        id=row["id"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        moderator_id=row["moderator_id"],
        action=row["action"],
        reason=row["reason"],
        duration_seconds=row["duration_seconds"],
        source=row["source"],
        created_at=row["created_at"],
        external_id=row["external_id"],
        active=row["active"],
        expires_at=row["expires_at"],
    )


async def record_case(
    *,
    guild_id: int,
    user_id: int,
    action: str,
    moderator_id: int | None = None,
    reason: str | None = None,
    duration_seconds: int | None = None,
    source: str = "panel",
    external_id: str | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> int | None:
    """Returns the new case id, or None if external_id was already recorded (idempotent sync)."""
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO mod_cases (
            guild_id, user_id, moderator_id, action, reason, duration_seconds, source, external_id, created_at,
            expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, now()), $10)
        ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO NOTHING
        RETURNING id
        """,
        guild_id,
        user_id,
        moderator_id,
        action,
        reason,
        duration_seconds,
        source,
        external_id,
        created_at,
        expires_at,
    )


async def list_cases(
    guild_id: int,
    *,
    user_id: int | None = None,
    moderator_id: int | None = None,
    action: str | None = None,
    source: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[ModCase]:
    """Newest first. Optional filters are ANDed; before_id pages backwards."""
    conditions = ["guild_id = $1"]
    args: list[object] = [guild_id]
    for column, value in (("user_id", user_id), ("moderator_id", moderator_id), ("action", action), ("source", source)):
        if value is not None:
            args.append(value)
            conditions.append(f"{column} = ${len(args)}")
    if before_id is not None:
        args.append(before_id)
        conditions.append(f"id < ${len(args)}")
    args.append(limit)
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT * FROM mod_cases WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_case(row) for row in rows]


async def get_case(guild_id: int, case_id: int) -> ModCase | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM mod_cases WHERE guild_id = $1 AND id = $2", guild_id, case_id)
    return _case(row) if row else None


async def update_reason(guild_id: int, case_id: int, reason: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE mod_cases SET reason = $3 WHERE guild_id = $1 AND id = $2", guild_id, case_id, reason
    )
    return result.endswith(" 1")


async def deactivate_case(guild_id: int, case_id: int) -> ModCase | None:
    """Soft delete (delwarn/delnote, lifted tempban). Returns the case, or None if unknown/already inactive."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE mod_cases SET active = FALSE WHERE guild_id = $1 AND id = $2 AND active RETURNING *",
        guild_id,
        case_id,
    )
    return _case(row) if row else None


async def deactivate_user_cases(guild_id: int, user_id: int, action: str) -> int:
    """clearwarns / clearnotes, and closing any tempban when someone is unbanned early."""
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE mod_cases SET active = FALSE WHERE guild_id = $1 AND user_id = $2 AND action = $3 AND active",
        guild_id,
        user_id,
        action,
    )
    return int(result.rsplit(" ", 1)[-1])


async def count_active_since(guild_id: int, user_id: int, action: str, since: datetime | None) -> int:
    """since=None counts all time."""
    pool = await get_pool()
    return await pool.fetchval(
        """
        SELECT count(*) FROM mod_cases
        WHERE guild_id = $1 AND user_id = $2 AND action = $3 AND active AND ($4::timestamptz IS NULL OR created_at >= $4)
        """,
        guild_id,
        user_id,
        action,
        since,
    )


async def due_expirations(now: datetime) -> list[ModCase]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM mod_cases WHERE active AND expires_at IS NOT NULL AND expires_at <= $1 ORDER BY expires_at",
        now,
    )
    return [_case(row) for row in rows]


async def moderator_stats(guild_id: int, since: datetime | None) -> list[tuple[int, str, int]]:
    """(moderator_id, action, count) for human moderators, busiest first."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT moderator_id, action, count(*) AS n FROM mod_cases
        WHERE guild_id = $1 AND moderator_id IS NOT NULL AND ($2::timestamptz IS NULL OR created_at >= $2)
        GROUP BY moderator_id, action ORDER BY n DESC
        """,
        guild_id,
        since,
    )
    return [(row["moderator_id"], row["action"], row["n"]) for row in rows]


# --- lock bookkeeping (mod_locked_channels) ---------------------------------------------------


async def save_lock(
    guild_id: int, channel_id: int, *, prev_send: bool | None, prev_send_threads: bool | None, locked_by: int | None
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO mod_locked_channels (channel_id, guild_id, prev_send, prev_send_threads, locked_by)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (channel_id) DO NOTHING
        """,
        channel_id,
        guild_id,
        prev_send,
        prev_send_threads,
        locked_by,
    )


async def pop_lock(channel_id: int) -> tuple[bool | None, bool | None] | None:
    """Removes the lock row and returns the overwrites to restore, or None if we never locked it."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "DELETE FROM mod_locked_channels WHERE channel_id = $1 RETURNING prev_send, prev_send_threads", channel_id
    )
    return (row["prev_send"], row["prev_send_threads"]) if row else None


async def locked_channel_ids(guild_id: int) -> list[int]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT channel_id FROM mod_locked_channels WHERE guild_id = $1", guild_id)
    return [row["channel_id"] for row in rows]
