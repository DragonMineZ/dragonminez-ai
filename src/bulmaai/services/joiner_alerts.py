"""Flagged-joiner alerts (table joiner_alerts, cogs/raid_guard.py): recorded the moment the alert is
posted, so it's visible in the web panel right away, and reviewable across a bot restart — a staff
quick-action click and the 1h sweep both resolve the same row, first one in wins."""

from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool

HANDLED = "handled"
AUTO_DISMISSED = "auto_dismissed"


@dataclass(frozen=True, slots=True)
class JoinerAlert:
    id: int
    guild_id: int
    user_id: int
    reason: str
    action_taken: str
    alert_message_id: int | None
    expires_at: datetime
    outcome: str | None
    reviewed_by: int | None
    reviewed_at: datetime | None
    created_at: datetime


def _alert(row) -> JoinerAlert:
    return JoinerAlert(
        id=row["id"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        reason=row["reason"],
        action_taken=row["action_taken"],
        alert_message_id=row["alert_message_id"],
        expires_at=row["expires_at"],
        outcome=row["outcome"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
    )


async def record(
    *,
    guild_id: int,
    user_id: int,
    reason: str,
    action_taken: str,
    alert_message_id: int,
    expires_at: datetime,
) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO joiner_alerts (guild_id, user_id, reason, action_taken, alert_message_id, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """,
        guild_id,
        user_id,
        reason,
        action_taken,
        alert_message_id,
        expires_at,
    )


async def alert_for_message(alert_message_id: int) -> JoinerAlert | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM joiner_alerts WHERE alert_message_id = $1", alert_message_id)
    return _alert(row) if row else None


async def set_outcome(alert_id: int, outcome: str, reviewer_id: int | None) -> bool:
    """First review wins: a staff click and the 1h sweep race for the same row, only one can land."""
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE joiner_alerts SET outcome = $2, reviewed_by = $3, reviewed_at = now() WHERE id = $1 AND outcome IS NULL",
        alert_id,
        outcome,
        reviewer_id,
    )
    return result.endswith(" 1")


async def due(now: datetime) -> list[JoinerAlert]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM joiner_alerts WHERE outcome IS NULL AND expires_at <= $1 ORDER BY expires_at",
        now,
    )
    return [_alert(row) for row in rows]


async def list_alerts(guild_id: int, *, user_id: int | None = None, before_id: int | None = None, limit: int = 50) -> list[JoinerAlert]:
    """Newest first, for the staff panel; before_id pages backwards."""
    conditions = ["guild_id = $1"]
    args: list[object] = [guild_id]
    if user_id is not None:
        args.append(user_id)
        conditions.append(f"user_id = ${len(args)}")
    if before_id is not None:
        args.append(before_id)
        conditions.append(f"id < ${len(args)}")
    args.append(limit)
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT * FROM joiner_alerts WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_alert(row) for row in rows]
