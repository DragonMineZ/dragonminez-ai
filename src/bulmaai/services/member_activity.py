import json
import math
from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


# xp required for level N is FACTOR * N^2, so early levels come quickly and
# later ones take progressively more messages.
XP_THRESHOLD_FACTOR = 50


def xp_threshold(level: int) -> int:
    if level <= 0:
        return 0
    return XP_THRESHOLD_FACTOR * level * level


def level_for_xp(xp: int) -> int:
    if xp <= 0:
        return 0
    level = math.isqrt(xp // XP_THRESHOLD_FACTOR)
    while xp_threshold(level + 1) <= xp:
        level += 1
    while xp_threshold(level) > xp:
        level -= 1
    return level


def parse_role_reward_map(raw: str) -> dict[int, int]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    rewards: dict[int, int] = {}
    for key, value in data.items():
        try:
            rewards[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return rewards


@dataclass(frozen=True)
class MemberActivity:
    xp: int
    level: int
    last_award_at: datetime | None


@dataclass(frozen=True)
class LeaderboardRow:
    user_id: int
    xp: int
    level: int


async def get_member_activity(guild_id: int, user_id: int) -> MemberActivity:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp, level, last_award_at FROM member_activity WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
    if row is None:
        return MemberActivity(xp=0, level=0, last_award_at=None)
    return MemberActivity(xp=row["xp"], level=row["level"], last_award_at=row["last_award_at"])


async def award_xp(guild_id: int, user_id: int, xp_gained: int) -> int:
    """Atomically add xp_gained and return the member's new total xp."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO member_activity (guild_id, user_id, xp, level, last_award_at)
            VALUES ($1, $2, $3, 0, now())
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET xp = member_activity.xp + $3, last_award_at = now()
            RETURNING xp
            """,
            guild_id,
            user_id,
            xp_gained,
        )
    return row["xp"]


async def set_level(guild_id: int, user_id: int, level: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE member_activity SET level = $3 WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
            level,
        )


async def get_leaderboard(guild_id: int, limit: int) -> tuple[LeaderboardRow, ...]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, xp, level FROM member_activity WHERE guild_id = $1 ORDER BY xp DESC LIMIT $2",
            guild_id,
            limit,
        )
    return tuple(
        LeaderboardRow(user_id=row["user_id"], xp=row["xp"], level=row["level"]) for row in rows
    )
