"""Moderation case log (table mod_cases): panel actions and automod hits per user."""

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
) -> int | None:
    """Returns the new case id, or None if external_id was already recorded (idempotent sync)."""
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO mod_cases (
            guild_id, user_id, moderator_id, action, reason, duration_seconds, source, external_id, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, now()))
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
    return [
        ModCase(
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
        )
        for row in rows
    ]
